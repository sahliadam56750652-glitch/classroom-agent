"""A project's links and files (2026-10-16).

Links are http or https, never fetched, titled by their host when untitled.
Files are uploaded, 50 MB at most, stored under a generated name -- the original
filename is display data only -- and served through the authenticated API with
ranges, an ETag and nosniff: PDFs and images inline, everything else as a
download, and the type decided by the bytes, not the name. A library document
of the project's own course can be pinned. Uploaded files are the only copy
anywhere, so the backup carries them, hard-linked to the previous snapshot's
copy when unchanged.
"""

from __future__ import annotations

import os

import pytest
from api_fixtures import COURSE, client, make_config, seed

from agent import backup, entries
from agent.db import store

PDF = b"%PDF-1.4\n" + b"x" * 5000


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    return made


@pytest.fixture
def session(config, monkeypatch):
    signed_in = client(config, monkeypatch)
    made = signed_in.post("/api/projects", json={"subject": "Database", "title": "P", "milestones": []})
    assert made.status_code == 201, made.text
    return signed_in


def upload(session, name, body, project=1):
    return session.post(f"/api/projects/{project}/files", files={"file": (name, body, "application/pdf")})


# ---------------------------------------------------------------- links


def test_a_link_keeps_its_url_and_falls_back_to_the_host(session):
    body = session.post("/api/projects/1/links", json={"url": "https://www.canva.com/design/abc"}).json()
    (link,) = body["attachments"]
    assert link["kind"] == "link" and link["url"] == "https://www.canva.com/design/abc"
    assert link["title"] == "canva.com" and link["host"] == "canva.com"
    named = session.post("/api/projects/1/links", json={"url": "http://example.org/x", "title": "Brief"}).json()
    assert named["attachments"][-1]["title"] == "Brief"
    assert (named["links"], named["files"]) == (2, 0)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://host/x", "file:///etc/passwd", "canva.com", ""])
def test_only_http_and_https_links_are_accepted(session, url):
    response = session.post("/api/projects/1/links", json={"url": url})
    assert response.status_code == 400
    assert session.get("/api/projects/1").json()["attachments"] == []


def test_a_replayed_link_is_added_once(session):
    """The phone's outbox replays with the same Idempotency-Key."""
    for _ in range(2):
        session.post("/api/projects/1/links", json={"url": "https://canva.com/x"},
                     headers={"Idempotency-Key": "op-link"})
    assert session.get("/api/projects/1").json()["links"] == 1


def test_a_link_is_never_fetched():
    """The module that validates a link imports nothing that could fetch one."""
    source = open(entries.__file__, encoding="utf-8").read()
    assert "urllib.request" not in source and "httpx" not in source and "requests" not in source


# ---------------------------------------------------------------- files


def test_an_upload_is_stored_under_a_generated_name(session, config):
    body = upload(session, "../../Brief final.pdf", PDF).json()
    (attached,) = body["attachments"]
    assert attached["title"] == "Brief final.pdf"
    stored = list((config.data_dir / "projects" / "1").iterdir())
    assert len(stored) == 1
    assert "Brief" not in stored[0].name and len(stored[0].name) == 32
    assert stored[0].read_bytes() == PDF
    assert body["files"] == 1


def test_a_pdf_is_served_inline_and_opens_in_the_viewer(session):
    attached = upload(session, "brief.pdf", PDF).json()["attachments"][0]
    assert attached["document"]["view"] == "pdf"
    assert attached["document"]["pdf_url"] == attached["file_url"]
    response = session.get(attached["file_url"])
    assert response.status_code == 200 and response.content == PDF
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_file_named_pdf_that_is_not_one_is_a_download(session):
    """The bytes decide the type: HTML called notes.pdf must never render."""
    attached = upload(session, "notes.pdf", b"<html><script>alert(1)</script></html>").json()["attachments"][0]
    assert attached["document"] is None
    response = session.get(attached["file_url"])
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_anything_else_is_a_download(session):
    attached = upload(session, "network.pkt", b"\x00\x01binary").json()["attachments"][0]
    response = session.get(attached["file_url"])
    assert response.headers["content-disposition"].startswith("attachment;")


def test_ranges_and_a_conditional_get(session):
    url = upload(session, "brief.pdf", PDF).json()["attachments"][0]["file_url"]
    part = session.get(url, headers={"Range": "bytes=0-7"})
    assert part.status_code == 206 and part.content == PDF[:8]
    etag = session.get(url).headers["etag"]
    assert session.get(url, headers={"If-None-Match": etag}).status_code == 304


