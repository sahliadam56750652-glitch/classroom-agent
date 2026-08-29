"""Repository layer over plain sqlite3. Storage only -- no business logic.

Nothing here decides what changed or what is worth telling anyone about. It
writes rows, reads rows, and keeps first_seen_at honest.
"""

from __future__ import annotations

import json
import shutil
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
SCHEMA_VERSION = 3

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

    # After the script, because the script is what creates schema_version on a
    # fresh file, and before the guard below, because migrating is exactly how
    # a file stops failing it.
    if schema_version(conn) == 2:
        _migrate_2_to_3(conn, db_path)

    found = schema_version(conn)
    if found != SCHEMA_VERSION:
        conn.close()
        # CREATE TABLE IF NOT EXISTS cannot add a column to a table that already
        # exists, so an older file is missing columns the code now reads and
        # would fail later with an opaque "no such column". There is still no
        # migration framework -- _migrate_2_to_3 above is one hand-written step
        # for one version, not the start of one -- and the Classroom mirror
        # rebuilds from the API in one sync, which is why deleting stays an
        # acceptable answer for a version nothing knows how to carry forward.
        direction = "newer" if found > SCHEMA_VERSION else "older"
        raise StoreError(
            f"{db_path} has schema version {found}; this build expects "
            f"{SCHEMA_VERSION} (the file is {direction}).\n"
            f"No step exists to carry version {found} forward. Delete the "
            f"database and re-run `agent courses` then `agent sync --seed` to "
            f"rebuild it.\n"
            f"Note that events, study_items and quiz_attempts cannot be "
            f"rebuilt from the API -- check they are empty before deleting."
        )
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    return int(row["version"]) if row else 0


# --------------------------------------------------------------------------
# migration
# --------------------------------------------------------------------------

# The reason `agent studyitems --seed` gives, and the only thing separating the
# historical backlog from a skip I actually chose. Duplicated from cli.py
# rather than imported: this has to keep meaning what it meant at the moment
# those rows were written, and an import would let a later rewording silently
# reclassify them.
_SEED_SKIP_REASON = "backlog before the gate existed"

_BACKUP_SUFFIX = ".bak-v2"

_STUDY_ITEMS_V3 = """
    CREATE TABLE study_items_v3 (
        id           INTEGER PRIMARY KEY,
        entity_type  TEXT NOT NULL
            CHECK (entity_type IN ('coursework', 'coursework_material', 'announcement')),
        entity_id    TEXT NOT NULL,
        course_id    TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
        state        TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'delivered', 'reviewed', 'verified', 'skipped')),
        skip_reason  TEXT,
        skip_source  TEXT CHECK (skip_source IN ('seed', 'user')),
        created_at   TEXT NOT NULL,
        delivered_at TEXT,
        reviewed_at  TEXT,
        verified_at  TEXT,
        UNIQUE (entity_type, entity_id)
    )
"""


def _migrate_2_to_3(conn: sqlite3.Connection, db_path: Path) -> None:
    """Rebuild study_items with the `reviewed` state. Version 2 -> 3.

    SQLite cannot alter a CHECK constraint, so widening the state column means
    create/copy/drop/rename. That is a destructive sequence run against the one
    file in this project that cannot be rebuilt from anywhere: `events` holds
    the only record of which alerts were already sent, and `study_items` the
    only record of what has actually been revised. So this takes a backup first
    and refuses to run if it cannot write one.

    Everything else happens in one transaction. A crash leaves a version-2 file
    that connect()'s guard refuses, which is recoverable; a half-migrated file
    that opens cleanly is not.
    """
    backup = db_path.with_name(db_path.name + _BACKUP_SUFFIX)
    try:
        # WAL keeps recent writes outside the .db file, so copying an
        # un-checkpointed database produces a backup missing the newest rows --
        # precisely the backup that looks fine until it is needed.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(db_path, backup)
    except (OSError, sqlite3.Error) as err:
        conn.close()
        raise StoreError(
            f"{db_path} is at schema version 2 and needs a one-time rebuild of "
            f"study_items to add the 'reviewed' state.\n"
            f"Refusing to start it, because the backup could not be written to "
            f"{backup}: {err}\n"
            f"events.notified_at and study_items cannot be rebuilt from the "
            f"API, so this migration does not run without one."
        ) from err

    try:
        # quiz_attempts references study_items(id), so the DROP below would
        # otherwise be refused. Ids are copied verbatim, so every reference is
        # still valid on the other side.
        conn.execute("PRAGMA foreign_keys = OFF")
        with conn:
            before = conn.execute("SELECT count(*) AS n FROM study_items").fetchone()["n"]
            conn.execute(_STUDY_ITEMS_V3)
            # skip_source is derived here because this is the last moment the
            # information exists: after the rebuild every skip looks alike, and
            # Phase 4 has to exclude a finished academic year from its coverage
            # denominator without also excluding the times I ducked the gate.
            conn.execute(
                "INSERT INTO study_items_v3 (id, entity_type, entity_id, course_id, "
                "state, skip_reason, skip_source, created_at, delivered_at, "
                "reviewed_at, verified_at) "
                "SELECT id, entity_type, entity_id, course_id, state, skip_reason, "
                "       CASE WHEN state != 'skipped' THEN NULL "
                "            WHEN skip_reason = ? THEN 'seed' "
                "            ELSE 'user' END, "
                "       created_at, delivered_at, NULL, verified_at "
                "  FROM study_items",
                (_SEED_SKIP_REASON,),
            )
            moved = conn.execute("SELECT count(*) AS n FROM study_items_v3").fetchone()["n"]
            if moved != before:
                # Unreachable through an INSERT with no WHERE. Checked anyway:
                # silently migrating 66 of 67 items would be indistinguishable
                # from a clean run, and that is the failure this project keeps
                # relearning.
                raise StoreError(f"would have moved {moved} of {before} study items")

            conn.execute("DROP TABLE study_items")
            conn.execute("ALTER TABLE study_items_v3 RENAME TO study_items")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_study_items_course_state "
                "ON study_items (course_id, state)"
            )
            conn.execute(
                "UPDATE schema_version SET version = 3, applied_at = ? WHERE id = 1",
                (_utc_now_iso(),),
            )

        # Only meaningful once the transaction has committed.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise StoreError(f"left {len(violations)} dangling reference(s)")
    except (sqlite3.Error, StoreError) as err:
        conn.close()
        raise StoreError(
            f"the schema 2 -> 3 migration of {db_path} failed: {err}\n"
            f"The change was rolled back, and a copy taken before it started "
            f"is at {backup}."
        ) from err
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


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
# extractions
# --------------------------------------------------------------------------

