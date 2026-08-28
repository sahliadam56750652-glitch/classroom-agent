"""Assemble one course's extracted text into a single study document.

These exist to be read by something else. NotebookLM has no API, so a pack
reaches it the way any file does: it is written to a folder, and if that folder
is Drive-synced it appears in Drive without this project ever uploading
anything. Invariant 6 is untouched -- nothing here talks to Google, and
`drive.readonly` could not upload even if it did.

Two properties make the output trustworthy rather than merely present.

Provenance is explicit. Every document names the post it came from, when it was
posted, and a link back to it, because a study document that cannot be traced to
a lecture is worse than no document -- NotebookLM cites its sources, and those
citations have to lead somewhere real.

Absence is explicit. A page nothing has read yet appears as a placeholder
saying so. A silent gap would read as a page the lecturer left blank, and
Phase 3 will generate quiz questions from this text: a quiz confidently built
on a page the system never actually read is the exact failure the gate exists
to prevent.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..classroom.models import ISO_FORMAT, content_hash
from ..config import Config
from ..db import store
from . import extract

# Marks text a model produced rather than the PDF. Phase 3 reads packs as
# source material, so where a sentence came from is not a detail.
OCR_MARKER = "> _[transcribed from an image by {model}]_"

# What a page nothing has read yet leaves behind. Never omitted silently.
UNREAD_MARKER = "> _[page {page}: an image this agent has not transcribed yet]_"

# NotebookLM caps a source at roughly half a million words. Nothing in this
# library is close, but a pack that silently exceeded it would be truncated at
# the far end where nobody would notice.
WORD_WARNING_THRESHOLD = 450_000

SEPARATOR = "\n\n---\n\n"


class PackError(Exception):
    """A pack cannot be written -- not one document failing, which is recorded."""


@dataclass
class PackResult:
    course_id: str
    course_name: str
    path: Path
    sources: int = 0
    words: int = 0
    ocr_pages: int = 0
    unread_pages: int = 0
    written: bool = False
    reason: str = ""


@dataclass
class PacksResult:
    packs: list[PackResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def written(self) -> int:
        return sum(1 for pack in self.packs if pack.written)

    @property
    def unchanged(self) -> int:
        return sum(1 for pack in self.packs if not pack.written and not pack.reason)

    def items_seen(self) -> dict[str, int]:
        return {
            "courses": len(self.packs),
            "written": self.written,
            "unchanged": self.unchanged,
            "sources": sum(pack.sources for pack in self.packs),
        }


def slugify(name: str) -> str:
    """A filename from a course name, safe on Windows and Linux alike.

    Course names in this account contain '&', '-' and accented characters, and
    will eventually contain a '/'. Nothing but ASCII letters, digits and
    hyphens survives.
    """
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "course"


def _date(value: str | None) -> str:
    """A Classroom timestamp as a plain date, or a note that there wasn't one."""
    if not value:
        return "date unknown"
    try:
        return datetime.strptime(value, ISO_FORMAT).strftime("%Y-%m-%d")
    except ValueError:
        return str(value)[:10]


def label(row: dict[str, Any]) -> str:
    """The best human name for the post an attachment hangs off.

    Public because the gate needs exactly this fallback too, and a second copy
    of it would drift from this one the first time either is touched.

    Announcements have no title, only a body, so the first line stands in --
    the same fallback the digest uses, for the same reason.
    """
    title = row["parent_title"]
    if title:
        return str(title)
    body = row["parent_body"]
    if body:
        first = str(body).strip().splitlines()[0].strip()
        if len(first) > 90:
            first = first[:87].rstrip() + "..."
        if first:
            return first
    return f"(untitled {str(row['entity_type']).replace('_', ' ')})"


def _fingerprint(course_name: str, rows: list[sqlite3.Row]) -> str:
    """A hash of exactly what would go into the pack.

    Covers the text identity of every source -- not the text itself, which
    would mean reading every file to decide whether to read every file. chars
    and extracted_at change whenever the text does, and ocr_pages changes when
    a transcription lands, which is the other thing that alters a pack.
    """
    return content_hash(
        {
            "course": course_name,
            "sources": [
                {
                    "drive_id": row["drive_id"],
                    "parent": f"{row['entity_type']}:{row['parent_id']}",
                    "chars": row["chars"],
                    "pages": row["pages"],
                    "ocr_pages": row["ocr_pages"],
                    "scan_pages": row["scan_pages"],
                    "extracted_at": row["extracted_at"],
                }
                for row in rows
            ],
        }
    )


