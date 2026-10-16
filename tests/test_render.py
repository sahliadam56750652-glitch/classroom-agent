"""PowerPoint and Word to PDF, on the PC, for the reader. Phase 5e.

LibreOffice itself is not run here -- it may not be installed, and a test suite
that depends on a 300 MB office suite is a test suite nobody runs. The command
line is checked as text, and the conversion is stood in for by a function that
writes a real PDF with PyMuPDF. What IS checked for real: that a missing
LibreOffice fails loudly and names itself, that nothing is re-converted when
the source has not changed, that a rendering never changes the page text a quiz
or an anchor is built from, and that the API serves it.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from api_fixtures import COURSE, client, make_config, seed

from agent.classroom.models import parse_coursework_material, parse_materials
from agent.db import store
from agent.files import render
from agent.files.extract import DOCX_MIME, PPTX_MIME
from agent.gate import quiz, sections

SLIDES = ("Slide one: relations and keys.", "Slide two: joins.", "Slide three: nulls.")


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    return made


def add_deck(config, *, drive_id="deck", mime=PPTX_MIME, slides=SLIDES, payload=b"PK fake deck",
             title="Chapter 2"):
    library = config.library_dir
    (library / "files").mkdir(parents=True, exist_ok=True)
    (library / "text").mkdir(parents=True, exist_ok=True)
    suffix = ".pptx" if mime == PPTX_MIME else ".docx"
    (library / "files" / f"{drive_id}{suffix}").write_bytes(payload)
    (library / "text" / f"{drive_id}.txt").write_text(sections.PAGE_BREAK.join(slides), encoding="utf-8")
    conn = store.connect(config.db_path)
    attachment = {"driveFile": {"driveFile": {"id": drive_id, "title": title, "alternateLink": "x"}}}
    material, _ = parse_coursework_material(
        {"id": f"p-{drive_id}", "title": "Chapter 2", "creationTime": "2026-10-01T09:00:00Z",
         "updateTime": "2026-10-01T09:00:00Z", "materials": [attachment]},
        COURSE,
    )
    store.upsert_coursework_material(conn, material)
    store.upsert_materials(conn, parse_materials("coursework_material", f"p-{drive_id}", COURSE, [attachment]))
    store.upsert_extraction(
        conn, drive_id, status="ok", local_path=f"files/{drive_id}{suffix}",
        text_path=f"text/{drive_id}.txt", mime_type=mime, size_bytes=len(payload),
        pages=len(slides), chars=sum(len(s) for s in slides), scan_pages=0, ocr_pages=0,
        method="pptx", md5_checksum=f"md5-{drive_id}", extracted_at="2026-10-02T00:00:00Z",
    )
    store.ensure_study_item(conn, course_id=COURSE, entity_type="coursework_material",
                            entity_id=f"p-{drive_id}")
    conn.commit()
    conn.close()


class FakeLibreOffice:
    """Stands in for `render.convert`: writes a real PDF with `pages` pages."""

    def __init__(self, pages=3, fail=()):
        self.pages = pages
        self.fail = set(fail)
        self.calls = []

    def __call__(self, soffice, source, out_dir, profile, mime_type):
        self.calls.append(source.name)
        if source.stem in self.fail:
            raise RuntimeError("LibreOffice exited 1 without a PDF.")
        out_dir.mkdir(parents=True, exist_ok=True)
        document = pymupdf.open()
        for n in range(self.pages):
            document.new_page().insert_text((50, 72), f"page {n + 1}")
        target = out_dir / f"{source.stem}.pdf"
        document.save(str(target))
        document.close()
        return target


@pytest.fixture
def soffice(monkeypatch, tmp_path):
    binary = tmp_path / "soffice.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(render, "find_soffice", lambda config: binary)
    fake = FakeLibreOffice()
    monkeypatch.setattr(render, "convert", fake)
    return fake


# ---------------------------------------------------------------- finding it


def test_a_missing_libreoffice_fails_loudly_and_names_itself(config, monkeypatch):
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    monkeypatch.setattr(render, "KNOWN_PLACES", ())
    with pytest.raises(render.RenderError) as err:
        render.find_soffice(config)
    said = str(err.value)
    assert "LibreOffice is not installed" in said
    assert "soffice" in said and "render.soffice" in said


def test_a_wrong_configured_path_says_so(config):
    import dataclasses

    wrong = dataclasses.replace(config, render_soffice=str(config.data_dir / "nowhere" / "soffice.exe"))
    with pytest.raises(render.RenderError) as err:
        render.find_soffice(wrong)
    assert "render.soffice in config.yaml points at" in str(err.value)


def test_nothing_is_written_when_libreoffice_is_missing(config, monkeypatch):
    add_deck(config)
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    monkeypatch.setattr(render, "KNOWN_PLACES", ())
    conn = store.connect(config.db_path)
    with pytest.raises(render.RenderError):
        render.run(conn, config, [COURSE])
    assert store.get_rendering(conn, "deck") is None


def test_the_command_exports_hidden_slides_and_keeps_its_profile_under_data_dir(config):
    profile = config.data_dir / "tmp" / "libreoffice-profile"
    line = render.command(Path("soffice"), Path("deck.pptx"), Path("out"), profile, PPTX_MIME)
    assert "--headless" in line and "--norestore" in line
    assert any("ExportHiddenSlides" in part for part in line)
    assert any(part.startswith("-env:UserInstallation=file:") for part in line)
    assert profile.resolve().as_uri() in " ".join(line)
    docx = render.command(Path("soffice"), Path("a.docx"), Path("out"), profile, DOCX_MIME)
    assert "pdf:writer_pdf_Export" in docx


# ---------------------------------------------------------------- converting


def test_a_deck_is_converted_and_recorded(config, soffice):
    add_deck(config)
    conn = store.connect(config.db_path)
    result = render.run(conn, config, [COURSE])
    assert result.converted == ["deck"]
    row = store.get_rendering(conn, "deck")
    assert row["pdf_path"] == "pdf/deck.pdf" and row["pages"] == 3 and row["error"] is None
    assert (config.library_dir / "pdf" / "deck.pdf").is_file()


def test_an_unchanged_source_is_never_converted_twice(config, soffice):
    add_deck(config)
    conn = store.connect(config.db_path)
    render.run(conn, config, [COURSE])
    again = render.run(conn, config, [COURSE])
    assert again.converted == [] and again.unchanged == 1
    assert soffice.calls == ["deck.pptx"]


def test_a_changed_source_is_converted_again(config, soffice):
    add_deck(config)
    conn = store.connect(config.db_path)
    render.run(conn, config, [COURSE])
    (config.library_dir / "files" / "deck.pptx").write_bytes(b"PK a new version")
    assert render.run(conn, config, [COURSE]).converted == ["deck"]


def test_a_failed_conversion_is_recorded_with_why_and_the_rest_continue(config, soffice):
    add_deck(config, drive_id="bad")
    add_deck(config, drive_id="good")
    soffice.fail = {"bad"}
    conn = store.connect(config.db_path)
    result = render.run(conn, config, [COURSE])
    assert result.converted == ["good"]
    assert result.failed and result.failed[0][0] == "bad"
    assert "without a PDF" in store.get_rendering(conn, "bad")["error"]


def test_a_page_count_that_differs_is_reported(config, soffice):
    add_deck(config)
    soffice.pages = 4
    conn = store.connect(config.db_path)
    assert render.run(conn, config, [COURSE]).mismatched == [("deck", 3, 4)]


def test_a_dry_run_plans_and_writes_nothing(config, soffice):
    add_deck(config)
    conn = store.connect(config.db_path)
    result = render.run(conn, config, [COURSE], dry_run=True)
    assert result.planned == ["deck"]
    assert soffice.calls == [] and store.get_rendering(conn, "deck") is None


def test_the_report_names_each_file_and_what_it_weighs(config, soffice, capsys):
    """By title, never by Drive id; before and after; and a total over every
    file that has a PDF now -- which is what the phone downloads."""
    from agent import cli

    add_deck(config, drive_id="d1", title="Chapter 2 - Joins.pptx", payload=b"P" * 2 * 1024 * 1024)
    add_deck(config, drive_id="d2", title="TD 3.docx", mime=DOCX_MIME, payload=b"P" * 1024 * 1024)
    conn = store.connect(config.db_path)
    cli._print_rendered(render.run(conn, config, [COURSE]))
    said = capsys.readouterr().out
    assert "Chapter 2 - Joins.pptx  2.0 MB -> " in said
    assert "TD 3.docx  1.0 MB -> " in said
    assert "d1" not in said and "d2" not in said
    assert "total: 2 file(s), 3.0 MB as PowerPoint and Word," in said

    # A second run converts nothing and still totals what is held.
    cli._print_rendered(render.run(conn, config, [COURSE]), every=True)
    again = capsys.readouterr().out
    assert "2 already converted and unchanged" in again
    # `agent render` still names each file when nothing changed.
    assert "TD 3.docx  1.0 MB -> " in again
    assert "total: 2 file(s), 3.0 MB as PowerPoint and Word," in again


def test_a_dry_run_names_what_it_would_convert(config, soffice, capsys):
    from agent import cli

    add_deck(config, drive_id="d1", title="Chapter 2 - Joins.pptx", payload=b"P" * 1024 * 1024)
    conn = store.connect(config.db_path)
    cli._print_rendered(render.run(conn, config, [COURSE], dry_run=True))
    said = capsys.readouterr().out
    assert "Chapter 2 - Joins.pptx  (1.0 MB)" in said
    assert "total: 1.0 MB to convert" in said


def test_only_the_courses_asked_for_are_converted(config, soffice):
    add_deck(config)
    conn = store.connect(config.db_path)
    assert render.run(conn, config, ["some-other-course"]).converted == []


# ---------------------------------------------------- one source of page text


def test_a_rendering_never_changes_what_a_quiz_or_an_anchor_is_built_from(config, soffice):
    """The extraction stays the one source of page text. Converting a deck must
    not move the quiz cache key or the extraction row it is computed from."""
    add_deck(config)
    conn = store.connect(config.db_path)
    rows = store.study_item_sources(conn, "coursework_material", "p-deck")
    before = quiz.fingerprint_of(rows, 6)
    extraction_before = dict(store.get_extraction(conn, "deck"))
    render.run(conn, config, [COURSE])
    rows = store.study_item_sources(conn, "coursework_material", "p-deck")
    assert quiz.fingerprint_of(rows, 6) == before
    assert dict(store.get_extraction(conn, "deck")) == extraction_before


# ---------------------------------------------------------------- the API


def test_an_unconverted_deck_is_not_readable_and_says_why(config, monkeypatch):
    add_deck(config)
    session = client(config, monkeypatch)
    doc = session.get("/api/documents/deck").json()
    assert doc["readable"] is False and doc["view"] is None
    assert "waiting to be converted" in doc["unreadable_because"]
    post = session.get("/api/library").json()[0]
    assert post["documents"][0]["readable"] is False


def test_a_converted_deck_is_read_as_its_pdf(config, monkeypatch, soffice):
    add_deck(config)
    conn = store.connect(config.db_path)
    render.run(conn, config, [COURSE])
    conn.close()
    session = client(config, monkeypatch)
    doc = session.get("/api/documents/deck").json()
    assert doc["readable"] is True and doc["view"] == "pdf"
    assert doc["pdf_url"] == "/api/documents/deck/pdf"
    assert doc["pdf_key"].startswith("sha256:")
    served = session.get(doc["pdf_url"])
    assert served.status_code == 200 and served.content.startswith(b"%PDF")
    ranged = session.get(doc["pdf_url"], headers={"Range": "bytes=0-7"})
    assert ranged.status_code == 206
    assert len(ranged.content) == 8 and ranged.content.startswith(b"%PDF")
    # The library says the same thing as the document route.
    assert session.get("/api/library").json()[0]["documents"][0]["pdf_url"] == doc["pdf_url"]


def test_a_failed_deck_says_why_it_cannot_be_read(config, monkeypatch, soffice):
    add_deck(config)
    soffice.fail = {"deck"}
    conn = store.connect(config.db_path)
    render.run(conn, config, [COURSE])
    conn.close()
    doc = client(config, monkeypatch).get("/api/documents/deck").json()
    assert doc["readable"] is False
    assert "could not be converted" in doc["unreadable_because"]


def test_a_mismatched_rendering_keeps_positions_by_page_number_only(config, monkeypatch, soffice):
    add_deck(config)
    soffice.pages = 5
    conn = store.connect(config.db_path)
    render.run(conn, config, [COURSE])
    conn.close()
    session = client(config, monkeypatch)
    saved = session.put("/api/documents/deck/position", json={"page_index": 1}).json()
    assert saved["page_hash"] is None
    assert session.get("/api/documents/deck/position").json()["page"] == 2


def test_the_pdf_route_explains_a_missing_rendering(config, monkeypatch):
    add_deck(config)
    response = client(config, monkeypatch).get("/api/documents/deck/pdf")
    assert response.status_code == 404
    assert "LibreOffice" in response.json()["detail"]
