"""Repository layer over plain sqlite3. Storage only -- no business logic.

Nothing here decides what changed or what is worth telling anyone about. It
writes rows, reads rows, and keeps first_seen_at honest.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..classroom.models import (
    Announcement,
    Course,
    CourseWork,
    CourseWorkMaterial,
    Material,
    Submission,
)
from ..config import Config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2

# Tables the differ reconciles against live state, and the only ones
# soft_delete_missing and load_rows will touch.
RESOURCE_TABLES = frozenset(
    {"coursework", "coursework_materials", "announcements", "submissions", "materials"}
)


class StoreError(Exception):
    """The database is unusable -- wrong schema version, or it will not open."""


def _utc_now_iso() -> str:
    """Now, in the UTC ISO-8601 form every timestamp column uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# opening
# --------------------------------------------------------------------------

def open_db(config: Config) -> sqlite3.Connection:
    """Open the database, applying schema.sql idempotently."""
    return connect(config.db_path)


def connect(db_path: Path) -> sqlite3.Connection:
    """The path-level entry point, so tests can point at a temp file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Connection-scoped and a no-op inside a transaction, so it has to be set
    # here rather than in schema.sql, and before the script runs.
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()

    found = schema_version(conn)
    if found != SCHEMA_VERSION:
        conn.close()
        # CREATE TABLE IF NOT EXISTS cannot add a column to a table that already
        # exists, so an older file is missing columns the code now reads and
        # would fail later with an opaque "no such column". There is no
        # migration framework by design; the Classroom mirror rebuilds from the
        # API in one sync, which is why deleting is an acceptable answer.
        direction = "newer" if found > SCHEMA_VERSION else "older"
        raise StoreError(
            f"{db_path} has schema version {found}; this build expects "
            f"{SCHEMA_VERSION} (the file is {direction}).\n"
            f"There is no migration framework. Delete the database and re-run "
            f"`agent courses` then `agent sync --seed` to rebuild it.\n"
            f"Note that events, study_items and quiz_attempts cannot be "
            f"rebuilt from the API -- check they are empty before deleting."
        )
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    return int(row["version"]) if row else 0


# --------------------------------------------------------------------------
# upserts
# --------------------------------------------------------------------------

# first_seen_at answers "when did we first see this", so an update must never
# touch it. id is the conflict target and never reassigned.
_IMMUTABLE = frozenset({"id", "first_seen_at"})


def _upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    """INSERT ... ON CONFLICT(id) DO UPDATE, preserving first_seen_at.

    Table and column names come from this module's own literals, never from
    input, so interpolating them into the statement is safe. Values are always
    bound.
    """
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(
        f"{column} = excluded.{column}" for column in columns if column not in _IMMUTABLE
    )
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {assignments}",
        [row[column] for column in columns],
    )


def upsert_course(conn: sqlite3.Connection, course: Course, *, now: str | None = None) -> None:
    _upsert(
        conn,
        "courses",
        {
            "id": course.id,
            "name": course.name,
            "section": course.section,
            "room": course.room,
            "owner_id": course.owner_id,
            "course_state": course.course_state,
            "enrollment_code": course.enrollment_code,
            "alternate_link": course.alternate_link,
            "creation_time": course.creation_time,
            "update_time": course.update_time,
            "content_hash": course.content_hash,
            "first_seen_at": now or _utc_now_iso(),
        },
    )


def upsert_coursework(conn: sqlite3.Connection, work: CourseWork, *, now: str | None = None) -> None:
    _upsert(
        conn,
        "coursework",
        {
            "id": work.id,
            "course_id": work.course_id,
            "title": work.title,
            "description": work.description,
            "state": work.state,
            "work_type": work.work_type,
            "topic_id": work.topic_id,
            "max_points": work.max_points,
            "due_at": work.due_at,
            "alternate_link": work.alternate_link,
            "creation_time": work.creation_time,
            "update_time": work.update_time,
            "content_hash": work.content_hash,
            "first_seen_at": now or _utc_now_iso(),
            "deleted_at": None,
        },
    )


def upsert_coursework_material(
    conn: sqlite3.Connection, material: CourseWorkMaterial, *, now: str | None = None
) -> None:
    _upsert(
        conn,
        "coursework_materials",
        {
            "id": material.id,
            "course_id": material.course_id,
            "title": material.title,
            "description": material.description,
            "state": material.state,
            "topic_id": material.topic_id,
            "alternate_link": material.alternate_link,
            "creation_time": material.creation_time,
            "update_time": material.update_time,
            "content_hash": material.content_hash,
            "first_seen_at": now or _utc_now_iso(),
            "deleted_at": None,
        },
    )


def upsert_announcement(
    conn: sqlite3.Connection, announcement: Announcement, *, now: str | None = None
) -> None:
    _upsert(
        conn,
        "announcements",
        {
            "id": announcement.id,
            "course_id": announcement.course_id,
            "text": announcement.text,
            "state": announcement.state,
            "alternate_link": announcement.alternate_link,
            "creation_time": announcement.creation_time,
            "update_time": announcement.update_time,
            "content_hash": announcement.content_hash,
            "first_seen_at": now or _utc_now_iso(),
            "deleted_at": None,
        },
    )


def upsert_submission(
    conn: sqlite3.Connection, submission: Submission, *, now: str | None = None
) -> None:
    _upsert(
        conn,
        "submissions",
        {
            "id": submission.id,
            "course_id": submission.course_id,
            "coursework_id": submission.coursework_id,
            "state": submission.state,
            "late": int(submission.late),
            "assigned_grade": submission.assigned_grade,
            "alternate_link": submission.alternate_link,
            "creation_time": submission.creation_time,
            "update_time": submission.update_time,
            "content_hash": submission.content_hash,
            "first_seen_at": now or _utc_now_iso(),
            "deleted_at": None,
        },
    )


def upsert_material(conn: sqlite3.Connection, material: Material, *, now: str | None = None) -> None:
    """Insert or update one attachment.

    trashed and fetch_error are left alone on update: they are Drive-side facts
    discovered when the bytes are fetched, not Classroom-side facts that arrive
    with the parent, so a Classroom re-sync must not wipe them.
    """
    _upsert(
        conn,
        "materials",
        {
            "id": material.id,
            "parent_type": material.parent_type,
            "parent_id": material.parent_id,
            "course_id": material.course_id,
            "kind": material.kind,
            "ref": material.ref,
            "drive_id": material.drive_id,
            "title": material.title,
            "url": material.url,
            "content_hash": material.content_hash,
            "first_seen_at": now or _utc_now_iso(),
            "deleted_at": None,
        },
    )


def upsert_materials(
    conn: sqlite3.Connection, materials: Iterable[Material], *, now: str | None = None
) -> None:
    for material in materials:
        upsert_material(conn, material, now=now)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def list_courses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every stored course, ordered by name. Filtering is the caller's job."""
    return conn.execute("SELECT * FROM courses ORDER BY name COLLATE NOCASE").fetchall()


