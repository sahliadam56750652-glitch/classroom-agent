"""What tomorrow needs: subjects, not sessions.

The batching rule is the design constraint of the whole phase. ~20 sessions a
week across ~11 subjects means a per-session prompt would arrive three times a
day and be muted inside a fortnight, so a subject that meets twice tomorrow has
to collapse to one entry -- and a joint session has to expand to two.

The other property here is that readiness is never assumed. A post with pages
nothing has transcribed is reported as such, because quizzing on material the
agent has never read is the failure the gate exists to prevent.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from agent.classroom.models import Course, Material
from agent.db import store
from agent.gate import scheduler
from agent.gate import timetable as tt

TIMETABLE = """
subjects:
  DSA: "c-dsa"
  Database: "c-db"
  OS: "c-os"
  Stats: "c-stats"
  Calculus II: null

exceptions:
  - 2026-09-17

versions:
  - label: S1
    status: provisional
    effective_from: 2026-09-01
    effective_to: 2026-12-31
    sessions:
      # DSA twice on the Monday -- a lecture and a tutorial.
      - { day: mon, start: "08:30", end: "10:00", kind: LEC, subject: DSA,
          teacher: Khelifi, room: A1 }
      - { day: mon, start: "13:45", end: "15:15", kind: TUT, subject: DSA,
          teacher: Saidi, room: B2 }
      - { day: mon, start: "16:00", end: "17:00", kind: LEC,
          subject: Calculus II, teacher: Ben Salah }
      # A joint session on the Tuesday.
      - day: tue
        start: "10:30"
        end: "12:30"
        kind: LAB
        subject: Database
        teacher: Gharbi
        room: Lab3
        also:
          - { subject: OS, teacher: Mansour, room: Lab4 }
      # Thursday is the exception day.
      - { day: thu, start: "09:00", end: "10:00", kind: LEC, subject: DSA,
          teacher: Khelifi }
      - { day: fri, start: "09:00", end: "10:00", kind: LEC, subject: Stats,
          teacher: Jelassi }
