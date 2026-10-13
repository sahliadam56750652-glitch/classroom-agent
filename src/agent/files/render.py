"""PDFs of what the reader cannot open: PowerPoint and Word, converted on the PC.

Phase 5e. The reader is PDF.js, and on 2026-10-13 82% of this semester's bytes
were PowerPoint decks it cannot open at all -- online or off. So the scheduled
pipeline converts each in-scope PPTX and DOCX to a PDF with LibreOffice,
headless, and the phone syncs and reads the PDF.

**LibreOffice, not PowerPoint.** This runs inside `agent run`, unattended, from
Task Scheduler. Driving PowerPoint over COM from a scheduled task is unsupported
and hangs on the first dialog it raises, with nobody there to click it. If
`soffice` cannot be found, this fails LOUDLY and names it -- a stage that
silently converts nothing would leave the reader quietly unable to open most of
the term.

**One source of page text, and it is not this.** A rendering is for reading
only. It is never extracted, never OCR'd, and never feeds a quiz or a reading
anchor; the original file's extraction stays the only source of page text. The
reason is that OCR caches are keyed by page, quiz sets by a fingerprint of the
extraction, and saved positions by a content hash of the extraction's pages --
re-deriving any of them from a PDF would silently retire all three the day a
deck became one. LibreOffice exports one PDF page per slide (hidden slides
included, on purpose), so page N of the rendering is slide N of the extraction;
where the counts differ anyway, the rendering says so and positions are kept
by page number only.

Everything LibreOffice writes -- its profile, its output -- stays under DATA_DIR
(invariant 5), and a private profile means a LibreOffice I happen to have open
does not lock the converter out.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..db import store
from .extract import DOCX_MIME, PPTX_MIME

# What gets a rendering, and which LibreOffice export filter does it. Hidden
# slides are exported so the PDF stays page-for-slide with the extraction.
FILTERS = {
    PPTX_MIME: 'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}',
    DOCX_MIME: "pdf:writer_pdf_Export",
}

# One file at a time, and no file may hold the pipeline: a deck that makes
# LibreOffice spin is recorded as failed and the run moves on.
TIMEOUT_SECONDS = 300

RENDERER = "libreoffice"

# Where LibreOffice is usually installed, after PATH and config.yaml.
KNOWN_PLACES = (
    Path("C:/Program Files/LibreOffice/program/soffice.exe"),
    Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    Path("/usr/bin/soffice"),
    Path("/usr/lib/libreoffice/program/soffice"),
)


class RenderError(Exception):
    """Something that stops every conversion, said so it can be fixed."""


def find_soffice(config: Config) -> Path:
    """The LibreOffice binary, or a RenderError that says how to get one."""
    if config.render_soffice:
        found = Path(config.render_soffice)
        if found.is_file():
            return found
        raise RenderError(
            f"render.soffice in config.yaml points at {found}, which does not exist. "
            f"Fix the path, or remove the setting to search the usual places."
        )
    on_path = shutil.which("soffice") or shutil.which("soffice.exe")
    if on_path:
        return Path(on_path)
    for place in KNOWN_PLACES:
        if place.is_file():
            return place
    raise RenderError(
        "LibreOffice is not installed: `soffice` was not found on PATH or in the usual "
        "places. PowerPoint and Word files cannot be converted to PDF, so the reader "
        "cannot open them. Install LibreOffice from https://www.libreoffice.org/ , or "
        "set render.soffice in config.yaml to the full path of soffice."
    )


def command(soffice: Path, source: Path, out_dir: Path, profile: Path, mime_type: str) -> list[str]:
    """The exact command line, built in one place so a test can read it."""
    return [
        str(soffice),
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to",
        FILTERS[mime_type],
        "--outdir",
        str(out_dir),
        str(source),
    ]


def convert(soffice: Path, source: Path, out_dir: Path, profile: Path, mime_type: str) -> Path:
    """Run LibreOffice on one file. The PDF it wrote, or an exception saying why."""
    out_dir.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(
        command(soffice, source, out_dir, profile, mime_type),
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        creationflags=flags,
        check=False,
    )
    produced = out_dir / (source.stem + ".pdf")
    if result.returncode != 0 or not produced.is_file():
        said = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"LibreOffice exited {result.returncode} without a PDF. {said[:300]}".strip())
    return produced


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_pages(path: Path) -> int | None:
    try:
        import pymupdf

        with pymupdf.open(str(path)) as document:
            return document.page_count
    except Exception:  # an unreadable PDF is recorded, never fatal
        return None


@dataclass
class Rendered:
    converted: list[str] = field(default_factory=list)
    unchanged: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    mismatched: list[tuple[str, int, int]] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)


def run(conn, config: Config, course_ids: list[str], *, dry_run: bool = False, now: str | None = None) -> Rendered:
    """Render every in-scope PPTX and DOCX whose source changed since last time.

    Finds `soffice` first, and before anything else: if it is missing, nothing
    is half-done and the RenderError names the fix.
    """
    result = Rendered()
    rows = store.renderable_files(conn, course_ids, list(FILTERS))
    library = Path(config.library_dir)

    wanted: list[tuple] = []
    for row in rows:
        source = library / str(row["local_path"])
        if not source.is_file():
            result.failed.append((str(row["drive_id"]), "the file is not on disk"))
            continue
        digest = sha256_of(source)
        if row["source_sha256"] == digest and row["pdf_path"] and not row["error"]:
            if (library / str(row["pdf_path"])).is_file():
                result.unchanged += 1
                continue
        wanted.append((row, source, digest))

    if dry_run:
        result.planned = [str(row["drive_id"]) for row, _, _ in wanted]
        return result
    if not wanted:
        return result

    soffice = find_soffice(config)
    profile = config.data_dir / "tmp" / "libreoffice-profile"
    work = config.data_dir / "tmp" / "render"
    out_root = library / "pdf"
    out_root.mkdir(parents=True, exist_ok=True)

    for row, source, digest in wanted:
        drive_id = str(row["drive_id"])
        shutil.rmtree(work, ignore_errors=True)
        try:
            produced = convert(soffice, source, work, profile, str(row["mime_type"]))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as err:
            said = (
                f"LibreOffice took longer than {TIMEOUT_SECONDS} s and was stopped"
                if isinstance(err, subprocess.TimeoutExpired)
                else str(err)
            )
            store.save_rendering(
                conn, drive_id, source_sha256=digest, pdf_path=None, sha256=None,
                size=None, pages=None, renderer=RENDERER, error=said, now=now,
            )
            conn.commit()
            result.failed.append((drive_id, said))
            continue
        target = out_root / f"{drive_id}.pdf"
        os.replace(produced, target)
        pages = pdf_pages(target)
        store.save_rendering(
            conn, drive_id, source_sha256=digest,
            pdf_path=target.relative_to(library).as_posix(),
            sha256=sha256_of(target), size=target.stat().st_size, pages=pages,
            renderer=RENDERER, now=now,
        )
        conn.commit()
        result.converted.append(drive_id)
        if row["mime_type"] == PPTX_MIME and row["pages"] and pages and pages != row["pages"]:
            result.mismatched.append((drive_id, int(row["pages"]), int(pages)))
    shutil.rmtree(work, ignore_errors=True)
    return result
