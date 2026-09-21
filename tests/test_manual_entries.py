"""Manual sessions and tutorials, and the scanner they join rather than copy.

`sync/deadlines.py` is stateless on purpose: it recomputes every candidate from
stored state on every run and has no "last scanned" timestamp anywhere in it,
which is what lets a laptop closed for four days still report every threshold
it slept through.

A manual task is a due date on a row, which is all that scanner needs -- so the
tests here are largely the SAME tests `test_deadlines.py` already runs against
Classroom coursework, re-asked of a hand-typed one. That duplication is the
point: it is the evidence that the second kind of deadline inherited the first
kind's guarantees rather than getting its own approximation of them.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from agent import cli
from agent.classroom.models import parse_course
from agent.config import Config
from agent.db import store
from agent.sync import deadlines

MANUAL = "manual-calculus-iii"

TIMETABLE = """
subjects:
  UNIX: "809686876412"
  Calculus III: manual-calculus-iii
  Philosophy: null

versions:
  - label: S1
    status: provisional
    effective_from: 2026-09-15
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC, subject: UNIX }
      - { day: tue, start: "08:30", end: "10:00", kind: LEC,
          subject: Calculus III }
      - { day: wed, start: "08:30", end: "10:00", kind: LEC, subject: Philosophy }
"""

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class KeepOpen:
    def __init__(self, connection):
        self._conn = connection

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


@pytest.fixture
def config(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "library").mkdir(parents=True)
    path = tmp_path / "timetable.yaml"
    path.write_text(TIMETABLE, encoding="utf-8")
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=data_dir,
        tracked_courses=["809686876412"],
        ignored_courses=[],
        timetable_path_override=path,
    )


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    store.upsert_course(
        connection, parse_course({"id": "809686876412", "name": "UNIX programming"})
    )
    store.ensure_manual_course(connection, MANUAL, "Calculus III")
    connection.commit()
    yield connection
    connection.close()


def a_task(conn, *, due_in_hours=60, title="TD 3, exercises 1-6", course=MANUAL):
    return store.add_manual_task(
        conn,
        course_id=course,
        title=title,
        due_at=stamp(NOW + timedelta(hours=due_in_hours)),
    )


def write(conn, result):
    """What `cli._do_deadlines` does with a scan: persist both lists."""
    for event in result.events:
        store.insert_event(conn, event)
    for event in result.suppressed:
        store.insert_event(conn, event, notified_at=stamp(NOW))
    conn.commit()


# --------------------------------------------------------------------------
# the scanner's guarantees, re-asked of a hand-typed deadline
# --------------------------------------------------------------------------

def test_a_manual_task_alerts_at_each_threshold_exactly_once(conn):
    a_task(conn, due_in_hours=60)

    first = deadlines.scan(conn, ["809686876412"], now=NOW)
    assert [event.type for event in first.events] == ["deadline_t72"]
    assert first.events[0].entity_type == "manual_task"
    write(conn, first)

    # The property the whole scanner protects.
    second = deadlines.scan(conn, ["809686876412"], now=NOW)
    assert second.events == []

    # And the next threshold, when it is genuinely crossed.
    later = deadlines.scan(conn, ["809686876412"], now=NOW + timedelta(hours=40))
    assert [event.type for event in later.events] == ["deadline_t24"]


def test_a_scanner_that_slept_for_four_days_reports_what_it_missed(conn):
    """Catch-up safety, inherited rather than re-earned. The most urgent
    threshold speaks for all of them; the rest are still written, stamped, so
    they cannot fire late."""
    a_task(conn, due_in_hours=2)

    result = deadlines.scan(conn, ["809686876412"], now=NOW)
    assert [event.type for event in result.events] == ["deadline_t3"]
    assert sorted(event.type for event in result.suppressed) == [
        "deadline_t24",
        "deadline_t72",
    ]
    write(conn, result)

    assert deadlines.scan(conn, ["809686876412"], now=NOW).events == []


def test_a_task_marked_done_is_not_chased(conn):
    task_id = a_task(conn, due_in_hours=60)
    store.complete_manual_task(conn, task_id)
    conn.commit()
    assert deadlines.scan(conn, ["809686876412"], now=NOW).events == []


def test_a_deadline_already_past_generates_nothing(conn):
    """Without this the scanner announces at 03:00 that something was due last
    month, every run, forever."""
    a_task(conn, due_in_hours=-5)
    assert deadlines.scan(conn, ["809686876412"], now=NOW).events == []


def test_an_undated_task_is_recorded_but_never_alerted_on(conn):
    store.add_manual_task(conn, course_id=MANUAL, title="Read chapter 4")
    conn.commit()
    result = deadlines.scan(conn, ["809686876412"], now=NOW)
    assert result.events == []
    assert store.manual_tasks(conn)[0]["title"] == "Read chapter 4"


def test_the_event_carries_what_the_digest_needs(conn):
    a_task(conn, due_in_hours=20, title="TD 4")
    event = deadlines.scan(conn, ["809686876412"], now=NOW).events[0]

    assert event.payload["title"] == "TD 4"
    assert event.payload["hours_before"] == 24
    assert event.payload["kind"] == "tutorial"
    assert event.course_id == MANUAL


def test_a_manual_deadline_renders_in_the_digest(conn, config):
    """The composer needed no branch: it reads the payload, and `entity_links`
    simply finds no link for an entity type it does not know."""
    from agent.digest import composer

    a_task(conn, due_in_hours=20, title="TD 4, exercises 1-6")
    write(conn, deadlines.scan(conn, ["809686876412"], now=NOW))

    rows = store.list_events(conn)
    text = composer.compose(
        rows, timezone_name=config.timezone, links=store.entity_links(conn, rows)
    )
    assert text is not None
    assert "due in under 24 h" in text
    assert "TD 4, exercises 1-6" in text


def test_a_manual_task_for_a_TRACKED_subject_is_scanned_too(conn):
    """The task table is not filtered by courses.tracked, but it must not
    *exclude* tracked subjects either -- an exercise sheet handed out on paper
    for UNIX is exactly the thing Classroom does not know about."""
    a_task(conn, due_in_hours=60, course="809686876412", title="Paper sheet 1")
    events = deadlines.scan(conn, ["809686876412"], now=NOW).events
    assert [event.payload["title"] for event in events] == ["Paper sheet 1"]


def test_classroom_and_manual_deadlines_coexist(conn):
    """Two sources, one scanner, and neither crowds the other out."""
    conn.execute(
        "INSERT INTO coursework (id, course_id, title, due_at, content_hash, "
        "first_seen_at) VALUES ('w1', '809686876412', 'TP 2', ?, 'h', ?)",
        (stamp(NOW + timedelta(hours=60)), stamp(NOW)),
    )
    a_task(conn, due_in_hours=60, title="TD 3")
    conn.commit()

    events = deadlines.scan(conn, ["809686876412"], now=NOW).events
    assert sorted(event.entity_type for event in events) == ["coursework", "manual_task"]
    assert sorted(event.payload["title"] for event in events) == ["TD 3", "TP 2"]


def test_the_scanner_still_has_no_last_scanned_state_anywhere():
    """Pinned by reading the source, because the failure is silent: a cursor
    added here would drop every threshold crossed while the box was off."""
    from pathlib import Path

    source = Path(deadlines.__file__).read_text(encoding="utf-8")
    for forbidden in ("last_scan", "last_run", "since", "scanned_at"):
        assert forbidden not in source.lower().replace("since it", ""), forbidden


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def session_args(**kwargs):
    return argparse.Namespace(**{
        "subject": None, "on": None, "kind": "LEC", "covered": None,
        "dry_run": False, **kwargs,
    })


def task_args(**kwargs):
    return argparse.Namespace(**{
        "subject": None, "title": None, "due": None, "kind": "tutorial",
        "notes": None, "source": None, "done": None, "include_done": False,
        "dry_run": False, **kwargs,
    })


@pytest.fixture
def wired(conn, monkeypatch):
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    return conn


def test_logging_a_session_records_what_was_covered(config, wired, capsys):
    assert cli.cmd_sessions(config, session_args(
        subject="Calculus III", on="2026-09-21", kind="TUT",
        covered="Cauchy sequences, completeness",
    )) == 0
    assert "logged as session" in capsys.readouterr().out

    row = store.manual_sessions(wired)[0]
    assert (row["course_id"], row["held_on"], row["kind"]) == (MANUAL, "2026-09-21", "TUT")
    assert row["covered"] == "Cauchy sequences, completeness"


def test_listing_sessions_shows_them_newest_first(config, wired, capsys):
    for day in ("2026-09-14", "2026-09-21"):
        cli.cmd_sessions(config, session_args(
            subject="Calculus III", on=day, covered=f"week of {day}"))
    capsys.readouterr()

    assert cli.cmd_sessions(config, session_args()) == 0
    out = capsys.readouterr().out
    assert out.index("2026-09-21") < out.index("2026-09-14")


def test_a_session_for_an_unknown_subject_is_refused(config, wired, capsys):
    assert cli.cmd_sessions(config, session_args(
        subject="Calculus", on="2026-09-21", covered="x")) == 1
    assert "never matched approximately" in capsys.readouterr().err


def test_a_dated_task_is_due_at_end_of_day_locally(config, wired, capsys):
    """Classroom's own convention for an absent dueTime. A sheet due 'Friday'
    must not be silently due at Friday midnight, twenty-four hours early."""
    assert cli.cmd_tasks(config, task_args(
        subject="Calculus III", title="TD 3", due="2026-10-05")) == 0
    capsys.readouterr()

    row = store.manual_tasks(wired)[0]
    # Africa/Tunis is UTC+1, so 23:59 local is 22:59 UTC the same day.
    assert row["due_at"] == "2026-10-05T22:59:00Z"


def test_a_task_with_an_explicit_time_keeps_it(config, wired):
    cli.cmd_tasks(config, task_args(
        subject="Calculus III", title="TD 4", due="2026-10-05 08:30"))
    assert store.manual_tasks(wired)[0]["due_at"] == "2026-10-05T07:30:00Z"


def test_an_unparseable_due_date_is_refused_by_name(config, wired, capsys):
    assert cli.cmd_tasks(config, task_args(
        subject="Calculus III", title="TD 5", due="next friday")) == 1
    assert "--due must be" in capsys.readouterr().err


def test_recording_the_same_sheet_twice_is_refused_not_duplicated(config, wired, capsys):
    """Two identical rows would mean two alerts for one sheet."""
    same = dict(subject="Calculus III", title="TD 3", due="2026-10-05")
    cli.cmd_tasks(config, task_args(**same))
    capsys.readouterr()

    assert cli.cmd_tasks(config, task_args(**same)) == 0
    assert "already recorded" in capsys.readouterr().out
    assert len(store.manual_tasks(wired)) == 1


def test_an_undated_task_says_it_will_never_be_alerted_on(config, wired, capsys):
    """Recorded and invisible to the scanner are two facts, and the second one
    is the surprising one."""
    cli.cmd_tasks(config, task_args(subject="Calculus III", title="Read chapter 4"))
    assert "the deadline scanner will never alert" in capsys.readouterr().out


def test_marking_a_task_done_twice_says_which_happened(config, wired, capsys):
    cli.cmd_tasks(config, task_args(
        subject="Calculus III", title="TD 3", due="2026-10-05"))
    task_id = store.manual_tasks(wired)[0]["id"]
    capsys.readouterr()

    assert cli.cmd_tasks(config, task_args(done=task_id)) == 0
    assert "done: TD 3" in capsys.readouterr().out

    assert cli.cmd_tasks(config, task_args(done=task_id)) == 0
    assert "was already done" in capsys.readouterr().out


def test_marking_a_task_that_does_not_exist_is_an_error(config, wired, capsys):
    assert cli.cmd_tasks(config, task_args(done=999)) == 1
    assert "no task with id 999" in capsys.readouterr().err


def test_dry_run_writes_nothing(config, wired, capsys):
    cli.cmd_tasks(config, task_args(
        subject="Calculus III", title="TD 3", due="2026-10-05", dry_run=True))
    cli.cmd_sessions(config, session_args(
        subject="Calculus III", on="2026-09-21", covered="x", dry_run=True))
    assert store.manual_tasks(wired) == []
    assert store.manual_sessions(wired) == []


# --------------------------------------------------------------------------
# and a sync does nothing to any of it
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, courses):
        self.courses = courses

    def _for(self, course_id, key):
        return list(self.courses.get(course_id, {}).get(key, []))

    def list_coursework(self, course_id):
        return self._for(course_id, "coursework")

    def list_coursework_materials(self, course_id):
        return self._for(course_id, "coursework_materials")

    def list_announcements(self, course_id):
        return self._for(course_id, "announcements")

    def list_submissions(self, course_id):
        return self._for(course_id, "submissions")


def test_sessions_and_tasks_survive_a_full_sync(config, conn):
    from agent.sync.poller import sync

    store.log_manual_session(
        conn, course_id=MANUAL, held_on="2026-09-21", kind="LEC", covered="Cauchy"
    )
    a_task(conn, due_in_hours=60)
    # One against a TRACKED subject too, which is where the reconciler runs.
    a_task(conn, due_in_hours=60, course="809686876412", title="Paper sheet 1")
    conn.commit()

    class FakeConfig:
        tracked_courses = ["809686876412"]
        ignored_courses: list[str] = []

    sync(FakeConfig(), conn, client=FakeClient({"809686876412": {}}))
    sync(FakeConfig(), conn, client=FakeClient({"809686876412": {}}))

    assert len(store.manual_sessions(conn)) == 1
    assert len(store.manual_tasks(conn)) == 2