"""

MONDAY = date(2026, 9, 14)
TUESDAY = date(2026, 9, 15)
THURSDAY = date(2026, 9, 17)     # in `exceptions`
FRIDAY = date(2026, 9, 18)
SUNDAY = date(2026, 9, 20)
BEFORE_TERM = date(2026, 8, 1)

TRACKED = ["c-dsa", "c-db", "c-os", "c-stats"]


@pytest.fixture
def table(tmp_path):
    path = tmp_path / "timetable.yaml"
    path.write_text(TIMETABLE, encoding="utf-8")
    return tt.load(path)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    for course_id, name in (
        ("c-dsa", "DSA- pi1A"),
        ("c-db", "Database GA 2026"),
        ("c-os", "Operating Systems"),
        ("c-stats", "Probability & Statistics"),
    ):
        store.upsert_course(
            connection,
            Course(
                id=course_id, name=name, section=None, room=None, owner_id=None,
                course_state="ACTIVE", enrollment_code=None, alternate_link=None,
                creation_time=None, update_time=None, content_hash="h",
            ),
        )
    yield connection
    connection.close()


def post(conn, *, course_id, parent_id, created, drive_id=None,
         pages=20, scan=0, ocr=0, chars=5000, state="pending",
         parent_type="coursework_material", title=None, extraction="ok"):
    """One post with an extracted attachment, and a study item for it."""
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, creation_time, "
        "content_hash, first_seen_at) VALUES (?, ?, ?, ?, 'h', 'now')"
        if parent_type == "coursework_material" else
        "INSERT INTO announcements (id, course_id, text, creation_time, "
        "content_hash, first_seen_at) VALUES (?, ?, ?, ?, 'h', 'now')",
        (parent_id, course_id, title if title is not None else f"Lecture {parent_id}", created),
    )
    if drive_id:
        store.upsert_material(
            conn,
            Material(
                id=f"{parent_type}:{parent_id}:driveFile:{drive_id}",
                parent_type=parent_type, parent_id=parent_id, course_id=course_id,
                kind="driveFile", ref=drive_id, drive_id=drive_id,
                title=f"{drive_id}.pdf", url=None, content_hash="h",
            ),
        )
        store.upsert_extraction(
            conn, drive_id, status=extraction, pages=pages, chars=chars,
            scan_pages=scan, ocr_pages=ocr, text_path=f"text/{drive_id}.txt",
            local_path=f"files/{drive_id}.pdf", size_bytes=1000,
        )
    store.ensure_study_item(
        conn, entity_type=parent_type, entity_id=parent_id,
        course_id=course_id, state=state,
        skip_reason="chose not to" if state == "skipped" else None,
        skip_source="user" if state == "skipped" else None,
    )
    conn.commit()


def plan(conn, table, day, tracked=TRACKED):
    return scheduler.plan_for(conn, tracked, table, day)


# --------------------------------------------------------------- batching

def test_a_subject_meeting_twice_tomorrow_is_one_entry(conn, table):
    """The design constraint. DSA has a lecture AND a tutorial on the Monday."""
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    result = plan(conn, table, MONDAY)

    dsa = [subject for subject in result.subjects if subject.name == "DSA"]
    assert len(dsa) == 1
    # ...but both of its sessions are still shown.
    assert len(dsa[0].sessions) == 2
    assert [session.kind for session in dsa[0].sessions] == ["LEC", "TUT"]


def test_a_subject_meeting_twice_counts_its_backlog_once(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    post(conn, course_id="c-dsa", parent_id="p2", created="2026-09-02T00:00:00Z",
         drive_id="d2")
    assert plan(conn, table, MONDAY).total_items == 2


def test_a_joint_session_gates_both_of_its_subjects(conn, table):
    post(conn, course_id="c-db", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    post(conn, course_id="c-os", parent_id="p2", created="2026-09-01T00:00:00Z",
         drive_id="d2")
    result = plan(conn, table, TUESDAY)

    assert [subject.name for subject in result.actionable] == ["Database", "OS"]
    assert result.total_items == 2


def test_subjects_are_ordered_by_when_they_first_meet(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    names = [subject.name for subject in plan(conn, table, MONDAY).subjects]
    assert names == ["DSA", "Calculus II"]      # 08:30 before 16:00


# --------------------------------------------------------------- silence

def test_an_exception_day_sends_nothing(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    result = plan(conn, table, THURSDAY)

    assert not result.worth_sending
    assert "listed as an exception" in result.silent_because
    assert result.subjects == ()


def test_a_date_no_version_covers_sends_nothing(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    result = plan(conn, table, BEFORE_TERM)

    assert not result.worth_sending
    assert "no timetable version" in result.silent_because


def test_a_day_with_no_sessions_sends_nothing(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    result = plan(conn, table, SUNDAY)

    assert not result.worth_sending
    assert "Sunday" in result.silent_because


def test_a_day_i_am_up_to_date_on_sends_nothing(conn, table):
    """Not news. Sending "you are fine" every evening is how it gets muted."""
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1", state="verified")
    result = plan(conn, table, MONDAY)

    assert result.subjects        # the day is still described
    assert not result.worth_sending


def test_verified_and_skipped_items_are_not_backlog(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1", state="verified")
    post(conn, course_id="c-dsa", parent_id="p2", created="2026-09-02T00:00:00Z",
         drive_id="d2", state="skipped")
    post(conn, course_id="c-dsa", parent_id="p3", created="2026-09-03T00:00:00Z",
         drive_id="d3", state="reviewed")
    # Only the reviewed one is still unfinished.
    assert plan(conn, table, MONDAY).total_items == 1


# --------------------------------------------------------------- readiness

def test_a_post_with_untranscribed_pages_is_not_ready(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1", pages=41, scan=14, ocr=2)
    subject = plan(conn, table, MONDAY).actionable[0]

    item = subject.next_item
    assert not item.ready
    assert item.unread == 12
    assert "12 of 41" in item.blocked_reason
    assert (subject.ready_count, subject.blocked_count) == (0, 1)


def test_a_fully_transcribed_post_is_ready(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1", pages=92, scan=14, ocr=14)
    subject = plan(conn, table, MONDAY).actionable[0]

    assert subject.next_item.ready
    assert subject.next_item.blocked_reason == ""


def test_a_post_with_almost_no_text_is_not_ready_either(conn, table):
    """Distinct from untranscribed: there is nothing there to read at all."""
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1", pages=1, scan=0, ocr=0, chars=40)
    item = plan(conn, table, MONDAY).actionable[0].next_item

    assert not item.ready
    assert "almost no readable text" in item.blocked_reason


def test_readiness_is_per_item_not_per_subject(conn, table):
    """Measured on the real data: one DSA post is 14/14 transcribed and another
    is 0/26. Blocking the whole subject would block one that is mostly ready."""
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1", pages=92, scan=14, ocr=14)
    post(conn, course_id="c-dsa", parent_id="p2", created="2026-09-02T00:00:00Z",
         drive_id="d2", pages=155, scan=26, ocr=0)
    subject = plan(conn, table, MONDAY).actionable[0]

    assert (subject.ready_count, subject.blocked_count) == (1, 1)
    assert subject.unread_pages == 26


def test_the_same_file_attached_twice_is_not_counted_twice(conn, table):
    """Summing straight through the join would double `unread` and block an
    item that is actually complete."""
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1", pages=10, scan=4, ocr=4)
    store.upsert_material(
        conn,
        Material(
            id="coursework_material:p1:driveFile:d1-again", parent_type="coursework_material",
            parent_id="p1", course_id="c-dsa", kind="driveFile", ref="d1-again",
            drive_id="d1", title="same file", url=None, content_hash="h",
        ),
    )
    conn.commit()
    item = plan(conn, table, MONDAY).actionable[0].next_item
    assert (item.pages, item.unread) == (10, 0)


# ------------------------------------------------- nothing readable at all

def test_a_course_with_no_study_items_is_not_up_to_date(conn, table):
    """Probability & Statistics: 20 of 20 attachments are 404, so it has no
    items. Reporting that as finished would be the most flattering lie here."""
    store.upsert_material(
        conn,
        Material(
            id="coursework_material:s1:driveFile:gone", parent_type="coursework_material",
            parent_id="s1", course_id="c-stats", kind="driveFile", ref="gone",
            drive_id="gone", title="Chapter 1", url=None, content_hash="h",
        ),
    )
    store.upsert_extraction(conn, "gone", status="missing", error="404")
    conn.commit()

    subject = [s for s in plan(conn, table, FRIDAY).subjects if s.name == "Stats"][0]
    assert not subject.has_items
    assert subject.dead_files == 1


def test_a_course_with_items_but_none_left_is_up_to_date(conn, table):
    post(conn, course_id="c-stats", parent_id="s1", created="2026-09-01T00:00:00Z",
         drive_id="d1", state="verified")
    subject = [s for s in plan(conn, table, FRIDAY).subjects if s.name == "Stats"][0]
    assert subject.has_items
    assert subject.items == ()


# --------------------------------------------------------------- mapping

def test_a_subject_with_no_course_is_listed_but_never_gated(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    result = plan(conn, table, MONDAY)

    calculus = [s for s in result.subjects if s.name == "Calculus II"][0]
    assert not calculus.gated
    assert calculus not in result.actionable


def test_a_mapped_but_untracked_course_is_treated_as_untracked(conn, table):
    """Nothing syncs it, so its backlog would always read empty. Saying "up to
    date" about a course that is not even being fetched would be a lie."""
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    result = plan(conn, table, MONDAY, tracked=["c-db"])

    dsa = [s for s in result.subjects if s.name == "DSA"][0]
    assert not dsa.gated


