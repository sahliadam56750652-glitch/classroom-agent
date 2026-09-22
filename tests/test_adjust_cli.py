"""`agent adjust`, and the day view that explains why a day differs.

Two things this command must never do: write `timetable.yaml`, and guess which
session I meant. The first is the Phase 3a constraint -- the file holds the
weekly pattern and keeps one writer. The second is the same rule that governs
subject names, one level down: a subject meeting twice on a Monday is two
sessions, and adjusting the wrong one is a failure that looks like success.
"""

from __future__ import annotations

import argparse
from datetime import date

import pytest

from agent import cli
from agent.classroom.models import parse_course
from agent.config import Config
from agent.db import store
from agent.gate import timetable as tt

MANUAL = "manual-calculus-iii"
NETWORKS = "842149328479"

PATTERN = """
subjects:
  Calculus III: manual-calculus-iii
  Computer Networks: "842149328479"
  Database: "842149328479"
  OS: "840878703017"

exceptions:
  - 2026-10-15

versions:
  - label: S1 2026-27
    status: provisional
    effective_from: 2026-09-15
    effective_to: 2027-01-23
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC,
          subject: Calculus III, teacher: Yassine, room: "17" }
      - { day: mon, start: "14:45", end: "16:15", kind: TUT,
          subject: Calculus III, teacher: Taieb }
      - { day: tue, start: "13:00", end: "14:30", kind: LEC,
          subject: Computer Networks, teacher: Mohsen, room: "14" }
      - day: wed
        start: "10:30"
        end: "12:30"
        kind: LAB
        subject: Database
        teacher: Gharbi
        room: Lab3
        also:
          - { subject: OS, teacher: Mansour, room: Lab4 }
"""


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
    path.write_text(PATTERN, encoding="utf-8")
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=data_dir,
        tracked_courses=[NETWORKS],
        ignored_courses=[],
        timetable_path_override=path,
    )


@pytest.fixture
def conn(config, monkeypatch):
    connection = store.connect(config.db_path)
    store.upsert_course(connection, parse_course({"id": NETWORKS, "name": "Networks"}))
    store.ensure_manual_course(connection, MANUAL, "Calculus III")
    connection.commit()
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(connection))
    yield connection
    connection.close()


def args(**kwargs):
    return argparse.Namespace(**{
        "cancel": False, "move": False, "extra": False, "subject": None,
        "on": None, "at": None, "to_date": None, "to_time": None, "end": None,
        "kind": None, "room": None, "teacher": None, "reason": None,
        "remove": None, "repoint": None, "include_past": False, "dry_run": False,
        **kwargs,
    })


def timetable_args(**kwargs):
    return argparse.Namespace(**{"check": False, "on": None, **kwargs})


# --------------------------------------------------------------------------
# refuse, do not guess
# --------------------------------------------------------------------------

def test_an_ambiguous_session_is_refused_with_the_times(config, conn, capsys):
    """Calculus III meets twice on Monday. Adjusting the wrong one is a failure
    that looks exactly like success."""
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21")) == 1
    err = capsys.readouterr().err
    assert "meets 2 times" in err
    assert "08:30, 14:45" in err
    assert "--at" in err


def test_naming_the_session_resolves_it(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30")) == 0
    assert store.list_adjustments(conn)[0]["session_start"] == "08:30"


def test_a_time_with_no_session_is_refused_and_lists_what_is_there(
    config, conn, capsys
):
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="11:00")) == 1
    err = capsys.readouterr().err
    assert "no Calculus III session at 11:00" in err
    assert "08:30, 14:45" in err


def test_a_subject_with_no_session_that_day_points_at_extra(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Computer Networks", on="2026-09-21")) == 1
    err = capsys.readouterr().err
    assert "no Computer Networks session on Mon 21 Sep" in err
    assert "--extra" in err


def test_an_unknown_subject_is_never_matched_approximately(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus", on="2026-09-21")) == 1
    err = capsys.readouterr().err
    assert "never matched approximately" in err
    assert "Known subjects" in err


def test_two_kinds_at_once_is_refused(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        cancel=True, move=True, subject="Calculus III", on="2026-09-21")) == 1
    assert "three different things" in capsys.readouterr().err


def test_a_second_adjustment_on_one_session_names_the_first(config, conn, capsys):
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    capsys.readouterr()

    assert cli.cmd_adjust(config, args(
        move=True, subject="Calculus III", on="2026-09-21", at="08:30",
        to_time="11:00")) == 1
    out = capsys.readouterr().out
    assert "already adjusted: #1 cancelled" in out
    assert "--remove 1" in out
    assert len(store.list_adjustments(conn)) == 1


def test_a_move_with_nowhere_to_go_is_refused(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        move=True, subject="Calculus III", on="2026-09-21", at="08:30")) == 1
    assert "--move needs --to" in capsys.readouterr().err


def test_a_malformed_time_is_refused_by_name(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="8h30")) == 1
    assert '--at must look like "08:30"' in capsys.readouterr().err


