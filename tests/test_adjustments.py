"""Resolving the pattern against what actually happened to a date.

The property this file protects: **a move changes which evening gates it.** The
gate fires the night before a session, so a lecture moved from Monday to
Wednesday must be prompted on Tuesday evening and not on Sunday. Everything
else here is an edge that would otherwise resolve silently and wrongly.

Dates used throughout: 2026-09-21 is a Monday, 2026-09-23 a Wednesday.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.classroom.models import parse_course
from agent.db import store
from agent.gate import adjustments as adjust
from agent.gate import scheduler, timetable as tt

UNIX = "809686876412"
MANUAL = "manual-calculus-iii"

PATTERN = """
subjects:
  Calculus III: manual-calculus-iii
  UNIX: "809686876412"
  Database: "842149328479"
  OS: "840878703017"

exceptions:
  - 2026-10-15

versions:
  - label: S1
    status: provisional
    effective_from: 2026-09-15
    effective_to: 2026-12-18
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC,
          subject: Calculus III, teacher: Yassine, room: "17" }
      - { day: mon, start: "14:45", end: "16:15", kind: TUT,
          subject: Calculus III, teacher: Taieb }
      - { day: tue, start: "13:00", end: "14:30", kind: LEC,
          subject: UNIX, teacher: Firas, room: "14" }
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

MONDAY = date(2026, 9, 21)
WEDNESDAY = date(2026, 9, 23)
HOLIDAY = date(2026, 10, 15)
AFTER_TERM = date(2027, 2, 1)


@pytest.fixture
def table(tmp_path):
    path = tmp_path / "timetable.yaml"
    path.write_text(PATTERN, encoding="utf-8")
    return tt.load(path)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    store.upsert_course(connection, parse_course({"id": UNIX, "name": "UNIX"}))
    store.ensure_manual_course(connection, MANUAL, "Calculus III")
    connection.commit()
    yield connection
    connection.close()


def adjust_row(conn, **kwargs):
    kwargs.setdefault("session_start", "08:30")
    kwargs.setdefault("subject", "Calculus III")
    result = store.add_adjustment(conn, **kwargs)
    conn.commit()
    return result


def subjects_on(conn, table, day):
    return [
        item.session.subjects for item in adjust.sessions_on(conn, table, day)
    ]


# --------------------------------------------------------------------------
# the pattern, unadjusted
# --------------------------------------------------------------------------

def test_with_no_adjustments_the_pattern_is_the_answer(conn, table):
    resolved = adjust.sessions_on(conn, table, MONDAY)
    assert [item.session.start for item in resolved] == ["08:30", "14:45"]
    assert not any(item.adjusted for item in resolved)
    assert all(item.note == "" for item in resolved)


# --------------------------------------------------------------------------
# cancelled
# --------------------------------------------------------------------------

def test_a_cancellation_removes_the_session(conn, table):
    adjust_row(conn, applies_on="2026-09-21", kind="cancelled")
    assert [item.session.start for item in adjust.sessions_on(conn, table, MONDAY)] == [
        "14:45"
    ]


def test_a_cancellation_touches_only_its_own_date(conn, table):
    adjust_row(conn, applies_on="2026-09-21", kind="cancelled")
    next_monday = date(2026, 9, 28)
    assert len(adjust.sessions_on(conn, table, next_monday)) == 2


def test_cancelling_a_joint_session_cancels_the_session(conn, table):
    """A JOINT is one session, in one room, at one time, serving two subjects.
    The adjustment names one of them to identify it; the thing that does not
    happen is the session."""
    assert subjects_on(conn, table, WEDNESDAY) == [("Database", "OS")]

    adjust_row(conn, applies_on="2026-09-23", kind="cancelled",
               subject="Database", session_start="10:30")
    assert adjust.sessions_on(conn, table, WEDNESDAY) == ()


def test_either_subject_of_a_joint_session_identifies_it(conn, table):
    """Naming the second subject must find the same session, not miss it."""
    adjust_row(conn, applies_on="2026-09-23", kind="cancelled",
               subject="OS", session_start="10:30")
    assert adjust.sessions_on(conn, table, WEDNESDAY) == ()


# --------------------------------------------------------------------------
# moved -- and which evening gates it
# --------------------------------------------------------------------------

def test_a_move_to_another_date_leaves_the_original_date(conn, table):
    adjust_row(conn, applies_on="2026-09-21", kind="moved",
               to_date="2026-09-23", to_start="16:00")
    assert [item.session.start for item in adjust.sessions_on(conn, table, MONDAY)] == [
        "14:45"
    ]


