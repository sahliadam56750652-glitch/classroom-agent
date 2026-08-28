"""Read the pages PyMuPDF could not, using a vision model.

Why a vision model and not Tesseract
------------------------------------
Measured on the tracked library: 322 of 1287 pages need OCR, and 71 of 72 PDFs
average over 100 chars/page. The pages needing help are therefore not scanned
documents -- they are images embedded *inside* otherwise-native slide decks:
diagrams, equations, code screenshots and photographed boards. Tesseract is at
its worst on exactly that content. It reads a photographed whiteboard as a
sprinkling of punctuation, turns an equation into line noise, and gives back
indented code with the indentation gone, and it does all of that without
signalling that it failed. A quarter of this library is too much to lose to
output that looks like text and is not.

That puts a model inside ingestion, which sits awkwardly with invariant 4, and
two things here are what keep it honest. Every page is cached by the hash of
its rendered image, so a given page is transcribed exactly once and the text
never changes underneath the library afterwards. And every transcribed page is
recorded in `ocr_pages` with its model, so nothing downstream has to guess
which text came from the PDF and which came from a model.

Nothing here ever loses a file. Quota exhaustion, an outage or a refusal all
leave the page `pending` and the run completes; the next run picks it up.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ..config import Config
from ..db import store
from ..llm.provider import (
    LLMAuthError,
    LLMError,
    LLMModelUnavailable,
    LLMProvider,
    LLMQuotaError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
)
from . import extract

# 150 DPI is the point where a photographed board becomes legible without the
# payload doubling for nothing. A 4:3 slide renders to roughly 1200x1650, which
# is comfortably inside the inline-data limit and costs one image tile.
RENDER_DPI = 150

# Rendered pages go over the wire as PNG: lossless, so a JPEG artefact never
# turns a minus sign into something else in an equation.
RENDER_MIME = "image/png"

# Images posted as attachments in their own right. An image attachment is 100%
# OCR by definition -- there is no text layer to fall back on.
IMAGE_MIMES = frozenset({"image/png", "image/jpeg"})

# What a page that has not been transcribed yet leaves in the text file. A
# silent gap would read as a page the lecturer left blank, which is exactly the
# wrong conclusion; partial text must never present itself as complete.
PENDING_MARKER = "[page {page}: not transcribed yet -- run `agent ocr`]"

# How long to hold after a per-minute refusal the API did not put a delay on.
# A minute clears any per-minute window by definition.
RATE_LIMIT_PAUSE = 60.0
MAX_RATE_LIMIT_PAUSE = 120.0

# Waiting out a per-minute ceiling is right until it stops working. If this many
# refusals arrive with no page succeeding in between, the allowance is not a
# sixty-second window that will clear -- it is gone -- and continuing would mean
# sleeping through several hundred pages to learn that one page at a time.
MAX_CONSECUTIVE_RATE_LIMITS = 3

PROMPT = """\
You are transcribing one page from a university lecture handout. The material \
is in English.

Reproduce everything readable on the page as plain text, in reading order.

- Transcribe text exactly as it appears. Do not summarise, correct, translate \
or comment on it.
- Mathematics: write equations in LaTeX, between $ signs. Preserve subscripts, \
superscripts, and every symbol.
- Diagrams, charts and figures: transcribe every label, axis title, legend \
entry, caption and annotation inside them.
- Code and terminal output: transcribe verbatim, preserving indentation, \
symbols and line breaks.
- Tables: one row per line, cells separated by tabs.
- Describe a figure in prose ONLY where it carries information that none of \
the text conveys, and mark that description [figure: ...].
- If the page has no readable content, reply with exactly: [no readable content]

