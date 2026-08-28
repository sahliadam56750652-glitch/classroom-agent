"""Fetch attachment bytes from Drive into the local library.

Classroom hands out Drive file IDs and never content, so this is the only place
the actual material arrives. It is read-only: `files.get`, `files.get_media` and
`files.export_media`, and nothing else (invariant 6).

The governing fact is that roughly a sixth of the references are dead. Measured
on the five tracked courses: of 118 driveFile attachments, 16 point at files in
the owner's trash and 4 return HTTP 404. Teachers delete and move files after
posting and Classroom serves the stale reference indefinitely, so this is the
steady state rather than a transient to retry through. Every one of those
outcomes is recorded as a row and the run carries on; a fetcher that raises on
the first dead file never reaches the end of the library.
"""

from __future__ import annotations

import io
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from ..classroom.client import execute, is_retryable
from ..config import Config
from ..db import store

# What files.get is asked for. `trashed` is the only thing that distinguishes a
# binned file from a live one -- its metadata is otherwise perfectly valid, so
# without this the download succeeds and the library fills with dead material.
# `md5Checksum` drives the skip-if-unchanged check; the Phase 0 probe never
# requested it, so how often Drive actually supplies it is unmeasured and
# modified_time is the documented fallback.
METADATA_FIELDS = (
    "id,name,mimeType,size,trashed,md5Checksum,modifiedTime,shortcutDetails"
)

# A shortcut is a pointer, not a file. Resolving it costs one extra get.
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Google-native documents have no bytes to download -- files.get_media returns
# 403 on them, which reads exactly like a permissions problem and is not one.
# They must be exported to a format that does. None of these exist in the
# tracked courses today (5 Sheets and 1 Doc live in untracked ones), so this is
# three lines against a measured zero, kept only because the failure it prevents
# is so misleading.
EXPORT_FORMATS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
}

# Suffix by mime type. The teacher's title is never used to build a filename:
# real titles in this account include "Chapitre1_25_26.pdf" and will eventually
# include ':', '?' and '/', none of which are legal on Windows. Drive IDs are
# [A-Za-z0-9_-] and safe everywhere.
EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/x-sql": ".sql",
    "text/x-python": ".py",
    "application/json": ".ipynb",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/zip": ".zip",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "video/mp4": ".mp4",
}

DEFAULT_EXTENSION = ".bin"
CHUNK_SIZE = 4 * 1024 * 1024
MAX_DOWNLOAD_ATTEMPTS = 5

FILES_SUBDIR = "files"


class DriveError(Exception):
    """Drive is unusable -- not a dead reference, which is recorded instead."""


@dataclass
class FetchResult:
    """Counts for one fetch run, keyed the way sync_runs.items_seen wants them."""

    references: int = 0
    files: int = 0
    fetched: int = 0
    skipped: int = 0
    trashed: int = 0
    missing: int = 0
    unsupported: int = 0
    errors: int = 0
    bytes_downloaded: int = 0
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    def items_seen(self) -> dict[str, int]:
        return {
            "references": self.references,
            "files": self.files,
            "fetched": self.fetched,
            "skipped": self.skipped,
            "trashed": self.trashed,
            "missing": self.missing,
            "unsupported": self.unsupported,
            "errors": self.errors,
        }


def _status_of(err: HttpError) -> int | None:
    return getattr(err.resp, "status", None)


def extension_for(mime_type: str | None, *, name: str | None = None) -> str:
    """Suffix for a mime type, falling back to the Drive name's own suffix.

    The name is consulted only for the extension and never for the stem, which
    is where the unsafe characters live.
    """
    if mime_type in EXTENSIONS:
        return EXTENSIONS[mime_type]
    if name and "." in name:
        suffix = Path(name).suffix.lower()
        # Guard against a "title" that is really a sentence with a full stop.
        if 1 < len(suffix) <= 6 and suffix[1:].isalnum():
            return suffix
    return DEFAULT_EXTENSION


