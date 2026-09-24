"""The `manual-` id namespace, and the three guards that keep it out of the sync.

PLAN.md states the constraint as sharply as it can be stated: *a manual course
id must never enter `courses.tracked`*. If one does, the poller 404s on it and
a single sync stamps `deleted_at` across everything typed in by hand.

That is enforced in three places rather than remembered, and each one is
pinned here with a control -- a case where the guard must NOT fire -- so that
deleting the guard cannot leave the suite green.
"""

from __future__ import annotations

import pytest

from agent import manual
from agent.classroom.models import Material, parse_course
from agent.config import ConfigError, load_config
from agent.db import store

MANUAL = "manual-calculus-iii"
POST = "manual-post-abcdef123456"
FILE = "manual-file-0123456789abcdef"


# --------------------------------------------------------------------------
# minting
# --------------------------------------------------------------------------

def test_a_minted_course_id_cannot_be_mistaken_for_a_classroom_one():
    """Classroom course, coursework and announcement ids are decimal digits."""
    minted = manual.course_id("Calculus III")
    assert minted == MANUAL
    assert not minted.isdigit()
    assert manual.is_manual(minted)
    assert not manual.is_manual("840946407460")
    assert not manual.is_manual(None)


def test_minting_is_deterministic():
    """So `agent subjects --add` twice is one course, not two."""
    assert manual.course_id("Calculus III") == manual.course_id("Calculus III")
    assert manual.course_id("calculus iii") == MANUAL


def test_accents_fold_rather_than_splitting_a_subject_in_two():
    """The real timetable is half French; Decision and Décision are one subject."""
    assert manual.course_id("Théorie de la Décision") == manual.course_id(
        "Theorie de la Decision"
    )


def test_every_minted_id_is_filename_safe():
    """`library/files/<drive_id>.<ext>` puts a file id straight into a path, and
    ':' -- the obvious namespace separator -- is illegal on Windows."""
    minted = [
        manual.course_id("Micro-processor & Microcontroller"),
        manual.file_id(b"some bytes"),
        manual.post_id(MANUAL, "board.jpg", "2026-09-21T10:00:00Z"),
    ]
    for identifier in minted:
        assert not set(identifier) - set(
            "abcdefghijklmnopqrstuvwxyz0123456789-"
        ), identifier


def test_a_name_with_no_letters_is_refused_rather_than_mangled():
    with pytest.raises(manual.ManualIdError, match="no letters or digits"):
        manual.course_id("---")


def test_a_file_id_is_a_hash_of_the_file():
    """Which is what gives an upload idempotency: the same photograph twice is
    one row. Invariant 2's rule, applied to a photograph."""
    assert manual.file_id(b"board") == manual.file_id(b"board")
    assert manual.file_id(b"board") != manual.file_id(b"board ")


def test_two_posts_on_one_day_for_one_subject_are_two_posts():
    first = manual.post_id(MANUAL, "board-1.jpg", "2026-09-21T10:00:00Z")
    second = manual.post_id(MANUAL, "board-2.jpg", "2026-09-21T10:00:00Z")
    assert first != second


# --------------------------------------------------------------------------
# guard 1 -- courses.tracked refuses a manual id
# --------------------------------------------------------------------------

def a_config_file(tmp_path, tracked):
    listed = "\n".join(f'    - "{course}"' for course in tracked)
    (tmp_path / "config.yaml").write_text(
        "account: someone@example.com\n"
        "timezone: Africa/Tunis\n"
        "data_dir: ./data\n"
        "courses:\n"
        "  tracked:\n"
        f"{listed}\n"
        "  ignored: []\n",
        encoding="utf-8",
    )
    return tmp_path / "config.yaml"


def test_a_manual_id_in_courses_tracked_is_refused_by_name(tmp_path):
    """The hard constraint. Every command loads config, so there is no path to
    the poller that does not pass this check."""
    path = a_config_file(tmp_path, ["840946407460", MANUAL])
    with pytest.raises(ConfigError) as err:
        load_config(path)

    message = str(err.value)
    assert MANUAL in message
    assert "would mark every manually entered row" in message
    assert "timetable.yaml" in message