def test_a_move_to_another_date_arrives_there(conn, table):
    adjust_row(conn, applies_on="2026-09-21", kind="moved",
               to_date="2026-09-23", to_start="16:00")

    landed = [
        item for item in adjust.sessions_on(conn, table, WEDNESDAY)
        if "Calculus III" in item.session.subjects
    ]
    assert len(landed) == 1
    assert landed[0].session.start == "16:00"
    assert landed[0].session.day == "wed"
    assert landed[0].moved_from == MONDAY
    assert "moved from Mon 21 Sep" in landed[0].note


def test_a_move_within_the_day_keeps_the_date(conn, table):
    adjust_row(conn, applies_on="2026-09-21", kind="moved", to_start="11:00")
    resolved = adjust.sessions_on(conn, table, MONDAY)
    assert [item.session.start for item in resolved] == ["11:00", "14:45"]
    assert resolved[0].note == "moved from 08:30"


def test_a_move_keeps_the_sessions_length_when_no_end_is_given(conn, table):
    """A lecture moved from 08:30 to 13:00 is still ninety minutes, and making
    me retype that is friction with no answer attached."""
    adjust_row(conn, applies_on="2026-09-21", kind="moved", to_start="13:00")
    moved = adjust.sessions_on(conn, table, MONDAY)[0].session
    assert (moved.start, moved.end) == ("13:00", "14:30")


def test_an_explicit_end_wins_over_the_derived_one(conn, table):
    adjust_row(conn, applies_on="2026-09-21", kind="moved",
               to_start="13:00", to_end="13:45")
    moved = adjust.sessions_on(conn, table, MONDAY)[0].session
    assert (moved.start, moved.end) == ("13:00", "13:45")


def test_a_move_can_change_the_room_and_the_kind(conn, table):
    adjust_row(conn, applies_on="2026-09-21", kind="moved",
               to_start="13:00", to_room="A12", to_kind="TUT")
    moved = adjust.sessions_on(conn, table, MONDAY)[0].session
    assert moved.kind == "TUT"
    assert [part.room for part in moved.parts] == ["A12"]
    # The teacher is untouched: a moved session is the same session.
    assert [part.teacher for part in moved.parts] == ["Yassine"]


def test_a_move_keeps_both_halves_of_a_joint_session(conn, table):
    adjust_row(conn, applies_on="2026-09-23", kind="moved", subject="Database",
               session_start="10:30", to_date="2026-09-24", to_start="09:00")
    thursday = date(2026, 9, 24)
    landed = adjust.sessions_on(conn, table, thursday)
    assert [item.session.subjects for item in landed] == [("Database", "OS")]
    assert landed[0].session.joint


def test_the_gate_follows_the_move_to_the_new_evening(conn, table):
    """The property the whole layer exists for. A lecture moved from Tuesday to
    Wednesday is prompted on Tuesday evening, not on Monday evening.

    UNIX rather than Calculus III, because Calculus III meets twice on Monday:
    moving one of its sessions away correctly leaves the subject on the day,
    and this test is about the subject leaving it entirely.
    """
    tuesday = date(2026, 9, 22)
    adjust_row(conn, applies_on="2026-09-22", kind="moved", subject="UNIX",
               session_start="13:00", to_date="2026-09-23", to_start="16:00")

    gone = scheduler.plan_for(conn, {MANUAL, UNIX}, table, tuesday)
    assert "UNIX" not in [s.name for s in gone.subjects]

    arrived = scheduler.plan_for(conn, {MANUAL, UNIX}, table, WEDNESDAY)
    assert "UNIX" in [s.name for s in arrived.subjects]


def test_moving_one_of_a_subjects_two_sessions_leaves_the_subject_in_place(
    conn, table
):
    """Calculus III meets twice on Monday. Moving the 08:30 lecture away does
    not take the 14:45 tutorial with it, and the gate still covers the day."""
    adjust_row(conn, applies_on="2026-09-21", kind="moved",
               to_date="2026-09-23", to_start="16:00")

    plan = scheduler.plan_for(conn, {MANUAL, UNIX}, table, MONDAY)
    assert [s.name for s in plan.subjects] == ["Calculus III"]
    assert [session.start for session in plan.sessions] == ["14:45"]


# --------------------------------------------------------------------------
# extra
# --------------------------------------------------------------------------

