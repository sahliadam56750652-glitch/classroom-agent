"""Serving documents: ranges, conditional GET, path safety, and the cursor.

The two that matter most are the ones a silent failure would hide.

A **range** that quietly falls back to 200 sends a 92-page deck whole and the only
symptom is "the reader feels slow" -- a misreport, not an error. Starlette handles
ranges natively and a test pins that, so a version which stops fails loudly.

A **lost anchor** that quietly becomes page one is the same failure one layer up:
the app would show me the wrong material and look exactly like it was working. So
`changed: true` with a sentence, never a silent reset.
"""

from __future__ import annotations

import pytest
from api_fixtures import COURSE, MANUAL, client, make_config, seed

from agent.classroom.models import parse_coursework_material, parse_materials
from agent.db import store
from agent.gate import sections

# 40 x 256 = 10,240 bytes, so a 1 KiB range is comfortably inside it.
PDF_BYTES = bytes(range(256)) * 40

PAGE_ONE = "Introduction to C++ and the compilation model, in some detail."
PAGE_TWO = "Iteration statements: while, do-while and the three-part for loop."
PAGE_THREE = "Subprograms, parameter passing by value and by reference here."


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    return made


def add_document(
    config,
    *,
    drive_id="d1",
    title="Chapter 1",
    pages=(PAGE_ONE, PAGE_TWO, PAGE_THREE),
    status="ok",
    md5="abc123",
    course=COURSE,
    parent="p1",
):
    """One fetched, extracted attachment, with bytes and text on disk."""
    library = config.library_dir
    (library / "files").mkdir(parents=True, exist_ok=True)
    (library / "text").mkdir(parents=True, exist_ok=True)
    (library / "files" / f"{drive_id}.pdf").write_bytes(PDF_BYTES)
    text = sections.PAGE_BREAK.join(pages)
    (library / "text" / f"{drive_id}.txt").write_text(text, encoding="utf-8")

    conn = store.connect(config.db_path)
    # Through the real parsers rather than raw INSERTs. An INSERT OR IGNORE that
    # violates a constraint is silently a no-op, and a fixture that quietly wrote
    # nothing would make these tests pass for reasons unrelated to the API.
    material, _ = parse_coursework_material(
        {
            "id": parent,
            "title": title,
            "creationTime": "2026-09-01T09:00:00Z",
            "updateTime": "2026-09-01T09:00:00Z",
            "materials": [
                {"driveFile": {"driveFile": {
                    "id": drive_id, "title": title,
                    "alternateLink": "https://drive/x",
                }}}
            ],
        },
        course,
    )
    store.upsert_coursework_material(conn, material)
    store.upsert_materials(
        conn,
        parse_materials("coursework_material", parent, course, [
            {"driveFile": {"driveFile": {
                "id": drive_id, "title": title,
                "alternateLink": "https://drive/x",
            }}}
        ]),
    )
    store.upsert_extraction(
        conn,
        drive_id,
        status=status,
        local_path=f"files/{drive_id}.pdf",
        text_path=f"text/{drive_id}.txt",
        mime_type="application/pdf",
        size_bytes=len(PDF_BYTES),
        pages=len(pages),
        chars=len(text),
        md5_checksum=md5,
        method="pdf",
    )
    conn.commit()
    conn.close()
    return drive_id


# ---------------------------------------------------------------------------
# range requests -- the reason the cookie exists
# ---------------------------------------------------------------------------


def test_a_byte_range_returns_206_and_only_those_bytes(config, monkeypatch):
    """The whole point of point 4: a 92-page document is not downloaded whole."""
    add_document(config)
    session = client(config, monkeypatch)
    response = session.get(
        "/api/documents/d1/file", headers={"Range": "bytes=0-1023"}
    )

    assert response.status_code == 206
    assert len(response.content) == 1024
    assert response.headers["content-range"] == f"bytes 0-1023/{len(PDF_BYTES)}"
    assert response.content == PDF_BYTES[:1024]


def test_the_full_document_advertises_that_ranges_work(config, monkeypatch):
    add_document(config)
    session = client(config, monkeypatch)
    response = session.get("/api/documents/d1/file")

    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert len(response.content) == len(PDF_BYTES)


