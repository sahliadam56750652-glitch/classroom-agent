"""Storage layer: schema application, idempotent upserts, sync runs."""

from __future__ import annotations

import json
import sqlite3

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
     "materials", "study_items", "events", "timetable", "quiz_attempts", "sync_runs"],
)
def test_every_expected_table_exists(conn, table):
    assert store.count_rows(conn, table) == 0


def test_foreign_keys_are_enforced(conn):
    """PRAGMA foreign_keys is connection-scoped -- easy to declare and not enable."""
    work, _ = parse_coursework({"id": "w1", "title": "TD"}, "no-such-course")
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_coursework(conn, work)


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


@pytest.mark.parametrize("offset,expected", [(1, "newer"), (-1, "older")])
def test_a_mismatched_schema_version_is_refused(tmp_path, offset, expected):
    """CREATE TABLE IF NOT EXISTS cannot add a column to an existing table.

    An older file is silently missing columns the code now reads, which would
    surface much later as an opaque "no such column". Refusing at open time with
    instructions is the only honest option without a migration framework.
    """
    path = tmp_path / "academic.db"
    connection = store.connect(path)
    connection.execute("UPDATE schema_version SET version = ? WHERE id = 1",
                       (store.SCHEMA_VERSION + offset,))
    connection.commit()
    connection.close()

    with pytest.raises(store.StoreError) as err:
        store.connect(path)

    assert expected in str(err.value)
    assert "no migration framework" in str(err.value)
