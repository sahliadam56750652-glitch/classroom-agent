"""The schema 2 -> 3 rebuild of study_items.

This is the only destructive operation in the project, run against the only
file that cannot be rebuilt from anywhere. `events.notified_at` is the record
of which alerts were already sent and `study_items` the record of what has
actually been revised; the Classroom mirror around them re-syncs in one call,
but those two do not.

The real database it will run against holds 67 study items -- all skipped, all
seeded -- and 113 events, every one already notified. The fixture below is that
shape in miniature.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from agent.db import store

# study_items as it stood at version 2: no `reviewed` in the CHECK, no
# reviewed_at, no skip_source. Copied verbatim rather than imported, because
# the point is to reproduce a file this build can no longer create.
V2_STUDY_ITEMS = """
CREATE TABLE study_items (
    id           INTEGER PRIMARY KEY,
    entity_type  TEXT NOT NULL
        CHECK (entity_type IN ('coursework', 'coursework_material', 'announcement')),
    entity_id    TEXT NOT NULL,
    course_id    TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    state        TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'verified', 'skipped')),
    skip_reason  TEXT,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    verified_at  TEXT,
    UNIQUE (entity_type, entity_id)
);
CREATE INDEX ix_study_items_course_state ON study_items (course_id, state);
"""

SEED_REASON = "backlog before the gate existed"


def build_v2(path, *, items=(), events=2):
    """A version-2 database with study items, events and a quiz attempt."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (1, 2, '2026-01-01T00:00:00Z');
        CREATE TABLE courses (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, section TEXT, room TEXT,
            owner_id TEXT, course_state TEXT NOT NULL, enrollment_code TEXT,
            alternate_link TEXT, creation_time TEXT, update_time TEXT,
            content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL);
        INSERT INTO courses (id, name, course_state, content_hash, first_seen_at)
        VALUES ('c1', 'Operating Systems', 'ACTIVE', 'h', '2026-01-01T00:00:00Z');
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, type TEXT NOT NULL, entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL, course_id TEXT, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, notified_at TEXT);
        CREATE TABLE quiz_attempts (
            id INTEGER PRIMARY KEY,
            study_item_id INTEGER NOT NULL REFERENCES study_items (id) ON DELETE CASCADE,
            started_at TEXT NOT NULL, finished_at TEXT, score REAL, passed INTEGER,
            questions TEXT, flagged INTEGER NOT NULL DEFAULT 0);
        """
        + V2_STUDY_ITEMS
    )
    for index in range(events):
        conn.execute(
            "INSERT INTO events (type, entity_type, entity_id, course_id, payload, "
            "created_at, notified_at) VALUES ('new_material', 'coursework_material', "
            "?, 'c1', '{}', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')",
            (f"e{index}",),
        )
    for item in items:
        conn.execute(
            "INSERT INTO study_items (id, entity_type, entity_id, course_id, state, "
            "skip_reason, created_at, delivered_at, verified_at) "
            "VALUES (?, ?, ?, 'c1', ?, ?, ?, ?, ?)",
            item,
        )
    conn.commit()
    conn.close()
    return path


# (id, entity_type, entity_id, state, skip_reason, created_at, delivered_at, verified_at)
SEEDED = (1, "coursework_material", "m1", "skipped", SEED_REASON, "2026-01-01T00:00:00Z", None, None)
DUCKED = (2, "announcement", "a1", "skipped", "not tonight", "2026-01-01T00:00:00Z",
          "2026-01-03T00:00:00Z", None)
DONE = (3, "coursework", "w1", "verified", None, "2026-01-01T00:00:00Z",
        "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z")
WAITING = (4, "coursework_material", "m2", "pending", None, "2026-01-01T00:00:00Z", None, None)


@pytest.fixture
def v2(tmp_path):
    return build_v2(tmp_path / "academic.db", items=(SEEDED, DUCKED, DONE, WAITING))


def rows(conn):
    return {row["id"]: row for row in conn.execute("SELECT * FROM study_items")}


# --------------------------------------------------------------- the happy path

def test_every_study_item_survives_the_rebuild(v2):
    conn = store.connect(v2)
    try:
        assert store.schema_version(conn) == 3
        assert len(rows(conn)) == 4
    finally:
        conn.close()


def test_every_notified_at_survives(v2):
    """The 113 stamps in the real file are the only record of what was sent."""
    conn = store.connect(v2)
    try:
        pending = conn.execute(
            "SELECT count(*) AS n FROM events WHERE notified_at IS NULL"
        ).fetchone()["n"]
        assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 2
        assert pending == 0
    finally:
        conn.close()


def test_ids_and_timestamps_are_carried_verbatim(v2):
    conn = store.connect(v2)
    try:
        done = rows(conn)[3]
        assert done["entity_id"] == "w1"
        assert done["state"] == "verified"
        assert done["delivered_at"] == "2026-01-03T00:00:00Z"
        assert done["verified_at"] == "2026-01-04T00:00:00Z"
    finally:
        conn.close()


def test_reviewed_at_starts_null_for_everything(v2):
    """There is no honest value to invent: nothing before v3 recorded it."""
    conn = store.connect(v2)
    try:
        assert all(row["reviewed_at"] is None for row in rows(conn).values())
    finally:
        conn.close()


def test_a_seed_skip_and_a_real_skip_are_told_apart(v2):
    """The last moment this is knowable. Phase 4 has to exclude a finished
    academic year from its coverage denominator without also excluding the
    times I ducked the gate."""
    conn = store.connect(v2)
    try:
        current = rows(conn)
        assert current[1]["skip_source"] == "seed"
        assert current[2]["skip_source"] == "user"
        assert current[2]["skip_reason"] == "not tonight"
    finally:
        conn.close()


def test_unskipped_items_get_no_skip_source(v2):
    conn = store.connect(v2)
    try:
        current = rows(conn)
        assert current[3]["skip_source"] is None
        assert current[4]["skip_source"] is None
    finally:
        conn.close()


def test_the_new_state_is_accepted_afterwards(v2):
    conn = store.connect(v2)
    try:
        conn.execute("UPDATE study_items SET state = 'reviewed' WHERE id = 4")
        assert rows(conn)[4]["state"] == "reviewed"
    finally:
        conn.close()


def test_the_check_constraint_still_refuses_nonsense(v2):
    conn = store.connect(v2)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE study_items SET state = 'nearly' WHERE id = 4")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE study_items SET skip_source = 'somewhere' WHERE id = 1")
    finally:
        conn.close()


def test_the_unique_identity_survives(v2):
    conn = store.connect(v2)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO study_items (entity_type, entity_id, course_id, "
                "created_at) VALUES ('coursework_material', 'm1', 'c1', 'now')"
            )
    finally:
        conn.close()


def test_the_index_is_recreated(v2):
    conn = store.connect(v2)
    try:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("ix_study_items_course_state",),
        ).fetchone()
        assert found is not None
    finally:
        conn.close()


def test_quiz_attempt_references_still_resolve(tmp_path):
    """quiz_attempts FKs study_items(id), and the rebuild drops that table."""
    path = build_v2(tmp_path / "academic.db", items=(DONE,))
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO quiz_attempts (id, study_item_id, started_at) "
        "VALUES (1, 3, '2026-01-04T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    conn = store.connect(path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        joined = conn.execute(
            "SELECT s.entity_id FROM quiz_attempts q "
            "JOIN study_items s ON s.id = q.study_item_id"
        ).fetchone()
        assert joined["entity_id"] == "w1"
    finally:
        conn.close()


def test_foreign_keys_are_on_again_afterwards(v2):
    conn = store.connect(v2)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


# --------------------------------------------------------------- idempotence

def test_running_it_twice_is_a_no_op(v2):
    store.connect(v2).close()
    conn = store.connect(v2)
    try:
        assert store.schema_version(conn) == 3
        assert len(rows(conn)) == 4
        assert rows(conn)[1]["skip_source"] == "seed"
    finally:
        conn.close()


def test_a_fresh_database_is_created_at_version_3(tmp_path):
    conn = store.connect(tmp_path / "new.db")
    try:
        assert store.schema_version(conn) == 3
    finally:
        conn.close()


def test_a_fresh_database_has_no_timetable_table(tmp_path):
    """The table moved to YAML. A new file must not resurrect it."""
    conn = store.connect(tmp_path / "new.db")
    try:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'timetable'"
        ).fetchone()
        assert found is None
    finally:
        conn.close()


def test_an_existing_empty_timetable_table_is_left_alone(v2):
    """Orphaned rather than dropped: a DROP here would run on every open, for
    every future file, to tidy one empty table once."""
    conn = sqlite3.connect(v2)
    conn.execute("CREATE TABLE timetable (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    conn = store.connect(v2)
    try:
        assert conn.execute("SELECT count(*) AS n FROM timetable").fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------- the backup

def test_a_backup_is_written_before_anything_changes(v2):
    backup = v2.with_name(v2.name + ".bak-v2")
    assert not backup.exists()
    store.connect(v2).close()

    assert backup.exists()
    conn = sqlite3.connect(backup)
    try:
        # The copy is the file as it was: still version 2, still four items.
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM study_items").fetchone()[0] == 4
    finally:
        conn.close()


def test_the_backup_holds_writes_that_were_still_in_the_wal(tmp_path):
    """A plain copy of an un-checkpointed WAL database is a backup missing the
    newest rows -- exactly the backup that looks fine until it is needed."""
    path = build_v2(tmp_path / "academic.db", items=(SEEDED,))
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        "INSERT INTO study_items (id, entity_type, entity_id, course_id, state, "
        "created_at) VALUES (9, 'coursework', 'late', 'c1', 'pending', 'now')"
    )
    conn.commit()
    conn.close()

    store.connect(path).close()
    conn = sqlite3.connect(path.with_name(path.name + ".bak-v2"))
    try:
        assert conn.execute(
            "SELECT count(*) FROM study_items WHERE entity_id = 'late'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_it_refuses_to_migrate_when_no_backup_can_be_written(v2, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(shutil, "copy2", refuse)
    with pytest.raises(store.StoreError) as err:
        store.connect(v2)
    assert "backup" in str(err.value)

    # And the file is untouched: still version 2, still openable next time.
    conn = sqlite3.connect(v2)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM study_items").fetchone()[0] == 4
    finally:
        conn.close()


# --------------------------------------------------------------- the guard

def test_an_unknown_older_version_is_still_refused(tmp_path):
    """_migrate_2_to_3 is one hand-written step, not a migration framework."""
    path = build_v2(tmp_path / "academic.db")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 1")
    conn.commit()
    conn.close()

    with pytest.raises(store.StoreError) as err:
        store.connect(path)
    assert "version 1" in str(err.value)
    assert "cannot be rebuilt from the API" in str(err.value)


def test_a_newer_version_is_refused(tmp_path):
    path = build_v2(tmp_path / "academic.db")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 9")
    conn.commit()
    conn.close()

    with pytest.raises(store.StoreError, match="newer"):
        store.connect(path)
