"""Fetching attachment bytes from Drive.

Nothing here touches the network: the Drive service is a hand-written fake and
MediaIoBaseDownload is replaced in the module under test.

The cases that matter most are the ones about dead references. Measured on the
five tracked courses, 16 of 118 driveFile attachments point at trashed files and
4 return 404, so "the run finishes and records what happened" is the property
under test, not "the happy path works".
"""

from __future__ import annotations

import json

import pytest
from googleapiclient.errors import HttpError

from agent.classroom.models import Course, Material
from agent.config import Config
from agent.db import store
from agent.files import drive

# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeRequest:
    """One prepared Drive call.

    `errors` is consumed one exception per attempt, so a request can fail twice
    and then succeed, which is what the retry tests need.
    """

    def __init__(self, result=None, *, payload=b"", errors=()):
        self.result = result
        self.payload = payload
        self.errors = list(errors)
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


class FakeFiles:
    """drive.files().get / .get_media / .export_media, recording every call."""

    def __init__(self, metadata=None, media=None, exports=None):
        self.metadata = metadata or {}
        self.media = media or {}
        self.exports = exports or {}
        self.get_calls: list[dict] = []
        self.media_calls: list[dict] = []
        self.export_calls: list[dict] = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        entry = self.metadata[kwargs["fileId"]]
        return entry if isinstance(entry, FakeRequest) else FakeRequest(entry)

    def get_media(self, **kwargs):
        self.media_calls.append(kwargs)
        entry = self.media.get(kwargs["fileId"], b"bytes")
        return entry if isinstance(entry, FakeRequest) else FakeRequest(payload=entry)

    def export_media(self, **kwargs):
        self.export_calls.append(kwargs)
        entry = self.exports.get(kwargs["fileId"], b"exported")
        return entry if isinstance(entry, FakeRequest) else FakeRequest(payload=entry)


class FakeService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


class FakeDownload:
    """Stands in for MediaIoBaseDownload, whose real form needs a live http."""

    def __init__(self, fd, request, chunksize=None):
        self.fd = fd
        self.request = request

    def next_chunk(self):
        if self.request.errors:
            raise self.request.errors.pop(0)
        self.fd.write(self.request.payload)
        return None, True


def http_error(status, reason=None):
    body = {"error": {"code": status, "message": "nope"}}
    if reason:
        body["error"]["errors"] = [{"reason": reason}]

    class Resp:
        def __init__(self):
            self.status = status
            self.reason = "error"

    return HttpError(Resp(), json.dumps(body).encode("utf-8"))


def make_client(monkeypatch, files, *, slept=None):
    monkeypatch.setattr(drive, "build", lambda *a, **k: FakeService(files))
    monkeypatch.setattr(drive, "MediaIoBaseDownload", FakeDownload)
    sleep = slept.append if slept is not None else (lambda _seconds: None)
    return drive.DriveClient(credentials=None, sleep=sleep)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


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
    store.upsert_course(connection, a_course())
    yield connection
    connection.close()


def a_course(course_id="c1", name="Operating Systems") -> Course:
    return Course(
        id=course_id,
        name=name,
        section=None,
        room=None,
        owner_id=None,
        course_state="ACTIVE",
        enrollment_code=None,
        alternate_link=None,
        creation_time=None,
        update_time=None,
        content_hash="h",
    )


def a_material(drive_id, *, parent_id="p1", course_id="c1", title="Chapter 6.pdf") -> Material:
    return Material(
        id=f"coursework_material:{parent_id}:driveFile:{drive_id}",
        parent_type="coursework_material",
        parent_id=parent_id,
        course_id=course_id,
        kind="driveFile",
        ref=drive_id,
        drive_id=drive_id,
        title=title,
        url=f"https://drive.google.com/file/d/{drive_id}/view",
        content_hash="h",
    )


