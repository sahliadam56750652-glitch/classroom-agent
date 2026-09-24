"""Storage layer: schema application, idempotent upserts, sync runs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from agent.classroom.models import (
    parse_announcement,
    parse_course,
    parse_coursework,
    parse_coursework_material,
    parse_submission,
)
from agent.db import store

DRIVE = {"driveFile": {"driveFile": {"id": "d1", "title": "lecture.pdf"}}}

# Raw SQL rather than upsert_course, because the concurrency tests below need
# to write from a plain sqlite3 connection that knows nothing about the store.
INSERT_COURSE = (
    "INSERT INTO courses (id, name, course_state, content_hash, first_seen_at) "
    "VALUES (?, ?, 'ACTIVE', 'h', 't')"
)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    yield connection
    connection.close()


def a_course(course_id="c1", name="Databases", state="ACTIVE"):
    return parse_course({"id": course_id, "name": name, "courseState": state})


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

def test_schema_applies_twice_without_error(tmp_path):
    """open_db runs on every invocation, so it has to be idempotent."""
    path = tmp_path / "academic.db"

    first = store.connect(path)
    first.close()
    second = store.connect(path)
    second.close()


def test_reopening_preserves_data(tmp_path):
    path = tmp_path / "academic.db"
    first = store.connect(path)
    store.upsert_course(first, a_course())
    first.commit()
    first.close()

    second = store.connect(path)
    assert store.count_rows(second, "courses") == 1
    second.close()


def test_schema_version_is_recorded(conn):
    assert store.schema_version(conn) == store.SCHEMA_VERSION


def test_row_factory_gives_named_access(conn):
    store.upsert_course(conn, a_course())
    assert store.list_courses(conn)[0]["name"] == "Databases"


@pytest.mark.parametrize(
    "table",
    ["courses", "coursework", "coursework_materials", "announcements", "submissions",
     "materials", "study_items", "events", "quiz_attempts", "sync_runs",
     "api_sessions"],
)
def test_every_expected_table_exists(conn, table):
    assert store.count_rows(conn, table) == 0


# --------------------------------------------------------------------------
# connect(initialise=False) -- the server's per-request connection
# --------------------------------------------------------------------------

def test_a_connection_can_skip_applying_the_schema(tmp_path):
    """What the API opens per request, once the file already exists.

    Applying schema.sql is free once per CLI invocation and wasteful once per
    HTTP request: it re-reads the file, re-runs every CREATE TABLE IF NOT EXISTS,
    and runs an INSERT OR IGNORE, which is a write.
    """
    path = tmp_path / "academic.db"
    first = store.connect(path)
    store.upsert_course(first, a_course())
    first.commit()
    first.close()

    quick = store.connect(path, initialise=False)
    assert store.count_rows(quick, "courses") == 1
    assert store.schema_version(quick) == store.SCHEMA_VERSION
    quick.close()


def test_skipping_the_schema_still_sets_both_pragmas(tmp_path):
    """Both are connection-scoped, so skipping the script must not skip them.

    A per-request connection without foreign_keys would accept a row the CLI
    would refuse, and one without busy_timeout is the `database is locked` this
    project already went looking for once.
    """
    path = tmp_path / "academic.db"
    store.connect(path).close()

    quick = store.connect(path, initialise=False)
    assert quick.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert quick.execute("PRAGMA busy_timeout").fetchone()[0] == store.BUSY_TIMEOUT_MS
    quick.close()


def test_skipping_the_schema_on_an_empty_file_refuses_rather_than_confusing(tmp_path):
    """Version 0 is named as such, not reported as "no such table".

    The guard is deliberately NOT skipped along with the script: it is one SELECT
    against a one-row table, and a connection that silently opens a file this
    build cannot read is exactly what it exists to prevent.
    """
    path = tmp_path / "empty.db"
    path.write_bytes(b"")

    with pytest.raises(store.StoreError) as caught:
        store.connect(path, initialise=False)
    assert "schema version 0" in str(caught.value)


def test_initialising_is_still_the_default(tmp_path):
    """So that no existing caller changed behaviour when the flag was added."""
    path = tmp_path / "fresh.db"
    conn = store.connect(path)
    assert store.schema_version(conn) == store.SCHEMA_VERSION
    conn.close()


# --------------------------------------------------------------------------
# api sessions
# --------------------------------------------------------------------------

def test_a_session_is_live_until_it_expires(conn):
    store.create_api_session(
        conn, "s1", expires_at="2099-01-01T00:00:00Z", user_agent="Pixel"
    )
    row = store.touch_api_session(conn, "s1", expires_at="2099-02-01T00:00:00Z")
    assert row is not None
    assert row["user_agent"] == "Pixel"


def test_an_expired_session_is_never_returned(conn):
    """The comparison is in the WHERE clause, so no caller can forget it."""
    store.create_api_session(conn, "old", expires_at="2020-01-01T00:00:00Z")
    assert store.touch_api_session(conn, "old", expires_at="2099-01-01T00:00:00Z") is None


def test_touching_a_session_slides_its_expiry(conn):
    store.create_api_session(conn, "s1", expires_at="2099-01-01T00:00:00Z")
    store.touch_api_session(conn, "s1", expires_at="2099-06-01T00:00:00Z")

    row = conn.execute("SELECT * FROM api_sessions WHERE id = 's1'").fetchone()
    assert row["expires_at"] == "2099-06-01T00:00:00Z"


def test_a_session_can_be_revoked(conn):
    """The whole reason sessions are rows rather than a signed cookie."""
    store.create_api_session(conn, "s1", expires_at="2099-01-01T00:00:00Z")
    assert store.delete_api_session(conn, "s1") is True
    assert store.count_api_sessions(conn) == 0
    assert store.delete_api_session(conn, "s1") is False


def test_the_sweep_drops_only_expired_sessions(conn):
    store.create_api_session(conn, "live", expires_at="2099-01-01T00:00:00Z")
    store.create_api_session(conn, "dead", expires_at="2020-01-01T00:00:00Z")

    assert store.sweep_api_sessions(conn) == 1
    remaining = [row["id"] for row in conn.execute("SELECT id FROM api_sessions")]
    assert remaining == ["live"]


def test_foreign_keys_are_enforced(conn):
    """PRAGMA foreign_keys is connection-scoped -- easy to declare and not enable."""
    work, _ = parse_coursework({"id": "w1", "title": "TD"}, "no-such-course")
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_coursework(conn, work)


def test_busy_timeout_is_set(conn):
    """Connection-scoped like foreign_keys, and just as easy to lose."""
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == store.BUSY_TIMEOUT_MS


def test_a_second_writer_waits_for_the_first(tmp_path):
    """Two writers serialise, and the loser must wait rather than fail.

    This is what the server makes routine: the 19:30 `agent run` writes in
    bursts while the always-on `agent bot` is trying to commit a button press.
    Without the timeout the bot raises "database is locked" and the tap does
    nothing, which is the silent failure this project likes least.
    """
    path = tmp_path / "academic.db"
    store.connect(path).close()

    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(INSERT_COURSE, ("c1", "held"))

    failure: list[BaseException] = []

    def write() -> None:
        try:
            other = store.connect(path)
            other.execute(INSERT_COURSE, ("c2", "waited"))
            other.commit()
            other.close()
        except BaseException as err:  # noqa: BLE001 -- reported, not swallowed
            failure.append(err)

    writer = threading.Thread(target=write)
    writer.start()
    time.sleep(0.2)
    holder.commit()
    holder.close()
    writer.join(timeout=10)

    assert not writer.is_alive(), "the second writer never finished"
    assert not failure, f"the second writer raised {failure[0]!r}"

    check = store.connect(path)
    assert store.count_rows(check, "courses") == 2
    check.close()


def test_the_contention_is_real(tmp_path):
    """The control for the test above.

    Without it, a second writer that never actually contended would pass and
    the timeout could be deleted with the suite still green.
    """
    path = tmp_path / "academic.db"
    store.connect(path).close()

    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(INSERT_COURSE, ("c1", "held"))

    impatient = sqlite3.connect(path, timeout=0)
    with pytest.raises(sqlite3.OperationalError):
        impatient.execute(INSERT_COURSE, ("c2", "nope"))
    impatient.close()

    holder.rollback()
    holder.close()


def test_events_dedupe_index_rejects_a_repeat(conn):
    """The deadline scanner is stateless and leans on this index."""
    row = ("deadline_t24", "coursework", "w1", "c1", "{}", "2025-05-01T00:00:00Z")
    sql = ("INSERT INTO events (type, entity_type, entity_id, course_id, payload, "
           "created_at) VALUES (?, ?, ?, ?, ?, ?)")
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)


def test_material_kind_is_constrained(conn):
    store.upsert_course(conn, a_course())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO materials (id, parent_type, parent_id, course_id, kind, ref, "
            "content_hash, first_seen_at) VALUES ('x','coursework','w1','c1','pdf','r','h','t')"
        )


def test_parent_type_is_constrained(conn):
    store.upsert_course(conn, a_course())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO materials (id, parent_type, parent_id, course_id, kind, ref, "
            "content_hash, first_seen_at) VALUES ('x','quiz','w1','c1','link','r','h','t')"
        )


# --------------------------------------------------------------------------
# upserts are idempotent
# --------------------------------------------------------------------------

def test_upserting_a_course_twice_makes_one_row(conn):
    store.upsert_course(conn, a_course())
    store.upsert_course(conn, a_course())
    assert store.count_rows(conn, "courses") == 1


def test_upsert_updates_changed_fields(conn):
    store.upsert_course(conn, a_course(name="Databases"))
    store.upsert_course(conn, a_course(name="Databases II"))

    (row,) = store.list_courses(conn)
    assert row["name"] == "Databases II"
    assert store.count_rows(conn, "courses") == 1


def test_upsert_preserves_first_seen_at(conn):
    """When we first saw something is history, not something a re-sync rewrites."""
    store.upsert_course(conn, a_course(), now="2025-01-01T00:00:00Z")
    store.upsert_course(conn, a_course(name="Renamed"), now="2025-06-06T00:00:00Z")

    (row,) = store.list_courses(conn)
    assert row["first_seen_at"] == "2025-01-01T00:00:00Z"
    assert row["name"] == "Renamed"


def test_upserting_every_content_table_twice_makes_one_row_each(conn):
    """Running a sync twice must not duplicate anything. Invariant, not detail."""
    store.upsert_course(conn, a_course())

    work, work_materials = parse_coursework(
        {"id": "w1", "title": "TD 3", "materials": [DRIVE]}, "c1"
    )
    material, material_attachments = parse_coursework_material(
        {"id": "m1", "title": "Slides", "materials": [DRIVE]}, "c1"
    )
    announcement, announcement_attachments = parse_announcement(
        {"id": "a1", "text": "Week 3", "materials": [DRIVE]}, "c1"
    )
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN"}
    )

    for _ in range(2):
        store.upsert_course(conn, a_course())
        store.upsert_coursework(conn, work)
        store.upsert_coursework_material(conn, material)
        store.upsert_announcement(conn, announcement)
        store.upsert_submission(conn, submission)
        store.upsert_materials(conn, work_materials)
        store.upsert_materials(conn, material_attachments)
        store.upsert_materials(conn, announcement_attachments)

    assert store.count_rows(conn, "courses") == 1
    assert store.count_rows(conn, "coursework") == 1
    assert store.count_rows(conn, "coursework_materials") == 1
    assert store.count_rows(conn, "announcements") == 1
    assert store.count_rows(conn, "submissions") == 1
    # The same Drive file under three different parents is three attachments.
    assert store.count_rows(conn, "materials") == 3


def test_same_drive_file_under_two_parents_stays_two_rows(conn):
    store.upsert_course(conn, a_course())
    _, from_work = parse_coursework({"id": "w1", "materials": [DRIVE]}, "c1")
    _, from_announcement = parse_announcement({"id": "a1", "materials": [DRIVE]}, "c1")

    store.upsert_materials(conn, from_work)
    store.upsert_materials(conn, from_announcement)

    rows = conn.execute("SELECT parent_type FROM materials ORDER BY parent_type").fetchall()
    assert [row["parent_type"] for row in rows] == ["announcement", "coursework"]


def test_submission_needs_its_coursework_first(conn):
    """Submissions arrive via courseWorkId='-', so ordering is a real constraint."""
    store.upsert_course(conn, a_course())
    submission = parse_submission({"id": "s1", "courseId": "c1", "courseWorkId": "w1"})

    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_submission(conn, submission)

    work, _ = parse_coursework({"id": "w1"}, "c1")
    store.upsert_coursework(conn, work)
    store.upsert_submission(conn, submission)
    assert store.count_rows(conn, "submissions") == 1


def test_null_due_at_round_trips(conn):
    """No deadline is the common case, not an error state."""
    store.upsert_course(conn, a_course())
    work, _ = parse_coursework({"id": "w1", "title": "No deadline"}, "c1")
    store.upsert_coursework(conn, work)

    row = conn.execute("SELECT due_at FROM coursework WHERE id = 'w1'").fetchone()
    assert row["due_at"] is None


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def test_list_courses_is_ordered_by_name(conn):
    for course_id, name in [("3", "Zoology"), ("1", "algebra"), ("2", "Databases")]:
        store.upsert_course(conn, a_course(course_id, name))

    assert [row["name"] for row in store.list_courses(conn)] == \
        ["algebra", "Databases", "Zoology"]


def test_get_course(conn):
    store.upsert_course(conn, a_course("c9", "Networks"))
    assert store.get_course(conn, "c9")["name"] == "Networks"
    assert store.get_course(conn, "nope") is None


def test_count_rows_rejects_a_non_identifier(conn):
    with pytest.raises(ValueError):
        store.count_rows(conn, "courses; DROP TABLE courses")


# --------------------------------------------------------------------------
# sync runs
# --------------------------------------------------------------------------

def test_sync_run_starts_as_running(conn):
    run_id = store.start_sync_run(conn)
    (row,) = store.recent_sync_runs(conn)
    assert row["id"] == run_id
    assert row["status"] == "running"
    assert row["finished_at"] is None


def test_finish_sync_run_records_the_tally(conn):
    run_id = store.start_sync_run(conn)
    store.finish_sync_run(conn, run_id, status="ok", items_seen={"courses": 25},
                          events_emitted=3)

    (row,) = store.recent_sync_runs(conn)
    assert row["status"] == "ok"
    assert row["finished_at"] is not None
    assert json.loads(row["items_seen"]) == {"courses": 25}
    assert row["events_emitted"] == 3


def test_finish_sync_run_records_an_error(conn):
    run_id = store.start_sync_run(conn)
    store.finish_sync_run(conn, run_id, status="error", error="HttpError 403")

    (row,) = store.recent_sync_runs(conn)
    assert row["status"] == "error"
    assert "403" in row["error"]


def test_finish_sync_run_rejects_an_unknown_status(conn):
    run_id = store.start_sync_run(conn)
    with pytest.raises(ValueError):
        store.finish_sync_run(conn, run_id, status="probably fine")


def test_open_db_uses_the_configured_path(tmp_path):
    class FakeConfig:
        db_path = tmp_path / "nested" / "academic.db"

    connection = store.open_db(FakeConfig())
    assert FakeConfig.db_path.is_file()
    connection.close()


# Version 2 is deliberately absent: it is the one version a step exists for,
# and _migrate_2_to_3 carries it forward instead of refusing it. That path is
# covered end to end in test_migration.py.
@pytest.mark.parametrize("version,expected", [(4, "newer"), (1, "older")])
def test_a_mismatched_schema_version_is_refused(tmp_path, version, expected):
    """CREATE TABLE IF NOT EXISTS cannot add a column to an existing table.

    An older file is silently missing columns the code now reads, which would
    surface much later as an opaque "no such column". Refusing at open time with
    instructions is the only honest option for a version nothing knows how to
    carry forward -- and one hand-written step for one version is not a
    migration framework.
    """
    path = tmp_path / "academic.db"
    connection = store.connect(path)
    connection.execute("UPDATE schema_version SET version = ? WHERE id = 1", (version,))
    connection.commit()
    connection.close()

    with pytest.raises(store.StoreError) as err:
        store.connect(path)

    assert expected in str(err.value)
    assert "No step exists to carry version" in str(err.value)


# --------------------------------------------------------------------------
# links and notification stamping
# --------------------------------------------------------------------------

def _link_event(conn, entity_type, entity_id, event_type="new_material"):
    from agent.sync.differ import Event

    store.insert_event(
        conn,
        Event(
            type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            course_id="c1",
            payload={},
            created_at="2026-08-27T12:00:00Z",
        ),
    )
    conn.commit()


def test_entity_links_finds_the_link_for_each_entity_type(tmp_path):
    from agent.classroom.models import (
        parse_announcement,
        parse_course,
        parse_coursework,
        parse_coursework_material,
    )

    conn = store.connect(tmp_path / "academic.db")
    store.upsert_course(conn, parse_course({"id": "c1", "name": "Databases"}))

    work, _ = parse_coursework(
        {"id": "w1", "title": "TD", "alternateLink": "https://x.test/w1"}, "c1"
    )
    store.upsert_coursework(conn, work)
    material, _ = parse_coursework_material(
        {"id": "m1", "title": "L7", "alternateLink": "https://x.test/m1"}, "c1"
    )
    store.upsert_coursework_material(conn, material)
    announcement, _ = parse_announcement(
        {"id": "a1", "text": "hi", "alternateLink": "https://x.test/a1"}, "c1"
    )
    store.upsert_announcement(conn, announcement)
    conn.commit()

    _link_event(conn, "coursework", "w1")
    _link_event(conn, "coursework_material", "m1")
    _link_event(conn, "announcement", "a1")

    links = store.entity_links(conn, store.list_events(conn))

    assert links[("coursework", "w1")] == "https://x.test/w1"
    assert links[("coursework_material", "m1")] == "https://x.test/m1"
    assert links[("announcement", "a1")] == "https://x.test/a1"
    conn.close()


def test_entity_links_omits_entities_it_cannot_find(tmp_path):
    from agent.classroom.models import parse_course

    conn = store.connect(tmp_path / "academic.db")
    store.upsert_course(conn, parse_course({"id": "c1", "name": "Databases"}))
    _link_event(conn, "coursework", "gone")

    assert store.entity_links(conn, store.list_events(conn)) == {}
    conn.close()


def test_entity_links_of_no_events_is_empty(tmp_path):
    conn = store.connect(tmp_path / "academic.db")
    assert store.entity_links(conn, []) == {}
    conn.close()


def test_mark_notified_only_touches_the_given_events(tmp_path):
    from agent.classroom.models import parse_course

    conn = store.connect(tmp_path / "academic.db")
    store.upsert_course(conn, parse_course({"id": "c1", "name": "Databases"}))
    _link_event(conn, "coursework", "w1")
    _link_event(conn, "coursework", "w2")

    rows = store.list_events(conn)
    target = rows[0]["id"]

    assert store.mark_notified(conn, [target], now="2026-08-27T13:00:00Z") == 1
    assert store.count_pending_events(conn) == 1
    conn.close()