def get_course(conn: sqlite3.Connection, course_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    """Row count for one table. Used by sync reporting and by tests."""
    if not table.isidentifier():
        raise ValueError(f"not a table name: {table!r}")
    return int(conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])


# --------------------------------------------------------------------------
# sync runs
# --------------------------------------------------------------------------

def start_sync_run(conn: sqlite3.Connection, *, now: str | None = None) -> int:
    """Open a sync_runs row and return its id.

    Written before any work happens, so a crash mid-sync leaves a row stuck in
    'running' rather than no evidence at all.
    """
    cursor = conn.execute(
        "INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')",
        (now or _utc_now_iso(),),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    items_seen: dict[str, int] | None = None,
    events_emitted: int = 0,
    error: str | None = None,
    now: str | None = None,
) -> None:
    if status not in {"ok", "error"}:
        raise ValueError(f"status must be 'ok' or 'error', got {status!r}")
    conn.execute(
        "UPDATE sync_runs SET finished_at = ?, status = ?, items_seen = ?, "
        "events_emitted = ?, error = ? WHERE id = ?",
        (
            now or _utc_now_iso(),
            status,
            json.dumps(items_seen, sort_keys=True) if items_seen is not None else None,
            events_emitted,
            error,
            run_id,
        ),
    )
    conn.commit()


def recent_sync_runs(conn: sqlite3.Connection, limit: int = 10) -> Sequence[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sync_runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()


# --------------------------------------------------------------------------
# state for the differ
# --------------------------------------------------------------------------

def _check_resource_table(table: str) -> str:
    if table not in RESOURCE_TABLES:
        raise ValueError(f"not a reconcilable resource table: {table!r}")
    return table


def load_rows(conn: sqlite3.Connection, table: str, course_id: str) -> dict[str, sqlite3.Row]:
    """Every stored row for one course, keyed by ID -- including soft-deleted.

    Soft-deleted rows are included on purpose: if a teacher restores a post, it
    is the same item coming back, not a brand new one, and re-announcing it
    would be a duplicate.
    """
    _check_resource_table(table)
    rows = conn.execute(f"SELECT * FROM {table} WHERE course_id = ?", (course_id,)).fetchall()
    return {row["id"]: row for row in rows}


def soft_delete_missing(
    conn: sqlite3.Connection,
    table: str,
    course_id: str,
    live_ids: Iterable[str],
    *,
    now: str | None = None,
) -> list[str]:
    """Stamp deleted_at on stored rows that the live state no longer contains.

    Rows are never removed. Returns the IDs newly marked, so the caller can
    report them; no event is emitted for a deletion in this phase.
    """
    _check_resource_table(table)
    live = set(live_ids)
    stored = conn.execute(
        f"SELECT id FROM {table} WHERE course_id = ? AND deleted_at IS NULL", (course_id,)
    ).fetchall()

    missing = [row["id"] for row in stored if row["id"] not in live]
    stamp = now or _utc_now_iso()
    for entity_id in missing:
        conn.execute(f"UPDATE {table} SET deleted_at = ? WHERE id = ?", (stamp, entity_id))
    return missing


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def insert_event(conn: sqlite3.Connection, event, *, notified_at: str | None = None) -> bool:
    """Append one event. Returns False if the dedupe index already had it.

    The unique index on (type, entity_type, entity_id, created_at) is what makes
    a stateless re-scan safe, so a collision is an expected outcome rather than
    an error.
    """
    try:
        conn.execute(
            "INSERT INTO events (type, entity_type, entity_id, course_id, payload, "
            "created_at, notified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.type,
                event.entity_type,
                event.entity_id,
                event.course_id,
                json.dumps(event.payload, sort_keys=True, default=str),
                event.created_at,
                notified_at,
            ),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def count_events(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"])


def count_pending_events(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT count(*) AS n FROM events WHERE notified_at IS NULL"
        ).fetchone()["n"]
    )


def list_events(
    conn: sqlite3.Connection, *, include_notified: bool = False, limit: int | None = None
) -> list[sqlite3.Row]:
    """Events newest first, with the course name joined on for display.

    Selection for sending is always notified_at IS NULL; include_notified is for
    looking at history, never for deciding what to send.
    """
    sql = (
        "SELECT e.*, c.name AS course_name FROM events e "
        "LEFT JOIN courses c ON c.id = e.course_id"
    )
    if not include_notified:
        sql += " WHERE e.notified_at IS NULL"
    sql += " ORDER BY e.created_at DESC, e.id DESC"

    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()