def meta(file_id, **overrides) -> dict:
    base = {
        "id": file_id,
        "name": "Chapter 6.pdf",
        "mimeType": "application/pdf",
        "size": "1024",
        "trashed": False,
        "md5Checksum": "abc123",
        "modifiedTime": "2026-03-01T10:00:00.000Z",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------


def test_metadata_asks_for_the_fields_that_decide_everything(monkeypatch):
    """trashed is the ONLY signal that separates a binned file from a live one.

    Its metadata is otherwise entirely valid, so omitting the field means
    downloading dead material and never knowing.
    """
    files = FakeFiles(metadata={"f1": meta("f1")})
    client = make_client(monkeypatch, files)

    client.metadata("f1")

    (call,) = files.get_calls
    for required in ("trashed", "md5Checksum", "modifiedTime"):
        assert required in call["fields"]
    assert call["supportsAllDrives"] is True


def test_a_shortcut_resolves_to_its_target(monkeypatch):
    files = FakeFiles(
        metadata={
            "short": meta("short", mimeType=drive.SHORTCUT_MIME, shortcutDetails={"targetId": "real"}),
            "real": meta("real", name="Real.pdf"),
        }
    )
    client = make_client(monkeypatch, files)

    assert client.metadata("short")["id"] == "real"


def test_a_shortcut_with_no_target_is_an_error(monkeypatch):
    files = FakeFiles(metadata={"short": meta("short", mimeType=drive.SHORTCUT_MIME)})
    client = make_client(monkeypatch, files)

    with pytest.raises(drive.DriveError):
        client.metadata("short")


def test_a_rate_limited_call_is_retried_with_growing_backoff(monkeypatch):
    slept: list[float] = []
    request = FakeRequest(meta("f1"), errors=[http_error(429), http_error(503)])
    files = FakeFiles(metadata={"f1": request})
    client = make_client(monkeypatch, files, slept=slept)

    assert client.metadata("f1")["id"] == "f1"
    assert request.calls == 3
    assert slept[0] < slept[1]  # exponential, not flat


def test_a_download_retries_on_429_too(monkeypatch, tmp_path):
    """MediaIoBaseDownload does not go through execute(), so it needs its own loop."""
    request = FakeRequest(payload=b"pdf-bytes", errors=[http_error(429)])
    files = FakeFiles(media={"f1": request})
    client = make_client(monkeypatch, files)

    written = client.download("f1", tmp_path / "out.pdf")

    assert written == len(b"pdf-bytes")
    assert (tmp_path / "out.pdf").read_bytes() == b"pdf-bytes"


def test_a_permission_403_is_not_retried(monkeypatch):
    """A policy 403 is a wall. Retrying into it wastes the run and hides the cause."""
    request = FakeRequest(meta("f1"), errors=[http_error(403, "insufficientPermissions")])
    files = FakeFiles(metadata={"f1": request})
    client = make_client(monkeypatch, files)

    with pytest.raises(HttpError):
        client.metadata("f1")
    assert request.calls == 1


# --------------------------------------------------------------------------
# extensions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mime_type,expected",
    [
        ("application/pdf", ".pdf"),
        ("text/x-sql", ".sql"),
        ("application/vnd.ms-powerpoint", ".ppt"),
        ("application/zip", ".zip"),
    ],
)
def test_the_extension_comes_from_the_mime_type(mime_type, expected):
    assert drive.extension_for(mime_type) == expected


def test_an_unknown_mime_type_falls_back_to_the_drive_name():
    assert drive.extension_for("application/x-weird", name="exam.seb") == ".seb"


def test_a_title_that_is_a_sentence_does_not_become_an_extension():
    """Real titles contain full stops. 'Lecture 3. Revision' must not yield '. Revision'."""
    assert drive.extension_for("application/x-weird", name="Lecture 3. Revision") == ".bin"


# --------------------------------------------------------------------------
# fetch: dead references
# --------------------------------------------------------------------------


def test_a_trashed_file_is_recorded_and_never_downloaded(config, conn, monkeypatch):
    store.upsert_material(conn, a_material("f1"))
    files = FakeFiles(metadata={"f1": meta("f1", trashed=True)})
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client)

    assert result.trashed == 1
    assert result.fetched == 0
    assert files.media_calls == []
    assert store.get_extraction(conn, "f1")["status"] == "trashed"


