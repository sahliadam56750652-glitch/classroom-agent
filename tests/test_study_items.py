"""Creating the revision backlog.

One study item per POST, not per attachment: a lecture with slides and a
handout is one thing to revise. The schema settles this -- study_items is
UNIQUE (entity_type, entity_id) over the three parent kinds -- and these tests
pin the two properties that make the gate trustworthy later: progress is never
reset by a re-run, and a skip is recorded as a skip.
"""

from __future__ import annotations

import argparse

import pytest

from agent import cli
from agent.classroom.models import Course, Material
from agent.config import Config
from agent.db import store


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
    for course_id, name in (("c1", "Operating Systems"), ("c2", "Untracked")):
        store.upsert_course(
            connection,
            Course(
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
            ),
        )
    yield connection
    connection.close()


def attach(conn, drive_id, *, parent_id="p1", parent_type="coursework_material",
           course_id="c1", status="ok"):
    """One attachment on one post, with a recorded extraction outcome."""
    store.upsert_material(
        conn,
        Material(
            id=f"{parent_type}:{parent_id}:driveFile:{drive_id}",
            parent_type=parent_type,
            parent_id=parent_id,
            course_id=course_id,
            kind="driveFile",
            ref=drive_id,
            drive_id=drive_id,
            title=f"{drive_id}.pdf",
            url=None,
            content_hash="h",
        ),
    )
    store.upsert_extraction(conn, drive_id, status=status)


def args(**overrides):
    base = {"dry_run": False, "seed": False, "force": False, "reopen": None}
    base.update(overrides)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------
# what becomes a study item
# --------------------------------------------------------------------------


def test_one_item_per_post_not_per_attachment(config, conn):
    """Slides plus a handout on one lecture is one thing to revise."""
    attach(conn, "slides", parent_id="lecture-1")
    attach(conn, "handout", parent_id="lecture-1")

    created, seen = cli._do_studyitems(config, conn)

    assert (created, seen) == (1, 1)
    (row,) = conn.execute("SELECT * FROM study_items").fetchall()
    assert row["entity_id"] == "lecture-1"
    assert row["state"] == "pending"


def test_a_post_whose_only_file_is_dead_yields_nothing(config, conn):
    """20 of 118 tracked references are dead. Those posts have nothing to revise."""
    attach(conn, "binned", parent_id="lecture-1", status="trashed")
    attach(conn, "gone", parent_id="lecture-2", status="missing")
    attach(conn, "zipped", parent_id="lecture-3", status="unsupported")

    created, seen = cli._do_studyitems(config, conn)

    assert (created, seen) == (0, 0)


def test_a_post_with_one_live_file_among_dead_ones_still_counts(config, conn):
    attach(conn, "binned", parent_id="lecture-1", status="trashed")
    attach(conn, "readable", parent_id="lecture-1", status="ok")

    assert cli._do_studyitems(config, conn)[0] == 1


def test_only_tracked_courses_produce_items(config, conn):
    attach(conn, "mine", parent_id="mine", course_id="c1")
    attach(conn, "theirs", parent_id="theirs", course_id="c2")

    cli._do_studyitems(config, conn)

    (row,) = conn.execute("SELECT course_id FROM study_items").fetchall()
    assert row["course_id"] == "c1"


def test_all_three_parent_kinds_are_eligible(config, conn):
    """Announcements carry material too -- 211 of 375 attachments account-wide."""
    for parent_type in ("coursework", "coursework_material", "announcement"):
        attach(conn, f"f-{parent_type}", parent_id=parent_type, parent_type=parent_type)

    assert cli._do_studyitems(config, conn)[0] == 3


def test_a_soft_deleted_post_is_not_revisable(config, conn):
    attach(conn, "f1", parent_id="lecture-1")
    store.soft_delete_missing(conn, "materials", "c1", live_ids=[])

    assert cli._do_studyitems(config, conn)[0] == 0


# --------------------------------------------------------------------------
# idempotence and progress
# --------------------------------------------------------------------------


def test_a_second_run_creates_nothing(config, conn):
    attach(conn, "f1", parent_id="lecture-1")
    cli._do_studyitems(config, conn)

    assert cli._do_studyitems(config, conn)[0] == 0
    assert store.count_rows(conn, "study_items") == 1