Output only the transcription, with no preamble."""

EMPTY_SENTINEL = "[no readable content]"

# The page_hash recorded for a page that was deliberately never rasterised --
# deferred by --limit or by a spent quota. A hash cannot be known without
# rendering, and rendering is the whole cost being avoided, so the column says
# "not hashed" rather than carrying a plausible-looking value that is not one.
# Nothing matches it: the result cache compares against real digests, and the
# cross-file lookup only ever considers rows that are already 'ok'.
UNRENDERED = ""


class OCRError(Exception):
    """OCR cannot run at all -- not one page failing, which is recorded."""


@dataclass
class OCRResult:
    files: int = 0
    pages_considered: int = 0
    transcribed: int = 0
    cached: int = 0
    pending: int = 0
    failed: int = 0
    chars: int = 0
    quota_exhausted: bool = False
    rate_limited: int = 0
    # Counted apart from `pending`: a timeout is neither a refusal nor a
    # quota problem, and it is the one failure that costs a full minute to
    # discover. Hidden inside a pending count it is invisible.
    timed_out: int = 0
    # 'daily-quota' or 'repeated-rate-limits'. Which one it was decides
    # whether to come back in a minute or tomorrow.
    stop_reason: str | None = None
    dry_run: bool = False
    would_send: list[tuple[str, int]] = field(default_factory=list)

    # A page recorded as `pending` says nothing about why. These two separate
    # "we asked and were turned down" from "we never asked", which the counts
    # alone could not distinguish -- and not distinguishing them is what made a
    # dead API key, a retired model and a broken TLS chain all look the same.
    attempted: int = 0
    never_attempted: int = 0

    @property
    def call_failures(self) -> int:
        """Calls issued that produced no text. attempted - transcribed."""
        return self.attempted - self.transcribed

    def items_seen(self) -> dict[str, int]:
        return {
            "files": self.files,
            "pages_considered": self.pages_considered,
            "attempted": self.attempted,
            "transcribed": self.transcribed,
            "cached": self.cached,
            "pending": self.pending,
            "never_attempted": self.never_attempted,
            "rate_limited": self.rate_limited,
            "timed_out": self.timed_out,
            "failed": self.failed,
        }


def page_hash(image: bytes) -> str:
    """Cache key for one rendered page."""
    return hashlib.sha256(image).hexdigest()


def render_page(page, dpi: int = RENDER_DPI) -> bytes:
    """One PDF page as PNG bytes."""
    return page.get_pixmap(dpi=dpi).tobytes("png")


OCR_SUFFIX = "+ocr"


def _with_ocr_suffix(method: str | None) -> str:
    """Mark an extraction as partly model-generated, idempotently."""
    base = method or "pymupdf"
    return base if base.endswith(OCR_SUFFIX) else f"{base}{OCR_SUFFIX}"


def _clean(text: str) -> str:
    """The model's answer, with its own 'nothing here' sentinel turned into ''."""
    stripped = text.strip()
    return "" if stripped == EMPTY_SENTINEL else stripped


