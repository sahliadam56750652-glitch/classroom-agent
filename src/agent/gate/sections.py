"""Cut one long document into windows a single evening can actually hold.

One study item is one Classroom post, and for seven posts in ten that is the
right unit: the median item in this library is 3 pages. It is the wrong unit for
the fifth of the corpus that holds three quarters of the pages -- measured, 14
items over 30 pages holding 999 of 1290. "Review Chapter 1 (92 pages) before
tomorrow's lab" is more than an evening allows, so it gets skipped, and a gate
that asks for something impossible is a gate that gets muted.

Nothing here decides anything. It computes where a document could reasonably be
cut, and `agent sections` prints the answer so it can be judged by eye. The
cursor, the gate message and the window-scoped quiz are deliberately not here.

Why a page budget rather than real sections
-------------------------------------------
Measured across the sixteen largest PDFs in this library: five carry a bookmark
table and eleven carry none. Where one exists it is PowerPoint's per-slide
export -- one entry per page, titled "Diapositive 12 Programming languages" --
so a RUN OF IDENTICAL TITLES IS A TOPIC. `Introduction` pp. 4-7,
`Programming languages` pp. 9-14, `Conditional Statements` pp. 41-46.

The obvious fallback for the other eleven does not work, and this was tested
rather than assumed: taking the first non-empty line of each page as its title
reproduces the bookmark table where a bookmark table already exists (155 pages
gave 22 runs either way) and collapses everywhere else -- 126 runs from 129
pages, 57 from 57, 27 from 29. It is the same signal, not a second one.

So there is no derivable heading structure for most long documents here, and a
design that DEPENDS on good boundaries is betting on data that is not there. A
page budget always works; a good boundary is a bonus it snaps to when one is in
reach. Below the budget a document is exactly one window, so the ~70% of items
that were already a session's worth behave precisely as they do today.

Windows are computed and never stored. They are a pure function of the page
count, the titles and the budget, so a re-fetch recomputes them rather than
reconciling them, and there is no second source of truth about which pages
belong together.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

# How many pages one gate may ask for. Twenty is a starting value and not a
# measured one -- how much a session actually covers cannot be known until a
# real session happens -- so it is an argument everywhere and a constant
# nowhere. At 20 the 92-page chapter becomes five windows.
DEFAULT_BUDGET = 20

# A boundary is only worth snapping to if it leaves a window worth an evening.
# Runs are short -- median 4 pages -- so the nearest boundary before the budget
# is usually close to it. But a 4-page run followed by an 18-page one would snap
# to page 4 and hand over a window a fifth of the size asked for, which is the
# budget being ignored rather than respected. Below this fraction the cut falls
# on the budget and `snapped` says it was arbitrary.
SNAP_FLOOR = 0.5

# PowerPoint's exporter names every bookmark after the slide it points at, in
# the UI language of whoever exported it. This library has French exports
# ("Diapositive 12 OUTLINE") and English ones; the number is the slide index and
# is noise, the text after it is the slide's own title and is the signal.
SLIDE_PREFIX = re.compile(r"^(?:diapositive|slide|folie|diapositiva)\s+\d+\s*", re.I)

# Under this many characters a page has no content to be identified by -- a
# section divider, or a scan nothing has transcribed. It anchors to None rather
# than to the hash of an empty string, which every blank page would share.
MIN_ANCHOR_CHARS = 40


@dataclass(frozen=True)
class Window:
    """A contiguous run of pages within ONE file, at most `budget` long.

    Page indices are 0-based and inclusive at both ends, matching
    `ocr_pages.page_index`. `label` is the 1-based form, for humans only.
    """

    index: int
    first: int
    last: int
    title: str = ""
    topics: int = 1
    snapped: bool = False
    unread: int = 0
    # True when this window opens part-way through a topic rather than at the
    # start of one -- a run longer than the budget has to span two windows.
    # Without this the two read as the same title twice and look like a bug.
    continues: bool = False

    @property
    def pages(self) -> int:
        return self.last - self.first + 1

    @property
    def label(self) -> str:
        if self.first == self.last:
            return f"page {self.first + 1}"
        return f"pages {self.first + 1}-{self.last + 1}"

    @property
    def ready(self) -> bool:
        """Whether a quiz on THIS window would be a quiz on what I was given.

        The strict rule of `scheduler.Item.ready` unchanged, applied to what is
        actually being asked about. Today one untranscribed page blocks a whole
        item, which permanently blocks the biggest documents: CHAPTER 2 is 155
        pages with 26 scans and none of them read. A window whose own pages have
        all been read is answerable regardless of page 140.
        """
        return self.unread == 0


@dataclass(frozen=True)
class Document:
    """One file of a post, cut into windows."""

    drive_id: str
    title: str
    pages: int
    windows: tuple[Window, ...]
    titled: bool
    anchors: tuple[str | None, ...] = ()
    # Set when OCR has never run over this file at all. Then which pages are
    # images is known only in aggregate, exactly as packs.render_pages says, so
    # the per-window `unread` figures are incomplete and must not be read as
    # "this window is ready". Said out loud rather than papered over.
    untracked_scans: int = 0

    @property
    def anchored(self) -> int:
        return sum(1 for value in self.anchors if value)


# --------------------------------------------------------------------------
# titles
# --------------------------------------------------------------------------

def clean_title(raw: str) -> str:
    """A bookmark's text without the exporter's slide numbering."""
    return re.sub(r"\s+", " ", SLIDE_PREFIX.sub("", raw.strip())).strip()


def page_titles(path: Path) -> list[str] | None:
    """One title per page from the PDF's bookmark table, or None if it has none.

    Each page takes the title of the last bookmark at or before it, which reads
    a per-slide export and a real hierarchical table of contents the same way.
    Pages before the first bookmark get "" and simply never carry a boundary.

    None rather than a list of empty strings, because "this document has no
    structure" and "this document's first four pages precede its first heading"
    are different facts and the caller reports them differently.
    """
    try:
        with pymupdf.open(path) as document:
            count = document.page_count
            toc = document.get_toc() or []
    except Exception:
        # A malformed PDF must not end a run. The document is simply untitled,
        # which is the same state as the eleven that genuinely have no table.
        return None

    if len(toc) < 2 or count < 1:
        # One bookmark offers no boundary, so it is not structure.
        return None

    titles = [""] * count
    for entry in toc:
        index = int(entry[2]) - 1
        if 0 <= index < count:
            titles[index] = clean_title(str(entry[1]))

    # Carry each title forward until the next one starts.
    current = ""
    for index, title in enumerate(titles):
        if title:
            current = title
        titles[index] = current
    return titles


def runs(titles: list[str]) -> list[tuple[int, int, str]]:
    """(first page, last page, title) for each maximal run of equal titles."""
    out: list[tuple[int, int, str]] = []
    start = 0
    for title, group in itertools.groupby(titles):
        length = len(list(group))
        out.append((start, start + length - 1, title))
        start += length
    return out


# --------------------------------------------------------------------------
# the windows themselves
# --------------------------------------------------------------------------

def windows(
    pages: int,
    titles: list[str] | None = None,
    *,
    budget: int = DEFAULT_BUDGET,
    unread_pages: frozenset[int] = frozenset(),
) -> list[Window]:
    """Cut `pages` into contiguous windows of at most `budget` pages each.

    Pure: no I/O, no database, no clock. The same arguments give the same
    windows, which is what lets them be recomputed after a re-fetch instead of
    stored and reconciled.

    Every page lands in exactly one window and the windows are in order, so a
    cursor walking them covers the document once and completely.
    """
    if pages <= 0:
        return []
    if budget < 1:
        raise ValueError(f"a window budget of {budget} pages cannot hold anything")
    if titles is not None and len(titles) != pages:
        # Refused rather than zipped to the shorter of the two. A title list of
        # a different length belongs to a different revision of the file, so
        # every boundary it offers is in the wrong place -- and `snapped` would
        # then claim the document asked for a cut it never asked for.
        raise ValueError(
            f"{len(titles)} title(s) for {pages} page(s): these describe "
            f"different revisions of the file"
        )

    boundaries: set[int] = set()
    if titles:
        # The last page of every run is a place the document itself changes
        # subject.
        boundaries = {last for _first, last, _title in runs(titles)}

    out: list[Window] = []
    start = 0
    while start < pages:
        ceiling = min(start + budget - 1, pages - 1)
        end, snapped = ceiling, False

        if boundaries and ceiling < pages - 1:
            # The last window never snaps: it ends where the document ends.
            floor = start + max(0, int(budget * SNAP_FLOOR) - 1)
            candidates = [b for b in boundaries if floor <= b <= ceiling]
            if candidates:
                end, snapped = max(candidates), True

        covered = titles[start : end + 1] if titles else []
        out.append(
            Window(
                index=len(out),
                first=start,
                last=end,
                title=(titles[start] if titles else ""),
                topics=len({title for title in covered if title}) or 1,
                snapped=snapped,
                unread=sum(1 for page in range(start, end + 1) if page in unread_pages),
                continues=bool(titles) and start > 0 and titles[start - 1] == titles[start],
            )
        )
        start = end + 1

    return out


# --------------------------------------------------------------------------
# identifying a page by what is on it
# --------------------------------------------------------------------------

def anchor(page_text: str, ocr_text: str | None = None) -> str | None:
    """A page's identity, as a hash of its content rather than its index.

    This is what lets a position survive the document being re-fetched with a
    changed checksum. A professor who inserts four slides moves every page after
    them; an index-based cursor then silently points at different material,
    which is the misreporting failure this project has already paid for four
    times. A content hash is looked up in the new page list instead.

    Measured across the sixteen largest documents in this library: 1093 of 1094
    pages hash distinctly, zero collisions, one genuinely blank page. The same
    instinct as invariant 2 and as `ocr_pages.page_hash` -- identity is what the
    thing IS, never when it was seen.

    A scan carries its content in its transcription rather than in its own text,
    so that is folded in where the page's own text is too thin to identify it.
    """
    body = re.sub(r"\s+", " ", page_text or "").strip()
    if len(body) < MIN_ANCHOR_CHARS and ocr_text:
        body = (re.sub(r"\s+", " ", ocr_text).strip() + " " + body).strip()
    if len(body) < MIN_ANCHOR_CHARS:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# one file, assembled
# --------------------------------------------------------------------------

def read_document(
    row: Any,
    library_dir: Path,
    ocr_rows: dict[int, sqlite3.Row],
    *,
    budget: int = DEFAULT_BUDGET,
) -> Document | None:
    """Cut one attachment of a post into windows. None when its text is gone.

    `row` is one row of `store.study_item_sources` and `ocr_rows` one call to
    `store.ocr_pages_for` -- the same two the quiz already assembles from, so
    this sees exactly the text a question could be built on and never more.
    """
    from ..files import extract

    drive_id = str(row["drive_id"])
    title = str(row["file_title"] or drive_id)
    try:
        raw = (library_dir / str(row["text_path"] or "")).read_text(encoding="utf-8")
    except OSError:
        # The row says there is text and the disk disagrees. Reported by the
        # caller as a missing file, never silently treated as an empty one.
        return None

    pages = raw.split(extract.PAGE_BREAK)
    titles = _titles_for(row, library_dir, len(pages))

    unread = frozenset(
        index for index, ocr in ocr_rows.items() if ocr["status"] != "ok"
    )
    anchors = tuple(
        anchor(page, (ocr_rows[index]["text"] if index in ocr_rows else None))
        for index, page in enumerate(pages)
    )

    return Document(
        drive_id=drive_id,
        title=title,
        pages=len(pages),
        windows=tuple(windows(len(pages), titles, budget=budget, unread_pages=unread)),
        titled=titles is not None,
        anchors=anchors,
        untracked_scans=int(row["scan_pages"] or 0) if not ocr_rows else 0,
    )


def _titles_for(row: Any, library_dir: Path, pages: int) -> list[str] | None:
    """The bookmark titles of a row's PDF, when it has any that still fit.

    A bookmark table that disagrees with the extracted page count describes a
    different revision of the file than the text does. Dropped rather than
    aligned by guesswork: a boundary in the wrong place is worse than no
    boundary, because `snapped` would claim it came from the document.
    """
    try:
        local = row["local_path"]
    except (IndexError, KeyError):
        return None
    if not local:
        return None
    source = library_dir / str(local)
    if source.suffix.lower() != ".pdf" or not source.exists():
        return None
    found = page_titles(source)
    return found if found is not None and len(found) == pages else None