# Every column upsert_extraction knows how to write. Listed once so the INSERT
# and the ON CONFLICT clause cannot drift apart.
_EXTRACTION_FIELDS = (
    "status",
    "mime_type",
    "size_bytes",
    "md5_checksum",
    "modified_time",
    "local_path",
    "text_path",
    "method",
    "pages",
    "chars",
    "scan_pages",
    "ocr_pages",
    "fetched_at",
    "extracted_at",
    "error",
)


def drive_references(
    conn: sqlite3.Connection, course_ids: Sequence[str] | None = None
) -> list[sqlite3.Row]:
    """Live driveFile attachments, each with whatever is known about its file.

    One row per material reference, not per file -- deduplicating is the
    fetcher's job, because it is the thing that knows a file only needs
    downloading once. Soft-deleted materials are excluded: the post is gone
    from Classroom, so there is nothing to revise.
    """
    sql = [
        "SELECT m.id, m.drive_id, m.title, m.url, m.course_id,",
        "       m.parent_type, m.parent_id,",
        "       e.status AS known_status, e.md5_checksum AS known_md5,",
        "       e.modified_time AS known_modified, e.local_path AS known_path",
        "  FROM materials m",
        "  LEFT JOIN extractions e ON e.drive_id = m.drive_id",
        " WHERE m.kind = 'driveFile' AND m.drive_id IS NOT NULL",
        "   AND m.deleted_at IS NULL",
    ]
    params: list[Any] = []
    if course_ids is not None:
        placeholders = ", ".join("?" for _ in course_ids)
        # An empty allowlist means "no courses", not "every course". IN () is a
        # syntax error in SQLite, so the impossible predicate is spelled out.
        sql.append(f"   AND m.course_id IN ({placeholders})" if course_ids else "   AND 0")
        params.extend(course_ids)
    sql.append(" ORDER BY m.course_id, m.parent_id, m.id")
    return conn.execute("\n".join(sql), params).fetchall()


def get_extraction(conn: sqlite3.Connection, drive_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM extractions WHERE drive_id = ?", (drive_id,)
    ).fetchone()


def upsert_extraction(conn: sqlite3.Connection, drive_id: str, **fields: Any) -> None:
    """Insert or replace what is known about one Drive file.

    Only the fields passed are written; anything omitted keeps its stored value.
    That is what lets the fetch stage record the bytes and the extract stage
    record the text without either clobbering the other's columns.
    """
    unknown = set(fields) - set(_EXTRACTION_FIELDS)
    if unknown:
        raise ValueError(f"not extraction columns: {sorted(unknown)}")

    if "status" not in fields:
        # SQLite checks NOT NULL against the row the INSERT proposes, before the
        # ON CONFLICT clause gets a chance to turn it into an UPDATE. So an
        # update that does not mention status still has to carry the stored one,
        # or it fails on a row that was never going to be inserted.
        existing = get_extraction(conn, drive_id)
        if existing is None:
            raise ValueError(f"first write for {drive_id!r} must set a status")
        fields = {"status": existing["status"], **fields}

    columns = ["drive_id", *fields]
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{name} = excluded.{name}" for name in fields)
    conn.execute(
        f"INSERT INTO extractions ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(drive_id) DO UPDATE SET {assignments}",
        [drive_id, *fields.values()],
    )


def update_material_file(
    conn: sqlite3.Connection,
    drive_id: str,
    *,
    mime_type: str | None = None,
    md5_checksum: str | None = None,
    local_path: str | None = None,
    trashed: bool = False,
    fetch_error: str | None = None,
) -> int:
    """Mirror one file's Drive-side facts onto every reference to it.

    extractions is authoritative; these five columns exist because schema.sql
    reserved them for Phase 2 and they keep a single-table query useful. They
    are written here and nowhere else -- upsert_material deliberately leaves
    them alone so a Classroom re-sync cannot wipe them.

    Nothing latches: a file that comes back out of the trash is written live
    again on the next fetch, clearing both trashed and fetch_error.
    """
    cursor = conn.execute(
        "UPDATE materials SET mime_type = ?, md5_checksum = ?, local_path = ?, "
        "trashed = ?, fetch_error = ? WHERE drive_id = ?",
        (mime_type, md5_checksum, local_path, 1 if trashed else 0, fetch_error, drive_id),
    )
    return cursor.rowcount


def get_ocr_page(conn: sqlite3.Connection, drive_id: str, page_index: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ocr_pages WHERE drive_id = ? AND page_index = ?",
        (drive_id, page_index),
    ).fetchone()


def find_ocr_by_hash(conn: sqlite3.Connection, page_hash: str) -> sqlite3.Row | None:
    """Any successful transcription of this exact image, from any file.

    The same diagram reused across two decks is one page of quota, not two.
    """
    return conn.execute(
        "SELECT * FROM ocr_pages WHERE page_hash = ? AND status = 'ok' LIMIT 1",
        (page_hash,),
    ).fetchone()


def ocr_pages_for(conn: sqlite3.Connection, drive_id: str) -> dict[int, sqlite3.Row]:
    """Every recorded page for one file, keyed by page index."""
    rows = conn.execute(
        "SELECT * FROM ocr_pages WHERE drive_id = ? ORDER BY page_index", (drive_id,)
    ).fetchall()
    return {int(row["page_index"]): row for row in rows}