# --------------------------------------------------------------- ordering

def test_the_oldest_post_is_served_first(conn, table):
    post(conn, course_id="c-dsa", parent_id="new", created="2026-09-09T00:00:00Z",
         drive_id="d2")
    post(conn, course_id="c-dsa", parent_id="old", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    assert plan(conn, table, MONDAY).actionable[0].next_item.entity_id == "old"


def test_the_oldest_is_served_even_when_a_newer_one_is_readable(conn, table):
    """Not reordered to make a quiz possible: that would mean revising out of
    order, and would hide the backlog OCR has not caught up with."""
    post(conn, course_id="c-dsa", parent_id="old", created="2026-09-01T00:00:00Z",
         drive_id="d1", pages=20, scan=5, ocr=0)
    post(conn, course_id="c-dsa", parent_id="new", created="2026-09-05T00:00:00Z",
         drive_id="d2", pages=20, scan=0, ocr=0)
    item = plan(conn, table, MONDAY).actionable[0].next_item

    assert item.entity_id == "old"
    assert not item.ready


def test_an_announcement_is_labelled_from_its_body(conn, table):
    post(conn, course_id="c-dsa", parent_id="a1", created="2026-09-01T00:00:00Z",
         drive_id="d1", parent_type="announcement",
         title="Slides for tomorrow's session are attached\nsecond line")
    assert plan(conn, table, MONDAY).actionable[0].next_item.label == (
        "Slides for tomorrow's session are attached"
    )


# --------------------------------------------------------------- the stored plan

def test_the_stored_plan_keeps_keyboard_order(conn, table):
    post(conn, course_id="c-db", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    post(conn, course_id="c-os", parent_id="p2", created="2026-09-01T00:00:00Z",
         drive_id="d2")
    result = plan(conn, table, TUESDAY)

    stored = scheduler.stored_subjects(result.to_json())
    assert [entry["name"] for entry in stored] == ["Database", "OS"]
    assert [entry["course_id"] for entry in stored] == ["c-db", "c-os"]


def test_the_stored_plan_records_the_items_it_offered(conn, table):
    post(conn, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    post(conn, course_id="c-dsa", parent_id="p2", created="2026-09-02T00:00:00Z",
         drive_id="d2")
    stored = scheduler.stored_subjects(plan(conn, table, MONDAY).to_json())
    assert len(stored[0]["item_ids"]) == 2


def test_subject_at_resolves_an_index_and_tolerates_a_stale_one(conn, table):
    post(conn, course_id="c-db", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1")
    result = plan(conn, table, TUESDAY)

    assert result.subject_at(0).name == "Database"
    assert result.subject_at(7) is None
    assert result.subject_at(-1) is None


def test_a_broken_stored_plan_yields_no_subjects_rather_than_raising():
    assert scheduler.stored_subjects("not json") == []
    assert scheduler.stored_subjects(json.dumps({"subjects": "nope"})) == []
