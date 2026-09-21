"""`agent backup`, and the restore that makes it a backup rather than a belief.

deploy/README.md says it plainly: *a backup that has never been restored is a
belief, not a backup.* So the central test here builds a database with every
kind of manual row, snapshots it, restores into a **fresh empty** database, and
checks that what comes back is what went in -- rows and file bytes both -- and
that a quiz can still be generated from the restored library.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent import backup, cli, manual
from agent.classroom.models import Material, material_id, parse_course
from agent.config import Config
from agent.db import store

MANUAL = "manual-calculus-iii"
UNIX = "809686876412"
NOW = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)


class KeepOpen:
    def __init__(self, connection):
        self._conn = connection

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def a_config(root: Path) -> Config:
    data_dir = root / "data"
    (data_dir / "library").mkdir(parents=True, exist_ok=True)
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=data_dir,
        tracked_courses=[UNIX],
        ignored_courses=[],
    )


@pytest.fixture
def config(tmp_path):
    return a_config(tmp_path / "live")


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    store.upsert_course(connection, parse_course({"id": UNIX, "name": "UNIX"}))
    store.ensure_manual_course(connection, MANUAL, "Calculus III")
    connection.commit()
    yield connection
    connection.close()


def a_full_database(config: Config, conn) -> dict:
    """One of everything Phase 6 can create, including an upload's bytes."""
    payload = b"\xff\xd8\xff\xe0" + b"BOARD" * 20 + b"\xff\xd9"
    file_id = manual.file_id(payload)
    post_id = manual.post_id(MANUAL, "board.jpg", "2026-09-21T12:00:00Z")

    destination = config.library_dir / "files" / f"{file_id}.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)

    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, content_hash, "
        "first_seen_at, creation_time) VALUES (?, ?, 'Board, 21 Sep', 'h', ?, ?)",
        (post_id, MANUAL, "2026-09-21T12:00:00Z", "2026-09-21T12:00:00Z"),
    )
    store.upsert_material(conn, Material(
        id=material_id("coursework_material", post_id, "driveFile", file_id),
        parent_type="coursework_material", parent_id=post_id, course_id=MANUAL,
        kind="driveFile", ref=file_id, drive_id=file_id, title="board.jpg",
        url=None, content_hash="h",
    ))
    store.upsert_extraction(
        conn, file_id, status="ok", mime_type="image/jpeg",
        local_path=f"files/{file_id}.jpg", text_path=f"text/{file_id}.txt",
        method="image", pages=1, scan_pages=1, chars=0,
    )
    store.upsert_ocr_page(
        conn, drive_id=file_id, page_index=0, page_hash="ph1", status="ok",
        text="A Cauchy sequence converges in a complete space.", model="fake",
    )
    store.ensure_study_item(
        conn, entity_type="coursework_material", entity_id=post_id,
        course_id=MANUAL, state="pending",
    )
    item_id = conn.execute(
        "SELECT id FROM study_items WHERE entity_id = ?", (post_id,)
    ).fetchone()["id"]
    store.advance_study_item(conn, item_id, "delivered")
    store.save_questions(
        conn, item_id=item_id, source_hash="sh1", model="fake",
        questions=json.dumps([{"question": "q", "options": ["a"], "correct": 0}]),
    )
    attempt_id = store.start_quiz_attempt(conn, item_id=item_id, state="{}")
    store.record_flag(
        conn, item_id=item_id, attempt_id=attempt_id, source_hash="sh1",
        model="fake", question_index=0, question='{"question": "q"}',
    )
    store.remember_telegram_file(conn, drive_id=file_id, file_id="tg-123")

    store.log_manual_session(
        conn, course_id=MANUAL, held_on="2026-09-21", kind="LEC", covered="Cauchy"
    )
    store.add_manual_task(
        conn, course_id=MANUAL, title="TD 3", due_at="2026-10-05T22:59:00Z"
    )
    # One against a TRACKED subject, so the prefix selection is exercised on
    # both sides: a manual row inside a Classroom course.
    store.add_manual_task(conn, course_id=UNIX, title="Paper sheet 1")

    project_id = store.add_project(
        conn, course_id=MANUAL, title="Compiler front end",
        deadline_at="2026-12-15T22:59:00Z", deliverables="Report\nDemo",
        team="me\nSami", brief_source="verbal", coursework_id="w1",
    )
    first = store.add_milestone(conn, project_id, "Lexer")
    store.add_milestone(conn, project_id, "Parser")
    store.complete_milestone(conn, first)
    conn.commit()
    return {"file_id": file_id, "post_id": post_id, "item_id": item_id,
            "payload": payload, "project_id": project_id}


