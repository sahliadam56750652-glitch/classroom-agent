"""Poller: idempotence, catch-up equivalence, seed mode, soft deletes.

Everything runs against a stubbed client. No test here reaches the network.
"""

from __future__ import annotations

import pytest

from agent.classroom.models import parse_course
from agent.db import store
from agent.sync import poller
from agent.sync.poller import SeedRefused, UnknownCourse, sync

DRIVE = {"driveFile": {"driveFile": {"id": "d1", "title": "lecture.pdf"}}}


class FakeClient:
    """Serves a fixed snapshot of one or more courses, and counts its calls."""

    def __init__(self, courses: dict[str, dict]):
        self.courses = courses
        self.calls = 0

    def _for(self, course_id, key):
        self.calls += 1
        return list(self.courses.get(course_id, {}).get(key, []))

    def list_coursework(self, course_id):
        return self._for(course_id, "coursework")

    def list_coursework_materials(self, course_id):
        return self._for(course_id, "coursework_materials")

    def list_announcements(self, course_id):
        return self._for(course_id, "announcements")

    def list_submissions(self, course_id):
        return self._for(course_id, "submissions")


class FakeConfig:
    def __init__(self, tracked):
        self.tracked_courses = list(tracked)
        self.ignored_courses = []


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    store.upsert_course(connection, parse_course({"id": "c1", "name": "Databases"}))
    store.upsert_course(connection, parse_course({"id": "c2", "name": "Networks"}))
    connection.commit()
    yield connection
    connection.close()


def snapshot(**overrides):
    base = {
        "coursework": [{"id": "w1", "title": "TD 1", "materials": [DRIVE]}],
        "coursework_materials": [{"id": "m1", "title": "Slides 1"}],
        "announcements": [{"id": "a1", "text": "Welcome", "materials": [DRIVE]}],
        "submissions": [
            {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "CREATED"}
        ],
    }
    base.update(overrides)
    return {"c1": base}


def run(conn, data, tracked=("c1",), **kwargs):
    return sync(FakeConfig(tracked), conn, client=FakeClient(data), **kwargs)


# --------------------------------------------------------------------------
# the central property
# --------------------------------------------------------------------------

def test_a_second_identical_sync_produces_zero_events(conn):
    """The most important test in the project.

    If an unchanged sync emits anything, every notification becomes noise and
    the whole system stops being worth reading.
    """
    data = snapshot()

    first = run(conn, data)
    second = run(conn, data)

    assert first.events != []
    assert second.events == []
    assert second.events_written == 0


def test_a_third_and_fourth_sync_stay_silent(conn):
    data = snapshot()
    run(conn, data)
    for _ in range(3):
        assert run(conn, data).events == []


def test_rows_are_not_duplicated_by_repeat_syncs(conn):
    data = snapshot()
    run(conn, data)
    run(conn, data)

    assert store.count_rows(conn, "coursework") == 1
    assert store.count_rows(conn, "coursework_materials") == 1
    assert store.count_rows(conn, "announcements") == 1
    assert store.count_rows(conn, "submissions") == 1
    assert store.count_rows(conn, "materials") == 2  # one on w1, one on a1


def test_update_time_churn_produces_no_event(conn):
    quiet = snapshot(
        coursework=[{"id": "w1", "title": "TD 1", "updateTime": "2026-01-01T00:00:00Z"}]
    )
    noisy = snapshot(
        coursework=[{"id": "w1", "title": "TD 1", "updateTime": "2026-08-27T10:00:00Z"}]
    )

    run(conn, quiet)
    assert run(conn, noisy).events == []


# --------------------------------------------------------------------------
# catch-up safety
# --------------------------------------------------------------------------

def fingerprint(conn):
    """Stored state as {(table, id): (content_hash, deleted_at)}."""
    out = {}
    for table in ("coursework", "coursework_materials", "announcements",
                  "submissions", "materials"):
        for row in conn.execute(f"SELECT id, content_hash, deleted_at FROM {table}"):
            out[(table, row["id"])] = (row["content_hash"], row["deleted_at"])
    return out