def test_a_404_is_recorded_rather_than_raised(config, conn, monkeypatch):
    """At 17% prevalence a fetcher that raises here never finishes the library."""
    store.upsert_material(conn, a_material("gone"))
    files = FakeFiles(metadata={"gone": FakeRequest(errors=[http_error(404)])})
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client)

    assert result.missing == 1
    row = store.get_extraction(conn, "gone")
    assert row["status"] == "missing"
    assert "404" in row["error"]


def test_a_mixed_batch_runs_to_completion(config, conn, monkeypatch):
    """The whole point: one live, one trashed and one gone all get recorded."""
    for drive_id in ("live", "binned", "gone"):
        store.upsert_material(conn, a_material(drive_id, parent_id=drive_id))
    files = FakeFiles(
        metadata={
            "live": meta("live"),
            "binned": meta("binned", trashed=True),
            "gone": FakeRequest(errors=[http_error(404)]),
        }
    )
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client)

    assert (result.fetched, result.trashed, result.missing) == (1, 1, 1)
    assert store.count_extractions_by_status(conn) == {
        "fetched": 1,
        "missing": 1,
        "trashed": 1,
    }


def test_a_folder_is_recorded_unsupported_rather_than_recursed(config, conn, monkeypatch):
    store.upsert_material(conn, a_material("dir"))
    files = FakeFiles(metadata={"dir": meta("dir", mimeType=drive.FOLDER_MIME)})
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client)

    assert result.unsupported == 1
    assert store.get_extraction(conn, "dir")["status"] == "unsupported"
    assert result.notes  # visible in the run summary, not silently skipped


def test_an_unexpected_error_does_not_stop_the_run(config, conn, monkeypatch):
    store.upsert_material(conn, a_material("bad", parent_id="a"))
    store.upsert_material(conn, a_material("good", parent_id="b"))
    files = FakeFiles(
        metadata={
            "bad": FakeRequest(errors=[http_error(403, "insufficientPermissions")]),
            "good": meta("good"),
        }
    )
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client)

    assert result.errors == 1
    assert result.fetched == 1


def test_a_file_that_leaves_the_trash_is_downloaded_again(config, conn, monkeypatch):
    """Nothing latches. A teacher restoring a file must not leave it dead forever."""
    store.upsert_material(conn, a_material("f1"))
    trashed = FakeFiles(metadata={"f1": meta("f1", trashed=True)})
    drive.fetch(config, conn, client=make_client(monkeypatch, trashed))
    assert conn.execute("SELECT trashed FROM materials").fetchone()["trashed"] == 1

    live = FakeFiles(metadata={"f1": meta("f1")})
    result = drive.fetch(config, conn, client=make_client(monkeypatch, live))

    assert result.fetched == 1
    assert store.get_extraction(conn, "f1")["status"] == "fetched"
    assert conn.execute("SELECT trashed FROM materials").fetchone()["trashed"] == 0


# --------------------------------------------------------------------------
# fetch: downloading
# --------------------------------------------------------------------------


def test_a_binary_file_lands_under_the_library_named_by_its_drive_id(config, conn, monkeypatch):
    """Never named from the title: real ones contain ':' and '/', illegal on Windows."""
    store.upsert_material(conn, a_material("f1", title="TD n°3 : Révisions / corrigé.pdf"))
    files = FakeFiles(metadata={"f1": meta("f1")}, media={"f1": b"%PDF-1.7"})
    client = make_client(monkeypatch, files)

    drive.fetch(config, conn, client=client)

    landed = config.library_dir / "files" / "f1.pdf"
    assert landed.read_bytes() == b"%PDF-1.7"
    # Stored relative, so moving DATA_DIR stays a copy (invariant 5).
    assert store.get_extraction(conn, "f1")["local_path"] == "files/f1.pdf"


def test_the_string_size_becomes_an_integer(config, conn, monkeypatch):
    """Drive returns size as "909108". Left as text every comparison silently fails."""
    store.upsert_material(conn, a_material("f1"))
    files = FakeFiles(metadata={"f1": meta("f1", size="909108")})
    client = make_client(monkeypatch, files)

    drive.fetch(config, conn, client=client)

    assert store.get_extraction(conn, "f1")["size_bytes"] == 909108