# --------------------------------------------------------------------------
# what goes in
# --------------------------------------------------------------------------

def test_a_snapshot_holds_every_kind_of_manual_row(config, conn, tmp_path):
    a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)

    # The manual course, plus UNIX as an anchor for the paper sheet recorded
    # against it. See test_a_classroom_course_is_carried_as_an_anchor.
    assert snapshot.rows["courses"] == 2
    assert snapshot.rows["coursework_materials"] == 1
    assert snapshot.rows["materials"] == 1
    assert snapshot.rows["extractions"] == 1
    assert snapshot.rows["ocr_pages"] == 1
    assert snapshot.rows["study_items"] == 1
    assert snapshot.rows["quiz_questions"] == 1
    assert snapshot.rows["quiz_attempts"] == 1
    assert snapshot.rows["quiz_flags"] == 1
    assert snapshot.rows["telegram_files"] == 1
    assert snapshot.rows["manual_sessions"] == 1
    assert snapshot.rows["manual_tasks"] == 2
    assert snapshot.rows["projects"] == 1
    assert snapshot.rows["project_milestones"] == 2
    # And the bytes, because a photographed board exists in exactly one place.
    assert snapshot.files == 1


def test_the_snapshot_never_carries_classroom_content(config, conn, tmp_path):
    """Restoring a stale mirror over a fresh one would be a worse failure than
    the one being recovered from. Everything Google can re-serve is left out."""
    a_full_database(config, conn)
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, content_hash, "
        "first_seen_at) VALUES ('m1', ?, 'Real slides', 'h', ?)",
        (UNIX, "2026-09-01T10:00:00Z"),
    )
    store.upsert_extraction(conn, "1RealDriveId", status="ok",
                            local_path="files/1RealDriveId.pdf")
    conn.commit()

    rows = backup.collect(conn)
    assert all(manual.is_manual(row["id"]) for row in rows["coursework_materials"])
    assert all(manual.is_manual(row["drive_id"]) for row in rows["extractions"])
    assert all(
        manual.is_manual(row["parent_id"]) for row in rows["materials"]
    )
    # A post Classroom serves is not in the file at all.
    assert "m1" not in [row["id"] for row in rows["coursework_materials"]]
    assert "1RealDriveId" not in [row["drive_id"] for row in rows["extractions"]]


def test_a_classroom_course_is_carried_as_an_anchor(config, conn):
    """Found by restoring rather than by thinking.

    An exercise sheet handed out on paper for UNIX is a manual row whose
    course_id is a Classroom course, and `courses` has a foreign key. Into an
    empty database that row has nothing to attach to. So the snapshot carries
    the course rows its own rows point at -- and only those.
    """
    a_full_database(config, conn)
    store.upsert_course(conn, parse_course({"id": "999", "name": "Unrelated"}))
    conn.commit()

    carried = {row["id"] for row in backup.collect(conn)["courses"]}
    assert carried == {MANUAL, UNIX}
    assert "999" not in carried


def test_two_snapshots_of_an_unchanged_database_are_identical(config, conn, tmp_path):
    """Stable order, so `diff` is a usable check on whether anything moved."""
    a_full_database(config, conn)
    first = backup.write(config, conn, tmp_path / "a", now=NOW)
    second = backup.write(config, conn, tmp_path / "b", now=NOW)
    assert (first.path / backup.ROWS_FILE).read_text(encoding="utf-8") == (
        second.path / backup.ROWS_FILE
    ).read_text(encoding="utf-8")


def test_writing_into_a_non_empty_directory_is_refused(config, conn, tmp_path):
    a_full_database(config, conn)
    target = tmp_path / "snap"
    target.mkdir()
    (target / "something").write_text("x", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="already exists and is not empty"):
        backup.write(config, conn, target)


