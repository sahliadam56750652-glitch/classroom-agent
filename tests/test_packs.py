"""Assembling extracted text into one study document per course.

Nothing here touches the network or a model: packs are built entirely from text
that is already on disk.

The properties under test are the two that decide whether a pack can be trusted
as source material for Phase 3's quiz generation -- text a model produced is
marked as such, and a page nothing has read appears as a placeholder rather than
as an absence that reads like a blank slide.
"""

from __future__ import annotations

import argparse

import pytest

from agent import cli
from agent.classroom.models import Announcement, Course, CourseWorkMaterial, Material
from agent.config import Config
from agent.db import store
from agent.files import extract, packs


@pytest.fixture
def config(tmp_path) -> Config:
    data_dir = tmp_path / "data"
    (data_dir / "library").mkdir(parents=True)
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=data_dir,
        tracked_courses=["c1"],
        ignored_courses=[],
    )


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    store.upsert_course(
        connection,
        Course(
            id="c1", name="Operating Systems", section=None, room=None, owner_id=None,
            course_state="ACTIVE", enrollment_code=None, alternate_link=None,
            creation_time=None, update_time=None, content_hash="h",
        ),
    )
    yield connection
    connection.close()


def a_post(conn, parent_id="p1", title="Chapter 6", created="2026-03-14T09:00:00Z"):
    store.upsert_coursework_material(
        conn,
        CourseWorkMaterial(
            id=parent_id, course_id="c1", title=title, description=None,
            state="PUBLISHED", topic_id=None,
            alternate_link=f"https://classroom.google.com/c/x/m/{parent_id}",
            creation_time=created, update_time=created, content_hash="h",
        ),
    )


def a_source(
    config, conn, drive_id, *, parent_id="p1", text="Native page text.",
    pages=1, scan_pages=0, ocr_pages=0, title="Chapter 6.pdf", extracted_at="2026-03-15T00:00:00Z",
):
    """An extracted attachment, as fetch + extract + ocr would have left it."""
    store.upsert_material(
        conn,
        Material(
            id=f"coursework_material:{parent_id}:driveFile:{drive_id}",
            parent_type="coursework_material", parent_id=parent_id, course_id="c1",
            kind="driveFile", ref=drive_id, drive_id=drive_id, title=title,
            url=f"https://drive.google.com/file/d/{drive_id}/view", content_hash="h",
        ),
    )
    path = config.library_dir / extract.TEXT_SUBDIR / f"{drive_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    store.upsert_extraction(
        conn, drive_id, status="ok", mime_type="application/pdf",
        local_path=f"files/{drive_id}.pdf",
        text_path=f"{extract.TEXT_SUBDIR}/{drive_id}.txt",
        method="pymupdf", pages=pages, scan_pages=scan_pages, ocr_pages=ocr_pages,
        chars=len(text), extracted_at=extracted_at,
    )
    return path