def test_a_google_native_document_is_exported_not_downloaded(config, conn, monkeypatch):
    """get_media returns 403 on these, which reads exactly like a permissions bug."""
    store.upsert_material(conn, a_material("doc"))
    files = FakeFiles(
        metadata={"doc": meta("doc", mimeType="application/vnd.google-apps.document", size=None)}
    )
    client = make_client(monkeypatch, files)

    drive.fetch(config, conn, client=client)

    assert files.media_calls == []
    assert files.export_calls[0]["mimeType"] == "application/pdf"
    assert (config.library_dir / "files" / "doc.pdf").exists()


def test_a_spreadsheet_exports_to_csv_not_pdf(config, conn, monkeypatch):
    """A PDF of a spreadsheet is a picture of a table and extracts to noise."""
    store.upsert_material(conn, a_material("sheet"))
    files = FakeFiles(
        metadata={
            "sheet": meta("sheet", mimeType="application/vnd.google-apps.spreadsheet", size=None)
        }
    )
    client = make_client(monkeypatch, files)

    drive.fetch(config, conn, client=client)

    assert files.export_calls[0]["mimeType"] == "text/csv"


def test_one_download_per_file_even_when_two_posts_share_it(config, conn, monkeypatch):
    """The material key is composite, so one file on two posts is two rows."""
    store.upsert_material(conn, a_material("shared", parent_id="lecture-1"))
    store.upsert_material(conn, a_material("shared", parent_id="lecture-2"))
    files = FakeFiles(metadata={"shared": meta("shared")})
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client)

    assert result.references == 2
    assert result.files == 1
    assert len(files.media_calls) == 1
    # Both references learn the file's facts.
    rows = conn.execute("SELECT local_path FROM materials ORDER BY id").fetchall()
    assert [row["local_path"] for row in rows] == ["files/shared.pdf", "files/shared.pdf"]


# --------------------------------------------------------------------------
# fetch: skipping unchanged files
# --------------------------------------------------------------------------


def test_a_second_fetch_downloads_nothing(config, conn, monkeypatch):
    """The file-layer equivalent of "a second sync emits zero events"."""
    store.upsert_material(conn, a_material("f1"))
    files = FakeFiles(metadata={"f1": meta("f1")})
    drive.fetch(config, conn, client=make_client(monkeypatch, files))

    again = FakeFiles(metadata={"f1": meta("f1")})
    result = drive.fetch(config, conn, client=make_client(monkeypatch, again))

    assert result.skipped == 1
    assert result.fetched == 0
    assert again.media_calls == []


def test_a_changed_checksum_downloads_again(config, conn, monkeypatch):
    store.upsert_material(conn, a_material("f1"))
    drive.fetch(config, conn, client=make_client(monkeypatch, FakeFiles(metadata={"f1": meta("f1")})))

    changed = FakeFiles(metadata={"f1": meta("f1", md5Checksum="different")})
    result = drive.fetch(config, conn, client=make_client(monkeypatch, changed))

    assert result.fetched == 1


def test_without_a_checksum_the_modified_time_decides(config, conn, monkeypatch):
    """Google-native files carry no md5, and the probe never measured how common that is."""
    store.upsert_material(conn, a_material("f1"))
    first = FakeFiles(metadata={"f1": meta("f1", md5Checksum=None)})
    drive.fetch(config, conn, client=make_client(monkeypatch, first))

    same = FakeFiles(metadata={"f1": meta("f1", md5Checksum=None)})
    assert drive.fetch(config, conn, client=make_client(monkeypatch, same)).skipped == 1

    moved = FakeFiles(
        metadata={"f1": meta("f1", md5Checksum=None, modifiedTime="2026-04-01T10:00:00.000Z")}
    )
    assert drive.fetch(config, conn, client=make_client(monkeypatch, moved)).fetched == 1