def test_catch_up_matches_syncing_after_every_batch(tmp_path):
    """Ten days offline must produce what ten daily syncs would have.

    Two databases start from the same seeded baseline. One syncs after each of
    three batches of change; the other stays offline and syncs once at the end.

    Literal event-list equality is the wrong assertion and would be a bug if it
    held: the offline database never observed the intermediate states, so it
    cannot report a title that was edited and then edited again. The invariant
    that actually matters is no gaps and no repeats -- every entity that changed
    is reported exactly once, and both databases converge on identical state.
    """
    baseline = snapshot()
    batches = [
        # w2 appears
        snapshot(coursework=[{"id": "w1", "title": "TD 1", "materials": [DRIVE]},
                             {"id": "w2", "title": "TD 2"}]),
        # w2 gains a deadline
        snapshot(coursework=[{"id": "w1", "title": "TD 1", "materials": [DRIVE]},
                             {"id": "w2", "title": "TD 2",
                              "dueDate": {"year": 2026, "month": 9, "day": 30}}]),
        # w2 is renamed, and s1 is turned in and graded
        snapshot(
            coursework=[{"id": "w1", "title": "TD 1", "materials": [DRIVE]},
                        {"id": "w2", "title": "TD 2 (revised)",
                         "dueDate": {"year": 2026, "month": 9, "day": 30}}],
            submissions=[{"id": "s1", "courseId": "c1", "courseWorkId": "w1",
                          "state": "TURNED_IN", "assignedGrade": 88}],
        ),
    ]

    def seeded(path):
        connection = store.connect(path)
        store.upsert_course(connection, parse_course({"id": "c1", "name": "Databases"}))
        connection.commit()
        run(connection, baseline, seed=True)
        return connection

    daily = seeded(tmp_path / "daily.db")
    daily_events = []
    for batch in batches:
        daily_events.extend(run(daily, batch).events)

    offline = seeded(tmp_path / "offline.db")
    offline_events = run(offline, batches[-1]).events

    touched = lambda events: {(e.entity_type, e.entity_id) for e in events}

    # No gaps: every entity the daily runs reported on, the offline run reports.
    assert touched(offline_events) == touched(daily_events) != set()

    # No repeats: the offline run says each thing once, not once per batch.
    signatures = [(e.type, e.entity_id) for e in offline_events]
    assert len(signatures) == len(set(signatures))

    # And both databases end up storing exactly the same thing.
    assert fingerprint(offline) == fingerprint(daily)

    # A further sync on either changes nothing.
    assert run(daily, batches[-1]).events == []
    assert run(offline, batches[-1]).events == []

    daily.close()
    offline.close()


def test_no_event_is_lost_when_a_sync_is_skipped(conn):
    """An item that appears and is then edited before we look is still new."""
    run(conn, snapshot())

    later = snapshot(
        coursework=[{"id": "w1", "title": "TD 1", "materials": [DRIVE]},
                    {"id": "w9", "title": "Appeared and then renamed"}],
    )
    events = run(conn, later).events

    assert [(e.type, e.entity_id) for e in events] == [("new_coursework", "w9")]


# --------------------------------------------------------------------------
# seed mode
# --------------------------------------------------------------------------

def test_seed_leaves_zero_pending_events(conn):
    result = run(conn, snapshot(), seed=True)

    assert result.events != []
    assert result.events_written == len(result.events)
    assert store.count_pending_events(conn) == 0
    assert store.count_events(conn) == len(result.events)


def test_after_seeding_a_real_change_is_pending(conn):
    run(conn, snapshot(), seed=True)

    run(conn, snapshot(
        coursework=[{"id": "w1", "title": "TD 1", "materials": [DRIVE]},
                    {"id": "w2", "title": "Genuinely new"}]
    ))

    pending = store.list_events(conn)
    assert [row["type"] for row in pending] == ["new_coursework"]


def test_seed_is_refused_when_events_exist(conn):
    run(conn, snapshot())
    with pytest.raises(SeedRefused, match="already holds"):
        run(conn, snapshot(), seed=True)


def test_force_overrides_the_seed_refusal(conn):
    run(conn, snapshot())
    result = run(conn, snapshot(coursework=[{"id": "w5", "title": "New"}]),
                 seed=True, force=True)
    assert result.seeded is True


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------

def test_dry_run_writes_nothing(conn):
    result = run(conn, snapshot(), dry_run=True)

    assert result.events != []
    assert result.events_written == 0
    assert result.run_id is None
    assert store.count_rows(conn, "coursework") == 0
    assert store.count_rows(conn, "events") == 0
    assert store.count_rows(conn, "sync_runs") == 0


def test_dry_run_then_real_run_reports_the_same_events(conn):
    data = snapshot()
    dry = run(conn, data, dry_run=True)
    wet = run(conn, data)

    assert [e.type for e in dry.events] == [e.type for e in wet.events]


