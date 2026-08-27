"""Deadline scanner: fires once per threshold, never for finished or past work."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.classroom.models import iso, parse_course, parse_coursework, parse_submission
from agent.db import store
from agent.sync import deadlines

COURSE = "c1"
DUE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    store.upsert_course(connection, parse_course({"id": COURSE, "name": "Databases"}))
    yield connection
    connection.close()


def add_coursework(conn, work_id="w1", due=DUE, title="TD 3"):
    raw = {"id": work_id, "title": title, "alternateLink": f"https://x.test/{work_id}"}
    if due is not None:
        raw["dueDate"] = {"year": due.year, "month": due.month, "day": due.day}
        raw["dueTime"] = {"hours": due.hour, "minutes": due.minute, "seconds": due.second}
    work, _ = parse_coursework(raw, COURSE)
    store.upsert_coursework(conn, work)
    conn.commit()
    return work


def add_submission(conn, state, work_id="w1", submission_id="s1"):
    submission = parse_submission(
        {
            "id": submission_id,
            "courseId": COURSE,
            "courseWorkId": work_id,
            "state": state,
        }
    )
    store.upsert_submission(conn, submission)
    conn.commit()
    return submission


def record(conn, result):
    """Persist a scan exactly the way the CLI does.

    Both halves, or the test would not reproduce the real behaviour: the
    suppressed thresholds are written stamped, which is what stops them firing
    late on a later scan.
    """
    written = 0
    for event in result.events:
        if store.insert_event(conn, event):
            written += 1
    for event in result.suppressed:
        store.insert_event(conn, event, notified_at="2026-08-27T12:00:00Z")
    conn.commit()
    return written


def at(hours_before):
    return DUE - timedelta(hours=hours_before)


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------

def test_nothing_fires_before_the_first_threshold(conn):
    add_coursework(conn)
    result = deadlines.scan(conn, [COURSE], now=at(100))
    assert result.events == []


def test_t72_fires_just_inside_the_window(conn):
    add_coursework(conn)
    result = deadlines.scan(conn, [COURSE], now=at(71))

    assert [event.type for event in result.events] == ["deadline_t72"]
    assert result.events[0].entity_id == "w1"
    assert result.events[0].course_id == COURSE
    assert result.events[0].payload["due_at"] == iso(DUE)
    assert result.events[0].payload["hours_before"] == 72


def test_exactly_at_the_threshold_fires(conn):
    add_coursework(conn)
    assert [e.type for e in deadlines.scan(conn, [COURSE], now=at(72)).events] == [
        "deadline_t72"
    ]


def test_each_threshold_fires_as_it_is_crossed(conn):
    """Walking forward in time, each run emits only the newly crossed threshold."""
    add_coursework(conn)

    first = deadlines.scan(conn, [COURSE], now=at(71))
    record(conn, first)
    assert [event.type for event in first.events] == ["deadline_t72"]

    second = deadlines.scan(conn, [COURSE], now=at(23))
    record(conn, second)
    assert [event.type for event in second.events] == ["deadline_t24"]

    third = deadlines.scan(conn, [COURSE], now=at(2))
    record(conn, third)
    assert [event.type for event in third.events] == ["deadline_t3"]


def test_a_catching_up_scan_emits_only_the_most_urgent_threshold(conn):
    """The required case: one alert, and it is the one that tells the truth.

    All three thresholds are behind us and none was ever recorded. Sending all
    three would be three alerts about one assignment, and the first one read
    would say "72 hours" when the real answer is one.
    """
    add_coursework(conn)
    result = deadlines.scan(conn, [COURSE], now=at(1))

    assert len(result.events) == 1
    assert result.events[0].type == "deadline_t3"


def test_a_catching_up_scan_emits_nothing_on_the_next_run(conn):
    """The other half of the required case: the suppressed ones never fire."""
    add_coursework(conn)
    record(conn, deadlines.scan(conn, [COURSE], now=at(1)))

    assert deadlines.scan(conn, [COURSE], now=at(1)).events == []
    # And still nothing as the deadline gets closer.
    assert deadlines.scan(conn, [COURSE], now=at(0.5)).events == []


def test_the_skipped_thresholds_are_recorded_as_already_notified(conn):
    """Written, not dropped -- otherwise the next scan fires them late."""
    add_coursework(conn)
    result = deadlines.scan(conn, [COURSE], now=at(1))

    assert [event.type for event in result.suppressed] == [
        "deadline_t72",
        "deadline_t24",
    ]

    record(conn, result)

    # Three rows exist, but only the t3 one is ever going to be sent.
    assert store.count_events(conn) == 3
    assert store.count_pending_events(conn) == 1
    (pending,) = [
        row for row in store.list_events(conn, include_notified=False)
    ]
    assert pending["type"] == "deadline_t3"


def test_partial_catch_up_emits_only_the_nearest_of_what_is_left(conn):
    """t72 already sent, then downtime: t24 and t3 both cross, only t3 fires."""
    add_coursework(conn)
    record(conn, deadlines.scan(conn, [COURSE], now=at(71)))

    result = deadlines.scan(conn, [COURSE], now=at(2))

    assert [event.type for event in result.events] == ["deadline_t3"]
    assert [event.type for event in result.suppressed] == ["deadline_t24"]


def test_normal_operation_is_unaffected(conn):
    """Scanning twice a day, each threshold still fires on its own."""
    add_coursework(conn)

    for moment, expected in ((at(71), "deadline_t72"), (at(23), "deadline_t24"), (at(2), "deadline_t3")):
        result = deadlines.scan(conn, [COURSE], now=moment)
        assert [event.type for event in result.events] == [expected]
        # Nothing is being suppressed -- there is only ever one newly crossed.
        assert result.suppressed == []
        record(conn, result)

    assert store.count_events(conn) == 3
    assert store.count_pending_events(conn) == 3


# --------------------------------------------------------------------------
# firing once
# --------------------------------------------------------------------------

def test_a_threshold_never_fires_twice(conn):
    add_coursework(conn)

    first = deadlines.scan(conn, [COURSE], now=at(71))
    assert record(conn, first) == 1

    again = deadlines.scan(conn, [COURSE], now=at(70))
    assert again.events == []


def test_rescanning_the_same_moment_is_silent(conn):
    """The single most useful property: running it twice changes nothing."""
    add_coursework(conn)
    record(conn, deadlines.scan(conn, [COURSE], now=at(2)))
    before = store.count_events(conn)

    record(conn, deadlines.scan(conn, [COURSE], now=at(2)))

    assert store.count_events(conn) == before


def test_created_at_is_the_threshold_not_the_scan_time(conn):
    """A wall-clock created_at would give the same alert a new dedupe key each run."""
    add_coursework(conn)
    result = deadlines.scan(conn, [COURSE], now=at(50))

    assert result.events[0].created_at == iso(at(72))


def test_the_unique_index_catches_a_duplicate_insert(conn):
    """Belt and braces: even inserted directly twice, only one row lands."""
    add_coursework(conn)
    (event,) = deadlines.scan(conn, [COURSE], now=at(71)).events

    assert store.insert_event(conn, event) is True
    assert store.insert_event(conn, event) is False


def test_an_unsent_alert_does_not_produce_a_second_one(conn):
    """Dedupe ignores notified_at: a pending alert is already in the digest."""
    add_coursework(conn)
    record(conn, deadlines.scan(conn, [COURSE], now=at(71)))
    assert store.count_pending_events(conn) == 1

    record(conn, deadlines.scan(conn, [COURSE], now=at(71)))

    assert store.count_pending_events(conn) == 1


# --------------------------------------------------------------------------
# what is excluded
# --------------------------------------------------------------------------

def test_turned_in_generates_nothing(conn):
    add_coursework(conn)
    add_submission(conn, "TURNED_IN")

    assert deadlines.scan(conn, [COURSE], now=at(2)).events == []


def test_returned_generates_nothing(conn):
    add_coursework(conn)
    add_submission(conn, "RETURNED")

    assert deadlines.scan(conn, [COURSE], now=at(2)).events == []


def test_created_but_not_handed_in_still_fires(conn):
    """CREATED means Classroom made a row, not that anything was submitted."""
    add_coursework(conn)
    add_submission(conn, "CREATED")

    # Only t72 has been crossed at this point, so exactly one alert is due.
    assert [e.type for e in deadlines.scan(conn, [COURSE], now=at(71)).events] == [
        "deadline_t72"
    ]


def test_reclaimed_work_is_outstanding_again(conn):
    """Pulling work back means it is not handed in, so alerts resume."""
    add_coursework(conn)
    add_submission(conn, "RECLAIMED_BY_STUDENT")

    assert deadlines.scan(conn, [COURSE], now=at(2)).events != []


def test_a_past_deadline_generates_nothing(conn):
    """Otherwise the bot announces at 03:00 that something was due last month."""
    add_coursework(conn)

    assert deadlines.scan(conn, [COURSE], now=DUE + timedelta(days=30)).events == []


def test_exactly_at_the_deadline_generates_nothing(conn):
    add_coursework(conn)
    assert deadlines.scan(conn, [COURSE], now=DUE).events == []


def test_coursework_without_a_due_date_is_skipped_silently(conn):
    """54% of measured coursework has no due date. This is normal, not a warning."""
    add_coursework(conn, due=None)
    result = deadlines.scan(conn, [COURSE], now=at(2))

    assert result.events == []
    assert result.without_due_date == 1
    assert result.considered == 1


def test_soft_deleted_coursework_is_skipped(conn):
    add_coursework(conn)
    store.soft_delete_missing(conn, "coursework", COURSE, [])
    conn.commit()

    assert deadlines.scan(conn, [COURSE], now=at(2)).events == []


def test_untracked_courses_are_not_scanned(conn):
    add_coursework(conn)
    assert deadlines.scan(conn, [], now=at(2)).events == []
    assert deadlines.scan(conn, ["other"], now=at(2)).events == []


def test_mixed_corpus_counts_what_it_skipped(conn):
    add_coursework(conn, work_id="w1", due=DUE)
    add_coursework(conn, work_id="w2", due=None)
    add_coursework(conn, work_id="w3", due=None)

    result = deadlines.scan(conn, [COURSE], now=at(2))

    assert result.considered == 3
    assert result.without_due_date == 2
    assert {event.entity_id for event in result.events} == {"w1"}