def render_pages(
    text: str, ocr_rows: dict[int, sqlite3.Row], scan_pages: int
) -> tuple[str, int, int]:
    """(body, ocr pages included, pages still unread).

    The .txt is form-feed separated by page, and ocr_pages knows which indices a
    model read, so the two line up by index and the origin of every page is
    recoverable without storing it twice.

    Public because the quiz generator assembles its prompt from exactly this,
    markers and all. A second splicer would drift from this one the first time
    either is touched, and a quiz built on a silently different reading of a
    lecture than the pack shows is the kind of divergence nobody would notice.
    """
    pages = text.split(extract.PAGE_BREAK)
    rendered: list[str] = []
    transcribed = 0
    unread = 0

    for index, page in enumerate(pages):
        row = ocr_rows.get(index)
        body = page.strip()

        if row is not None and row["status"] == "ok":
            transcribed += 1
            marker = OCR_MARKER.format(model=row["model"] or "a vision model")
            rendered.append(f"{marker}\n\n{body}" if body else marker)
            continue

        if row is not None:
            # Recorded but not transcribed: deferred, refused or timed out. The
            # page's own text is whatever the PDF had, which for a scan is
            # nothing, so say so rather than leaving an empty stretch.
            unread += 1
            rendered.append(UNREAD_MARKER.format(page=index + 1))
            continue

        rendered.append(body)

    if not ocr_rows and scan_pages:
        # OCR has never run over this file, so which pages are images is known
        # only in aggregate. Still said out loud.
        unread += scan_pages

    return "\n\n".join(part for part in rendered if part), transcribed, unread


def render(
    course_name: str,
    rows: list[dict[str, Any]],
    library_dir: Path,
    *,
    today: str,
) -> tuple[str, int, int]:
    """The whole pack as markdown. (text, ocr pages, unread pages).

    Rows are dicts rather than sqlite3.Row because each carries an `_ocr_rows`
    entry the query cannot produce.
    """
    header = [
        f"# {course_name}",
        "",
        f"Study pack assembled by classroom-agent on {today} "
        f"from {len(rows)} source document(s).",
        "",
        "Each section below is the extracted text of one file posted to this "
        "course, with a link back to the post it came from. Passages marked as "
        "transcribed were read from an image by a vision model, not taken from "
        "the document's own text.",
    ]

    body: list[str] = []
    total_ocr = 0
    total_unread = 0
    missing: list[str] = []

    for row in rows:
        path = library_dir / row["text_path"]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # The row says there is text and the disk disagrees. Recorded in the
            # pack rather than dropped, so the gap is visible where it matters.
            missing.append(str(row["file_title"] or row["drive_id"]))
            continue

        ocr_rows = row["_ocr_rows"]
        rendered, transcribed, unread = render_pages(text, ocr_rows, int(row["scan_pages"] or 0))
        total_ocr += transcribed
        total_unread += unread

        facts = []
        if row["pages"]:
            facts.append(f"{row['pages']} page(s)")
        if transcribed:
            facts.append(f"{transcribed} transcribed from images")
        if unread:
            facts.append(f"{unread} not yet transcribed")

        section = [
            f"## {label(row)}",
            "",
            f"- Posted: {_date(row['creation_time'])}",
            f"- File: {row['file_title'] or row['drive_id']}"
            + (f" ({', '.join(facts)})" if facts else ""),
        ]
        source_link = row["file_url"] or row["alternate_link"]
        if source_link:
            section.append(f"- Source: {source_link}")
        section.extend(["", rendered or "_(no text could be extracted from this file)_"])
        body.append("\n".join(section))

    if missing:
        body.append(
            "## Files that could not be read back\n\n"
            + "\n".join(f"- {name}" for name in missing)
            + "\n\nTheir extracted text is recorded in the database but was not "
            "on disk when this pack was built."
        )

    return SEPARATOR.join(["\n".join(header), *body]) + "\n", total_ocr, total_unread


def build(
    config: Config,
    db: sqlite3.Connection,
    *,
    course_ids: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    now: str | None = None,
) -> PacksResult:
    """Write one pack per course whose extracted text has changed.

    dry_run reports what would be written without touching the filesystem.
    force rewrites even when the fingerprint says nothing changed.
    """
    courses = config.tracked_courses if course_ids is None else course_ids
    result = PacksResult(dry_run=dry_run)
    stamp = now or store._utc_now_iso()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for course_id in courses:
        course = store.get_course(db, course_id)
        name = str(course["name"]) if course else course_id
        destination = config.packs_dir / f"{slugify(name)}.md"

        rows = store.pack_sources(db, course_id)
        pack = PackResult(
            course_id=course_id, course_name=name, path=destination, sources=len(rows)
        )

        if not rows:
            pack.reason = "no extracted text yet"
            result.packs.append(pack)
            continue

        fingerprint = _fingerprint(name, rows)
        previous = store.get_pack(db, course_id)
        unchanged = (
            previous is not None
            and previous["content_hash"] == fingerprint
            and destination.exists()
        )
        if unchanged and not force:
            result.packs.append(pack)
            continue

        # Only now is it worth reading every text file off disk.
        enriched = [dict(row) for row in rows]
        for row in enriched:
            row["_ocr_rows"] = store.ocr_pages_for(db, str(row["drive_id"]))

        text, ocr_pages, unread = render(name, enriched, config.library_dir, today=today)
        pack.words = len(text.split())
        pack.ocr_pages = ocr_pages
        pack.unread_pages = unread

        if pack.words > WORD_WARNING_THRESHOLD:
            pack.reason = f"{pack.words} words -- over NotebookLM's per-source limit"

        if dry_run:
            pack.reason = pack.reason or "would be written"
            result.packs.append(pack)
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        store.upsert_pack(
            db,
            course_id=course_id,
            path=str(destination),
            content_hash=fingerprint,
            sources=len(rows),
            now=stamp,
        )
        pack.written = True
        result.packs.append(pack)

    return result