def test_an_extra_session_appears_where_the_pattern_has_none(conn, table):
    friday = date(2026, 9, 25)
    assert adjust.sessions_on(conn, table, friday) == ()

    adjust_row(conn, applies_on="2026-09-25", kind="extra", subject="UNIX",
               session_start="09:00", to_start="09:00", to_end="10:30",
               to_kind="LAB", to_teacher="Firas", to_room="Lab1")

    resolved = adjust.sessions_on(conn, table, friday)
    assert len(resolved) == 1
    session = resolved[0].session
    assert (session.subjects, session.start, session.end) == (("UNIX",), "09:00", "10:30")
    assert session.kind == "LAB"
    assert session.day == "fri"
    assert resolved[0].note == "added"


def test_an_extra_session_carries_its_subjects_course(conn, table):
    """So the gate can serve its backlog like any other session."""
    adjust_row(conn, applies_on="2026-09-25", kind="extra", subject="Calculus III",
               session_start="09:00", to_start="09:00", to_end="10:30")
    session = adjust.sessions_on(conn, table, date(2026, 9, 25))[0].session
    assert session.parts[0].course_id == MANUAL


def test_an_extra_session_is_gated(conn, table):
    adjust_row(conn, applies_on="2026-09-25", kind="extra", subject="UNIX",
               session_start="09:00", to_start="09:00", to_end="10:30")
    plan = scheduler.plan_for(conn, {MANUAL, UNIX}, table, date(2026, 9, 25))
    assert [s.name for s in plan.subjects] == ["UNIX"]


# --------------------------------------------------------------------------
# edge: a date the pattern cannot describe
# --------------------------------------------------------------------------

def test_a_session_moved_to_a_date_with_no_version_still_happens(conn, table):
    """The professor scheduled it there. A resolution that silently dropped it
    would be the failure this whole layer exists to prevent, one step along."""
    assert table.version_for(AFTER_TERM) is None

    adjust_row(conn, applies_on="2026-09-21", kind="moved",
               to_date=AFTER_TERM.isoformat(), to_start="10:00")

    resolved = adjust.sessions_on(conn, table, AFTER_TERM)
    assert [item.session.subjects for item in resolved] == [("Calculus III",)]
    assert resolved[0].moved_from == MONDAY


def test_the_gate_opens_for_a_session_moved_past_the_end_of_term(conn, table):
    """plan_for used to return silence before it ever looked at the sessions."""
    adjust_row(conn, applies_on="2026-09-21", kind="moved",
               to_date=AFTER_TERM.isoformat(), to_start="10:00")

    plan = scheduler.plan_for(conn, {MANUAL, UNIX}, table, AFTER_TERM)
    assert plan.silent_because == ""
    assert [s.name for s in plan.subjects] == ["Calculus III"]


def test_a_session_moved_onto_a_holiday_still_happens(conn, table):
    """A makeup class during a reading week is a real thing, and the adjustment
    is both more specific and more recent than the blanket rule."""
    assert table.sessions_on(HOLIDAY) == ()

    adjust_row(conn, applies_on="2026-10-12", kind="moved",
               to_date=HOLIDAY.isoformat(), to_start="10:00")

    assert [item.session.subjects for item in adjust.sessions_on(conn, table, HOLIDAY)] == [
        ("Calculus III",)
    ]


def test_an_extra_session_on_a_holiday_still_happens(conn, table):
    adjust_row(conn, applies_on=HOLIDAY.isoformat(), kind="extra", subject="UNIX",
               session_start="09:00", to_start="09:00", to_end="10:30")
    assert len(adjust.sessions_on(conn, table, HOLIDAY)) == 1


def test_an_untouched_holiday_is_still_silent(conn, table):
    """The control. Without it, the rule above could be "exceptions do nothing"."""
    assert adjust.sessions_on(conn, table, HOLIDAY) == ()
    plan = scheduler.plan_for(conn, {MANUAL, UNIX}, table, HOLIDAY)
    assert "no sessions" in plan.silent_because


def test_a_day_emptied_by_adjustments_says_so_specifically(conn, table):
    """"No sessions on a Monday" would be a wrong answer to the question."""
    adjust_row(conn, applies_on="2026-09-21", kind="cancelled", session_start="08:30")
    adjust_row(conn, applies_on="2026-09-21", kind="cancelled", session_start="14:45")

    plan = scheduler.plan_for(conn, {MANUAL, UNIX}, table, MONDAY)
    assert plan.silent_because == "every session on this date is cancelled or moved away"


# --------------------------------------------------------------------------
# edge: the file was edited underneath a stored adjustment
# --------------------------------------------------------------------------