def test_an_ordinary_tracked_list_still_loads(tmp_path):
    """The control. Without it the guard could be a blanket refusal."""
    config = load_config(a_config_file(tmp_path, ["840946407460", "809686876412"]))
    assert config.tracked_courses == ["840946407460", "809686876412"]


# --------------------------------------------------------------------------
# guard 2 -- the sync never stamps a manual row
# --------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    store.upsert_course(connection, parse_course({"id": "c1", "name": "UNIX"}))
    store.ensure_manual_course(connection, MANUAL, "Calculus III")
    connection.commit()
    yield connection
    connection.close()


def a_manual_post(conn, course_id, post_id=POST, drive_id=FILE):
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, content_hash, "
        "first_seen_at, creation_time) VALUES (?, ?, 'Board, 21 Sep', 'h', ?, ?)",
        (post_id, course_id, "2026-09-21T10:00:00Z", "2026-09-21T10:00:00Z"),
    )
    store.upsert_material(
        conn,
        Material(
            id=f"coursework_material:{post_id}:driveFile:{drive_id}",
            parent_type="coursework_material",
            parent_id=post_id,
            course_id=course_id,
            kind="driveFile",
            ref=drive_id,
            drive_id=drive_id,
            title="board.jpg",
            url=None,
            content_hash="h",
        ),
    )
    conn.commit()
    return f"coursework_material:{post_id}:driveFile:{drive_id}"


def test_a_manual_post_under_a_TRACKED_course_survives_reconciliation(conn):
    """The case that actually bites.

    A manual COURSE is never polled, so its rows are never reached. But a
    photographed board for UNIX -- a subject that IS tracked -- puts a manual
    post inside a tracked course, the live state legitimately does not contain
    it, and without the guard one sync deletes it.
    """
    material_id = a_manual_post(conn, "c1")

    # Exactly what the poller does at the end of a course: reconcile stored
    # rows against the live id set, which contains no manual anything.
    assert store.soft_delete_missing(conn, "coursework_materials", "c1", set()) == []
    assert store.soft_delete_missing(conn, "materials", "c1", set()) == []

    for table, key in (("coursework_materials", POST), ("materials", material_id)):
        row = conn.execute(
            f"SELECT deleted_at FROM {table} WHERE id = ?", (key,)
        ).fetchone()
        assert row["deleted_at"] is None, f"{table}: a sync deleted a manual row"


def test_a_vanished_classroom_row_is_still_soft_deleted(conn):
    """The control, so the guard cannot be widened into "never delete anything"."""
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, content_hash, "
        "first_seen_at) VALUES ('m1', 'c1', 'Slides 1', 'h', ?)",
        ("2026-09-01T10:00:00Z",),
    )
    conn.commit()

    assert store.soft_delete_missing(conn, "coursework_materials", "c1", set()) == ["m1"]
    row = conn.execute(
        "SELECT deleted_at FROM coursework_materials WHERE id = 'm1'"
    ).fetchone()
    assert row["deleted_at"] is not None


def test_a_manual_row_in_a_manual_course_survives_too(conn):
    """Belt as well as braces: reachable only if a manual id ever got tracked."""
    a_manual_post(conn, MANUAL)
    assert store.soft_delete_missing(conn, "coursework_materials", MANUAL, set()) == []


# --------------------------------------------------------------------------
# guard 3 -- Drive is never asked for a manual file
# --------------------------------------------------------------------------

def test_drive_references_omits_manual_files(conn):
    """Otherwise `agent fetch` hands manual-file-... to drive.files.get, takes
    the 404 as a dead reference, and rewrites a file that is on the disk from
    'fetched' to 'missing'."""
    a_manual_post(conn, "c1")
    store.upsert_material(
        conn,
        Material(
            id="coursework_material:m1:driveFile:1AbC",
            parent_type="coursework_material",
            parent_id="m1",
            course_id="c1",
            kind="driveFile",
            ref="1AbC",
            drive_id="1AbC",
            title="lecture.pdf",
            url=None,
            content_hash="h",
        ),
    )
    conn.commit()

    found = [row["drive_id"] for row in store.drive_references(conn, ["c1"])]
    assert found == ["1AbC"], "a manual file was offered to the Drive fetcher"

    # And with no course filter at all.
    everything = [row["drive_id"] for row in store.drive_references(conn)]
    assert FILE not in everything