def test_a_malformed_date_is_refused_by_name(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="next monday")) == 1
    assert "--on must be a date" in capsys.readouterr().err


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def test_the_timetable_file_is_never_written(config, conn):
    """The Phase 3a constraint: the file holds the pattern and keeps one writer."""
    before = config.timetable_path.read_text(encoding="utf-8")
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    cli.cmd_adjust(config, args(
        move=True, subject="Computer Networks", on="2026-09-22",
        to_date="2026-09-24", to_time="16:00"))
    cli.cmd_adjust(config, args(
        extra=True, subject="Database", on="2026-09-25", to_time="09:00"))
    assert config.timetable_path.read_text(encoding="utf-8") == before


def test_cancelling_a_joint_session_says_both_are_affected(config, conn, capsys):
    """"I cancelled Database" reading as "and OS with it" is worth a sentence."""
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Database", on="2026-09-23")) == 0
    out = capsys.readouterr().out
    assert "this session is Database + OS -- both are affected" in out


def test_a_cross_date_move_says_which_evening_now_gates_it(config, conn, capsys):
    """The consequence I would otherwise have to work out, and the one that
    matters: the prompt follows the session."""
    assert cli.cmd_adjust(config, args(
        move=True, subject="Computer Networks", on="2026-09-22",
        to_date="2026-09-24", to_time="16:00")) == 0
    out = capsys.readouterr().out
    assert "the prompt for it moves to Wed 23 Sep evening" in out


def test_a_move_within_the_day_mentions_no_new_evening(config, conn, capsys):
    cli.cmd_adjust(config, args(
        move=True, subject="Calculus III", on="2026-09-21", at="08:30",
        to_time="11:00"))
    assert "the prompt for it moves" not in capsys.readouterr().out


def test_an_extra_session_records_what_it_was_given(config, conn):
    cli.cmd_adjust(config, args(
        extra=True, subject="Calculus III", on="2026-09-25", to_time="09:00",
        end="10:30", kind="TUT", teacher="Taieb", room="A12"))
    row = store.list_adjustments(conn)[0]
    assert (row["kind"], row["to_start"], row["to_end"]) == ("extra", "09:00", "10:30")
    assert (row["to_kind"], row["to_teacher"], row["to_room"]) == ("TUT", "Taieb", "A12")
    assert row["course_id"] == MANUAL


def test_an_extra_with_no_time_is_refused(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        extra=True, subject="Calculus III", on="2026-09-25")) == 1
    assert "--extra needs --to-time" in capsys.readouterr().err


def test_a_reason_is_kept(config, conn):
    """How often a session moves is a fact about the semester worth having, and
    a file edited in place would never have answered it."""
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30",
        reason="professor at a conference"))
    assert store.list_adjustments(conn)[0]["reason"] == "professor at a conference"


def test_dry_run_writes_nothing(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30",
        dry_run=True)) == 0
    assert "dry run -- nothing written" in capsys.readouterr().out
    assert store.list_adjustments(conn) == []


def test_dry_run_still_performs_every_refusal(config, conn, capsys):
    assert cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", dry_run=True)) == 1
    assert "meets 2 times" in capsys.readouterr().err


# --------------------------------------------------------------------------
# listing and removing
# --------------------------------------------------------------------------

def test_an_empty_list_shows_a_subject_from_my_own_timetable(config, conn, capsys):
    assert cli.cmd_adjust(config, args()) == 0
    out = capsys.readouterr().out
    assert "No adjustments recorded" in out
    assert "Calculus III" in out


def test_the_listing_shows_each_kind_readably(config, conn, capsys):
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    cli.cmd_adjust(config, args(
        move=True, subject="Computer Networks", on="2026-09-22",
        to_date="2026-09-24", to_time="16:00"))
    cli.cmd_adjust(config, args(
        extra=True, subject="Database", on="2026-09-25", to_time="09:00",
        end="10:30"))
    capsys.readouterr()

    assert cli.cmd_adjust(config, args(include_past=True)) == 0
    out = capsys.readouterr().out
    assert "cancelled" in out
    assert "-> 2026-09-24 16:00" in out
    assert "extra, 09:00-10:30" in out


def test_listing_one_date_shows_the_day_as_adjusted(config, conn, capsys):
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    capsys.readouterr()

    assert cli.cmd_adjust(config, args(on="2026-09-21")) == 0
    out = capsys.readouterr().out
    assert "Mon 21 Sep 2026 -- as adjusted" in out
    assert "14:45-16:15" in out
    assert "08:30-10:00" not in out.split("--", 1)[0]


def test_removing_says_the_pattern_reasserts_itself(config, conn, capsys):
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    capsys.readouterr()

    assert cli.cmd_adjust(config, args(remove=1)) == 0
    out = capsys.readouterr().out
    assert "removed: cancelled Calculus III" in out
    assert "nothing in the file ever moved" in out
    assert store.list_adjustments(conn) == []


def test_removing_one_that_does_not_exist_is_an_error(config, conn, capsys):
    assert cli.cmd_adjust(config, args(remove=99)) == 1
    assert "no adjustment with id 99" in capsys.readouterr().err