def test_a_missing_local_file_is_downloaded_again(config, conn, monkeypatch):
    """A database row is not evidence that the bytes are still on disk."""
    store.upsert_material(conn, a_material("f1"))
    drive.fetch(config, conn, client=make_client(monkeypatch, FakeFiles(metadata={"f1": meta("f1")})))
    (config.library_dir / "files" / "f1.pdf").unlink()

    files = FakeFiles(metadata={"f1": meta("f1")})
    assert drive.fetch(config, conn, client=make_client(monkeypatch, files)).fetched == 1


def test_force_downloads_a_file_that_has_not_changed(config, conn, monkeypatch):
    store.upsert_material(conn, a_material("f1"))
    drive.fetch(config, conn, client=make_client(monkeypatch, FakeFiles(metadata={"f1": meta("f1")})))

    files = FakeFiles(metadata={"f1": meta("f1")})
    result = drive.fetch(config, conn, client=make_client(monkeypatch, files), force=True)

    assert result.fetched == 1


def test_new_bytes_discard_the_text_extracted_from_the_old_ones(config, conn, monkeypatch):
    """Otherwise the next pack cites a page that no longer exists in the file."""
    store.upsert_material(conn, a_material("f1"))
    drive.fetch(config, conn, client=make_client(monkeypatch, FakeFiles(metadata={"f1": meta("f1")})))
    store.upsert_extraction(
        conn, "f1", status="ok", text_path="text/f1.txt", method="pymupdf", pages=4, chars=1141
    )

    changed = FakeFiles(metadata={"f1": meta("f1", md5Checksum="different")})
    drive.fetch(config, conn, client=make_client(monkeypatch, changed))

    row = store.get_extraction(conn, "f1")
    assert row["status"] == "fetched"
    assert row["text_path"] is None
    assert row["chars"] is None


# --------------------------------------------------------------------------
# fetch: scope and dry run
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_calls_nothing(config, conn, monkeypatch):
    store.upsert_material(conn, a_material("f1"))
    files = FakeFiles(metadata={"f1": meta("f1")})
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client, dry_run=True)

    assert result.references == 1 and result.files == 1
    assert result.fetched == 0
    assert files.get_calls == []
    assert store.count_rows(conn, "extractions") == 0


def test_only_tracked_courses_are_fetched(config, conn, monkeypatch):
    store.upsert_course(conn, a_course("c2", "Untracked"))
    store.upsert_material(conn, a_material("mine"))
    store.upsert_material(conn, a_material("theirs", course_id="c2"))
    files = FakeFiles(metadata={"mine": meta("mine")})
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client)

    assert result.references == 1
    assert result.fetched == 1


def test_a_soft_deleted_material_is_left_alone(config, conn, monkeypatch):
    """The post is gone from Classroom, so there is nothing left to revise."""
    store.upsert_material(conn, a_material("f1"))
    store.soft_delete_missing(conn, "materials", "c1", live_ids=[])
    files = FakeFiles(metadata={})
    client = make_client(monkeypatch, files)

    assert drive.fetch(config, conn, client=client).references == 0


def test_links_and_youtube_attachments_are_not_fetched(config, conn, monkeypatch):
    """Only driveFile has bytes. The other union members are not downloadable."""
    conn.execute(
        "INSERT INTO materials (id, parent_type, parent_id, course_id, kind, ref, "
        "drive_id, title, url, content_hash, first_seen_at) VALUES "
        "('m-link', 'announcement', 'p9', 'c1', 'link', 'https://kaggle.com', NULL, "
        "'Kaggle', 'https://kaggle.com', 'h', '2026-01-01T00:00:00Z')"
    )
    client = make_client(monkeypatch, FakeFiles(metadata={}))

    assert drive.fetch(config, conn, client=client).references == 0


def test_limit_bounds_one_run(config, conn, monkeypatch):
    for drive_id in ("a", "b", "c"):
        store.upsert_material(conn, a_material(drive_id, parent_id=drive_id))
    files = FakeFiles(metadata={key: meta(key) for key in ("a", "b", "c")})
    client = make_client(monkeypatch, files)

    result = drive.fetch(config, conn, client=client, limit=2)

    assert result.fetched == 2
