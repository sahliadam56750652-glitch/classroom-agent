"""A restorable snapshot of everything no amount of re-syncing can recover.

`deploy/README.md` already has the mechanism -- a weekly `rsync` pull of the
whole `DATA_DIR`. Phase 6 is what makes losing it expensive, and this is the
part of it that can be checked rather than believed.

**What is irreplaceable, and what merely looks it.** Almost everything in this
database is a mirror: delete `coursework`, run `agent sync`, and it comes back.
The exceptions were `events.notified_at` and `study_items`, and Phase 6 adds
every manually entered row -- which, for a subject with no Classroom, is the
BULK of what that subject knows about itself. There is no API to re-ask.

**The bytes count too.** A backup of the rows alone restores a library of
dangling `local_path`s: `extractions` says the file is at
`files/manual-file-abc.jpg` and nothing is there, because nothing can fetch it.
A photographed board exists in exactly one place. So the snapshot carries the
`manual-file-*` bytes alongside the rows, and the manifest carries a sha256 of
each so a truncated copy is a refusal rather than a surprise.

**Restore never clobbers.** Rows are inserted, existing ones are left alone,
and the report says how many of each. A restore that silently overwrote newer
local state would be a worse failure than the one it was recovering from --
and the counts are printed per table so a partial restore is visible rather
than assumed. Running it twice is a no-op.

The selection is by the reserved `manual-` prefix for tables shared with
Classroom, and wholesale for the tables Phase 6 added. No Classroom CONTENT is
ever in the file -- a backup that quietly carried a mirror would restore a
stale one over a fresh one.

The one exception is an ANCHOR, and it was found by restoring rather than by
thinking: an exercise sheet handed out on paper for UNIX is a manual row whose
`course_id` is a Classroom course, and `courses` has a foreign key. Into an
empty database that row has nothing to attach to, so the restore failed
outright. The snapshot therefore carries the `courses` rows its own rows point
at -- id and name, nothing else that matters -- and the next `agent courses`
refreshes them. Without that, "restore into a fresh database" would have been
true only for subjects that have no Classroom at all, which is exactly the half
of the problem this phase did not need to solve.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import manual
from .config import Config
from .db import store

MANIFEST = "MANIFEST"
ROWS_FILE = "manual.json"
FILES_DIR = "files"
FORMAT_VERSION = 1


class BackupError(Exception):
    """The snapshot cannot be written or cannot be trusted. Never silent."""


@dataclass(frozen=True)
class Spec:
    """One table's share of the snapshot, and how to find it again.

    `where` is the prefix test that separates my rows from Google's. `key` is
    what makes a row the same row on the way back in -- always the table's real
    primary key, so a restore can be an insert that yields rather than a blind
    one that duplicates.
    """

    table: str
    where: str
    key: tuple[str, ...]


# Ordered so a parent is always restored before anything that references it.
# Foreign keys are ON (store.connect sets the pragma), so this is load-bearing
# rather than tidy: study_items references courses, quiz_flags references
# quiz_attempts, and an out-of-order insert is a refusal.
_PREFIX = f"{manual.PREFIX}%"

# Every table whose rows name a course. Their course_ids are what `courses`
# has to carry as anchors, over and above the manual courses themselves.
_COURSE_REFERENCES: tuple[str, ...] = (
    "coursework_materials",
    "materials",
    "study_items",
    "manual_sessions",
    "manual_tasks",
    "projects",
)

SPECS: tuple[Spec, ...] = (
    # Filled by `collect`, which needs the other tables first to know which
    # Classroom courses are anchors. The empty predicate here is never used.
    Spec("courses", "id LIKE ?", ("id",)),
    Spec("coursework_materials", "id LIKE ?", ("id",)),
    Spec("materials", "parent_id LIKE ?", ("id",)),
    Spec("extractions", "drive_id LIKE ?", ("drive_id",)),
    Spec("ocr_pages", "drive_id LIKE ?", ("drive_id", "page_index")),
    # A study item for manual material is reachable two ways: the whole course
    # is manual, or one uploaded post sits inside a Classroom course. Its STATE
    # is the irreplaceable part -- `agent studyitems` could recreate the row,
    # but never the fact that I verified it.
    Spec("study_items", "(course_id LIKE ? OR entity_id LIKE ?)", ("entity_type", "entity_id")),
    Spec(
        "quiz_questions",
        "study_item_id IN (SELECT id FROM study_items "
        "WHERE course_id LIKE ? OR entity_id LIKE ?)",
        ("id",),
    ),
    Spec(
        "quiz_attempts",
        "study_item_id IN (SELECT id FROM study_items "
        "WHERE course_id LIKE ? OR entity_id LIKE ?)",
        ("id",),
    ),
    Spec(
        "quiz_flags",
        "study_item_id IN (SELECT id FROM study_items "
        "WHERE course_id LIKE ? OR entity_id LIKE ?)",
        ("id",),
    ),
    Spec("telegram_files", "drive_id LIKE ?", ("drive_id",)),
    # Phase 6's own tables: every row is mine, so there is nothing to filter.
    Spec("manual_sessions", "1", ("id",)),
    Spec("manual_tasks", "1", ("id",)),
    Spec("projects", "1", ("id",)),
    Spec("project_milestones", "1", ("id",)),
    # No course anchor is needed: `course_id` here is a note with no foreign
    # key, so a row restores whether or not the course it mentions exists.
    Spec("timetable_adjustments", "1", ("id",)),
)


@dataclass
class Snapshot:
    """What a backup holds, or what a restore put back."""

    path: Path
    rows: dict[str, int] = field(default_factory=dict)
    files: int = 0
    bytes_copied: int = 0
    dry_run: bool = False

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


@dataclass
class Restored:
    added: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    files_added: int = 0
    files_present: int = 0
    dry_run: bool = False

    @property
    def total_added(self) -> int:
        return sum(self.added.values())

    @property
    def total_skipped(self) -> int:
        return sum(self.skipped.values())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _params(spec: Spec) -> tuple[str, ...]:
    return (_PREFIX,) * spec.where.count("?")


def collect(db: sqlite3.Connection) -> dict[str, list[dict]]:
    """Every row worth saving, per table, in a stable order.

    Stable so that two backups of an unchanged database are byte-identical --
    which is what makes `diff` a usable check on whether anything moved.
    """
    # `courses` is first in the dict because the restore inserts in this order
    # and everything else references it -- but it is filled last, because which
    # courses are needed is a fact about the other tables.
    found: dict[str, list[dict]] = {"courses": []}
    for spec in SPECS:
        if spec.table == "courses":
            continue
        order = ", ".join(spec.key)
        rows = db.execute(
            f"SELECT * FROM {spec.table} WHERE {spec.where} ORDER BY {order}",
            _params(spec),
        ).fetchall()
        found[spec.table] = [dict(row) for row in rows]

    wanted = {
        str(row["course_id"])
        for table in _COURSE_REFERENCES
        for row in found.get(table, [])
        if row.get("course_id")
    }
    if wanted:
        placeholders = ", ".join("?" for _ in wanted)
        clause = f"id LIKE ? OR id IN ({placeholders})"
        params = [_PREFIX, *sorted(wanted)]
    else:
        clause, params = "id LIKE ?", [_PREFIX]
    found["courses"] = [
        dict(row)
        for row in db.execute(
            f"SELECT * FROM courses WHERE {clause} ORDER BY id", params
        )
    ]
    return found


def _library_files(config: Config, rows: dict[str, list[dict]]) -> list[Path]:
    """The uploaded bytes, named by the extraction rows that point at them."""
    wanted = []
    for row in rows.get("extractions", []):
        local = row.get("local_path")
        if not local:
            continue
        path = config.library_dir / local
        if path.is_file():
            wanted.append(path)
    return wanted


def write(
    config: Config,
    db: sqlite3.Connection,
    out_dir: Path | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> Snapshot:
    """Write a snapshot, or report what one would contain."""
    moment = now or datetime.now(timezone.utc)
    if out_dir is None:
        out_dir = (
            config.data_dir / "backups" / f"manual-{moment.strftime('%Y%m%dT%H%M%SZ')}"
        )

    rows = collect(db)
    files = _library_files(config, rows)
    snapshot = Snapshot(
        path=out_dir,
        rows={table: len(values) for table, values in rows.items()},
        files=len(files),
        bytes_copied=sum(path.stat().st_size for path in files),
        dry_run=dry_run,
    )
    if dry_run:
        return snapshot

    if out_dir.exists() and any(out_dir.iterdir()):
        raise BackupError(
            f"{out_dir} already exists and is not empty. Refusing to write into "
            f"it -- a half-overwritten snapshot is worse than no snapshot."
        )
    try:
        (out_dir / FILES_DIR).mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"format": FORMAT_VERSION, "taken_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "tables": rows},
            indent=1, sort_keys=True, ensure_ascii=False,
        )
        (out_dir / ROWS_FILE).write_text(payload, encoding="utf-8")

        entries = [f"{hashlib.sha256(payload.encode('utf-8')).hexdigest()}  {ROWS_FILE}"]
        for path in files:
            shutil.copy2(path, out_dir / FILES_DIR / path.name)
            entries.append(f"{sha256_file(path)}  {FILES_DIR}/{path.name}")

        entries.append("")
        entries.append("-- row counts")
        for table, count in snapshot.rows.items():
            entries.append(f"{table:<24} {count}")
        (out_dir / MANIFEST).write_text("\n".join(entries) + "\n", encoding="utf-8")
    except OSError as err:
        raise BackupError(f"could not write the snapshot to {out_dir}: {err}") from err
    return snapshot


def _verify(snapshot_dir: Path) -> dict[str, list[dict]]:
    """Read the rows, having checked the manifest agrees with what is there.

    A corrupted or truncated snapshot must refuse by name. The failure mode
    this exists to prevent is a restore that half-works and is believed.
    """
    rows_path = snapshot_dir / ROWS_FILE
    manifest_path = snapshot_dir / MANIFEST
    for path in (rows_path, manifest_path):
        if not path.is_file():
            raise BackupError(f"{snapshot_dir} is not a snapshot: no {path.name}.")

    expected: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("--") or "  " not in line:
            continue
        digest, _, name = line.partition("  ")
        if len(digest) == 64:
            expected[name.strip()] = digest

    payload = rows_path.read_text(encoding="utf-8")
    actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if expected.get(ROWS_FILE) != actual:
        raise BackupError(
            f"{rows_path} does not match its checksum in {MANIFEST}. The "
            f"snapshot is corrupt or was edited; refusing to restore from it."
        )

    for name, digest in expected.items():
        if name == ROWS_FILE:
            continue
        path = snapshot_dir / name
        if not path.is_file():
            raise BackupError(f"{MANIFEST} lists {name}, which is not in {snapshot_dir}.")
        if sha256_file(path) != digest:
            raise BackupError(
                f"{path} does not match its checksum in {MANIFEST}. Restoring it "
                f"would put corrupted bytes back into the library."
            )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as err:
        raise BackupError(f"{rows_path} is not readable JSON: {err}") from err
    if data.get("format") != FORMAT_VERSION:
        raise BackupError(
            f"{rows_path} is format {data.get('format')!r}; this build writes "
            f"and reads format {FORMAT_VERSION}."
        )
    return data.get("tables", {})


def restore(
    config: Config,
    db: sqlite3.Connection,
    snapshot_dir: Path,
    *,
    dry_run: bool = False,
) -> Restored:
    """Put a snapshot back. Adds what is missing and never overwrites.

    A restore that silently overwrote newer local state would be a worse
    failure than the one it was recovering from, so an existing row is left
    alone and counted as skipped. That also makes running this twice a no-op.
    """
    tables = _verify(snapshot_dir)
    result = Restored(dry_run=dry_run)

    for spec in SPECS:
        rows = tables.get(spec.table, [])
        added = skipped = 0
        for row in rows:
            if _exists(db, spec, row):
                skipped += 1
                continue
            added += 1
            if not dry_run:
                _insert(db, spec.table, row)
        result.added[spec.table] = added
        result.skipped[spec.table] = skipped

    files_dir = snapshot_dir / FILES_DIR
    if files_dir.is_dir():
        for path in sorted(files_dir.iterdir()):
            if not path.is_file():
                continue
            destination = config.library_dir / "files" / path.name
            if destination.is_file():
                result.files_present += 1
                continue
            result.files_added += 1
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    return result


def _exists(db: sqlite3.Connection, spec: Spec, row: dict) -> bool:
    where = " AND ".join(f"{column} IS ?" for column in spec.key)
    found = db.execute(
        f"SELECT 1 FROM {spec.table} WHERE {where} LIMIT 1",
        [row.get(column) for column in spec.key],
    ).fetchone()
    return found is not None


def _insert(db: sqlite3.Connection, table: str, row: dict) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    try:
        db.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            [row[column] for column in columns],
        )
    except sqlite3.Error as err:
        # Named rather than swallowed: a restore that skipped a row it could
        # not place would be the recurring lesson exactly -- a failure reported
        # as a success with a smaller number next to it.
        raise BackupError(
            f"could not restore a row into {table}: {err}\n  row: {row}"
        ) from err


def describe(config: Config, db: sqlite3.Connection) -> list[str]:
    """What a snapshot would hold, for a listing that needs no file written."""
    rows = collect(db)
    lines = [f"{table:<24} {len(values)}" for table, values in rows.items()]
    files = _library_files(config, rows)
    lines.append(f"{'uploaded files':<24} {len(files)}")
    return lines
