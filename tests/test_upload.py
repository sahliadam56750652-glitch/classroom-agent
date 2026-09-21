"""`agent upload` -- a file I hand the project myself.

The claim this module makes is about code that is already written: **download
is the only stage an upload skips.** So the test that matters is not a unit
test of `commit`, it is the whole path -- upload a photograph, extract it, find
it at the head of the OCR queue ahead of last year's archive, transcribe it,
make a study item, and generate a quiz from it. That is Phase 6.2's stated
"done when", and it is `test_a_photographed_board_reaches_the_quiz` below.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agent import cli, manual, scope
from agent.classroom.models import parse_course
from agent.config import Config
from agent.db import store
from agent.files import extract, ocr, upload as upload_mod
from agent.gate import quiz, timetable as tt

MANUAL = "manual-calculus-iii"

TIMETABLE = """
subjects:
  UNIX: "809686876412"
  Calculus III: manual-calculus-iii
  Philosophy: null

versions:
  - label: S1
    status: provisional
    effective_from: 2026-09-15
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC, subject: UNIX }
      - { day: tue, start: "08:30", end: "10:00", kind: LEC,
          subject: Calculus III }
      - { day: wed, start: "08:30", end: "10:00", kind: LEC, subject: Philosophy }
"""


def a_jpeg(path: Path) -> Path:
    """Enough of a JPEG that the sniffer accepts it. Nothing renders it."""
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9")
    return path


def a_png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return path


def a_pdf(path: Path, pages: int = 1, text: str = "Cauchy sequences converge.") -> Path:
    """A real, minimal, native-text PDF. PyMuPDF has to be able to read it."""
    import pymupdf

    document = pymupdf.open()
    for number in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {number + 1}. {text}")
    document.save(path)
    document.close()
    return path


class KeepOpen:
    def __init__(self, connection):
        self._conn = connection

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


@pytest.fixture
def config(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "library").mkdir(parents=True)
    path = tmp_path / "timetable.yaml"
    path.write_text(TIMETABLE, encoding="utf-8")
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=data_dir,
        tracked_courses=["809686876412"],
        ignored_courses=[],
        timetable_path_override=path,
    )


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    store.upsert_course(
        connection, parse_course({"id": "809686876412", "name": "UNIX programming"})
    )
    store.upsert_course(connection, parse_course({"id": "old", "name": "Last year"}))
    store.ensure_manual_course(connection, MANUAL, "Calculus III")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def table(config):
    return tt.load(config.timetable_path)


def upload_args(file, subject, **kwargs):
    return argparse.Namespace(
        file=Path(file), subject=subject,
        title=kwargs.get("title"), posted=kwargs.get("posted"),
        dry_run=kwargs.get("dry_run", False),
    )


def run_upload(config, conn, monkeypatch, file, subject, **kwargs):
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    return cli.cmd_upload(config, upload_args(file, subject, **kwargs))


# --------------------------------------------------------------------------
# resolving the subject
# --------------------------------------------------------------------------

def test_the_subject_is_matched_exactly_and_never_approximately(table):
    with pytest.raises(upload_mod.UploadError) as err:
        upload_mod.resolve_subject(table, "Calculus")
    assert "never matched approximately" in str(err.value)
    assert "Known subjects" in str(err.value)


def test_case_is_not_an_approximation(table):
    assert upload_mod.resolve_subject(table, "calculus iii") == ("Calculus III", MANUAL)


def test_a_subject_awaiting_a_course_id_says_which_command_fixes_it(table):
    with pytest.raises(upload_mod.UploadError) as err:
        upload_mod.resolve_subject(table, "Philosophy")
    message = str(err.value)
    assert "no course id yet" in message
    assert "agent subjects --add" in message
    assert "agent courses" in message


# --------------------------------------------------------------------------
# believing the bytes, not the name
# --------------------------------------------------------------------------

def test_a_png_that_is_really_a_jpeg_is_refused_by_name(tmp_path):
    """Stored and left to `extract`, this surfaces as 'unsupported' three
    stages later, about a mime type nobody wrote down."""
    path = tmp_path / "board.png"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    with pytest.raises(upload_mod.UploadError) as err:
        upload_mod.read_payload(path)
    assert "named .png but its bytes are image/jpeg" in str(err.value)
    assert "Renaming a file does not convert it" in str(err.value)


def test_an_iphone_photo_is_named_rather_than_merely_rejected(tmp_path):
    path = tmp_path / "IMG_0001.heic"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32)
    with pytest.raises(upload_mod.UploadError) as err:
        upload_mod.read_payload(path)
    assert "HEIC" in str(err.value)
    assert ".jpg" in str(err.value)


def test_a_docx_is_refused_with_what_is_accepted(tmp_path):
    path = tmp_path / "notes.docx"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    with pytest.raises(upload_mod.UploadError, match="accepts"):
        upload_mod.read_payload(path)


def test_an_empty_file_is_refused(tmp_path):
    (tmp_path / "board.jpg").write_bytes(b"")
    with pytest.raises(upload_mod.UploadError, match="is empty"):
        upload_mod.read_payload(tmp_path / "board.jpg")


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(upload_mod.UploadError, match="no such file"):
        upload_mod.read_payload(tmp_path / "nope.pdf")


@pytest.mark.parametrize(
    "builder,suffix,mime",
    [(a_jpeg, ".jpg", "image/jpeg"), (a_png, ".png", "image/png"), (a_pdf, ".pdf", "application/pdf")],
)
def test_every_accepted_format_round_trips(tmp_path, builder, suffix, mime):
    payload, found = upload_mod.read_payload(builder(tmp_path / f"f{suffix}"))
    assert found == mime
    assert payload


# --------------------------------------------------------------------------
# what it writes
# --------------------------------------------------------------------------

def test_an_upload_lands_exactly_where_fetch_would_have_left_it(
    config, conn, monkeypatch, tmp_path, capsys
):
    """`fetched` means "the bytes are on disk and nothing has read them", and
    that is the whole of why the rest of the pipeline needs no change."""
    assert run_upload(config, conn, monkeypatch, a_jpeg(tmp_path / "board.jpg"),
                      "Calculus III") == 0

    row = conn.execute("SELECT * FROM extractions").fetchone()
    assert row["status"] == "fetched"
    assert manual.is_manual(row["drive_id"])
    assert row["mime_type"] == "image/jpeg"
    assert row["text_path"] is None
    assert (config.library_dir / row["local_path"]).is_file()
    # Relative and forward-slashed, per invariant 5.
    assert "\\" not in row["local_path"]
    assert not Path(row["local_path"]).is_absolute()


def test_the_post_and_the_attachment_are_shaped_like_classroom_ones(
    config, conn, monkeypatch, tmp_path
):
    run_upload(config, conn, monkeypatch, a_jpeg(tmp_path / "board.jpg"),
               "Calculus III", title="Board, 21 Sep", posted="2026-09-21")

    post = conn.execute("SELECT * FROM coursework_materials").fetchone()
    assert post["title"] == "Board, 21 Sep"
    assert post["course_id"] == MANUAL
    assert post["creation_time"].startswith("2026-09-21")

    attachment = conn.execute("SELECT * FROM materials").fetchone()
    assert attachment["parent_type"] == "coursework_material"
    assert attachment["parent_id"] == post["id"]
    assert attachment["kind"] == "driveFile"


def test_uploading_the_same_bytes_twice_writes_nothing_and_says_so(
    config, conn, monkeypatch, tmp_path, capsys
):
    """Identity is content, so a re-upload is the same file -- not a conflict."""
    board = a_jpeg(tmp_path / "board.jpg")
    run_upload(config, conn, monkeypatch, board, "Calculus III")
    capsys.readouterr()

    copy = a_jpeg(tmp_path / "board-again.jpg")
    assert run_upload(config, conn, monkeypatch, copy, "Calculus III") == 0
    assert "already in the library" in capsys.readouterr().out
    assert conn.execute("SELECT count(*) AS n FROM extractions").fetchone()["n"] == 1


def test_dry_run_writes_no_file_and_no_row(config, conn, monkeypatch, tmp_path, capsys):
    assert run_upload(config, conn, monkeypatch, a_jpeg(tmp_path / "board.jpg"),
                      "Calculus III", dry_run=True) == 0
    assert "dry run -- nothing written" in capsys.readouterr().out
    assert conn.execute("SELECT count(*) AS n FROM extractions").fetchone()["n"] == 0
    assert not (config.library_dir / "files").exists()


def test_dry_run_still_performs_every_refusal(config, conn, monkeypatch, tmp_path, capsys):
    """A dry run that takes a different path is a dry run that proves nothing."""
    path = tmp_path / "board.png"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    assert run_upload(config, conn, monkeypatch, path, "Calculus III", dry_run=True) == 1
    assert "Renaming a file does not convert it" in capsys.readouterr().err


# --------------------------------------------------------------------------
# and what a sync does to it: nothing
# --------------------------------------------------------------------------

def test_an_upload_for_a_TRACKED_subject_is_never_offered_to_drive(
    config, conn, monkeypatch, tmp_path
):
    """The dangerous case. A board photographed for UNIX sits in a tracked
    course, so `agent fetch` would 404 on it and rewrite it to 'missing'."""
    run_upload(config, conn, monkeypatch, a_jpeg(tmp_path / "board.jpg"), "UNIX")

    offered = [row["drive_id"] for row in store.drive_references(conn, ["809686876412"])]
    assert offered == []

    # And the reconciler leaves it alone.
    store.soft_delete_missing(conn, "coursework_materials", "809686876412", set())
    store.soft_delete_missing(conn, "materials", "809686876412", set())
    row = conn.execute("SELECT status FROM extractions").fetchone()
    assert row["status"] == "fetched"
    assert conn.execute(
        "SELECT deleted_at FROM coursework_materials"
    ).fetchone()["deleted_at"] is None


# --------------------------------------------------------------------------
# the whole path -- Phase 6.2's stated "done when"
# --------------------------------------------------------------------------

class FakeProvider:
    """Transcribes any page as a fixed sentence. Counts what it was asked."""

    name = "fake:test-model"

    def __init__(self):
        self.calls = 0

    def transcribe_image(self, image, prompt, *, mime_type="image/png"):
        self.calls += 1
        return "The board reads: a Cauchy sequence converges in a complete space."


def test_a_photographed_board_reaches_the_quiz(config, conn, monkeypatch, tmp_path):
    """Upload -> extract -> the head of the OCR queue -> a study item -> a quiz.

    The archive is what it has to beat: last year's course, tracked, with
    material posted more recently than the board is dated. Before the
    tracked/in-scope split it would have won, and the board would have waited a
    fortnight at ~12 pages a day for the OCR the gate needs.
    """
    # Last year's archive: a tracked course with newer-looking material.
    archive = a_pdf(tmp_path / "archive.pdf", pages=2, text="Old material.")
    store.upsert_extraction(
        conn, "1ArchiveDriveId", status="fetched", mime_type="application/pdf",
        local_path="files/1ArchiveDriveId.pdf",
    )
    (config.library_dir / "files").mkdir(parents=True, exist_ok=True)
    (config.library_dir / "files" / "1ArchiveDriveId.pdf").write_bytes(
        archive.read_bytes()
    )
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, content_hash, "
        "first_seen_at, creation_time) VALUES ('arch1', 'old', 'Chapter 9', 'h', "
        "?, '2026-09-30T10:00:00Z')", ("2026-09-30T10:00:00Z",),
    )
    from agent.classroom.models import Material, material_id

    store.upsert_material(conn, Material(
        id=material_id("coursework_material", "arch1", "driveFile", "1ArchiveDriveId"),
        parent_type="coursework_material", parent_id="arch1", course_id="old",
        kind="driveFile", ref="1ArchiveDriveId", drive_id="1ArchiveDriveId",
        title="Chapter 9", url=None, content_hash="h",
    ))
    conn.commit()

    # 1. Upload the board.
    assert run_upload(config, conn, monkeypatch, a_jpeg(tmp_path / "board.jpg"),
                      "Calculus III", title="Board, 21 Sep",
                      posted="2026-09-21") == 0
    board_id = conn.execute(
        "SELECT drive_id FROM extractions WHERE drive_id LIKE 'manual-%'"
    ).fetchone()["drive_id"]

    # 2. Extract. An image is one page that is one scan page -- no text, and
    #    entirely unread, which is exactly what OCR is for.
    extract.extract(config, conn)
    conn.commit()
    row = store.get_extraction(conn, board_id)
    assert row["status"] == "ok"
    assert (row["pages"], row["scan_pages"], row["method"]) == (1, 1, "image")

    # 3. The queue puts it first, ahead of the archive.
    in_scope = sorted(scope.in_scope(tt.load(config.timetable_path)))
    ranked = ocr.queue(
        ocr.pending_candidates(conn), store.ocr_candidate_posts(conn),
        tracked=config.tracked_courses + ["old"], in_scope=in_scope,
    )
    assert [item.drive_id for item in ranked][0] == board_id
    assert ranked[0].why == "in scope this semester"
    assert ranked[0].course_name == "Calculus III"

    # 4. Transcribe it.
    provider = FakeProvider()
    result = ocr.run(config, conn, provider=provider, limit=1, in_scope=in_scope)
    conn.commit()
    assert result.transcribed == 1
    assert provider.calls == 1
    assert store.ocr_pages_for(conn, board_id)[0]["status"] == "ok"

    # 5. It becomes a study item -- through `local`, which is tracked | in scope.
    rows = store.parents_with_extracted_material(
        conn, sorted(scope.local(config, tt.load(config.timetable_path)))
    )
    assert any(row["course_id"] == MANUAL for row in rows)
    for row in rows:
        store.ensure_study_item(
            conn, entity_type=row["entity_type"], entity_id=row["entity_id"],
            course_id=row["course_id"],
        )
    conn.commit()
    item_row = conn.execute(
        "SELECT id FROM study_items WHERE course_id = ?", (MANUAL,)
    ).fetchone()
    assert item_row is not None

    # 6. And a quiz can be generated from it: the transcription is the source.
    row = store.backlog_item(conn, int(item_row["id"]))
    assert row is not None
    sources = quiz.collect(
        conn, config, _as_item(row), questions=config.quiz_question_count
    )
    assert "Cauchy sequence converges" in sources.text
    assert sources.files


def _as_item(row):
    """The backlog row as the gate's Item, without importing the scheduler's
    private helper into the assertion."""
    from agent.gate.scheduler import _item

    return _item(row)