def test_dry_run_writes_no_directory(config, conn, tmp_path):
    a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", dry_run=True)
    assert snapshot.total_rows > 0
    assert not (tmp_path / "snap").exists()


# --------------------------------------------------------------------------
# what comes back -- the test the whole stage exists for
# --------------------------------------------------------------------------

def test_a_restore_into_an_empty_database_brings_everything_back(
    config, conn, tmp_path
):
    """Row for row, byte for byte, and the library still quizzable afterwards."""
    facts = a_full_database(config, conn)
    before = backup.collect(conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)

    # A genuinely fresh installation: new DATA_DIR, new database, no library.
    fresh_config = a_config(tmp_path / "restored")
    fresh = store.connect(fresh_config.db_path)
    try:
        result = backup.restore(fresh_config, fresh, snapshot.path)
        fresh.commit()

        assert result.total_skipped == 0
        assert result.files_added == 1
        assert backup.collect(fresh) == before

        # The bytes, not just a row pointing at them.
        restored_file = fresh_config.library_dir / "files" / f"{facts['file_id']}.jpg"
        assert restored_file.read_bytes() == facts["payload"]

        # And the state that could never have been rebuilt: a delivered study
        # item, its cached questions, its flag, and a project half done.
        item = store.get_study_item(fresh, facts["item_id"])
        assert item["state"] == "delivered"
        assert store.cached_questions(fresh, facts["item_id"], "sh1") is not None
        assert len(store.list_flags(fresh)) == 1
        project = store.projects(fresh)[0]
        assert (project["milestones"], project["milestones_done"]) == (2, 1)

        # The transcription came back, so the material is still quizzable.
        assert "Cauchy" in store.ocr_pages_for(fresh, facts["file_id"])[0]["text"]
    finally:
        fresh.close()


def test_restoring_twice_is_a_no_op(config, conn, tmp_path):
    a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)

    fresh_config = a_config(tmp_path / "restored")
    fresh = store.connect(fresh_config.db_path)
    try:
        first = backup.restore(fresh_config, fresh, snapshot.path)
        fresh.commit()
        second = backup.restore(fresh_config, fresh, snapshot.path)
        fresh.commit()

        assert second.total_added == 0
        assert second.total_skipped == first.total_added
        assert second.files_added == 0
        assert backup.collect(fresh) == backup.collect(conn)
    finally:
        fresh.close()


def test_a_partial_restore_adds_only_what_is_missing_and_says_which(
    config, conn, tmp_path
):
    a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)

    # A database that already holds the course and the sessions but lost the
    # tasks and the project -- the shape of a half-finished recovery.
    fresh_config = a_config(tmp_path / "restored")
    fresh = store.connect(fresh_config.db_path)
    try:
        store.ensure_manual_course(fresh, MANUAL, "Calculus III")
        fresh.commit()

        result = backup.restore(fresh_config, fresh, snapshot.path)
        fresh.commit()

        # The manual course was already here; the UNIX anchor was not.
        assert result.skipped["courses"] == 1
        assert result.added["courses"] == 1
        assert result.added["projects"] == 1
        assert result.added["manual_sessions"] == 1
    finally:
        fresh.close()


def test_a_restore_never_overwrites_a_row_that_is_already_there(
    config, conn, tmp_path
):
    """Silently overwriting newer local state would be a worse failure than the
    one being recovered from."""
    a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)

    fresh_config = a_config(tmp_path / "restored")
    fresh = store.connect(fresh_config.db_path)
    try:
        store.ensure_manual_course(fresh, MANUAL, "Renamed since the backup")
        fresh.commit()
        backup.restore(fresh_config, fresh, snapshot.path)
        fresh.commit()
        assert store.get_course(fresh, MANUAL)["name"] == "Renamed since the backup"
    finally:
        fresh.close()


def test_restore_dry_run_changes_nothing(config, conn, tmp_path):
    a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)

    fresh_config = a_config(tmp_path / "restored")
    fresh = store.connect(fresh_config.db_path)
    try:
        result = backup.restore(fresh_config, fresh, snapshot.path, dry_run=True)
        assert result.total_added > 0
        assert store.manual_courses(fresh) == []
        assert not (fresh_config.library_dir / "files").exists()
    finally:
        fresh.close()