def args(**overrides):
    base = {"dry_run": False, "courses": None, "all": False, "force": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def pack_text(config, name="operating-systems.md"):
    return (config.packs_dir / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# what a pack contains
# --------------------------------------------------------------------------


def test_a_pack_carries_provenance_for_every_document(config, conn):
    """NotebookLM cites its sources, so a citation has to lead back to a lecture."""
    a_post(conn, title="Chapter 6 Memory Management", created="2026-03-14T09:00:00Z")
    a_source(config, conn, "f1", text="Paging and segmentation.")

    packs.build(config, conn)

    text = pack_text(config)
    assert "# Operating Systems" in text
    assert "Chapter 6 Memory Management" in text
    assert "2026-03-14" in text
    assert "https://drive.google.com/file/d/f1/view" in text
    assert "Paging and segmentation." in text


def test_ocr_text_is_marked_with_its_origin(config, conn):
    """Phase 3 quizzes from this text. Where a sentence came from is not a detail."""
    a_post(conn)
    a_source(
        config, conn, "f1",
        text=f"Slide title{extract.PAGE_BREAK}$E = mc^2$ from the whiteboard",
        pages=2, scan_pages=1, ocr_pages=1,
    )
    store.upsert_ocr_page(
        conn, drive_id="f1", page_index=1, page_hash="abc", status="ok",
        text="$E = mc^2$ from the whiteboard", model="gemini:gemini-3.6-flash",
    )

    packs.build(config, conn)

    text = pack_text(config)
    assert "transcribed from an image by gemini:gemini-3.6-flash" in text
    assert "$E = mc^2$ from the whiteboard" in text
    # The native page is NOT marked -- the whole point is telling them apart.
    assert text.index("Slide title") < text.index("transcribed from an image")


def test_a_pending_page_becomes_an_explicit_placeholder(config, conn):
    """A silent gap reads as a page the lecturer left blank. It is not."""
    a_post(conn)
    a_source(
        config, conn, "f1",
        text=f"Readable page{extract.PAGE_BREAK}",
        pages=2, scan_pages=1, ocr_pages=0,
    )
    store.upsert_ocr_page(
        conn, drive_id="f1", page_index=1, page_hash="", status="pending",
        error="run limit reached",
    )

    packs.build(config, conn)

    text = pack_text(config)
    assert "has not transcribed yet" in text
    assert "page 2" in text


def test_a_file_ocr_has_never_touched_still_declares_its_unread_pages(config, conn):
    """No ocr_pages rows at all: which pages are images is known only in total."""
    a_post(conn)
    a_source(config, conn, "f1", text="Some text", pages=10, scan_pages=4)

    result = packs.build(config, conn)

    assert result.packs[0].unread_pages == 4
    assert "4 not yet transcribed" in pack_text(config)


def test_documents_appear_in_the_order_the_course_was_taught(config, conn):
    a_post(conn, parent_id="late", title="Chapter 9", created="2026-05-01T09:00:00Z")
    a_post(conn, parent_id="early", title="Chapter 1", created="2026-01-05T09:00:00Z")
    a_source(config, conn, "f-late", parent_id="late", text="Later material.")
    a_source(config, conn, "f-early", parent_id="early", text="Earlier material.")

    packs.build(config, conn)

    text = pack_text(config)
    assert text.index("Chapter 1") < text.index("Chapter 9")


def test_an_announcement_without_a_title_is_labelled_from_its_body(config, conn):
    store.upsert_announcement(
        conn,
        Announcement(
            id="a1", course_id="c1",
            text="Reminder: the lab report is due Friday.\nBring your laptop.",
            state="PUBLISHED", alternate_link=None,
            creation_time="2026-02-02T09:00:00Z", update_time=None, content_hash="h",
        ),
    )
    store.upsert_material(
        conn,
        Material(
            id="announcement:a1:driveFile:f1", parent_type="announcement",
            parent_id="a1", course_id="c1", kind="driveFile", ref="f1",
            drive_id="f1", title="brief.pdf", url=None, content_hash="h",
        ),
    )
    path = config.library_dir / extract.TEXT_SUBDIR / "f1.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Lab brief.", encoding="utf-8")
    store.upsert_extraction(
        conn, "f1", status="ok", local_path="files/f1.pdf",
        text_path=f"{extract.TEXT_SUBDIR}/f1.txt", pages=1, chars=10,
    )

    packs.build(config, conn)

    assert "Reminder: the lab report is due Friday." in pack_text(config)


def test_a_deleted_post_is_not_resurrected_in_a_pack(config, conn):
    a_post(conn)
    a_source(config, conn, "f1", text="Withdrawn material.")
    conn.execute("UPDATE coursework_materials SET deleted_at = '2026-04-01T00:00:00Z'")

    result = packs.build(config, conn)

    assert result.packs[0].sources == 0
    assert result.packs[0].reason == "no extracted text yet"


def test_text_missing_from_disk_is_reported_not_dropped(config, conn):
    a_post(conn)
    path = a_source(config, conn, "f1", text="Gone.")
    path.unlink()

    packs.build(config, conn)

    assert "could not be read back" in pack_text(config)


# --------------------------------------------------------------------------
# rebuilding only when the extractions changed
# --------------------------------------------------------------------------


def test_an_unchanged_pack_is_not_rewritten(config, conn):
    """These land in a synced folder. Rewriting them unchanged is a sync storm."""
    a_post(conn)
    a_source(config, conn, "f1", text="Stable text.")
    packs.build(config, conn)
    stamp = (config.packs_dir / "operating-systems.md").stat().st_mtime_ns

    result = packs.build(config, conn)

    assert result.written == 0
    assert result.unchanged == 1
    assert (config.packs_dir / "operating-systems.md").stat().st_mtime_ns == stamp


def test_changed_extracted_text_rebuilds_the_pack(config, conn):
    a_post(conn)
    a_source(config, conn, "f1", text="First version.")
    packs.build(config, conn)

    a_source(config, conn, "f1", text="Second version, longer.",
             extracted_at="2026-06-01T00:00:00Z")
    result = packs.build(config, conn)

    assert result.written == 1
    assert "Second version, longer." in pack_text(config)


def test_a_new_transcription_rebuilds_the_pack(config, conn):
    """OCR landing is exactly the case a chars-only fingerprint would miss."""
    a_post(conn)
    a_source(config, conn, "f1", text="Stable text.", pages=2, scan_pages=1)
    packs.build(config, conn)

    a_source(config, conn, "f1", text="Stable text.", pages=2, scan_pages=1, ocr_pages=1)
    result = packs.build(config, conn)

    assert result.written == 1


def test_a_new_document_rebuilds_the_pack(config, conn):
    a_post(conn)
    a_source(config, conn, "f1", text="One.")
    packs.build(config, conn)

    a_post(conn, parent_id="p2", title="Chapter 7")
    a_source(config, conn, "f2", parent_id="p2", text="Two.")
    result = packs.build(config, conn)

    assert result.written == 1
    assert "Chapter 7" in pack_text(config)


def test_force_rewrites_an_unchanged_pack(config, conn):
    a_post(conn)
    a_source(config, conn, "f1", text="Stable.")
    packs.build(config, conn)

    assert packs.build(config, conn, force=True).written == 1


def test_a_deleted_pack_file_is_rebuilt(config, conn):
    """The database saying it was written is not evidence the file is still there."""
    a_post(conn)
    a_source(config, conn, "f1", text="Stable.")
    packs.build(config, conn)
    (config.packs_dir / "operating-systems.md").unlink()

    assert packs.build(config, conn).written == 1


def test_regenerating_produces_identical_bytes(config, conn):
    """No timestamp in the body, so 'did this change' is a file comparison."""
    a_post(conn)
    a_source(config, conn, "f1", text="Stable text.")
    packs.build(config, conn)
    first = pack_text(config)

    packs.build(config, conn, force=True)

    assert pack_text(config) == first


# --------------------------------------------------------------------------
# output location and the command
# --------------------------------------------------------------------------


def test_packs_default_under_the_library(config, conn):
    assert config.packs_dir == config.library_dir / "packs"


def test_a_configured_directory_is_used_instead(tmp_path, conn, config):
    """How packs reach NotebookLM: written into a Drive-synced folder."""
    synced = tmp_path / "OneDrive" / "Classroom"
    moved = Config(
        account=config.account, timezone=config.timezone, data_dir=config.data_dir,
        tracked_courses=["c1"], ignored_courses=[], packs_dir_override=synced,
    )
    a_post(conn)
    a_source(moved, conn, "f1", text="Text.")

    packs.build(moved, conn)

    assert (synced / "operating-systems.md").exists()


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Probability & Statistics 2025-26", "probability-statistics-2025-26"),
        ("DSA- pi1A", "dsa-pi1a"),
        ("Fundamentals of AI (2025-2026)", "fundamentals-of-ai-2025-2026"),
        ("Algèbre / Analyse", "algebre-analyse"),
        ("???", "course"),
    ],
)
def test_course_names_become_safe_filenames(name, expected):
    """Real names carry '&', accents and will eventually carry a '/'."""
    assert packs.slugify(name) == expected


def test_dry_run_writes_nothing(config, conn):
    a_post(conn)
    a_source(config, conn, "f1", text="Text.")

    result = packs.build(config, conn, dry_run=True)

    assert result.packs[0].reason == "would be written"
    assert not config.packs_dir.exists()
    assert store.count_rows(conn, "packs") == 0


def test_only_the_named_course_is_built(config, conn):
    store.upsert_course(
        conn,
        Course(
            id="c2", name="Databases", section=None, room=None, owner_id=None,
            course_state="ACTIVE", enrollment_code=None, alternate_link=None,
            creation_time=None, update_time=None, content_hash="h",
        ),
    )
    a_post(conn)
    a_source(config, conn, "f1", text="Text.")

    result = packs.build(config, conn, course_ids=["c2"])

    assert [pack.course_id for pack in result.packs] == ["c2"]


def test_the_command_prints_and_exits_zero(config, conn, monkeypatch, capsys):
    a_post(conn)
    a_source(config, conn, "f1", text="Text.")

    class KeepOpen:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            pass

    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))

    assert cli.cmd_packs(config, args()) == 0
    assert "operating-systems.md" in capsys.readouterr().out
