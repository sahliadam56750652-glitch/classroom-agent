"""A file I hand the project myself: a photographed board, a classmate's notes.

The whole design of this module is a claim about code that is already written:
**download is the only stage an upload skips.** `agent extract` selects
`FROM extractions WHERE status IN ('fetched', 'ok')`, and `fetched` means
exactly "the bytes are on disk and nothing has read them". So an upload that
writes into `library/files/` and inserts a `fetched` row lands precisely where
`agent fetch` would have left it, and extract, OCR, packs, quiz and the gate
read the database and the library and cannot tell where the bytes came from.

That is why there is no `--extract` flag here and no pipeline to re-run: the
19:30 `agent run` picks an upload up unattended, exactly as it picks up a
lecture the professor posted at lunchtime. The stages stay separately runnable,
which is the rule the sync-and-diff skill states and this project keeps.

Three things this module is careful about:

**Identity is content.** The file id is a hash of the bytes, so uploading the
same photograph twice is one row and costs nothing -- invariant 2's rule ("diff
on content, not on when") applied to a photograph. It also means a re-upload
after a failed extract is free and safe.

**The mime type is checked, not believed.** A `.png` that is really a JPEG
would reach `extract_file`, match no reader, and be recorded as `unsupported`
three stages later -- a failure whose message describes the wrong thing. The
magic bytes are compared against the suffix here, where the answer is known.

**The subject is resolved exactly.** Never a near-match, for the reason PLAN.md
settled for the gate: a wrong match files material under the wrong subject and
looks exactly like the feature working.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import manual
from ..classroom.models import Material, content_hash, material_id
from ..config import FILES_SUBDIR, Config
from ..db import store

# What `agent upload` accepts, and why it is exactly this.
#
# PDF is the handout and the emailed chapter. JPEG and PNG are the photographed
# board, and they need no special case anywhere downstream because
# `extract.extract_file` already returns one page that is one scan page for
# them -- which is what a PDF page hiding a diagram looks like, so files/ocr.py
# picks it up through the same route.
#
# Anything else is refused BY NAME rather than stored and discovered to be
# unreadable later. A .docx or .pptx would in fact extract, but Drive is where
# those come from; accepting them here would mean two paths to one kind of
# file and no way to tell which one a row came from.
#
# The magic bytes are the authority. A phone that writes .HEIC, or a rename
# from .heic to .jpg, must fail here with a message naming the real format.
ACCEPTED: dict[str, tuple[str, tuple[bytes, ...]]] = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
}

# Magic bytes we can name in an error even though we do not accept them, so
# "that is a HEIC" beats "that is not a PNG".
_KNOWN_ELSEWHERE: tuple[tuple[bytes, int, str], ...] = (
    (b"ftypheic", 4, "HEIC (an iPhone photo)"),
    (b"ftypheix", 4, "HEIC (an iPhone photo)"),
    (b"ftypmif1", 4, "HEIF"),
    (b"RIFF", 0, "WebP or another RIFF container"),
    (b"GIF8", 0, "GIF"),
    (b"PK\x03\x04", 0, "a zip package (.docx, .pptx, .odt)"),
    (b"\xd0\xcf\x11\xe0", 0, "a legacy Office file (.doc, .ppt)"),
)

MAX_BYTES = 64 * 1024 * 1024


class UploadError(Exception):
    """The upload cannot proceed, for a reason the message states exactly."""


@dataclass
class Uploaded:
    """What one upload did, or would have done under --dry-run."""

    subject: str
    course_id: str
    course_name: str
    file_id: str
    post_id: str
    title: str
    posted_at: str
    mime_type: str
    size_bytes: int
    local_path: str
    already_held: bool = False
    dry_run: bool = False


def _sniff(payload: bytes) -> str | None:
    """What the bytes actually are, among the formats worth naming."""
    for suffix, (mime, signatures) in ACCEPTED.items():
        if any(payload.startswith(signature) for signature in signatures):
            return mime
    for signature, offset, name in _KNOWN_ELSEWHERE:
        if payload[offset : offset + len(signature)] == signature:
            return name
    return None


def resolve_subject(table, name: str) -> tuple[str, str]:
    """(subject, course id) for a name, or a refusal that says what to do.

    Exact, case-insensitively, through the timetable's `subjects:` map and
    nowhere else. A subject still mapped to null has no identity to file
    anything under, and the fix is one command, so the message says it.
    """
    matches = [key for key in table.subjects if key.casefold() == str(name).casefold()]
    if not matches:
        known = ", ".join(sorted(table.subjects)) or "(none)"
        raise UploadError(
            f"{name!r} is not a subject in {table.path.name}, and names are "
            f"never matched approximately -- a wrong match files material under "
            f"the wrong subject and looks exactly like this working.\n"
            f"  Known subjects: {known}"
        )
    subject = matches[0]
    course_id = table.subjects[subject]
    if course_id is None:
        raise UploadError(
            f"{subject!r} has no course id yet, so there is nothing to file this "
            f"under.\n"
            f"  If it has a Classroom: join it, run `agent courses`, and paste "
            f"the id into {table.path.name}.\n"
            f'  If it never will: agent subjects --add "{subject}"'
        )
    return subject, course_id


def read_payload(path: Path) -> tuple[bytes, str]:
    """The bytes and their real mime type, or a refusal naming the format."""
    if not path.is_file():
        raise UploadError(f"no such file: {path}")

    size = path.stat().st_size
    if size == 0:
        raise UploadError(f"{path.name} is empty (0 bytes).")
    if size > MAX_BYTES:
        raise UploadError(
            f"{path.name} is {size / 1_048_576:.0f} MB, over the {MAX_BYTES // 1_048_576} "
            f"MB ceiling. Split it, or put it in Drive and let the sync fetch it."
        )

    payload = path.read_bytes()
    suffix = path.suffix.lower()
    accepted = ", ".join(sorted(ACCEPTED))

    if suffix not in ACCEPTED:
        actual = _sniff(payload)
        detail = f" -- the bytes look like {actual}" if actual else ""
        raise UploadError(
            f"{path.name} has suffix {suffix or '(none)'}{detail}. "
            f"`agent upload` accepts: {accepted}.\n"
            f"  Convert a photo to JPEG or a document to PDF first; anything "
            f"already in Drive arrives through `agent sync` instead."
        )

    declared = ACCEPTED[suffix][0]
    actual = _sniff(payload)
    if actual != declared:
        # Said here, where the answer is known. Stored and left to `extract`,
        # this surfaces as "unsupported" three stages later, about a mime type
        # nobody wrote down -- the recurring lesson's exact shape.
        found = actual or "not a format this reads"
        raise UploadError(
            f"{path.name} is named {suffix} but its bytes are {found}. "
            f"Renaming a file does not convert it -- convert it and try again."
        )
    return payload, declared


def prepare(
    config: Config,
    db: sqlite3.Connection,
    table,
    *,
    subject: str,
    path: Path,
    title: str | None = None,
    posted: str | None = None,
    now: str | None = None,
) -> Uploaded:
    """Everything the upload decides, with nothing written yet.

    Separated from `commit` so `--dry-run` runs the same resolution, the same
    sniffing and the same refusals as a real upload -- a dry run that takes a
    different path is a dry run that proves nothing.
    """
    subject_name, course_id = resolve_subject(table, subject)
    payload, mime_type = read_payload(path)

    stamp = now or store._utc_now_iso()
    posted_at = _posted_at(posted, stamp)
    file_id = manual.file_id(payload)
    display_title = (title or path.name).strip() or path.name
    post_id = manual.post_id(course_id, display_title, posted_at)

    course = store.get_course(db, course_id)
    if course is None:
        raise UploadError(
            f"{subject_name!r} maps to course {course_id}, which is not in the "
            f"database.\n"
            f"  For a Classroom course: run `agent courses`.\n"
            f'  For a manual one: agent subjects --add "{subject_name}"'
        )

    suffix = ".jpg" if mime_type == "image/jpeg" else path.suffix.lower()
    return Uploaded(
        subject=subject_name,
        course_id=course_id,
        course_name=str(course["name"]),
        file_id=file_id,
        post_id=post_id,
        title=display_title,
        posted_at=posted_at,
        mime_type=mime_type,
        size_bytes=len(payload),
        local_path=f"{FILES_SUBDIR}/{file_id}{suffix}",
        already_held=store.get_extraction(db, file_id) is not None,
    )


def _posted_at(posted: str | None, stamp: str) -> str:
    """When the material is dated, which is what the OCR queue sorts on.

    A date with no time means midday rather than midnight: the queue sorts
    descending, and a board photographed today must not sit behind a lecture
    posted this morning just because "today" parsed as 00:00.
    """
    if posted is None:
        return stamp
    try:
        day = datetime.strptime(posted, "%Y-%m-%d").replace(
            hour=12, tzinfo=timezone.utc
        )
    except ValueError as err:
        raise UploadError(
            f"--posted must be a date like 2026-09-21, got {posted!r}."
        ) from err
    return day.strftime("%Y-%m-%dT%H:%M:%SZ")


def commit(
    config: Config,
    db: sqlite3.Connection,
    plan: Uploaded,
    payload: bytes,
    *,
    now: str | None = None,
) -> Uploaded:
    """Write the bytes and the three rows `agent fetch` would have written."""
    stamp = now or store._utc_now_iso()

    destination = config.library_dir / plan.local_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)

    # The post. A manual post lives in `coursework_materials` because that is
    # what it IS -- material posted with no submission attached -- and because
    # a fourth parent type would mean widening two CHECK constraints, rebuilding
    # `study_items` (which holds data no re-sync can restore), and forking every
    # query that unions the parent tables. The reserved id prefix is what keeps
    # the row distinguishable; see agent/manual.py.
    db.execute(
        "INSERT INTO coursework_materials (id, course_id, title, description, "
        "state, topic_id, alternate_link, creation_time, update_time, "
        "content_hash, first_seen_at) "
        "VALUES (?, ?, ?, NULL, 'PUBLISHED', NULL, NULL, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET title = excluded.title",
        (
            plan.post_id,
            plan.course_id,
            plan.title,
            plan.posted_at,
            plan.posted_at,
            content_hash({"manual_post": plan.post_id, "title": plan.title}),
            stamp,
        ),
    )

    store.upsert_material(
        db,
        Material(
            id=material_id("coursework_material", plan.post_id, "driveFile", plan.file_id),
            parent_type="coursework_material",
            parent_id=plan.post_id,
            course_id=plan.course_id,
            kind="driveFile",
            ref=plan.file_id,
            drive_id=plan.file_id,
            title=plan.title,
            url=None,
            content_hash=content_hash({"manual_file": plan.file_id}),
        ),
        now=stamp,
    )

    # `fetched`, not `ok`: the bytes are on disk and nothing has read them.
    # This is the exact resting state `agent fetch` leaves behind, which is the
    # whole of why the rest of the pipeline needs no change.
    store.upsert_extraction(
        db,
        plan.file_id,
        status="fetched",
        mime_type=plan.mime_type,
        size_bytes=plan.size_bytes,
        md5_checksum=hashlib.md5(payload).hexdigest(),
        modified_time=plan.posted_at,
        local_path=plan.local_path,
        fetched_at=stamp,
        error=None,
    )
    return plan