def test_a_range_past_the_end_is_416_not_a_silent_whole_file(config, monkeypatch):
    add_document(config)
    session = client(config, monkeypatch)
    response = session.get(
        "/api/documents/d1/file", headers={"Range": "bytes=99999-100000"}
    )

    assert response.status_code == 416


def test_an_open_ended_range_returns_the_tail(config, monkeypatch):
    add_document(config)
    session = client(config, monkeypatch)
    response = session.get(
        "/api/documents/d1/file", headers={"Range": "bytes=10000-"}
    )

    assert response.status_code == 206
    assert response.content == PDF_BYTES[10000:]


# ---------------------------------------------------------------------------
# conditional GET -- ours, because Starlette does not honour If-None-Match
# ---------------------------------------------------------------------------


def test_a_second_open_with_the_same_etag_is_304(config, monkeypatch):
    """What makes "readable offline" true rather than aspirational."""
    add_document(config)
    session = client(config, monkeypatch)
    first = session.get("/api/documents/d1/file")
    etag = first.headers["etag"]

    second = session.get("/api/documents/d1/file", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag


def test_a_weak_validator_still_matches(config, monkeypatch):
    """A proxy may weaken the tag on the way back. Membership, not equality."""
    add_document(config)
    session = client(config, monkeypatch)
    etag = session.get("/api/documents/d1/file").headers["etag"]

    response = session.get(
        "/api/documents/d1/file", headers={"If-None-Match": f"W/{etag}"}
    )
    assert response.status_code == 304


def test_a_different_etag_gets_the_body(config, monkeypatch):
    add_document(config)
    session = client(config, monkeypatch)
    response = session.get(
        "/api/documents/d1/file", headers={"If-None-Match": '"something-else"'}
    )

    assert response.status_code == 200
    assert len(response.content) == len(PDF_BYTES)


def test_the_etag_is_drives_checksum_where_one_was_recorded(config, monkeypatch):
    """Content, not mtime -- so a re-fetch of unchanged bytes costs nothing."""
    add_document(config, md5="deadbeef")
    session = client(config, monkeypatch)
    assert session.get("/api/documents/d1/file").headers["etag"] == '"deadbeef"'


def test_a_document_is_cached_privately_only(config, monkeypatch):
    """One user. No shared cache should ever hold a lecture of mine."""
    add_document(config)
    session = client(config, monkeypatch)
    assert "private" in session.get("/api/documents/d1/file").headers["cache-control"]


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


def test_a_document_arrives_under_its_drive_title(config, monkeypatch):
    """The same rule as Telegram delivery, from the same function."""
    add_document(config, title="Chapitre 1 — Les bases")
    session = client(config, monkeypatch)
    disposition = session.get("/api/documents/d1/file").headers["content-disposition"]

    assert disposition.startswith("inline")
    assert "filename*=UTF-8''" in disposition
    assert "Chapitre" in disposition


def test_a_title_with_no_extension_gains_the_real_one(config, monkeypatch):
    """A Google-native doc has no extension in Drive and is a PDF on disk."""
    add_document(config, title="Chapter 1")
    session = client(config, monkeypatch)
    body = session.get("/api/documents/d1").json()

    assert body["filename"] == "Chapter 1.pdf"


# ---------------------------------------------------------------------------
# the four kinds of absent
# ---------------------------------------------------------------------------


def test_an_unknown_id_says_it_was_never_fetched(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.get("/api/documents/nope/file")

    assert response.status_code == 404
    assert "never have been fetched" in response.text


def test_a_trashed_file_says_so_rather_than_just_missing(config, monkeypatch):
    """Permanent loss, not a transient failure. The remedy is asking the teacher."""
    add_document(config, status="trashed")
    session = client(config, monkeypatch)
    response = session.get("/api/documents/d1/file")

    assert response.status_code == 404
    assert "trashed" in response.text
    assert "asking the teacher" in response.text


def test_a_row_whose_file_is_gone_says_the_two_disagree(config, monkeypatch):
    """Distinguishable from "never fetched", which is the whole recurring lesson."""
    add_document(config)
    (config.library_dir / "files" / "d1.pdf").unlink()
    session = client(config, monkeypatch)
    response = session.get("/api/documents/d1/file")

    assert response.status_code == 404
    assert "disagree" in response.text


def test_a_path_escaping_the_library_is_a_500_not_a_404(config, monkeypatch):
    """A corrupt row must not hide behind the twenty attachments genuinely gone."""
    add_document(config)
    conn = store.connect(config.db_path)
    conn.execute(
        "UPDATE extractions SET local_path = ? WHERE drive_id = 'd1'",
        ("../../../../etc/passwd",),
    )
    conn.commit()
    conn.close()

    session = client(config, monkeypatch, )
    response = session.get("/api/documents/d1/file")
    assert response.status_code == 500
    assert "outside the library" in response.text


def test_the_client_can_never_supply_a_path(config, monkeypatch):
    """It supplies a drive_id; the server looks the path up. There is no route
    that takes a filename, so traversal has nothing to travel through."""
    add_document(config)
    session = client(config, monkeypatch)

    for attempt in ("..%2F..%2Fconfig.yaml", "....//config.yaml"):
        assert session.get(f"/api/documents/{attempt}/file").status_code == 404


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


def test_the_extracted_text_is_readable(config, monkeypatch):
    """DESIGN.md forbids a state the app counts but will not show me, and this is
    the material a quiz is grounded in."""
    add_document(config)
    session = client(config, monkeypatch)
    response = session.get("/api/documents/d1/text")

    assert response.status_code == 200
    assert PAGE_TWO in response.text


def test_one_page_of_text_is_addressable_one_based(config, monkeypatch):
    """1-based because that is what the reader displays; Window is 0-based."""
    add_document(config)
    session = client(config, monkeypatch)

    assert session.get("/api/documents/d1/text", params={"page": 1}).text == PAGE_ONE
    assert session.get("/api/documents/d1/text", params={"page": 3}).text == PAGE_THREE


def test_a_page_past_the_end_says_how_many_there_are(config, monkeypatch):
    add_document(config)
    session = client(config, monkeypatch)
    response = session.get("/api/documents/d1/text", params={"page": 9})

    assert response.status_code == 404
    assert "3 page(s)" in response.text


# ---------------------------------------------------------------------------
# the read position -- anchored by content, never by index alone
# ---------------------------------------------------------------------------


def test_no_position_is_not_an_error(config, monkeypatch):
    add_document(config)
    session = client(config, monkeypatch)
    body = session.get("/api/documents/d1/position").json()

    assert body["page"] is None
    assert body["changed"] is False


def test_a_position_round_trips(config, monkeypatch):
    add_document(config)
    session = client(config, monkeypatch)
    anchor = sections.anchor(PAGE_TWO)

    written = session.put(
        "/api/documents/d1/position",
        json={"page_index": 1, "page_hash": anchor},
    )
    assert written.status_code == 200

    body = session.get("/api/documents/d1/position").json()
    assert body["page"] == 2
    assert body["changed"] is False
    assert body["note"] == ""


def test_a_position_survives_pages_being_inserted_above_it(config, monkeypatch):
    """The whole reason the anchor is a hash. An index would now be wrong material."""
    add_document(config)
    session = client(config, monkeypatch)
    anchor = sections.anchor(PAGE_TWO)
    session.put(
        "/api/documents/d1/position", json={"page_index": 1, "page_hash": anchor}
    )

    # The professor inserts a slide at the front; every page after it moves.
    add_document(config, pages=("A new opening slide with plenty of words on it.",
                                PAGE_ONE, PAGE_TWO, PAGE_THREE))

    body = session.get("/api/documents/d1/position").json()
    assert body["page"] == 3
    assert body["changed"] is False
    assert "moved from 2 to 3" in body["note"]


def test_a_lost_anchor_is_said_out_loud_rather_than_becoming_page_one(
    config, monkeypatch
):
    """A silent fall back is a misreport, which this project ranks as a defect."""
    add_document(config)
    session = client(config, monkeypatch)
    session.put(
        "/api/documents/d1/position",
        json={"page_index": 1, "page_hash": sections.anchor(PAGE_TWO)},
    )

    # Re-uploaded with that page gone entirely.
    add_document(config, pages=(PAGE_ONE, PAGE_THREE))

    body = session.get("/api/documents/d1/position").json()
    assert body["page"] is None
    assert body["changed"] is True
    assert "changed since it was last read" in body["note"]


def test_a_page_too_thin_to_hash_is_honoured_and_flagged(config, monkeypatch):
    """One page in the 1094 measured was genuinely blank. It still gets a position."""
    add_document(config)
    session = client(config, monkeypatch)
    session.put("/api/documents/d1/position", json={"page_index": 2})

    body = session.get("/api/documents/d1/position").json()
    assert body["page"] == 3
    assert body["changed"] is False
    assert "will not survive the document changing" in body["note"]


def test_a_position_for_an_unheld_document_is_refused(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.put(
        "/api/documents/nope/position", json={"page_index": 0}
    )
    assert response.status_code == 404


def test_a_position_is_one_row_per_document_not_a_history(config, monkeypatch):
    add_document(config)
    session = client(config, monkeypatch)
    for index in range(4):
        session.put("/api/documents/d1/position", json={"page_index": index})

    conn = store.connect(config.db_path)
    assert store.count_rows(conn, "read_positions") == 1
    assert store.get_read_position(conn, "d1")["page_index"] == 3
    conn.close()


def test_a_position_is_backed_up_and_a_session_is_not(config, monkeypatch):
    """read_positions is the fourth thing no re-sync can rebuild. api_sessions
    is the one table worth losing."""
    from agent import backup

    tables = {spec.table for spec in backup.SPECS}
    assert "read_positions" in tables
    assert "api_sessions" not in tables


# ---------------------------------------------------------------------------
# the library listing
# ---------------------------------------------------------------------------


def test_the_library_lists_posts_not_attachments(config, monkeypatch):
    """A lecture with slides and a handout is one thing to revise."""
    add_document(config, drive_id="d1", title="Slides", parent="p1")
    add_document(config, drive_id="d2", title="Handout", parent="p1")
    session = client(config, monkeypatch)
    body = session.get("/api/library").json()

    assert len(body) == 1
    assert body[0]["files"] == 2
    assert body[0]["pages"] == 6


def test_the_library_can_be_filtered_and_searched(config, monkeypatch):
    add_document(config, drive_id="d1", title="SQL joins", parent="p1")
    add_document(config, drive_id="d2", title="Normal forms", parent="p2")
    session = client(config, monkeypatch)

    assert len(session.get("/api/library").json()) == 2
    found = session.get("/api/library", params={"q": "joins"}).json()
    assert [post["title"] for post in found] == ["SQL joins"]


def test_a_course_outside_this_semester_is_refused_by_name(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.get("/api/library", params={"course": "999999"})

    assert response.status_code == 404
    assert "neither tracked nor" in response.text


# ---------------------------------------------------------------------------
# packs
# ---------------------------------------------------------------------------


def test_a_built_pack_is_served_for_download(config, monkeypatch):
    """How a pack reaches a notebook now that the server is the host.

    This is what closed the PLAN.md open question in favour of HTTP, and it is
    why no second exception to invariant 6 was ever needed.
    """
    config.packs_dir.mkdir(parents=True, exist_ok=True)
    (config.packs_dir / "database-ga-2026.md").write_text(
        "# Database GA 2026\n\nsome pack\n", encoding="utf-8"
    )
    conn = store.connect(config.db_path)
    store.upsert_pack(
        conn,
        course_id=COURSE,
        path=str(config.packs_dir / "database-ga-2026.md"),
        content_hash="h",
        sources=3,
    )
    conn.commit()
    conn.close()

    session = client(config, monkeypatch)
    response = session.get(f"/api/packs/{COURSE}")

    assert response.status_code == 200
    assert "some pack" in response.text
    assert "study pack" in response.headers["content-disposition"]


def test_an_unbuilt_pack_says_which_command_builds_it(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.get(f"/api/packs/{COURSE}")

    assert response.status_code == 404
    assert "agent packs" in response.text