def test_the_cap_refuses_and_keeps_nothing(session, config, monkeypatch):
    monkeypatch.setattr(entries, "PROJECT_FILE_CAP", 1000)
    response = upload(session, "big.pdf", PDF)
    assert response.status_code == 413
    folder = config.data_dir / "projects" / "1"
    assert not folder.exists() or not any(folder.iterdir())
    assert session.get("/api/projects/1").json()["attachments"] == []


def test_a_file_needs_a_session(config, monkeypatch, session):
    url = upload(session, "brief.pdf", PDF).json()["attachments"][0]["file_url"]
    assert client(config, monkeypatch, sign_in=False).get(url).status_code == 401


def test_removing_keeps_the_bytes(session, config):
    attached = upload(session, "brief.pdf", PDF).json()["attachments"][0]
    after = session.delete(f"/api/projects/1/attachments/{attached['id']}").json()
    assert after["attachments"] == [] and after["files"] == 0
    assert any((config.data_dir / "projects" / "1").iterdir())
    again = session.delete(f"/api/projects/1/attachments/{attached['id']}")
    assert again.status_code == 200
    assert session.delete("/api/projects/1/attachments/999").status_code == 404


# ---------------------------------------------------------------- pinned documents


def _library_document(config, drive_id, course):
    conn = store.connect(config.db_path)
    conn.execute(
        "INSERT INTO materials (id, parent_type, parent_id, course_id, kind, ref, drive_id, "
        "title, content_hash, first_seen_at) VALUES (?, 'coursework_material', 'p', ?, "
        "'driveFile', ?, ?, 'Brief.pdf', 'h', 'now')",
        (f"m-{drive_id}", course, drive_id, drive_id),
    )
    (config.library_dir / "files").mkdir(parents=True, exist_ok=True)
    (config.library_dir / "files" / f"{drive_id}.pdf").write_bytes(PDF)
    store.upsert_extraction(conn, drive_id, status="ok", local_path=f"files/{drive_id}.pdf",
                            mime_type="application/pdf", size_bytes=len(PDF), pages=1, chars=0,
                            scan_pages=0, ocr_pages=0, extracted_at="2026-10-02T00:00:00Z")
    conn.commit()
    conn.close()


def test_a_library_document_of_the_course_can_be_pinned(session, config):
    _library_document(config, "brief", COURSE)
    body = session.post("/api/projects/1/documents", json={"drive_id": "brief"}).json()
    (pinned,) = body["attachments"]
    assert pinned["kind"] == "document" and pinned["document"]["pdf_url"] == "/api/documents/brief/file"
    assert body["files"] == 1


def test_another_courses_document_is_refused(session, config):
    _library_document(config, "elsewhere", "manual-calculus-iii")
    response = session.post("/api/projects/1/documents", json={"drive_id": "elsewhere"})
    assert response.status_code == 400
    assert "another subject" in response.json()["detail"]


# ---------------------------------------------------------------- the backup


def test_the_backup_carries_uploads_and_links_unchanged_ones(session, config, tmp_path):
    upload(session, "brief.pdf", PDF)
    conn = store.connect(config.db_path)
    first = backup.write(config, conn, config.data_dir / "backups" / "manual-20261016T080000Z")
    second = backup.write(config, conn, config.data_dir / "backups" / "manual-20261017T080000Z")
    (stored,) = (config.data_dir / "projects" / "1").iterdir()
    copies = [snap.path / "projects" / "1" / stored.name for snap in (first, second)]
    assert all(path.read_bytes() == PDF for path in copies)
    assert second.project_files_linked == 1
    assert os.stat(copies[0]).st_ino == os.stat(copies[1]).st_ino
    assert first.rows["project_attachments"] == 1
    conn.close()


def test_a_restore_puts_back_a_missing_upload_and_never_overwrites(session, config):
    upload(session, "brief.pdf", PDF)
    conn = store.connect(config.db_path)
    snap = backup.write(config, conn, config.data_dir / "backups" / "manual-20261016T080000Z")
    (stored,) = (config.data_dir / "projects" / "1").iterdir()
    stored.unlink()
    result = backup.restore(config, conn, snap.path)
    assert result.project_files_added == 1 and stored.read_bytes() == PDF
    stored.write_bytes(b"newer")
    again = backup.restore(config, conn, snap.path)
    assert again.project_files_present == 1 and stored.read_bytes() == b"newer"
    conn.close()


def test_a_tampered_upload_refuses_to_restore(session, config):
    upload(session, "brief.pdf", PDF)
    conn = store.connect(config.db_path)
    snap = backup.write(config, conn, config.data_dir / "backups" / "manual-20261016T080000Z")
    (copy,) = (snap.path / "projects" / "1").iterdir()
    copy.write_bytes(b"truncated")
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.restore(config, conn, snap.path)
    conn.close()
