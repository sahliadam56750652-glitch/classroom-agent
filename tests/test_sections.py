"""Cutting a long document into evening-sized windows.

Nothing here reaches a model, Drive, or the network. The properties worth
stating up front are the two the later phases will rest on:

  * **Every page lands in exactly one window.** A cursor walking the windows
    covers the document once and completely -- no page asked for twice, and
    more importantly none skipped, because a skipped page is material I was
    never asked to read and would still be quizzed on later.
  * **A document at or under the budget is exactly one window.** Seven items in
    ten in the real library are already a session's worth, so windowing has to
    be a no-op for them or it is a regression dressed up as a feature.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.gate import sections


# Runs of 4, 6, 6, 4 pages -- the shape a real slide deck has, where a topic
# spans several slides and the exporter names every one of them after it.
DECK = (
    ["Introduction"] * 4
    + ["Programming languages"] * 6
    + ["Conditional Statements"] * 6
    + ["Loops"] * 4
)


def titles_of(windows):
    return [window.title for window in windows]


def spans(windows):
    return [(window.first, window.last) for window in windows]


# --------------------------------------------------------------------------
# the properties that must hold whatever the input
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pages", [1, 2, 19, 20, 21, 29, 57, 92, 155])
@pytest.mark.parametrize("budget", [1, 3, 12, 20])
def test_windows_cover_every_page_exactly_once(pages, budget):
    """Contiguous, ordered, non-overlapping, complete -- with or without titles."""
    for titles in (None, [f"topic {index // 5}" for index in range(pages)]):
        got = sections.windows(pages, titles, budget=budget)
        assert got, "a document with pages must produce at least one window"
        assert got[0].first == 0
        assert got[-1].last == pages - 1
        for earlier, later in zip(got, got[1:]):
            assert later.first == earlier.last + 1
        assert sum(window.pages for window in got) == pages
        assert [window.index for window in got] == list(range(len(got)))


@pytest.mark.parametrize("budget", [1, 5, 20])
def test_no_window_exceeds_the_budget(budget):
    titles = (DECK * 8)[:155]
    for window in sections.windows(155, titles, budget=budget):
        assert window.pages <= budget


def test_titles_of_the_wrong_length_are_refused():
    """A shorter list belongs to a different revision of the file, so every
    boundary it offers is in the wrong place. Refused, never zipped."""
    with pytest.raises(ValueError, match="different revisions"):
        sections.windows(20, ["only one"])


def test_a_short_document_is_one_window():
    """The no-op case. The median item here is 3 pages and must not change."""
    for pages in (1, 3, 19, 20):
        got = sections.windows(pages, budget=20)
        assert len(got) == 1
        assert (got[0].first, got[0].last) == (0, pages - 1)
        assert got[0].snapped is False


def test_windows_are_deterministic():
    """Recomputed rather than stored, so the same input must give the same cut."""
    titles = DECK * 4 + ["Tail"] * 12
    first = sections.windows(92, titles, budget=20)
    second = sections.windows(92, titles, budget=20)
    assert first == second


def test_a_budget_below_one_is_refused():
    with pytest.raises(ValueError, match="cannot hold anything"):
        sections.windows(10, budget=0)


def test_no_pages_no_windows():
    assert sections.windows(0) == []


# --------------------------------------------------------------------------
# snapping to a boundary the document itself provides
# --------------------------------------------------------------------------

def test_boundaries_fall_on_title_runs():
    """With titles, a cut lands where the deck changes subject."""
    got = sections.windows(20, DECK, budget=12)
    # Runs end at pages 3, 9, 15, 19. Twelve pages from 0 reaches page 11; the
    # last boundary at or before it is 9, which is the end of "Programming
    # languages" rather than two slides into "Conditional Statements".
    assert spans(got) == [(0, 9), (10, 19)]
    assert got[0].snapped is True
    assert titles_of(got) == ["Introduction", "Conditional Statements"]


def test_the_last_window_never_snaps():
    """It ends where the document ends, so there is no boundary to claim."""
    got = sections.windows(20, DECK, budget=12)
    assert got[-1].last == 19
    assert got[-1].snapped is False


def test_without_titles_the_cut_is_the_budget_and_says_so():
    got = sections.windows(50, None, budget=20)
    assert spans(got) == [(0, 19), (20, 39), (40, 49)]
    assert all(not window.snapped for window in got)
    assert all(window.title == "" for window in got)


def test_a_run_longer_than_the_budget_falls_back_to_the_budget():
    """Eleven of sixteen real documents have no usable structure at all, and
    even a titled one can hold a 21-page run. There is no boundary in reach,
    so the cut is arbitrary and must not claim otherwise."""
    titles = ["One long topic"] * 30 + ["Next"] * 10
    got = sections.windows(40, titles, budget=20)
    assert spans(got) == [(0, 19), (20, 39)]
    assert got[0].snapped is False


def test_a_boundary_too_early_is_not_worth_snapping_to():
    """A 4-page run followed by an 18-page one would snap to page 4 and hand
    over a window a fifth of the size asked for. That is the budget being
    ignored, not respected."""
    titles = ["Short"] * 4 + ["Very long topic"] * 18
    got = sections.windows(22, titles, budget=20)
    assert got[0].last == 19
    assert got[0].snapped is False


def test_topics_counts_what_a_window_spans():
    got = sections.windows(20, DECK, budget=12)
    assert got[0].topics == 2   # Introduction + Programming languages
    assert got[1].topics == 2   # Conditional Statements + Loops


# --------------------------------------------------------------------------
# titles
# --------------------------------------------------------------------------

def test_the_exporters_slide_numbering_is_stripped():
    assert sections.clean_title("Diapositive 12 Programming languages") == (
        "Programming languages"
    )
    assert sections.clean_title("Slide 3  OUTLINE ") == "OUTLINE"
    assert sections.clean_title("Chapter 1: Programming Fundamentals") == (
        "Chapter 1: Programming Fundamentals"
    )


def test_runs_groups_equal_titles():
    assert sections.runs(["a", "a", "b", "c", "c", "c"]) == [
        (0, 1, "a"),
        (2, 2, "b"),
        (3, 5, "c"),
    ]


def test_page_titles_reads_a_bookmark_table(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / "deck.pdf"
    document = pymupdf.open()
    for _ in range(6):
        document.new_page()
    document.set_toc(
        [
            [1, "Diapositive 1 Introduction", 1],
            [1, "Diapositive 2 Introduction", 2],
            [1, "Diapositive 3 Arrays", 3],
            [1, "Diapositive 4 Arrays", 4],
            [1, "Diapositive 5 Arrays", 5],
            [1, "Diapositive 6 Sorting", 6],
        ]
    )
    document.save(path)
    document.close()

    assert sections.page_titles(path) == [
        "Introduction",
        "Introduction",
        "Arrays",
        "Arrays",
        "Arrays",
        "Sorting",
    ]


def test_a_pdf_with_no_bookmarks_has_no_titles(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / "plain.pdf"
    document = pymupdf.open()
    for _ in range(4):
        document.new_page()
    document.save(path)
    document.close()
    assert sections.page_titles(path) is None


def test_a_single_bookmark_is_not_structure(tmp_path):
    """One entry offers no boundary, so it must read as untitled rather than
    as a document whose every page belongs to one topic."""
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / "one.pdf"
    document = pymupdf.open()
    for _ in range(4):
        document.new_page()
    document.set_toc([[1, "Chapter 1", 1]])
    document.save(path)
    document.close()
    assert sections.page_titles(path) is None


def test_an_unreadable_pdf_is_untitled_rather_than_fatal(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf at all")
    assert sections.page_titles(path) is None


def test_pages_before_the_first_bookmark_carry_no_title(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / "late.pdf"
    document = pymupdf.open()
    for _ in range(4):
        document.new_page()
    document.set_toc([[1, "Body", 3], [1, "End", 4]])
    document.save(path)
    document.close()
    assert sections.page_titles(path) == ["", "", "Body", "End"]


# --------------------------------------------------------------------------
# anchors -- how a position survives a re-fetch
# --------------------------------------------------------------------------

PAGE = (
    "A binary search tree keeps its keys ordered so that a lookup can discard "
    "half of the remaining subtree at every step."
)


def test_an_anchor_is_the_content_and_not_the_position():
    """The whole point: the same page hashes the same wherever it moves to."""
    assert sections.anchor(PAGE) == sections.anchor(PAGE)
    assert sections.anchor(PAGE) != sections.anchor(PAGE + " And rotations rebalance it.")


def test_whitespace_does_not_change_an_anchor():
    """A re-export reflows the text layer without changing a word of the slide."""
    assert sections.anchor(PAGE) == sections.anchor(PAGE.replace(" ", "\n  "))


def test_a_page_with_nothing_on_it_anchors_to_none():
    """Not to the hash of an empty string, which every blank page would share
    -- an ambiguous anchor is worse than an absent one, because it resolves."""
    assert sections.anchor("") is None
    assert sections.anchor("   \n\n  ") is None
    assert sections.anchor("Page 4") is None


def test_a_scan_anchors_on_its_transcription():
    """A scan's own text layer is empty; its content lives in ocr_pages."""
    assert sections.anchor("", PAGE) is not None
    assert sections.anchor("3", PAGE) == sections.anchor("3", PAGE)
    assert sections.anchor("", PAGE) != sections.anchor("", PAGE + " Rotations.")