@pytest.mark.parametrize("state", ["delivered", "verified", "skipped"])
def test_a_re_run_never_undoes_progress(config, conn, state):
    """The one thing in this database that cannot be rebuilt from the API."""
    attach(conn, "f1", parent_id="lecture-1")
    cli._do_studyitems(config, conn)
    conn.execute("UPDATE study_items SET state = ?", (state,))
    conn.commit()

    cli._do_studyitems(config, conn)

    assert conn.execute("SELECT state FROM study_items").fetchone()["state"] == state


def test_a_new_attachment_on_a_revised_post_does_not_reopen_it(config, conn):
    attach(conn, "f1", parent_id="lecture-1")
    cli._do_studyitems(config, conn)
    conn.execute("UPDATE study_items SET state = 'verified'")
    conn.commit()

    attach(conn, "f2", parent_id="lecture-1")
    cli._do_studyitems(config, conn)

    assert conn.execute("SELECT state FROM study_items").fetchone()["state"] == "verified"


# --------------------------------------------------------------------------
# seeding the historical backlog
# --------------------------------------------------------------------------


def test_seed_records_the_backlog_as_skipped_with_a_reason(config, conn):
    """All five tracked courses are a finished year. ~90 pending items would be a lie."""
    for index in range(3):
        attach(conn, f"f{index}", parent_id=f"lecture-{index}")

    created, _seen = cli._do_studyitems(config, conn, seed=True)

    assert created == 3
    assert store.count_study_items_by_state(conn) == {"skipped": 3}
    rows = conn.execute("SELECT skip_reason FROM study_items").fetchall()
    assert all(row["skip_reason"] == cli.SEED_SKIP_REASON for row in rows)


def test_seed_never_writes_verified(config, conn):
    """A gate that quietly forgives makes the coverage number a lie."""
    attach(conn, "f1", parent_id="lecture-1")

    cli._do_studyitems(config, conn, seed=True)

    assert "verified" not in store.count_study_items_by_state(conn)


def test_seed_is_refused_once_items_exist(config, conn):
    attach(conn, "f1", parent_id="lecture-1")
    cli._do_studyitems(config, conn)

    with pytest.raises(cli.SeedWouldBuryBacklog):
        cli._do_studyitems(config, conn, seed=True)


def test_force_allows_seeding_anyway(config, conn):
    attach(conn, "f1", parent_id="lecture-1")
    cli._do_studyitems(config, conn)

    # Already 'pending', so DO NOTHING keeps it -- force lifts the refusal, it
    # does not rewrite history.
    created, _seen = cli._do_studyitems(config, conn, seed=True, force=True)
    assert created == 0


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------


def test_dry_run_reports_without_writing(config, conn):
    attach(conn, "f1", parent_id="lecture-1")
    attach(conn, "f2", parent_id="lecture-2")

    created, seen = cli._do_studyitems(config, conn, dry_run=True)

    assert (created, seen) == (2, 2)
    assert store.count_rows(conn, "study_items") == 0


def test_dry_run_counts_only_what_is_new(config, conn):
    attach(conn, "f1", parent_id="lecture-1")
    cli._do_studyitems(config, conn)
    attach(conn, "f2", parent_id="lecture-2")

    created, seen = cli._do_studyitems(config, conn, dry_run=True)

    assert (created, seen) == (1, 2)