# --------------------------------------------------------------------------
# surviving an edit to timetable.yaml
# --------------------------------------------------------------------------

def rename(config, old="Calculus III", new="Calculus 3"):
    config.timetable_path.write_text(
        PATTERN.replace(old, new), encoding="utf-8"
    )


def test_a_renamed_subject_is_reported_with_the_command_that_fixes_it(
    config, conn, capsys
):
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    capsys.readouterr()
    rename(config)

    assert cli.cmd_adjust(config, args(include_past=True)) == 0
    out = capsys.readouterr().out
    assert "no longer match" in out
    assert "are NOT applied" in out
    assert "now called 'Calculus 3'" in out
    assert 'agent adjust --repoint 1 --subject "Calculus 3"' in out


def test_repointing_makes_it_apply_again(config, conn, capsys):
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    rename(config)
    capsys.readouterr()

    assert cli.cmd_adjust(config, args(repoint=1, subject="Calculus 3")) == 0
    assert "repointed 1" in capsys.readouterr().out

    assert cli.cmd_adjust(config, args(on="2026-09-21")) == 0
    out = capsys.readouterr().out
    assert "no longer match" not in out
    assert "14:45-16:15" in out


def test_repointing_needs_a_subject_that_exists(config, conn, capsys):
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    capsys.readouterr()

    assert cli.cmd_adjust(config, args(repoint=1, subject="Nonexistent")) == 1
    assert "is not a subject in" in capsys.readouterr().err

    assert cli.cmd_adjust(config, args(repoint=1)) == 1
    assert "--repoint needs --subject" in capsys.readouterr().err


# --------------------------------------------------------------------------
# agent timetable --on
# --------------------------------------------------------------------------

def test_the_day_view_marks_a_session_that_moved_in(config, conn, capsys):
    cli.cmd_adjust(config, args(
        move=True, subject="Computer Networks", on="2026-09-22",
        to_date="2026-09-24", to_time="16:00"))
    capsys.readouterr()

    assert cli.cmd_timetable(config, timetable_args(on="2026-09-24")) == 0
    out = capsys.readouterr().out
    assert "16:00-17:30" in out
    assert "(moved from Tue 22 Sep 13:00)" in out


def test_the_day_view_shows_what_is_not_happening(config, conn, capsys):
    """A cancelled lecture that simply vanishes leaves the day unexplained, and
    an unexplained day is indistinguishable from a bug in the resolver."""
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30",
        reason="professor away"))
    capsys.readouterr()

    assert cli.cmd_timetable(config, timetable_args(on="2026-09-21")) == 0
    out = capsys.readouterr().out
    assert "not happening:" in out
    assert "08:30-10:00" in out
    assert "cancelled -- professor away" in out


def test_a_day_emptied_by_adjustments_still_shows_what_left_it(config, conn, capsys):
    cli.cmd_adjust(config, args(cancel=True, subject="Database", on="2026-09-23"))
    capsys.readouterr()

    assert cli.cmd_timetable(config, timetable_args(on="2026-09-23")) == 0
    out = capsys.readouterr().out
    assert "nothing left on this day" in out
    assert "Database + OS" in out


def test_a_day_the_move_left_shows_where_it_went(config, conn, capsys):
    cli.cmd_adjust(config, args(
        move=True, subject="Computer Networks", on="2026-09-22",
        to_date="2026-09-24", to_time="16:00"))
    capsys.readouterr()

    cli.cmd_timetable(config, timetable_args(on="2026-09-22"))
    out = capsys.readouterr().out
    assert "moved to Thu 24 Sep 16:00" in out


def test_an_untouched_day_reads_exactly_as_before(config, conn, capsys):
    """The control. Nothing about the day view changes for a day nothing
    happened to."""
    assert cli.cmd_timetable(config, timetable_args(on="2026-09-21")) == 0
    out = capsys.readouterr().out
    assert "08:30-10:00" in out and "14:45-16:15" in out
    assert "not happening" not in out
    assert "moved" not in out


def test_the_day_view_reports_orphans_too(config, conn, capsys):
    cli.cmd_adjust(config, args(
        cancel=True, subject="Calculus III", on="2026-09-21", at="08:30"))
    rename(config)
    capsys.readouterr()

    cli.cmd_timetable(config, timetable_args(on="2026-09-21"))
    out = capsys.readouterr().out
    assert "are NOT applied" in out
    # And the pattern is untouched: nothing was adjusted by a near-match.
    assert "not happening" not in out


def test_an_extra_session_shows_in_the_day_view(config, conn, capsys):
    cli.cmd_adjust(config, args(
        extra=True, subject="Calculus III", on="2026-09-25", to_time="09:00",
        end="10:30", kind="TUT"))
    capsys.readouterr()

    cli.cmd_timetable(config, timetable_args(on="2026-09-25"))
    out = capsys.readouterr().out
    assert "09:00-10:30" in out
    assert "(added)" in out