# --------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------

def test_only_tracked_courses_are_fetched(conn):
    client = FakeClient({"c1": snapshot()["c1"], "c2": snapshot()["c1"]})
    result = sync(FakeConfig(["c1"]), conn, client=client)

    assert result.courses_synced == ["c1"]
    assert result.items_seen["courses"] == 1


def test_no_tracked_courses_does_nothing(conn):
    client = FakeClient({})
    result = sync(FakeConfig([]), conn, client=client)

    assert result.courses_synced == []
    assert client.calls == 0
    assert store.count_rows(conn, "sync_runs") == 0


def test_tracking_an_unknown_course_is_a_clear_error(conn):
    with pytest.raises(UnknownCourse, match="agent courses"):
        sync(FakeConfig(["nope"]), conn, client=FakeClient({}))


# --------------------------------------------------------------------------
# bookkeeping
# --------------------------------------------------------------------------

def test_sync_run_records_per_resource_counts(conn):
    result = run(conn, snapshot())

    (row,) = store.recent_sync_runs(conn)
    assert row["status"] == "ok"
    assert result.items_seen["announcements"] == 1
    assert result.items_seen["materials"] == 2


def test_a_failing_fetch_records_an_error_run_and_reraises(conn):
    class Exploding(FakeClient):
        def list_announcements(self, course_id):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        sync(FakeConfig(["c1"]), conn, client=Exploding(snapshot()))

    (row,) = store.recent_sync_runs(conn)
    assert row["status"] == "error"
    assert "boom" in row["error"]


def test_a_file_attached_to_an_existing_announcement_is_reported(conn):
    """End to end, the highest-value case: content arriving on an old post.

    Announcements carry 211 of 375 measured attachments, so this is a primary
    delivery path. Before announcement_updated existed the row was quietly
    updated and nothing was reported.
    """
    run(conn, snapshot(announcements=[{"id": "a1", "text": "Week 3"}]), seed=True)

    result = run(conn, snapshot(
        announcements=[{"id": "a1", "text": "Week 3", "materials": [DRIVE]}]
    ))

    (event,) = [e for e in result.events if e.entity_id == "a1"]
    assert event.type == "announcement_updated"
    assert event.payload["attachments_added"] is True
    assert event.payload["added_count"] == 1
    assert store.count_pending_events(conn) == 1


def test_a_typo_fix_on_an_announcement_is_flagged_as_text_only(conn):
    run(conn, snapshot(
        announcements=[{"id": "a1", "text": "Week 3", "materials": [DRIVE]}]
    ), seed=True)

    result = run(conn, snapshot(
        announcements=[{"id": "a1", "text": "Week three", "materials": [DRIVE]}]
    ))

    (event,) = [e for e in result.events if e.entity_id == "a1"]
    assert event.payload["attachments_added"] is False
    assert event.payload["text_changed"] is True


def test_attachment_events_survive_a_repeat_sync(conn):
    """The new types must not break the zero-events-on-rerun property."""
    data = snapshot(announcements=[{"id": "a1", "text": "Week 3", "materials": [DRIVE]}])
    run(conn, data, seed=True)
    assert run(conn, data).events == []


def test_announcement_attachments_are_collected(conn):
    """Announcements carry more attachments than the other two parents combined."""
    run(conn, snapshot())

    rows = conn.execute(
        "SELECT parent_type FROM materials ORDER BY parent_type"
    ).fetchall()
    assert [row["parent_type"] for row in rows] == ["announcement", "coursework"]


# --------------------------------------------------------------------------
# soft deletion
# --------------------------------------------------------------------------

def test_a_vanished_item_is_soft_deleted_not_removed(conn):
    run(conn, snapshot())
    result = run(conn, snapshot(coursework=[]))

    row = conn.execute("SELECT deleted_at FROM coursework WHERE id = 'w1'").fetchone()
    assert row is not None, "the row must survive"
    assert row["deleted_at"] is not None
    assert result.deleted["coursework"] == 1


def test_a_deletion_emits_no_event(conn):
    run(conn, snapshot())
    assert run(conn, snapshot(announcements=[])).events == []


def test_a_restored_item_is_not_announced_twice(conn):
    run(conn, snapshot())
    run(conn, snapshot(announcements=[]))
    result = run(conn, snapshot())

    assert result.events == []
    row = conn.execute("SELECT deleted_at FROM announcements WHERE id = 'a1'").fetchone()
    assert row["deleted_at"] is None
