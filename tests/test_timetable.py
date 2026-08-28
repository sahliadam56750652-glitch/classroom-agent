"""My week, loaded from YAML.

Two properties are load-bearing and everything else here supports them. A
subject name is never matched approximately, because a wrong match gates the
wrong subject and looks like success. And every way the timetable can say
"nothing today" -- Sunday, a holiday, a date no version covers -- produces
silence rather than a guess.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.gate import timetable as tt

# One valid file, reused. Tests that need something broken edit a copy of the
# text rather than restating the whole thing.
VALID = """
subjects:
  Database: "842149328479"
  OS: "840878703017"
  DSA: "806468143345"
  Calculus II: null

exceptions:
  - 2026-10-15
  - { from: 2026-12-21, to: 2027-01-03, reason: winter break }

versions:
  - label: S1 provisional v1
    status: provisional
    effective_from: 2026-09-14
    effective_to: 2026-11-30
    sessions:
      - { day: mon, start: "12:00", end: "13:30", kind: LEC,
          subject: Database, teacher: Gharbi, room: A12 }
      - { day: mon, start: "08:30", end: "10:00", kind: LEC,
          subject: Calculus II, teacher: Ben Salah, room: A12 }
      - { day: thu, start: "14:45", end: "16:45", kind: Project,
          subject: DSA, teacher: Khelifi }
      - day: wed
        start: "10:30"
        end: "12:30"
        kind: LAB
        subject: Database
        teacher: Gharbi
        room: Lab3
        also:
          - { subject: OS, teacher: Mansour, room: Lab4 }
  - label: S1 confirmed
    status: confirmed
    effective_from: 2026-12-01
    effective_to: null
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC,
          subject: OS, teacher: Mansour, room: C01 }