class DriveClient:
    """One handle on the Drive API. Construct it with verified credentials."""

    def __init__(self, credentials, *, sleep=time.sleep):
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        # Injected rather than patched, so a test can assert on the backoff
        # schedule without waiting for it.
        self._sleep = sleep

    def _run(self, request) -> Any:
        return execute(request, sleep=self._sleep)

    def metadata(self, file_id: str) -> dict[str, Any]:
        """files.get for one file, following a shortcut to its target.

        supportsAllDrives costs nothing and removes a whole failure class the
        day a teacher posts from a shared drive.
        """
        raw = self._run(
            self._service.files().get(
                fileId=file_id, fields=METADATA_FIELDS, supportsAllDrives=True
            )
        )
        if raw.get("mimeType") == SHORTCUT_MIME:
            target = (raw.get("shortcutDetails") or {}).get("targetId")
            if not target:
                raise DriveError(f"shortcut {file_id} names no target")
            return self.metadata(target)
        return raw

    def download(self, file_id: str, dest: Path) -> int:
        """Stream one binary file to disk. Returns bytes written."""
        return self._stream(
            self._service.files().get_media(fileId=file_id, supportsAllDrives=True), dest
        )

    def export(self, file_id: str, mime_type: str, dest: Path) -> int:
        """Convert one Google-native document and stream it to disk."""
        return self._stream(
            self._service.files().export_media(fileId=file_id, mimeType=mime_type), dest
        )

    def _stream(self, request, dest: Path) -> int:
        """Chunked download with the same retry policy as everything else.

        MediaIoBaseDownload.next_chunk() does not go through request.execute(),
        so execute()'s loop cannot cover it. The predicate is shared even though
        the loop is not -- what counts as retryable must not have two answers.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=CHUNK_SIZE)
        done = False
        attempts = 0
        while not done:
            try:
                _progress, done = downloader.next_chunk()
            except HttpError as err:
                attempts += 1
                if attempts >= MAX_DOWNLOAD_ATTEMPTS or not is_retryable(err):
                    raise
                self._sleep(min(32.0, 2.0**attempts))
        payload = buffer.getvalue()
        # Written whole rather than streamed straight to the file: a download
        # that dies halfway must not leave a truncated PDF that extracts to
        # plausible-looking half-text.
        dest.write_bytes(payload)
        return len(payload)


def _unchanged(row: sqlite3.Row | None, meta: dict[str, Any], local: Path) -> bool:
    """Is the stored copy already this exact revision?

    md5 when Drive supplies one, modifiedTime when it does not. Either way the
    local file has to still be on disk -- a database row is not evidence that
    the bytes survived.
    """
    if row is None or not local.exists():
        return False
    if row["status"] not in ("fetched", "ok"):
        return False

    remote_md5 = meta.get("md5Checksum")
    if remote_md5:
        return bool(row["md5_checksum"]) and row["md5_checksum"] == remote_md5
    remote_modified = meta.get("modifiedTime")
    return bool(remote_modified) and row["modified_time"] == remote_modified


def _record(
    conn: sqlite3.Connection,
    drive_id: str,
    *,
    status: str,
    now: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    md5_checksum: str | None = None,
    modified_time: str | None = None,
    local_path: str | None = None,
    error: str | None = None,
    fresh_bytes: bool = False,
) -> None:
    """Write one file's outcome to both extractions and every reference to it."""
    extraction_fields: dict[str, Any] = {
        "status": status,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "md5_checksum": md5_checksum,
        "modified_time": modified_time,
        "local_path": local_path,
        "fetched_at": now,
        "error": error,
    }
    if fresh_bytes:
        # A teacher replacing a file leaves the old .txt on disk and the old
        # page counts in the row. Clearing them is what stops the next pack
        # being built from text that no longer matches the bytes it cites.
        extraction_fields.update(
            text_path=None, method=None, pages=None, chars=None, ocr_pages=0, extracted_at=None
        )
    store.upsert_extraction(conn, drive_id, **extraction_fields)
    store.update_material_file(
        conn,
        drive_id,
        mime_type=mime_type,
        md5_checksum=md5_checksum,
        local_path=local_path,
        trashed=status == "trashed",
        fetch_error=error,
    )