def test_anchors_are_distinct_across_a_document():
    """Measured on the real library at 1093 of 1094 pages distinct. Here the
    property is pinned rather than the number."""
    pages = [f"{PAGE} This is slide {index} of the deck." for index in range(60)]
    anchors = [sections.anchor(page) for page in pages]
    assert all(anchors)
    assert len(set(anchors)) == len(anchors)


# --------------------------------------------------------------------------
# one attachment, assembled from the rows the quiz already reads
# --------------------------------------------------------------------------

def _row(**overrides):
    """A study_item_sources row, as sqlite would hand one back."""
    values = {
        "drive_id": "d1",
        "file_title": "Chapter 1.pdf",
        "text_path": "text/d1.txt",
        "local_path": "files/d1.pdf",
        "scan_pages": 0,
    }
    values.update(overrides)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    columns = ", ".join(f"? AS {name}" for name in values)
    row = connection.execute(f"SELECT {columns}", list(values.values())).fetchone()
    connection.close()
    return row


def _library(tmp_path, pages, *, name="d1"):
    (tmp_path / "text").mkdir(exist_ok=True)
    (tmp_path / "text" / f"{name}.txt").write_text("\f".join(pages), encoding="utf-8")
    return tmp_path


def test_read_document_cuts_the_stored_text(tmp_path):
    pages = [f"{PAGE} Slide {index}." for index in range(50)]
    library = _library(tmp_path, pages)
    document = sections.read_document(_row(), library, {}, budget=20)

    assert document is not None
    assert document.pages == 50
    assert len(document.windows) == 3
    assert document.titled is False       # no PDF beside it, so no bookmarks
    assert document.anchored == 50