def upsert_ocr_page(
    conn: sqlite3.Connection,
    *,
    drive_id: str,
    page_index: int,
    page_hash: str,
    status: str,
    text: str | None = None,
    model: str | None = None,
    error: str | None = None,
    attempts: int | None = None,
    now: str | None = None,
) -> None:
    """Record one page's transcription outcome.

    created_at is preserved on update -- when a page was first read is history.
    """
    stamp = now or _utc_now_iso()
    conn.execute(
        "INSERT INTO ocr_pages (drive_id, page_index, page_hash, status, text, "
        "model, chars, attempts, created_at, updated_at, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(drive_id, page_index) DO UPDATE SET "
        "page_hash = excluded.page_hash, status = excluded.status, "
        "text = excluded.text, model = excluded.model, chars = excluded.chars, "
        "attempts = excluded.attempts, updated_at = excluded.updated_at, "
        "error = excluded.error",
        (
            drive_id,
            page_index,
            page_hash,
            status,
            text,
            model,
            len(text) if text is not None else None,
            attempts if attempts is not None else 0,
            stamp,
            stamp,
            error,
        ),
    )


def ocr_progress(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per file: which course it belongs to, and how far its OCR has got.

    `needed` is the denominator the extract stage measured; ok + pending +
    failed is what OCR has actually recorded. The difference is pages nothing
    has looked at yet, which is why a file can be at 0/12 rather than absent.

    Grouped by (file, course) rather than by file alone, so a document attached
    in two courses reports under both -- Phase 3 asks per subject, and a
    subject is not ready because some other subject holds the same file.
    """
    return conn.execute(
        "SELECT m.course_id, "
        "       COALESCE(c.name, m.course_id) AS course_name, "
        "       e.drive_id, m.title, e.mime_type, "
        "       e.scan_pages AS needed, e.pages, "
        "       COALESCE(SUM(o.status = 'ok'), 0)      AS ok, "
        "       COALESCE(SUM(o.status = 'pending'), 0) AS pending, "
        "       COALESCE(SUM(o.status = 'error'), 0)   AS failed "
        "  FROM extractions e "
        "  JOIN (SELECT DISTINCT drive_id, course_id, title FROM materials "
        "         WHERE deleted_at IS NULL AND drive_id IS NOT NULL) m "
        "    ON m.drive_id = e.drive_id "
        "  LEFT JOIN courses c ON c.id = m.course_id "
        "  LEFT JOIN ocr_pages o ON o.drive_id = e.drive_id "
        " WHERE e.scan_pages > 0 "
        " GROUP BY e.drive_id, m.course_id "
        " ORDER BY course_name COLLATE NOCASE, m.title COLLATE NOCASE"
    ).fetchall()


def ocr_candidate_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(drive_id, course_id, posted_at) for every post an attachment hangs off.

    The two facts the OCR queue is ordered by, and nothing else. `posted_at` is
    the parent post's creation_time -- when the material was PUT IN FRONT OF ME,
    which is what makes it likely to be asked about -- and never the extraction
    or fetch time, which say only when this project happened to get to it.

    One row per (file, course), because a file can be attached in two courses
    and one of them may be tracked while the other is not. The caller takes the
    best course and the newest post; deciding here would mean this query knowing
    about config.

    MAX(creation_time) because a file can also be attached to two posts in the
    same course. The newest wins: material re-posted for this week's lecture is
    this week's material, whatever the first posting said.
    """
    return conn.execute(
        f"""
        WITH parents AS ({_PARENTS_SQL})
        SELECT m.drive_id, m.course_id, MAX(p.creation_time) AS posted_at,
               MIN(m.title) AS title,
               COALESCE(MIN(c.name), m.course_id) AS course_name
          FROM materials m
          JOIN parents p
            ON p.entity_type = m.parent_type AND p.id = m.parent_id
          LEFT JOIN courses c ON c.id = m.course_id
         WHERE m.drive_id IS NOT NULL
           AND m.deleted_at IS NULL AND p.deleted_at IS NULL
         GROUP BY m.drive_id, m.course_id
        """
    ).fetchall()


def ocr_error_counts(conn: sqlite3.Connection, limit: int = 8) -> list[tuple[str, int]]:
    """[(reason, pages)] for every page that is not transcribed, commonest first.

    The reasons are already recorded per page; nothing surfaced them, so
    diagnosing "323 pending" meant opening the database by hand. Reading them
    back costs one query and turns a silent count into a stated cause.
    """
    rows = conn.execute(
        "SELECT error, count(*) AS n FROM ocr_pages "
        " WHERE status != 'ok' AND error IS NOT NULL "
        " GROUP BY error ORDER BY n DESC, error LIMIT ?",
        (limit,),
    ).fetchall()
    return [(str(row["error"]), int(row["n"])) for row in rows]


# The three parent tables normalised to one shape. Announcements carry no
# title, only a body, so `body` is where their text comes from and the caller
# derives a label from it -- the same fallback the digest uses.
_PARENTS_SQL = """
    SELECT 'coursework' AS entity_type, id, course_id, title, NULL AS body,
           alternate_link, creation_time, deleted_at
      FROM coursework
    UNION ALL
    SELECT 'coursework_material', id, course_id, title, NULL,
           alternate_link, creation_time, deleted_at
      FROM coursework_materials
    UNION ALL
    SELECT 'announcement', id, course_id, NULL, text,
           alternate_link, creation_time, deleted_at
      FROM announcements
"""


def pack_sources(conn: sqlite3.Connection, course_id: str) -> list[sqlite3.Row]:
    """Every extracted attachment in one course, with the post it came from.

    Ordered by when the post was created, so a pack reads in the order the
    course was taught rather than in database order. Soft-deleted posts and
    attachments are excluded: material the teacher removed should not come back
    in a study document.
    """
    return conn.execute(
        f"""
        WITH parents AS ({_PARENTS_SQL})
        SELECT p.entity_type, p.id AS parent_id, p.title AS parent_title,
               p.body AS parent_body, p.alternate_link, p.creation_time,
               m.id AS material_id, m.drive_id, m.title AS file_title, m.url AS file_url,
               e.text_path, e.method, e.pages, e.scan_pages, e.ocr_pages,
               e.chars, e.extracted_at, e.mime_type
          FROM parents p
          JOIN materials m
            ON m.parent_type = p.entity_type AND m.parent_id = p.id
          JOIN extractions e ON e.drive_id = m.drive_id
         WHERE p.course_id = ? AND p.deleted_at IS NULL AND m.deleted_at IS NULL
           AND e.status = 'ok' AND e.text_path IS NOT NULL
         ORDER BY p.creation_time, p.id, m.id
        """,
        (course_id,),
    ).fetchall()


def study_item_sources(
    conn: sqlite3.Connection, entity_type: str, entity_id: str
) -> list[sqlite3.Row]:
    """The extracted attachments of ONE post, in the order they were attached.

    pack_sources narrowed to a single parent. The quiz needs exactly this and
    nothing wider: a question generated from the course's whole pack could ask
    about a lecture I have not been given yet, which is the opposite of the
    gate's job.

    Same exclusions as pack_sources -- a soft-deleted post or attachment does
    not come back as a question, and only `ok` extractions have text to read.

    `local_path` is the one column pack_sources does not select. gate/sections.py
    needs the original PDF, not only its text: the bookmark table is where a
    slide deck says which pages belong to one topic, and it exists nowhere in
    the extracted .txt.
    """
    return conn.execute(
        f"""
        WITH parents AS ({_PARENTS_SQL})
        SELECT p.entity_type, p.id AS parent_id, p.title AS parent_title,
               p.body AS parent_body, p.alternate_link, p.creation_time,
               m.id AS material_id, m.drive_id, m.title AS file_title, m.url AS file_url,
               e.text_path, e.method, e.pages, e.scan_pages, e.ocr_pages,
               e.chars, e.extracted_at, e.mime_type, e.local_path
          FROM parents p
          JOIN materials m
            ON m.parent_type = p.entity_type AND m.parent_id = p.id
          JOIN extractions e ON e.drive_id = m.drive_id
         WHERE p.entity_type = ? AND p.id = ? AND p.deleted_at IS NULL
           AND m.deleted_at IS NULL
           AND e.status = 'ok' AND e.text_path IS NOT NULL
         ORDER BY m.id
        """,
        (entity_type, entity_id),
    ).fetchall()


def get_pack(conn: sqlite3.Connection, course_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM packs WHERE course_id = ?", (course_id,)).fetchone()


def upsert_pack(
    conn: sqlite3.Connection,
    *,
    course_id: str,
    path: str,
    content_hash: str,
    sources: int,
    now: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO packs (course_id, path, content_hash, sources, built_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(course_id) DO UPDATE SET path = excluded.path, "
        "content_hash = excluded.content_hash, sources = excluded.sources, "
        "built_at = excluded.built_at",
        (course_id, path, content_hash, sources, now or _utc_now_iso()),
    )


def material_summary(
    conn: sqlite3.Connection, rows: Iterable[sqlite3.Row]
) -> dict[tuple[str, str], dict[str, int]]:
    """Per event entity: how much text its attachments actually yielded.

    Lets the digest say "3 files, 34 pages" instead of "3 files", and say when
    some of those pages are still unreadable. Chunked like entity_links for the
    SQLite variable limit.
    """
    keys = {
        (str(row["entity_type"]), str(row["entity_id"]))
        for row in rows
        if row["entity_type"] in ("coursework", "coursework_material", "announcement")
    }
    if not keys:
        return {}

    summary: dict[tuple[str, str], dict[str, int]] = {}
    ids = sorted({key[1] for key in keys})
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT m.parent_type, m.parent_id, count(*) AS files, "
            f"       COALESCE(SUM(e.pages), 0) AS pages, "
            f"       COALESCE(SUM(e.ocr_pages), 0) AS ocr_pages, "
            f"       COALESCE(SUM(e.scan_pages), 0) AS scan_pages "
            f"  FROM materials m JOIN extractions e ON e.drive_id = m.drive_id "
            f" WHERE m.parent_id IN ({placeholders}) AND m.deleted_at IS NULL "
            f"   AND e.status = 'ok' "
            f" GROUP BY m.parent_type, m.parent_id",
            chunk,
        ).fetchall():
            key = (str(row["parent_type"]), str(row["parent_id"]))
            if key in keys:
                summary[key] = {
                    "files": int(row["files"]),
                    "pages": int(row["pages"]),
                    "ocr_pages": int(row["ocr_pages"]),
                    "scan_pages": int(row["scan_pages"]),
                }
    return summary


def unreadable_pages_by_course(conn: sqlite3.Connection) -> dict[str, int]:
    """{course_id: pages that need OCR and have not had it}.

    Phase 3 will generate quiz questions from this text, so a subject whose
    material the agent cannot yet read is a subject the gate would quiz badly.
    """
    rows = conn.execute(
        "SELECT m.course_id, "
        "       SUM(MAX(e.scan_pages - e.ocr_pages, 0)) AS unread "
        "  FROM extractions e "
        "  JOIN (SELECT DISTINCT drive_id, course_id FROM materials "
        "         WHERE deleted_at IS NULL AND drive_id IS NOT NULL) m "
        "    ON m.drive_id = e.drive_id "
        " WHERE e.status = 'ok' AND e.scan_pages > 0 "
        " GROUP BY m.course_id"
    ).fetchall()
    return {str(row["course_id"]): int(row["unread"]) for row in rows if row["unread"]}


def get_ocr_source_md5(conn: sqlite3.Connection, drive_id: str) -> str | None:
    """The file revision the stored page hashes were computed from, if known."""
    row = conn.execute(
        "SELECT source_md5 FROM ocr_sources WHERE drive_id = ?", (drive_id,)
    ).fetchone()
    return str(row["source_md5"]) if row else None


def set_ocr_source_md5(
    conn: sqlite3.Connection, drive_id: str, source_md5: str, *, now: str | None = None
) -> None:
    """Record which revision the current page hashes describe."""
    stamp = now or _utc_now_iso()
    conn.execute(
        "INSERT INTO ocr_sources (drive_id, source_md5, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(drive_id) DO UPDATE SET "
        "source_md5 = excluded.source_md5, updated_at = excluded.updated_at",
        (drive_id, source_md5, stamp),
    )


def count_ocr_pages_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM ocr_pages GROUP BY status ORDER BY status"
    ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def dead_references(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Attachments that exist in Classroom but no longer in Drive.

    Measured at 20 of 118 in the tracked courses -- 16 trashed and 4 deleted
    outright. This is permanent loss, not a transient failure, and the only
    remedy is asking the teacher, so it has to be reportable rather than a
    number that scrolled past during one fetch.
    """
    return conn.execute(
        "SELECT m.title, m.url, m.course_id, c.name AS course_name, "
        "       m.parent_type, m.parent_id, e.status, e.error "
        "  FROM materials m "
        "  JOIN extractions e ON e.drive_id = m.drive_id "
        "  LEFT JOIN courses c ON c.id = m.course_id "
        " WHERE m.deleted_at IS NULL AND e.status IN ('trashed', 'missing') "
        " ORDER BY c.name, e.status, m.title"
    ).fetchall()


def parents_with_extracted_material(
    conn: sqlite3.Connection, course_ids: Sequence[str] | None = None
) -> list[sqlite3.Row]:
    """Posts that have at least one attachment whose text we actually hold.

    The unit is the post, not the attachment: a lecture with slides and a
    handout is one thing to revise, and the gate says "1 unreviewed lecture"
    rather than naming files. A post whose only attachment is trashed yields
    nothing, which is why this joins through extractions rather than materials.
    """
    sql = [
        "SELECT m.parent_type AS entity_type, m.parent_id AS entity_id,",
        "       m.course_id, count(*) AS files",
        "  FROM materials m",
        "  JOIN extractions e ON e.drive_id = m.drive_id",
        " WHERE m.deleted_at IS NULL AND e.status = 'ok'",
    ]
    params: list[Any] = []
    if course_ids is not None:
        placeholders = ", ".join("?" for _ in course_ids)
        sql.append(f"   AND m.course_id IN ({placeholders})" if course_ids else "   AND 0")
        params.extend(course_ids)
    sql.append(" GROUP BY m.parent_type, m.parent_id, m.course_id")
    sql.append(" ORDER BY m.course_id, m.parent_type, m.parent_id")
    return conn.execute("\n".join(sql), params).fetchall()


def ensure_study_item(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    course_id: str,
    state: str = "pending",
    skip_reason: str | None = None,
    skip_source: str | None = None,
    now: str | None = None,
) -> bool:
    """Create one study item if it does not exist. True when a row was written.

    DO NOTHING rather than DO UPDATE, and that is the whole point: this runs
    after every extract, and an item already carried to 'verified' must not be
    knocked back to 'pending' by a re-run. Progress through the gate is the one
    thing in this database that cannot be rebuilt from the API.

    _upsert cannot be borrowed here -- it conflicts on `id`, and this table's id
    is a synthetic integer with the real identity in (entity_type, entity_id).
    """
    cursor = conn.execute(
        "INSERT INTO study_items (entity_type, entity_id, course_id, state, "
        "skip_reason, skip_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entity_type, entity_id) DO NOTHING",
        (
            entity_type,
            entity_id,
            course_id,
            state,
            skip_reason,
            skip_source,
            now or _utc_now_iso(),
        ),
    )
    return cursor.rowcount > 0


def get_study_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM study_items WHERE id = ?", (item_id,)).fetchone()


def reopen_study_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Put one skipped item back in the queue. True when a row changed.

    The only way out of 'skipped', and deliberately manual: a gate that
    silently re-offers what I already declined is a gate with no escape, and I
    would mute it within a week. Everything else about the item is left alone
    -- delivered_at in particular, because the material really was delivered
    and forgetting that would misreport the history rather than reset it.

    Narrow on purpose. This is not a general state setter; the WHERE clause is
    what stops it from being used to walk an item forward into 'verified'
    without a passed quiz.
    """
    cursor = conn.execute(
        "UPDATE study_items SET state = 'pending', skip_reason = NULL, "
        "skip_source = NULL WHERE id = ? AND state = 'skipped'",
        (item_id,),
    )
    return cursor.rowcount > 0


# States the gate still has something to do about. `verified` and `skipped` are
# both finished -- one honestly, one honestly -- and neither comes back except
# through reopen_study_item.
UNREVIEWED_STATES = ("pending", "delivered", "reviewed")


# One study item and how much of it is actually readable. Shared by
# gate_backlog and backlog_item so the two cannot drift: the difference between
# them is a WHERE clause, and a second copy of this arithmetic is how an item
# ends up quizzable in one code path and blocked in another.
_BACKLOG_SQL = """
        WITH parents AS ({parents}),
             attached AS (
                 SELECT DISTINCT parent_type, parent_id, drive_id
                   FROM materials
                  WHERE deleted_at IS NULL AND drive_id IS NOT NULL
             )
        SELECT si.id AS item_id, si.entity_type, si.entity_id, si.course_id,
               si.state, si.delivered_at, si.reviewed_at,
               c.name AS course_name,
               p.title AS parent_title, p.body AS parent_body,
               p.alternate_link, p.creation_time,
               COUNT(e.drive_id) AS files,
               COALESCE(SUM(e.pages), 0)  AS pages,
               COALESCE(SUM(e.chars), 0)  AS chars,
               COALESCE(SUM(MAX(e.scan_pages - e.ocr_pages, 0)), 0) AS unread
          FROM study_items si
          JOIN parents p
            ON p.entity_type = si.entity_type AND p.id = si.entity_id
          LEFT JOIN courses c ON c.id = si.course_id
          LEFT JOIN attached a
            ON a.parent_type = si.entity_type AND a.parent_id = si.entity_id
          LEFT JOIN extractions e
            ON e.drive_id = a.drive_id AND e.status = 'ok'
         WHERE {where}
           AND p.deleted_at IS NULL
         GROUP BY si.id
         ORDER BY p.creation_time, si.id
"""


def gate_backlog(
    conn: sqlite3.Connection, course_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Unreviewed study items in these courses, oldest post first, with how
    much of each is actually readable.

    Oldest first is deliberate and is not reordered to favour items whose text
    is complete. The oldest unreviewed post is the thing I am furthest behind
    on; serving a newer one because its diagrams happen to have been
    transcribed would mean revising out of order to make a quiz possible, which
    is the tail wagging the dog.

    `unread` is the honest measure of what the agent cannot read: pages that
    carry an image and almost no text, minus the ones a model has transcribed.
    Anything above zero means a quiz on this item would be a quiz on holes.

    The DISTINCT subquery matters. One post can reference the same Drive file
    twice, and summing straight through the join would count its pages twice --
    inflating `unread` and blocking an item that is actually complete.
    """
    if not course_ids:
        return []
    placeholders = ", ".join("?" for _ in course_ids)
    states = ", ".join("?" for _ in UNREVIEWED_STATES)
    return conn.execute(
        _BACKLOG_SQL.format(
            parents=_PARENTS_SQL,
            where=f"si.course_id IN ({placeholders}) AND si.state IN ({states})",
        ),
        [*course_ids, *UNREVIEWED_STATES],
    ).fetchall()


def backlog_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    """One study item in the same shape gate_backlog returns, whatever its state.

    Same columns, same readiness arithmetic, no state filter. The gate reads
    items through gate_backlog; `agent quiz --item N` and the buttons that name
    one item by id read it through here, and both have to see the same
    `unread` figure or the refusal rule would apply in one place and not the
    other.
    """
    return conn.execute(
        _BACKLOG_SQL.format(parents=_PARENTS_SQL, where="si.id = ?"),
        [item_id],
    ).fetchone()


def study_item_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """{course_id: study items of any state}.

    A course with none is not up to date -- it is a course whose material never
    became readable. Probability & Statistics is exactly that: 20 of 20
    attachments return 404, so it has no items at all, and reporting it as
    finished would be the most flattering possible lie.
    """
    rows = conn.execute(
        "SELECT course_id, count(*) AS n FROM study_items GROUP BY course_id"
    ).fetchall()
    return {str(row["course_id"]): int(row["n"]) for row in rows}


def dead_reference_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """{course_id: attachments that exist in Classroom but not in Drive}."""
    rows = conn.execute(
        "SELECT m.course_id, count(DISTINCT m.drive_id) AS n "
        "  FROM materials m JOIN extractions e ON e.drive_id = m.drive_id "
        " WHERE m.deleted_at IS NULL AND e.status IN ('trashed', 'missing') "
        " GROUP BY m.course_id"
    ).fetchall()
    return {str(row["course_id"]): int(row["n"]) for row in rows}


def study_item_files(
    conn: sqlite3.Connection, entity_type: str, entity_id: str
) -> list[sqlite3.Row]:
    """Every attachment on one post, readable or not, with a Telegram file id.

    Includes rows whose extraction failed, because delivery is not the same
    question as quizzing: a file the extractor could not read is still a file I
    may want on my phone, and a trashed one still deserves to be named rather
    than silently absent.
    """
    return conn.execute(
        "SELECT DISTINCT m.drive_id, m.title, m.url, "
        "       e.status, e.local_path, e.mime_type, e.size_bytes, e.pages, "
        "       t.file_id "
        "  FROM materials m "
        "  LEFT JOIN extractions e ON e.drive_id = m.drive_id "
        "  LEFT JOIN telegram_files t ON t.drive_id = m.drive_id "
        " WHERE m.parent_type = ? AND m.parent_id = ? AND m.deleted_at IS NULL "
        "   AND m.drive_id IS NOT NULL "
        " ORDER BY m.title COLLATE NOCASE",
        (entity_type, entity_id),
    ).fetchall()


# --------------------------------------------------------------------------
# the gate's own rows
# --------------------------------------------------------------------------

def get_gate_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM gate_runs WHERE id = ?", (run_id,)).fetchone()


def get_gate_run_for(conn: sqlite3.Connection, for_date: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM gate_runs WHERE for_date = ?", (for_date,)
    ).fetchone()


def create_gate_run(
    conn: sqlite3.Connection,
    *,
    for_date: str,
    plan: str,
    version_label: str | None,
    now: str | None = None,
) -> int:
    """Record that a day is being prepared for. UNIQUE(for_date) is the guard.

    Written before the message is sent, and `sent_at` stays NULL until it
    lands, so a crash in between leaves a row the next run re-sends rather than
    a day that silently believes it was covered.
    """
    cursor = conn.execute(
        "INSERT INTO gate_runs (for_date, created_at, version_label, plan) "
        "VALUES (?, ?, ?, ?)",
        (for_date, now or _utc_now_iso(), version_label, plan),
    )
    return int(cursor.lastrowid)


def replace_gate_plan(
    conn: sqlite3.Connection, run_id: int, *, plan: str, version_label: str | None
) -> None:
    """Rewrite an unsent run's plan. Only ever called before sent_at is set."""
    conn.execute(
        "UPDATE gate_runs SET plan = ?, version_label = ? WHERE id = ?",
        (plan, version_label, run_id),
    )


def mark_gate_sent(
    conn: sqlite3.Connection, run_id: int, message_id: int | None, *, now: str | None = None
) -> None:
    """Stamp a run as delivered, clearing any snooze it was waiting out."""
    conn.execute(
        "UPDATE gate_runs SET sent_at = ?, message_id = ?, snoozed_until = NULL "
        "WHERE id = ?",
        (now or _utc_now_iso(), message_id, run_id),
    )


def snooze_gate_run(conn: sqlite3.Connection, run_id: int, until: str) -> None:
    conn.execute("UPDATE gate_runs SET snoozed_until = ? WHERE id = ?", (until, run_id))


def close_gate_run(conn: sqlite3.Connection, run_id: int, *, now: str | None = None) -> None:
    """Nothing further to do for this day. Stops snooze re-sends."""
    conn.execute(
        "UPDATE gate_runs SET closed_at = ?, snoozed_until = NULL WHERE id = ?",
        (now or _utc_now_iso(), run_id),
    )


def due_gate_runs(conn: sqlite3.Connection, now_utc: str) -> list[sqlite3.Row]:
    """Runs whose snooze has expired and which nothing has closed."""
    return conn.execute(
        "SELECT * FROM gate_runs "
        " WHERE closed_at IS NULL AND snoozed_until IS NOT NULL AND snoozed_until <= ? "
        " ORDER BY for_date",
        (now_utc,),
    ).fetchall()


# --------------------------------------------------------------------------
# study item transitions
# --------------------------------------------------------------------------

# Every legal move, and nothing else. Written out rather than derived so that
# reading this file tells you what the gate can do to an item -- and so that
# `verified` having exactly one way in is visible rather than inferred.
#
# `verified` is absent from every value here: only Phase 3c's passed quiz
# writes it, through its own function.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "delivered": frozenset({"pending", "delivered"}),
    "reviewed": frozenset({"delivered", "reviewed"}),
    "skipped": frozenset({"pending", "delivered", "reviewed"}),
}


def advance_study_item(
    conn: sqlite3.Connection,
    item_id: int,
    to_state: str,
    *,
    skip_reason: str | None = None,
    now: str | None = None,
) -> bool:
    """Move one item forward. True when it actually moved.

    False is a normal answer, not a failure: Telegram redelivers updates and an
    old message keeps working buttons forever, so the same tap arriving twice
    must change the row once. The caller answers "already done" rather than
    applying it again.

    The current state is read before the write rather than inferred from
    rowcount, because SQLite counts a row as changed even when the UPDATE
    stores identical values -- so rowcount cannot tell a real move from a
    repeated tap.

    Timestamps are written once, via COALESCE. A second delivery of the same
    lecture is a convenience, not a new delivery, and moving delivered_at
    forward would quietly rewrite when I first saw it.
    """
    if to_state not in _TRANSITIONS:
        raise ValueError(
            f"{to_state!r} is not a state the gate may move an item to; "
            f"expected one of {', '.join(sorted(_TRANSITIONS))}"
        )
    if to_state == "skipped" and not skip_reason:
        # A skip with no reason is the dishonesty the gate cannot afford: a
        # year later it is indistinguishable from the seeded backlog.
        raise ValueError("a skip must record why")

    row = conn.execute("SELECT state FROM study_items WHERE id = ?", (item_id,)).fetchone()
    if row is None or row["state"] not in _TRANSITIONS[to_state]:
        return False
    already_there = row["state"] == to_state

    stamp = now or _utc_now_iso()
    sets = ["state = ?"]
    params: list[Any] = [to_state]
    column = {"delivered": "delivered_at", "reviewed": "reviewed_at"}.get(to_state)
    if column is not None:
        sets.append(f"{column} = COALESCE({column}, ?)")
        params.append(stamp)
    if to_state == "skipped":
        sets.extend(["skip_reason = ?", "skip_source = 'user'"])
        params.append(skip_reason)
    params.append(item_id)

    conn.execute(f"UPDATE study_items SET {', '.join(sets)} WHERE id = ?", params)
    return not already_there


# --------------------------------------------------------------------------
# quizzes: the cached questions, the attempt, and the one way to `verified`
# --------------------------------------------------------------------------

def cached_questions(
    conn: sqlite3.Connection, item_id: int, source_hash: str
) -> sqlite3.Row | None:
    """A question set for this exact text, if one was generated and not flagged.

    The flagged filter is the whole reason the flag button is worth having: a
    set I marked as wrong must not be handed back to me on the next attempt,
    however cheap serving it again would be.
    """
    return conn.execute(
        "SELECT * FROM quiz_questions "
        " WHERE study_item_id = ? AND source_hash = ? AND flagged = 0",
        (item_id, source_hash),
    ).fetchone()


def save_questions(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    source_hash: str,
    model: str,
    questions: str,
    now: str | None = None,
) -> int:
    """Store a generated set, replacing a flagged one for the same text.

    The UPDATE on conflict clears `flagged`, which is correct and is why
    quiz_flags exists separately: the regenerated set has not been complained
    about, and the complaint about the old one is already recorded there.
    """
    conn.execute(
        "INSERT INTO quiz_questions "
        "  (study_item_id, source_hash, model, questions, created_at, flagged) "
        "VALUES (?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(study_item_id, source_hash) DO UPDATE SET "
        "  model = excluded.model, questions = excluded.questions, "
        "  created_at = excluded.created_at, flagged = 0",
        (item_id, source_hash, model, questions, now or _utc_now_iso()),
    )
    row = conn.execute(
        "SELECT id FROM quiz_questions WHERE study_item_id = ? AND source_hash = ?",
        (item_id, source_hash),
    ).fetchone()
    return int(row["id"])


def flag_question_set(conn: sqlite3.Connection, item_id: int, source_hash: str) -> None:
    """Retire a cached set, so the next attempt generates a fresh one."""
    conn.execute(
        "UPDATE quiz_questions SET flagged = 1 "
        " WHERE study_item_id = ? AND source_hash = ?",
        (item_id, source_hash),
    )


def record_flag(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    attempt_id: int | None,
    source_hash: str | None,
    model: str | None,
    question_index: int,
    question: str,
    source_file: str | None = None,
    source_page: int | None = None,
    now: str | None = None,
) -> int:
    """Keep a flagged question verbatim, so `agent flagged` has something to show."""
    cursor = conn.execute(
        "INSERT INTO quiz_flags (study_item_id, attempt_id, source_hash, model, "
        "  question_index, question, source_file, source_page, flagged_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_id, attempt_id, source_hash, model, question_index, question,
            source_file, source_page, now or _utc_now_iso(),
        ),
    )
    return int(cursor.lastrowid or 0)


def list_flags(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Every flagged question, newest first, with enough context to go and look."""
    return conn.execute(
        "SELECT f.*, si.course_id, si.entity_type, si.entity_id, c.name AS course_name "
        "  FROM quiz_flags f "
        "  JOIN study_items si ON si.id = f.study_item_id "
        "  LEFT JOIN courses c ON c.id = si.course_id "
        " ORDER BY f.flagged_at DESC, f.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def start_quiz_attempt(
    conn: sqlite3.Connection, *, item_id: int, state: str, now: str | None = None
) -> int:
    cursor = conn.execute(
        "INSERT INTO quiz_attempts (study_item_id, started_at, questions) "
        "VALUES (?, ?, ?)",
        (item_id, now or _utc_now_iso(), state),
    )
    return int(cursor.lastrowid or 0)


def get_quiz_attempt(conn: sqlite3.Connection, attempt_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()


def open_quiz_attempt(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    """The unfinished attempt on this item, if there is one.

    This is what makes a restart free. The bot holds nothing, so resuming is
    reading the row that was already there.
    """
    return conn.execute(
        "SELECT * FROM quiz_attempts WHERE study_item_id = ? AND finished_at IS NULL "
        " ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def update_quiz_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    *,
    state: str,
    flagged: bool | None = None,
) -> None:
    if flagged is None:
        conn.execute(
            "UPDATE quiz_attempts SET questions = ? WHERE id = ?", (state, attempt_id)
        )
        return
    conn.execute(
        "UPDATE quiz_attempts SET questions = ?, flagged = ? WHERE id = ?",
        (state, 1 if flagged else 0, attempt_id),
    )


def finish_quiz_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    *,
    state: str,
    score: float,
    passed: bool,
    now: str | None = None,
) -> None:
    conn.execute(
        "UPDATE quiz_attempts SET questions = ?, score = ?, passed = ?, "
        "  finished_at = ? WHERE id = ?",
        (state, score, 1 if passed else 0, now or _utc_now_iso(), attempt_id),
    )


def count_failed_attempts(conn: sqlite3.Connection, item_id: int) -> int:
    """How many quizzes on this item have finished without passing.

    Read after a failure to decide whether to keep offering Retry as the
    obvious next step. Three failures on one lecture is more likely to mean the
    questions are wrong than that I am, and a loop I cannot leave is a gate I
    will mute.
    """
    row = conn.execute(
        "SELECT count(*) AS n FROM quiz_attempts "
        " WHERE study_item_id = ? AND finished_at IS NOT NULL AND passed = 0",
        (item_id,),
    ).fetchone()
    return int(row["n"])


def verify_study_item(
    conn: sqlite3.Connection, item_id: int, attempt_id: int, *, now: str | None = None
) -> bool:
    """Mark an item verified. The only function in this project that can.

    `verified` is deliberately absent from every value in _TRANSITIONS above,
    so advance_study_item cannot reach it however it is called. An item gets
    here or it does not get there at all -- and it only gets here behind a
    stored attempt that actually passed. That check is a query against the row
    rather than a promise from the caller, because "nothing reaches verified
    without a passed quiz" is the guarantee the coverage figure rests on, and a
    guarantee enforced by convention is not one.
    """
    attempt = conn.execute(
        "SELECT study_item_id, passed FROM quiz_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    if attempt is None or int(attempt["study_item_id"]) != item_id:
        return False
    if not attempt["passed"]:
        raise ValueError(
            f"quiz attempt {attempt_id} did not pass, so it cannot verify "
            f"study item {item_id}. A failed attempt leaves the item reviewed."
        )

    row = conn.execute(
        "SELECT state FROM study_items WHERE id = ?", (item_id,)
    ).fetchone()
    # Only from `reviewed`, which starting a quiz always sets. Never from
    # `pending` (the material was never sent) and never from `skipped` --
    # coming back from a skip is a decision I make with --reopen.
    if row is None or row["state"] not in ("reviewed", "verified"):
        return False
    already = row["state"] == "verified"

    conn.execute(
        "UPDATE study_items SET state = 'verified', "
        "  verified_at = COALESCE(verified_at, ?) WHERE id = ?",
        (now or _utc_now_iso(), item_id),
    )
    return not already


def get_bot_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_bot_state(
    conn: sqlite3.Connection, key: str, value: str, *, now: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value, now or _utc_now_iso()),
    )


def remember_telegram_file(
    conn: sqlite3.Connection,
    *,
    drive_id: str,
    file_id: str,
    file_size: int | None = None,
    now: str | None = None,
) -> None:
    """Keep what Telegram called an upload, so the next send costs no bytes."""
    conn.execute(
        "INSERT INTO telegram_files (drive_id, file_id, file_size, uploaded_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(drive_id) DO UPDATE SET "
        "file_id = excluded.file_id, file_size = excluded.file_size, "
        "uploaded_at = excluded.uploaded_at",
        (drive_id, file_id, file_size, now or _utc_now_iso()),
    )


def forget_telegram_file(conn: sqlite3.Connection, drive_id: str) -> None:
    """Drop a file id Telegram no longer accepts, so the bytes go again."""
    conn.execute("DELETE FROM telegram_files WHERE drive_id = ?", (drive_id,))


def count_study_items_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, count(*) AS n FROM study_items GROUP BY state ORDER BY state"
    ).fetchall()
    return {row["state"]: int(row["n"]) for row in rows}


def count_extractions_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """{'ok': 98, 'trashed': 16, ...}. Absent statuses are absent, not zero."""
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM extractions GROUP BY status ORDER BY status"
    ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


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


# Which table holds the alternate_link for each entity_type an event can name.
_LINK_TABLES = {
    "coursework": "coursework",
    "coursework_material": "coursework_materials",
    "announcement": "announcements",
    "submission": "submissions",
}


def entity_links(
    conn: sqlite3.Connection, rows: Iterable[sqlite3.Row]
) -> dict[tuple[str, str], str]:
    """alternate_link per (entity_type, entity_id) for the given events.

    Looked up at compose time rather than copied into the payload, so that
    events already sitting in the database from earlier phases still get links,
    and so a link the API later corrects is not frozen in old rows.
    """
    wanted: dict[str, set[str]] = {}
    for row in rows:
        entity_type = row["entity_type"]
        if entity_type in _LINK_TABLES:
            wanted.setdefault(entity_type, set()).add(row["entity_id"])

    links: dict[tuple[str, str], str] = {}
    for entity_type, ids in wanted.items():
        table = _LINK_TABLES[entity_type]
        id_list = list(ids)
        # Chunked to stay clear of SQLite's variable limit on a big first run.
        for start in range(0, len(id_list), 500):
            chunk = id_list[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            found = conn.execute(
                f"SELECT id, alternate_link FROM {table} WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for record in found:
                if record["alternate_link"]:
                    links[(entity_type, record["id"])] = record["alternate_link"]
    return links


def mark_notified(
    conn: sqlite3.Connection, event_ids: Sequence[int], *, now: str | None = None
) -> int:
    """Stamp notified_at on the given events and commit. Returns rows changed.

    Called only after a send has actually succeeded, and the commit is what
    makes "notify exactly once" true: an event with a non-null notified_at is
    never selected again. The `IS NULL` guard means re-stamping an already-sent
    event is a no-op rather than a rewritten timestamp.
    """
    if not event_ids:
        return 0

    stamp = now or _utc_now_iso()
    placeholders = ", ".join("?" for _ in event_ids)
    cursor = conn.execute(
        f"UPDATE events SET notified_at = ? WHERE id IN ({placeholders}) "
        f"AND notified_at IS NULL",
        [stamp, *event_ids],
    )
    conn.commit()
    return cursor.rowcount