class _Source:
    """One file's pages, rasterised only when a page is actually going to be sent.

    Classification and rendering are separated because their costs are three
    orders of magnitude apart: measured over the three largest decks in the
    library, classifying every page took 0.4s and rasterising the scan pages
    took 18.7s. Deciding *which* pages need OCR is therefore free and happens
    eagerly; producing the bytes to send is not, and happens on demand.
    """

    def __init__(self, path: Path, mime_type: str | None):
        self.path = path
        self.mime_type = mime_type or RENDER_MIME
        self.is_image = mime_type in IMAGE_MIMES
        self._document = None

        if self.is_image:
            # An image attachment is one page and no text layer at all.
            self.native: list[str] = [""]
            self.scan_indices: list[int] = [0]
            return

        self._document = pymupdf.open(path)
        self.native = []
        self.scan_indices = []
        for index, page in enumerate(self._document):
            kind, text = extract.classify_page(page)
            self.native.append(text)
            if kind == "scan":
                self.scan_indices.append(index)

    @property
    def page_mime(self) -> str:
        """What the bytes handed to the model will be."""
        return self.mime_type if self.is_image else RENDER_MIME

    def render(self, index: int) -> bytes:
        """The bytes for one page. The expensive call, made as late as possible."""
        if self.is_image:
            return self.path.read_bytes()
        return render_page(self._document[index])

    def close(self) -> None:
        if self._document is not None:
            self._document.close()
            self._document = None

    def __enter__(self) -> _Source:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _candidates(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Files with at least one page only a model can read.

    Image attachments qualify unconditionally; PDFs qualify on the scan_pages
    the extract stage counted.
    """
    placeholders = ", ".join("?" for _ in IMAGE_MIMES)
    return db.execute(
        f"SELECT * FROM extractions "
        f" WHERE status = 'ok' AND local_path IS NOT NULL "
        f"   AND (scan_pages > 0 OR mime_type IN ({placeholders})) "
        f" ORDER BY drive_id",
        tuple(sorted(IMAGE_MIMES)),
    ).fetchall()


def _merge(native: list[str], texts: dict[int, str], pending: set[int]) -> str:
    """Rebuild the file's text with transcriptions spliced in, in page order.

    Page order is structural: the list is indexed by page number and joined at
    the end, so there is no ordering to get wrong even when transcriptions
    arrive out of order or only some of them arrive at all.
    """
    merged = list(native)
    for index, text in texts.items():
        if index >= len(merged):
            continue
        # A native page with a little text plus an embedded diagram keeps both:
        # the caption under the figure is often the only thing tying them.
        base = merged[index].strip()
        merged[index] = f"{base}\n{text}".strip() if base else text
    for index in pending:
        if index < len(merged):
            marker = PENDING_MARKER.format(page=index + 1)
            base = merged[index].strip()
            merged[index] = f"{base}\n{marker}".strip() if base else marker
    return extract.PAGE_BREAK.join(merged)


def run(
    config: Config,
    db: sqlite3.Connection,
    *,
    provider: LLMProvider | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    verbose: bool = False,
    sleep=time.sleep,
    now: str | None = None,
) -> OCRResult:
    """Transcribe every page that needs it, and merge the results back.

    dry_run renders and hashes but sends nothing, so it reports exactly what a
    real run would spend -- cache hits included, which is usually most of it.

    limit bounds the number of pages actually sent, so a free-tier quota can be
    worked through over several runs without any of them failing.
    """
    result = OCRResult(dry_run=dry_run)
    rows = _candidates(db)
    stamp = now or store._utc_now_iso()
    sent = 0
    consecutive_rate_limits = 0

    # Diagnostic only: narrate what happens to the FIRST page and then go quiet,
    # so the question "did anything actually get sent?" has an answer that does
    # not depend on reading the code.
    traced = {"done": not verbose}

    def trace(message: str) -> None:
        if verbose:
            print(f"  {message}")

    trace(f"candidate files: {len(rows)}")

    for row in rows:
        drive_id = row["drive_id"]
        source = config.library_dir / row["local_path"]
        if not source.exists():
            continue

        try:
            opened = _Source(source, row["mime_type"])
        except Exception as err:
            # A malformed PDF costs its own row, never the run.
            store.upsert_extraction(db, drive_id, error=f"ocr render failed: {err!r}")
            result.failed += 1
            continue

        with opened as pages:
            if not pages.scan_indices:
                continue

            result.files += 1
            result.pages_considered += len(pages.scan_indices)

            known = store.ocr_pages_for(db, drive_id)
            # If the file is byte-identical to when its pages were last read,
            # nothing inside it can have changed, so the stored hashes still
            # describe it and none of it needs rasterising to find that out.
            current_md5 = row["md5_checksum"]
            trusted = bool(current_md5) and store.get_ocr_source_md5(db, drive_id) == current_md5

            texts: dict[int, str] = {}
            pending: set[int] = set()

            for index in pages.scan_indices:
                previous = known.get(index)

                first = not traced["done"]
                if first:
                    traced["done"] = True
                    trace(f"first page: {drive_id} page {index}")
                    trace(f"  hashes trusted without rendering: {trusted}")
                    trace(f"  previously recorded: {previous['status'] if previous else 'never'}")
                    trace(f"  limit={limit} sent={sent} quota_exhausted={result.quota_exhausted}")

                # Settled without touching the file at all.
                if not force and trusted and previous is not None and previous["page_hash"]:
                    if previous["status"] == "ok":
                        texts[index] = previous["text"] or ""
                        result.cached += 1
                        continue
                    if previous["status"] == "error":
                        result.failed += 1
                        continue

                known_digest = (
                    previous["page_hash"]
                    if (trusted and previous is not None and previous["page_hash"])
                    else None
                )

                if dry_run:
                    # A report, and a report does not get to cost minutes.
                    if previous is not None and previous["status"] == "ok":
                        result.cached += 1
                    else:
                        result.would_send.append((drive_id, index))
                        pending.add(index)
                    continue

                if result.quota_exhausted or (limit is not None and sent >= limit):
                    if not force and previous is not None and previous["status"] == "ok":
                        # Already transcribed, and this run is not going to look
                        # at it. Deferring must never downgrade paid work: without
                        # this guard an 'ok' page reached after the limit -- on a
                        # file whose md5 cannot be trusted, so the fast path above
                        # did not fire -- was rewritten as 'pending' and its text
                        # discarded. Whether that happened depended purely on
                        # which file sorted first.
                        texts[index] = previous["text"] or ""
                        result.cached += 1
                        continue
                    if first:
                        trace("  SHORT-CIRCUIT: recorded pending without any HTTP request")
                    # Deliberately not rendered: --limit must not pay to prepare
                    # pages it was never going to send. UNRENDERED records that
                    # honestly rather than storing a hash of something else.
                    pending.add(index)
                    result.pending += 1
                    result.never_attempted += 1
                    store.upsert_ocr_page(
                        db,
                        drive_id=drive_id,
                        page_index=index,
                        page_hash=known_digest or UNRENDERED,
                        status="pending",
                        error="quota exhausted" if result.quota_exhausted else "run limit reached",
                        now=stamp,
                    )
                    continue

                if provider is None:
                    raise OCRError("a provider is required for a run that is not a dry run")

                # This page is going to be sent, so now it has to be rasterised.
                try:
                    image = pages.render(index)
                except Exception as err:
                    store.upsert_extraction(db, drive_id, error=f"ocr render failed: {err!r}")
                    result.failed += 1
                    continue
                digest = page_hash(image)

                # The result cache, unchanged, against a hash known to be current.
                if not force and previous is not None and previous["page_hash"] == digest:
                    if previous["status"] == "ok":
                        texts[index] = previous["text"] or ""
                        result.cached += 1
                        continue
                    if previous["status"] == "error":
                        result.failed += 1
                        continue

                if not force:
                    shared = store.find_ocr_by_hash(db, digest)
                    if shared is not None:
                        texts[index] = shared["text"] or ""
                        result.cached += 1
                        store.upsert_ocr_page(
                            db,
                            drive_id=drive_id,
                            page_index=index,
                            page_hash=digest,
                            status="ok",
                            text=shared["text"],
                            model=shared["model"],
                            now=stamp,
                        )
                        continue

                sent += 1
                result.attempted += 1
                if first:
                    trace("  calling the provider now")
                try:
                    text = _clean(
                        provider.transcribe_image(image, PROMPT, mime_type=pages.page_mime)
                    )
                except (LLMAuthError, LLMModelUnavailable):
                    # Loudly, and immediately. Both are configuration faults that
                    # every remaining page would hit identically, so degrading them
                    # into "pending" buries one fixable cause under 323 pages of
                    # silence -- which is exactly how a retired model cost an hour.
                    raise
                except LLMRateLimited as err:
                    # A short-window ceiling, not a daily cap. Waiting is the whole
                    # remedy, so the run holds and carries on to the next page.
                    result.rate_limited += 1
                    consecutive_rate_limits += 1
                    pending.add(index)
                    result.pending += 1
                    store.upsert_ocr_page(
                        db,
                        drive_id=drive_id,
                        page_index=index,
                        page_hash=digest,
                        status="pending",
                        error=f"rate limited: {err}",
                        attempts=(previous["attempts"] + 1) if previous else 1,
                        now=stamp,
                    )
                    if consecutive_rate_limits >= MAX_CONSECUTIVE_RATE_LIMITS:
                        # Waiting has stopped helping. Stop asking, but say that
                        # this is what happened rather than blaming a daily cap.
                        result.quota_exhausted = True
                        result.stop_reason = "repeated-rate-limits"
                        continue
                    sleep(min(err.retry_after or RATE_LIMIT_PAUSE, MAX_RATE_LIMIT_PAUSE))
                    continue
                except LLMQuotaError as err:
                    result.quota_exhausted = True
                    result.stop_reason = "daily-quota"
                    pending.add(index)
                    result.pending += 1
                    store.upsert_ocr_page(
                        db,
                        drive_id=drive_id,
                        page_index=index,
                        page_hash=digest,
                        status="pending",
                        error=str(err),
                        attempts=(previous["attempts"] + 1) if previous else 1,
                        now=stamp,
                    )
                    continue
                except LLMRefused as err:
                    store.upsert_ocr_page(
                        db,
                        drive_id=drive_id,
                        page_index=index,
                        page_hash=digest,
                        status="error",
                        error=str(err),
                        attempts=(previous["attempts"] + 1) if previous else 1,
                        now=stamp,
                    )
                    result.failed += 1
                    continue
                except LLMTimeout as err:
                    # The request outlived the read timeout, after the retries
                    # inside the provider. Recoverable later, so pending -- but
                    # counted separately, because a timeout is neither a refusal
                    # nor a quota problem and costs the full timeout to discover.
                    # The run continues: one slow response must not end a batch
                    # with nineteen pages of allowance still to spend.
                    result.timed_out += 1
                    pending.add(index)
                    result.pending += 1
                    store.upsert_ocr_page(
                        db,
                        drive_id=drive_id,
                        page_index=index,
                        page_hash=digest,
                        status="pending",
                        error=f"timed out: {err}",
                        attempts=(previous["attempts"] + 1) if previous else 1,
                        now=stamp,
                    )
                    continue
                except LLMError as err:
                    # Transport or 5xx: recoverable later, so pending rather than
                    # error, and the run keeps going through the other pages.
                    pending.add(index)
                    result.pending += 1
                    store.upsert_ocr_page(
                        db,
                        drive_id=drive_id,
                        page_index=index,
                        page_hash=digest,
                        status="pending",
                        error=str(err),
                        attempts=(previous["attempts"] + 1) if previous else 1,
                        now=stamp,
                    )
                    continue

                texts[index] = text
                consecutive_rate_limits = 0
                result.transcribed += 1
                result.chars += len(text)
                store.upsert_ocr_page(
                    db,
                    drive_id=drive_id,
                    page_index=index,
                    page_hash=digest,
                    status="ok",
                    text=text,
                    model=provider.name,
                    attempts=(previous["attempts"] + 1) if previous else 1,
                    now=stamp,
                )

            if dry_run or not (texts or pending):
                continue

            # The hashes now on record describe this revision of the file, so the
            # next run can skip past them without opening it.
            if current_md5:
                store.set_ocr_source_md5(db, drive_id, current_md5, now=stamp)

            merged = _merge(pages.native, texts, pending)

        destination = config.library_dir / extract.TEXT_SUBDIR / f"{drive_id}.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(merged, encoding="utf-8")

        store.upsert_extraction(
            db,
            drive_id,
            text_path=f"{extract.TEXT_SUBDIR}/{drive_id}.txt",
            # The method records that some of this text is model-generated.
            # Nothing downstream should have to infer that.
            method=_with_ocr_suffix(row["method"]),
            chars=len(merged),
            # ocr_pages against scan_pages is the honest measure of what is
            # still missing: equal means complete, short means work is pending.
            ocr_pages=sum(1 for index in texts if index not in pending),
            extracted_at=stamp,
        )

    return result