def test_read_document_reports_text_that_is_not_on_disk(tmp_path):
    """The row says there is text and the disk disagrees. None, so the caller
    says which file -- never an empty document that reads as a short one."""
    assert sections.read_document(_row(), tmp_path, {}, budget=20) is None


def test_untranscribed_pages_land_in_the_window_that_holds_them(tmp_path):
    """Readiness per window is the point: today one untranscribed page blocks a
    whole 155-page item, which blocks it forever."""
    pages = [f"{PAGE} Slide {index}." for index in range(50)]
    library = _library(tmp_path, pages)
    ocr = {
        4: {"status": "ok", "text": "read"},
        44: {"status": "pending", "text": None},
    }
    document = sections.read_document(_row(scan_pages=2), library, ocr, budget=20)

    assert [window.unread for window in document.windows] == [0, 0, 1]
    assert [window.ready for window in document.windows] == [True, True, False]
    # OCR has run over this file, so the scan pages are located and there is
    # nothing left unaccounted for.
    assert document.untracked_scans == 0


def test_a_file_ocr_has_never_touched_says_its_scans_are_unlocated(tmp_path):
    """Which pages are images is then known only in aggregate, exactly as
    packs.render_pages says. Every window reads as ready and none of them can
    be trusted, so the document has to carry the warning."""
    library = _library(tmp_path, [f"{PAGE} Slide {index}." for index in range(30)])
    document = sections.read_document(_row(scan_pages=7), library, {}, budget=20)
    assert document.untracked_scans == 7
    assert all(window.unread == 0 for window in document.windows)


def test_bookmarks_are_used_when_the_pdf_is_beside_the_text(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    (tmp_path / "files").mkdir()
    pdf = tmp_path / "files" / "d1.pdf"
    document = pymupdf.open()
    for _ in range(20):
        document.new_page()
    document.set_toc(
        [[1, f"Diapositive {index + 1} {title}", index + 1]
         for index, title in enumerate(DECK)]
    )
    document.save(pdf)
    document.close()

    library = _library(tmp_path, [f"{PAGE} Slide {index}." for index in range(20)])
    got = sections.read_document(_row(), library, {}, budget=12)

    assert got.titled is True
    assert spans(got.windows) == [(0, 9), (10, 19)]
    assert got.windows[0].title == "Introduction"


def test_a_bookmark_table_of_the_wrong_length_is_dropped(tmp_path):
    """The PDF and the extracted text describe different revisions of the file.
    A boundary in the wrong place is worse than no boundary, because `snapped`
    would claim the document asked for it."""
    pymupdf = pytest.importorskip("pymupdf")
    (tmp_path / "files").mkdir()
    pdf = tmp_path / "files" / "d1.pdf"
    document = pymupdf.open()
    for _ in range(8):
        document.new_page()
    document.set_toc([[1, f"Diapositive {n + 1} Topic {n // 2}", n + 1] for n in range(8)])
    document.save(pdf)
    document.close()

    # Text extracted from an older 20-page revision.
    library = _library(tmp_path, [f"{PAGE} Slide {index}." for index in range(20)])
    got = sections.read_document(_row(), library, {}, budget=12)

    assert got.titled is False
    assert all(not window.snapped for window in got.windows)


def test_a_topic_longer_than_the_budget_marks_its_continuation():
    """A 24-slide topic spans two windows. Both carry its title, so the second
    has to say it is a continuation or the pair reads as a duplicate row."""
    titles = ["Basic instructions"] * 30 + ["Loops"] * 10
    got = sections.windows(40, titles, budget=12)
    assert [window.continues for window in got] == [False, True, True, False]


def test_a_window_opening_a_new_topic_never_continues():
    got = sections.windows(20, DECK, budget=12)
    assert [window.continues for window in got] == [False, False]


def test_without_titles_nothing_continues():
    assert all(not window.continues for window in sections.windows(50, budget=20))