def renamed(tmp_path, old="Calculus III", new="Calculus 3"):
    text = PATTERN.replace(old, new)
    path = tmp_path / "renamed.yaml"
    path.write_text(text, encoding="utf-8")
    return tt.load(path)


def test_a_renamed_subject_orphans_its_adjustment_rather_than_losing_it(
    conn, table, tmp_path
):
    """It survives: still stored, still visible, and reported as no longer
    matching. What it must never do is quietly adjust something else."""
    adjust_row(conn, applies_on="2026-09-21", kind="cancelled",
               course_id=MANUAL, reason="professor away")

    after = renamed(tmp_path)
    # The pattern is untouched -- the cancellation is not applied to anything.
    assert len(adjust.sessions_on(conn, after, MONDAY)) == 2
    # And the row is still there, named as an orphan.
    found = adjust.orphans(conn, after)
    assert len(found) == 1
    assert "no longer has a subject called 'Calculus III'" in found[0].why
    assert store.get_adjustment(conn, 1) is not None


def test_an_orphan_says_what_the_subject_is_called_now(conn, table, tmp_path):
    """"Calculus III is now called Calculus 3" is the whole answer, and making
    me work it out is a waste of the fact."""
    adjust_row(conn, applies_on="2026-09-21", kind="cancelled", course_id=MANUAL)
    found = adjust.orphans(conn, renamed(tmp_path))
    assert found[0].renamed_to == "Calculus 3"


def test_repointing_an_orphan_makes_it_apply_again(conn, table, tmp_path):
    adjustment_id = adjust_row(conn, applies_on="2026-09-21", kind="cancelled",
                               course_id=MANUAL)
    after = renamed(tmp_path)
    store.repoint_adjustment(conn, adjustment_id, subject="Calculus 3",
                             course_id=MANUAL)
    conn.commit()

    assert adjust.orphans(conn, after) == []
    assert [item.session.start for item in adjust.sessions_on(conn, after, MONDAY)] == [
        "14:45"
    ]


def test_an_adjustment_whose_session_moved_in_the_file_is_an_orphan(conn, table, tmp_path):
    """Not only renames. Editing the pattern's start time strands it too, and
    for the same reason: it now names nothing."""
    adjust_row(conn, applies_on="2026-09-21", kind="cancelled", session_start="08:30")

    path = tmp_path / "retimed.yaml"
    path.write_text(PATTERN.replace('start: "08:30", end: "10:00"',
                                    'start: "09:00", end: "10:30"'), encoding="utf-8")
    after = tt.load(path)

    found = adjust.orphans(conn, after)
    assert len(found) == 1
    assert "no Calculus III session at 08:30" in found[0].why
    assert len(adjust.sessions_on(conn, after, MONDAY)) == 2


def test_an_extra_for_a_renamed_subject_is_an_orphan_too(conn, table, tmp_path):
    """It cannot be built at all -- there is no subject to give it a course."""
    adjust_row(conn, applies_on="2026-09-25", kind="extra", session_start="09:00",
               to_start="09:00", to_end="10:30", course_id=MANUAL)
    after = renamed(tmp_path)

    assert adjust.sessions_on(conn, after, date(2026, 9, 25)) == ()
    assert len(adjust.orphans(conn, after)) == 1


def test_a_matching_adjustment_is_never_an_orphan(conn, table):
    """The control, so `orphans` cannot degrade into "everything"."""
    adjust_row(conn, applies_on="2026-09-21", kind="cancelled")
    adjust_row(conn, applies_on="2026-09-25", kind="extra", subject="UNIX",
               session_start="09:00", to_start="09:00", to_end="10:30")
    assert adjust.orphans(conn, table) == []


# --------------------------------------------------------------------------
# the gate message says why a day differs
# --------------------------------------------------------------------------

def test_the_prompt_says_a_session_was_moved_in(conn, table):
    """Without it, a lecture moved into tomorrow reads as the gate having
    invented one."""
    from agent.gate import messages

    adjust_row(conn, applies_on="2026-09-21", kind="moved",
               to_date="2026-09-23", to_start="16:00")
    plan = scheduler.plan_for(conn, {MANUAL, UNIX}, table, WEDNESDAY)
    text = messages.compose(plan)
    assert "moved from Mon 21 Sep" in text


def test_an_unadjusted_prompt_gains_no_note(conn, table):
    from agent.gate import messages

    plan = scheduler.plan_for(conn, {MANUAL, UNIX}, table, MONDAY)
    assert "moved" not in messages.compose(plan)