"""


def write(tmp_path, text=VALID, name="timetable.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def table(tmp_path):
    return tt.load(write(tmp_path))


# --------------------------------------------------------------- the mapping

def test_subject_names_map_to_course_ids(table):
    assert table.course_for("Database") == "842149328479"


def test_unmapped_subject_is_legal_and_reported(table):
    assert table.course_for("Calculus II") is None
    assert table.subjects_without_course() == ["Calculus II"]


def test_numeric_course_id_becomes_a_string(tmp_path):
    """YAML reads a bare 842149328479 as an int; every comparison is on strings."""
    text = VALID.replace('Database: "842149328479"', "Database: 842149328479")
    assert tt.load(write(tmp_path, text)).course_for("Database") == "842149328479"


def test_unknown_subject_is_an_error_not_a_near_match(tmp_path):
    """'Database GA 2026' must not quietly resolve to 'Database'."""
    text = VALID.replace("subject: Database, teacher: Gharbi, room: A12",
                         "subject: Database GA 2026, teacher: Gharbi, room: A12")
    with pytest.raises(tt.TimetableError) as err:
        tt.load(write(tmp_path, text))
    assert "Database GA 2026" in str(err.value)
    assert "not in the 'subjects' map" in str(err.value)


# --------------------------------------------------------------- joint sessions

def test_joint_session_has_two_parts_with_their_own_teachers(table):
    session = table.sessions_on(date(2026, 9, 16))[0]   # a Wednesday
    assert session.joint
    assert session.subjects == ("Database", "OS")
    assert [part.teacher for part in session.parts] == ["Gharbi", "Mansour"]
    assert [part.room for part in session.parts] == ["Lab3", "Lab4"]
    assert session.course_ids == ("842149328479", "840878703017")


def test_also_entry_inherits_the_session_room(tmp_path):
    text = VALID.replace("- { subject: OS, teacher: Mansour, room: Lab4 }",
                         "- { subject: OS, teacher: Mansour }")
    session = tt.load(write(tmp_path, text)).sessions_on(date(2026, 9, 16))[0]
    assert [part.room for part in session.parts] == ["Lab3", "Lab3"]


def test_a_single_subject_session_still_has_one_part(table):
    session = table.sessions_on(date(2026, 9, 17))[0]   # Thursday, DSA
    assert not session.joint
    assert len(session.parts) == 1
    assert session.parts[0].room is None


def test_the_same_subject_twice_in_one_session_is_an_error(tmp_path):
    text = VALID.replace("- { subject: OS, teacher: Mansour, room: Lab4 }",
                         "- { subject: Database, teacher: Mansour, room: Lab4 }")
    with pytest.raises(tt.TimetableError, match="twice in one session"):
        tt.load(write(tmp_path, text))


# --------------------------------------------------------------- sessions_on

def test_sessions_are_ordered_by_start_time(table):
    monday = table.sessions_on(date(2026, 9, 14))
    assert [session.start for session in monday] == ["08:30", "12:00"]


def test_afternoon_slots_sort_after_morning_ones(tmp_path):
    """'HH:MM' is zero-padded, so a string sort is a time sort. Pinned so that
    nobody 'improves' it into something that needs parsing."""
    text = VALID.replace('{ day: thu, start: "14:45", end: "16:45"',
                         '{ day: mon, start: "14:45", end: "16:45"')
    starts = [s.start for s in tt.load(write(tmp_path, text)).sessions_on(date(2026, 9, 14))]
    assert starts == ["08:30", "12:00", "14:45"]


def test_sunday_has_no_sessions(table):
    assert table.sessions_on(date(2026, 9, 20)) == ()


def test_a_single_date_exception_silences_the_day(table):
    # 2026-10-15 is a Thursday, which otherwise has the DSA project session.
    assert table.sessions_on(date(2026, 10, 15)) == ()
    assert table.sessions_on(date(2026, 10, 8)) != ()


def test_a_range_exception_covers_its_interior_and_both_ends(table):
    for day in (date(2026, 12, 21), date(2026, 12, 28), date(2027, 1, 3)):
        assert table.exception_for(day) is not None, day
        assert table.sessions_on(day) == ()
    assert table.exception_for(date(2026, 12, 20)) is None
    assert table.exception_for(date(2027, 1, 4)) is None


def test_a_date_no_version_covers_yields_nothing(table):
    """Over the summer the gate says nothing rather than guessing a version."""
    assert table.version_for(date(2026, 8, 28)) is None
    assert table.sessions_on(date(2026, 8, 28)) == ()


def test_the_right_version_wins_on_each_side_of_the_boundary(table):
    assert table.version_for(date(2026, 11, 30)).label == "S1 provisional v1"
    assert table.version_for(date(2026, 12, 1)).label == "S1 confirmed"
    # A Monday under each version has a different subject on it.
    assert table.sessions_on(date(2026, 11, 30))[0].subjects == ("Calculus II",)
    assert table.sessions_on(date(2026, 12, 7))[0].subjects == ("OS",)


def test_provisional_status_is_readable(table):
    assert table.version_for(date(2026, 9, 14)).provisional
    assert not table.version_for(date(2026, 12, 7)).provisional


def test_an_open_ended_version_never_expires(table):
    assert table.version_for(date(2030, 1, 1)).label == "S1 confirmed"


# --------------------------------------------------------------- validation

def test_overlapping_versions_are_rejected(tmp_path):
    """Last-one-wins would answer silently a question the file does not settle."""
    text = VALID.replace("effective_to: 2026-11-30", "effective_to: 2026-12-15")
    with pytest.raises(tt.TimetableError) as err:
        tt.load(write(tmp_path, text))
    assert "both cover" in str(err.value)


def test_an_earlier_open_ended_version_overlaps_everything_after_it(tmp_path):
    text = VALID.replace("effective_to: 2026-11-30", "effective_to: null")
    with pytest.raises(tt.TimetableError, match="open-ended"):
        tt.load(write(tmp_path, text))


def test_sunday_is_rejected_by_name(tmp_path):
    text = VALID.replace("day: thu, start:", "day: sun, start:")
    with pytest.raises(tt.TimetableError) as err:
        tt.load(write(tmp_path, text))
    assert "Monday to Saturday" in str(err.value)


def test_an_unquoted_time_says_to_quote_it(tmp_path):
    """YAML 1.1 reads a bare 12:00 as the integer 720 -- a session at a time
    nobody wrote, which would otherwise be entirely silent."""
    text = VALID.replace('start: "12:00"', "start: 12:00")
    with pytest.raises(tt.TimetableError) as err:
        tt.load(write(tmp_path, text))
    assert "must be quoted" in str(err.value)
    assert '"12:00"' in str(err.value)


def test_an_unquoted_morning_time_survives_and_that_is_the_trap(tmp_path):
    """08:30 unquoted stays a string, because YAML's sexagesimal pattern will
    not start on a zero. So quoting the mornings and not the afternoons LOOKS
    like it works. Pinned so the asymmetry is a recorded fact, not a surprise."""
    text = VALID.replace('start: "08:30"', "start: 08:30")
    session = tt.load(write(tmp_path, text)).sessions_on(date(2026, 9, 14))[0]
    assert session.start == "08:30"


def test_end_before_start_is_rejected(tmp_path):
    text = VALID.replace('start: "12:00", end: "13:30"', 'start: "13:30", end: "12:00"')
    with pytest.raises(tt.TimetableError, match="is not after"):
        tt.load(write(tmp_path, text))


def test_an_unknown_kind_is_rejected(tmp_path):
    text = VALID.replace("kind: Project", "kind: Seminar")
    with pytest.raises(tt.TimetableError, match="'kind' must be one of"):
        tt.load(write(tmp_path, text))


def test_a_bad_status_is_rejected(tmp_path):
    text = VALID.replace("status: provisional", "status: draft")
    with pytest.raises(tt.TimetableError, match="'status' must be one of"):
        tt.load(write(tmp_path, text))


def test_effective_to_before_effective_from_is_rejected(tmp_path):
    text = VALID.replace("effective_to: 2026-11-30", "effective_to: 2026-09-01")
    with pytest.raises(tt.TimetableError, match="is before"):
        tt.load(write(tmp_path, text))


def test_a_missing_file_names_the_example(tmp_path):
    with pytest.raises(tt.TimetableError) as err:
        tt.load(tmp_path / "nope.yaml")
    assert "timetable.example.yaml" in str(err.value)


def test_an_empty_file_is_rejected(tmp_path):
    with pytest.raises(tt.TimetableError, match="is empty"):
        tt.load(write(tmp_path, "\n"))


def test_broken_yaml_is_reported_as_such(tmp_path):
    with pytest.raises(tt.TimetableError, match="not valid YAML"):
        tt.load(write(tmp_path, "subjects: [\n"))


def test_no_versions_is_rejected(tmp_path):
    with pytest.raises(tt.TimetableError, match="'versions' must be a non-empty list"):
        tt.load(write(tmp_path, "subjects:\n  OS: \"1\"\nversions: []\n"))


# --------------------------------------------------------------- warnings

def test_unmapped_and_untracked_subjects_are_warnings_not_errors(table):
    notes = " | ".join(tt.warnings(table, ["842149328479"]))
    assert "Calculus II" in notes            # mapped to null
    assert "OS (840878703017)" in notes      # mapped but not in courses.tracked
    assert "DSA" in notes


def test_a_fully_tracked_timetable_still_warns_about_the_null_subject(table):
    notes = tt.warnings(table, ["842149328479", "840878703017", "806468143345"])
    assert len(notes) == 1
    assert "never gated" in notes[0]


def test_a_mapped_subject_that_never_meets_is_reported(tmp_path):
    text = VALID.replace('  DSA: "806468143345"',
                         '  DSA: "806468143345"\n  Linear Algebra: "999"')
    table = tt.load(write(tmp_path, text))
    notes = " | ".join(tt.warnings(table, []))
    assert "never meet in any version" in notes
    assert "Linear Algebra" in notes


def test_the_shipped_example_file_loads(tmp_path):
    """The example is documentation people copy; a broken one is worse than none."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "timetable.example.yaml"
    table = tt.load(example)
    assert table.sessions_on(date(2026, 9, 16))[0].joint
    assert table.sessions_on(date(2026, 10, 15)) == ()


def test_a_bare_date_exception_gets_a_reason_that_reads_as_a_cause(table):
    """It is printed after "no sessions:", so "no sessions" would say nothing."""
    assert table.exception_for(date(2026, 10, 15)).reason == tt.DEFAULT_REASON
    assert table.exception_for(date(2026, 12, 28)).reason == "winter break"