# --------------------------------------------------------------------------
# and a snapshot that cannot be trusted says so
# --------------------------------------------------------------------------

def test_a_tampered_rows_file_refuses_to_restore(config, conn, tmp_path):
    a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)
    (snapshot.path / backup.ROWS_FILE).write_text('{"format": 1}', encoding="utf-8")

    fresh = store.connect(tmp_path / "other.db")
    try:
        with pytest.raises(backup.BackupError, match="does not match its checksum"):
            backup.restore(config, fresh, snapshot.path)
    finally:
        fresh.close()


def test_a_truncated_upload_refuses_to_restore(config, conn, tmp_path):
    """Restoring it would put corrupted bytes back into the library."""
    facts = a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)
    corrupt = snapshot.path / backup.FILES_DIR / f"{facts['file_id']}.jpg"
    corrupt.write_bytes(b"\xff\xd8truncated")

    fresh = store.connect(tmp_path / "other.db")
    try:
        with pytest.raises(backup.BackupError, match="does not match its checksum"):
            backup.restore(config, fresh, snapshot.path)
    finally:
        fresh.close()


def test_a_missing_upload_refuses_to_restore(config, conn, tmp_path):
    facts = a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)
    (snapshot.path / backup.FILES_DIR / f"{facts['file_id']}.jpg").unlink()

    fresh = store.connect(tmp_path / "other.db")
    try:
        with pytest.raises(backup.BackupError, match="which is not in"):
            backup.restore(config, fresh, snapshot.path)
    finally:
        fresh.close()


def test_a_directory_that_is_not_a_snapshot_says_so(config, conn, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(backup.BackupError, match="is not a snapshot"):
        backup.restore(config, conn, empty)


def test_an_unknown_format_version_is_refused_rather_than_guessed(
    config, conn, tmp_path
):
    a_full_database(config, conn)
    snapshot = backup.write(config, conn, tmp_path / "snap", now=NOW)
    payload = json.dumps({"format": 99, "tables": {}}, indent=1, sort_keys=True)
    (snapshot.path / backup.ROWS_FILE).write_text(payload, encoding="utf-8")
    # Re-sign it, so the failure under test is the version and not the checksum.
    import hashlib

    lines = (snapshot.path / backup.MANIFEST).read_text(encoding="utf-8").splitlines()
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    lines[0] = f"{digest}  {backup.ROWS_FILE}"
    (snapshot.path / backup.MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(backup.BackupError, match="format"):
        backup.restore(config, conn, snapshot.path)


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------

def backup_args(**kwargs):
    return argparse.Namespace(**{
        "out": None, "restore": None, "show": False, "dry_run": False, **kwargs,
    })


def test_the_command_writes_a_snapshot_and_says_how_to_check_it(
    config, conn, monkeypatch, tmp_path, capsys
):
    a_full_database(config, conn)
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))

    assert cli.cmd_backup(config, backup_args(out=tmp_path / "snap")) == 0
    out = capsys.readouterr().out
    assert "manual_sessions" in out
    assert "uploaded files" in out
    assert "--restore" in out
    assert (tmp_path / "snap" / backup.MANIFEST).is_file()


def test_the_command_reports_added_and_kept_separately(
    config, conn, monkeypatch, tmp_path, capsys
):
    """Two different facts: what was missing, and what was deliberately left."""
    a_full_database(config, conn)
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    cli.cmd_backup(config, backup_args(out=tmp_path / "snap"))
    capsys.readouterr()

    # Restoring into the database it came from: everything is already there.
    assert cli.cmd_backup(config, backup_args(restore=tmp_path / "snap")) == 0
    out = capsys.readouterr().out
    assert "added" in out and "kept" in out
    assert "Nothing was missing" in out


def test_list_reports_what_a_snapshot_would_hold_without_writing_one(
    config, conn, monkeypatch, tmp_path, capsys
):
    a_full_database(config, conn)
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    assert cli.cmd_backup(config, backup_args(show=True)) == 0
    assert "uploaded files" in capsys.readouterr().out
    assert not (config.data_dir / "backups").exists()


def test_a_bad_snapshot_is_an_error_exit_not_a_traceback(
    config, conn, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.cmd_backup(config, backup_args(restore=empty)) == 1
    assert "is not a snapshot" in capsys.readouterr().err