def fetch(
    config: Config,
    db: sqlite3.Connection,
    *,
    client: DriveClient | None = None,
    course_ids: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    now: str | None = None,
) -> FetchResult:
    """Download every tracked driveFile attachment that is not already current.

    dry_run resolves nothing and writes nothing -- it reports what the run would
    consider, which is the cheap way to check the reference set before spending
    a hundred Drive calls on it.

    force re-downloads even when the checksum says the local copy is current.
    """
    courses = config.tracked_courses if course_ids is None else course_ids
    rows = store.drive_references(db, courses)

    result = FetchResult(dry_run=dry_run, references=len(rows))

    # One download per file, not per reference. The two are the same today --
    # 118 distinct drive_ids across 118 references -- but the material key is
    # composite, so the same file on two posts is two rows and one download.
    seen: dict[str, sqlite3.Row] = {}
    for row in rows:
        seen.setdefault(row["drive_id"], row)
    result.files = len(seen)

    if dry_run or not seen:
        return result

    targets = list(seen.items())
    if limit is not None:
        targets = targets[:limit]

    if client is None:  # pragma: no cover - needs real credentials
        from .. import auth

        client = DriveClient(auth.get_credentials(config))

    stamp = now or store._utc_now_iso()
    files_dir = config.library_dir / FILES_SUBDIR

    for drive_id, row in targets:
        try:
            meta = client.metadata(drive_id)
        except HttpError as err:
            if _status_of(err) == 404:
                # Measured at 4 of 118. The teacher deleted the file outright;
                # Classroom will keep serving the reference forever.
                _record(db, drive_id, status="missing", now=stamp, error="file not found (404)")
                result.missing += 1
                continue
            _record(db, drive_id, status="error", now=stamp, error=repr(err))
            result.errors += 1
            continue
        except DriveError as err:
            _record(db, drive_id, status="error", now=stamp, error=str(err))
            result.errors += 1
            continue

        mime_type = meta.get("mimeType")
        size = meta.get("size")
        # Drive returns size as a string ("909108"). Left as-is it lands in an
        # INTEGER column as text and every comparison against it quietly fails.
        size_bytes = int(size) if size is not None else None

        if meta.get("trashed"):
            _record(
                db,
                drive_id,
                status="trashed",
                now=stamp,
                mime_type=mime_type,
                size_bytes=size_bytes,
                error="file is in the owner's Drive trash",
            )
            result.trashed += 1
            continue

        if mime_type == FOLDER_MIME:
            # Zero folders exist in this account. Recursion here would be
            # untestable code for nothing; the recorded row is what makes the
            # first one visible on the day it appears.
            _record(
                db,
                drive_id,
                status="unsupported",
                now=stamp,
                mime_type=mime_type,
                error="folder attachment -- expansion not implemented",
            )
            result.unsupported += 1
            result.notes.append(f"folder attachment: {row['title'] or drive_id}")
            continue

        export = EXPORT_FORMATS.get(mime_type or "")
        suffix = export[1] if export else extension_for(mime_type, name=meta.get("name"))
        destination = files_dir / f"{drive_id}{suffix}"
        relative = str(destination.relative_to(config.library_dir).as_posix())

        known = store.get_extraction(db, drive_id)
        if not force and _unchanged(known, meta, destination):
            result.skipped += 1
            continue

        try:
            if export:
                written = client.export(drive_id, export[0], destination)
            else:
                written = client.download(drive_id, destination)
        except HttpError as err:
            status = _status_of(err)
            if status == 404:
                _record(db, drive_id, status="missing", now=stamp, error="file not found (404)")
                result.missing += 1
            else:
                _record(
                    db,
                    drive_id,
                    status="error",
                    now=stamp,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                    error=repr(err),
                )
                result.errors += 1
            continue
        except OSError as err:
            _record(db, drive_id, status="error", now=stamp, mime_type=mime_type, error=repr(err))
            result.errors += 1
            continue

        _record(
            db,
            drive_id,
            status="fetched",
            now=stamp,
            mime_type=mime_type,
            size_bytes=size_bytes if size_bytes is not None else written,
            md5_checksum=meta.get("md5Checksum"),
            modified_time=meta.get("modifiedTime"),
            local_path=relative,
            fresh_bytes=True,
        )
        result.fetched += 1
        result.bytes_downloaded += written

    return result