# --------------------------------------------------------------------------
# the course row itself
# --------------------------------------------------------------------------

def test_a_manual_course_is_structurally_an_ordinary_course(conn):
    """Which is the whole reason the pipeline needs no branch for it."""
    row = store.get_course(conn, MANUAL)
    assert row["name"] == "Calculus III"
    assert row["course_state"] == "MANUAL"
    assert row["content_hash"]
    assert row["first_seen_at"]


def test_registering_twice_is_one_course(conn):
    assert store.ensure_manual_course(conn, MANUAL, "Calculus III") is False
    assert len(store.manual_courses(conn)) == 1


def test_registering_renames_rather_than_duplicating(conn):
    store.ensure_manual_course(conn, MANUAL, "Calculus 3")
    assert store.get_course(conn, MANUAL)["name"] == "Calculus 3"


def test_a_classroom_id_cannot_be_registered_as_manual(conn):
    with pytest.raises(ValueError, match="not a manual course id"):
        store.ensure_manual_course(conn, "840946407460", "Nanotechnology")


def test_manual_courses_lists_only_manual_ones(conn):
    assert [row["id"] for row in store.manual_courses(conn)] == [MANUAL]


# --------------------------------------------------------------------------
# agent subjects
# --------------------------------------------------------------------------

class KeepOpen:
    """A connection the command may 'close' without ending the test."""

    def __init__(self, connection):
        self._conn = connection

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


TIMETABLE = """
subjects:
  UNIX: "809686876412"
  Calculus III: null

versions:
  - label: S1
    status: provisional
    effective_from: 2026-09-15
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC, subject: UNIX }
      - { day: tue, start: "08:30", end: "10:00", kind: LEC,
          subject: Calculus III }
"""


@pytest.fixture
def cli_config(tmp_path, conn, monkeypatch):
    from agent.config import Config

    path = tmp_path / "timetable.yaml"
    path.write_text(TIMETABLE, encoding="utf-8")
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=tmp_path / "data",
        tracked_courses=["809686876412"],
        ignored_courses=[],
        timetable_path_override=path,
    )


def args(**kwargs):
    import argparse

    return argparse.Namespace(**{"add": None, "standing": False, **kwargs})


def test_subjects_lists_each_identity_with_what_to_do_about_it(cli_config, capsys):
    from agent import cli

    assert cli.cmd_subjects(cli_config, args()) == 0
    out = capsys.readouterr().out
    assert "809686876412" in out and "tracked" in out
    assert "awaiting a Classroom id" in out
    assert "agent subjects --add" in out


def test_add_mints_registers_and_prints_the_line_to_paste(cli_config, conn, capsys):
    """It does the half that needs the database and prints the half that needs
    the file. The timetable keeps one writer -- me."""
    from agent import cli

    before = cli_config.timetable_path.read_text(encoding="utf-8")
    assert cli.cmd_subjects(cli_config, args(add="Calculus III")) == 0

    out = capsys.readouterr().out
    assert "manual-calculus-iii" in out
    assert "Calculus III: manual-calculus-iii" in out
    assert store.get_course(conn, "manual-calculus-iii") is not None
    # And it did NOT touch the file.
    assert cli_config.timetable_path.read_text(encoding="utf-8") == before


def test_add_is_case_insensitive_but_never_approximate(cli_config, capsys):
    from agent import cli

    assert cli.cmd_subjects(cli_config, args(add="calculus iii")) == 0
    assert cli.cmd_subjects(cli_config, args(add="Calculus")) == 1
    err = capsys.readouterr().err
    assert "never matched approximately" in err
    assert "Known subjects" in err


def test_add_refuses_a_subject_that_already_has_a_classroom_course(cli_config, capsys):
    from agent import cli

    assert cli.cmd_subjects(cli_config, args(add="UNIX")) == 1
    assert "already maps to Classroom course" in capsys.readouterr().err