class KeepOpen:
    """A connection whose close() does nothing.

    cmd_studyitems closes what it was handed, which would leave the test unable
    to inspect the database afterwards. sqlite3.Connection.close is read-only
    and cannot be patched, so it gets wrapped instead -- the same wrapper
    test_notify_cli.py needs for cmd_run.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def test_the_command_prints_and_exits_zero(config, conn, monkeypatch, capsys):
    attach(conn, "f1", parent_id="lecture-1")
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))

    assert cli.cmd_studyitems(config, args()) == 0
    assert "pending" in capsys.readouterr().out
    assert store.count_rows(conn, "study_items") == 1


# --------------------------------------------------------------------------
# --reopen: the only way back out of skipped
# --------------------------------------------------------------------------

def skipped_item(conn, entity_id="lecture-1", *, source="seed", reason=cli.SEED_SKIP_REASON):
    store.ensure_study_item(
        conn,
        entity_type="coursework_material",
        entity_id=entity_id,
        course_id="c1",
        state="skipped",
        skip_reason=reason,
        skip_source=source,
    )
    return conn.execute(
        "SELECT id FROM study_items WHERE entity_id = ?", (entity_id,)
    ).fetchone()["id"]


def test_seeding_records_where_the_skip_came_from(conn, config):
    """A seeded backlog and a skip I chose must stay distinguishable: Phase 4
    excludes a finished academic year from its coverage denominator, and must
    not also exclude the times I ducked the gate."""
    attach(conn, "f1", parent_id="lecture-1")
    cli._do_studyitems(config, conn, seed=True)

    row = conn.execute("SELECT * FROM study_items").fetchone()
    assert row["state"] == "skipped"
    assert row["skip_source"] == "seed"


def test_creating_normally_records_no_skip_source(conn, config):
    attach(conn, "f1", parent_id="lecture-1")
    cli._do_studyitems(config, conn)

    row = conn.execute("SELECT * FROM study_items").fetchone()
    assert (row["state"], row["skip_source"], row["skip_reason"]) == ("pending", None, None)


def test_reopen_returns_a_skipped_item_to_the_queue(conn):
    item_id = skipped_item(conn)
    assert store.reopen_study_item(conn, item_id) is True

    row = store.get_study_item(conn, item_id)
    assert row["state"] == "pending"
    assert row["skip_reason"] is None
    assert row["skip_source"] is None


def test_reopen_keeps_delivered_at(conn):
    """The material really was delivered. Forgetting that would misreport the
    history rather than reset it."""
    item_id = skipped_item(conn)
    conn.execute(
        "UPDATE study_items SET delivered_at = '2026-01-03T00:00:00Z' WHERE id = ?",
        (item_id,),
    )
    store.reopen_study_item(conn, item_id)
    assert store.get_study_item(conn, item_id)["delivered_at"] == "2026-01-03T00:00:00Z"


def test_reopen_refuses_anything_that_is_not_skipped(conn):
    """Narrow on purpose: this is not a general state setter, and in particular
    it is not a way to reach 'verified' without a passed quiz."""
    item_id = skipped_item(conn)
    for state in ("pending", "delivered", "reviewed", "verified"):
        conn.execute("UPDATE study_items SET state = ? WHERE id = ?", (state, item_id))
        assert store.reopen_study_item(conn, item_id) is False
        assert store.get_study_item(conn, item_id)["state"] == state


def test_reopen_reports_a_missing_id_separately_from_a_wrong_state(conn, capsys):
    """'0 reopened' would not distinguish the two, and they mean different
    things: a typo, versus an item that was never skipped."""
    item_id = skipped_item(conn)
    conn.execute("UPDATE study_items SET state = 'verified' WHERE id = ?", (item_id,))

    assert cli._do_reopen(conn, [item_id, 9999]) == 0
    captured = capsys.readouterr()
    assert "no such study item" in captured.err
    assert "already verified" in captured.out


def test_reopen_dry_run_writes_nothing(conn, capsys):
    item_id = skipped_item(conn)
    assert cli._do_reopen(conn, [item_id], dry_run=True) == 1
    assert "would reopen" in capsys.readouterr().out
    assert store.get_study_item(conn, item_id)["state"] == "skipped"


def test_the_reopen_command_exits_nonzero_when_something_was_missed(
    conn, config, monkeypatch
):
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    item_id = skipped_item(conn)

    assert cli.cmd_studyitems(config, args(reopen=[item_id])) == 0
    assert cli.cmd_studyitems(config, args(reopen=[9999])) == 1


def test_reopening_does_not_also_create_items(conn, config, monkeypatch):
    """Seeding a backlog and un-seeding part of it in one invocation would be
    two opposite verbs in one breath."""
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    item_id = skipped_item(conn)
    attach(conn, "f2", parent_id="lecture-2")

    cli.cmd_studyitems(config, args(reopen=[item_id]))
    assert store.count_rows(conn, "study_items") == 1
