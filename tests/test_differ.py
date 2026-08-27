"""Differ: what counts as a change, and what kind of news it is."""

from __future__ import annotations

import pytest

from agent.classroom.models import (
    parse_announcement,
    parse_course,
    parse_coursework,
    parse_coursework_material,
    parse_submission,
)
from agent.db import store
from agent.sync.differ import (
    diff_announcement,
    diff_coursework,
    diff_coursework_material,
    diff_submission,
)

NOW = "2026-08-27T12:00:00Z"


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    store.upsert_course(connection, parse_course({"id": "c1", "name": "Databases"}))
    yield connection
    connection.close()


def stored_coursework(conn, raw):
    work, _ = parse_coursework(raw, "c1")
    store.upsert_coursework(conn, work)
    return store.load_rows(conn, "coursework", "c1")[work.id]


def stored_submission(conn, raw, coursework_raw=None):
    work, _ = parse_coursework(coursework_raw or {"id": "w1", "title": "TD"}, "c1")
    store.upsert_coursework(conn, work)
    submission = parse_submission(raw)
    store.upsert_submission(conn, submission)
    return store.load_rows(conn, "submissions", "c1")[submission.id]


# --------------------------------------------------------------------------
# coursework
# --------------------------------------------------------------------------

def test_unseen_coursework_is_new():
    work, _ = parse_coursework({"id": "w1", "title": "TD 3"}, "c1")
    (event,) = diff_coursework(None, work, created_at=NOW)

    assert event.type == "new_coursework"
    assert event.entity_id == "w1"
    assert event.course_id == "c1"


def test_identical_coursework_is_silent(conn):
    raw = {"id": "w1", "title": "TD 3", "description": "Do it"}
    row = stored_coursework(conn, raw)
    work, _ = parse_coursework(raw, "c1")

    assert diff_coursework(row, work, created_at=NOW) == []


def test_changed_update_time_alone_is_silent(conn):
    """Classroom bumps updateTime on edits that never reach a student."""
    row = stored_coursework(
        conn, {"id": "w1", "title": "TD 3", "updateTime": "2026-01-01T00:00:00Z"}
    )
    work, _ = parse_coursework(
        {"id": "w1", "title": "TD 3", "updateTime": "2026-08-27T09:00:00Z"}, "c1"
    )

    assert diff_coursework(row, work, created_at=NOW) == []


def test_changed_due_date_gives_exactly_one_due_date_changed(conn):
    row = stored_coursework(
        conn, {"id": "w1", "title": "TD 3", "dueDate": {"year": 2026, "month": 5, "day": 1}}
    )
    work, _ = parse_coursework(
        {"id": "w1", "title": "TD 3", "dueDate": {"year": 2026, "month": 5, "day": 8}}, "c1"
    )

    events = diff_coursework(row, work, created_at=NOW)

    assert [event.type for event in events] == ["due_date_changed"]
    assert events[0].payload["before"] == "2026-05-01T23:59:59Z"
    assert events[0].payload["after"] == "2026-05-08T23:59:59Z"


def test_a_deadline_appearing_is_a_due_date_change(conn):
    row = stored_coursework(conn, {"id": "w1", "title": "TD 3"})
    work, _ = parse_coursework(
        {"id": "w1", "title": "TD 3", "dueDate": {"year": 2026, "month": 5, "day": 8}}, "c1"
    )

    (event,) = diff_coursework(row, work, created_at=NOW)

    assert event.type == "due_date_changed"
    assert event.payload["before"] is None


def test_title_change_is_an_update_not_a_due_date_change(conn):
    row = stored_coursework(conn, {"id": "w1", "title": "TD 3"})
    work, _ = parse_coursework({"id": "w1", "title": "TD 3 (corrected)"}, "c1")

    events = diff_coursework(row, work, created_at=NOW)

    assert [event.type for event in events] == ["coursework_updated"]
    assert events[0].payload["changed"] == ["title"]


def test_due_date_and_title_both_moving_gives_both_events(conn):
    row = stored_coursework(
        conn, {"id": "w1", "title": "TD 3", "dueDate": {"year": 2026, "month": 5, "day": 1}}
    )
    work, _ = parse_coursework(
        {"id": "w1", "title": "TD 4", "dueDate": {"year": 2026, "month": 6, "day": 1}}, "c1"
    )

    types = {event.type for event in diff_coursework(row, work, created_at=NOW)}

    assert types == {"due_date_changed", "coursework_updated"}


def test_coursework_gaining_an_attachment_sets_the_flag(conn):
    """The hash covers material IDs, so a hash change must never be silent."""
    row = stored_coursework(conn, {"id": "w1", "title": "TD 3"})
    work, live = parse_coursework(
        {"id": "w1", "title": "TD 3",
         "materials": [{"driveFile": {"driveFile": {"id": "d1", "title": "x.pdf"}}}]},
        "c1",
    )

    (event,) = diff_coursework(
        row, work, created_at=NOW, live_material_ids={m.id for m in live}
    )

    assert event.type == "coursework_updated"
    assert event.payload["attachments_added"] is True
    assert event.payload["added_count"] == 1
    assert event.payload["text_changed"] is False
    # No scalar field moved, so the coursework-specific detail is empty.
    assert event.payload["changed"] == []


def test_coursework_max_points_change_is_kept_in_changed(conn):
    """The coursework-specific detail survives alongside the shared keys."""
    row = stored_coursework(conn, {"id": "w1", "title": "TD 3", "maxPoints": 20})
    work, _ = parse_coursework({"id": "w1", "title": "TD 3", "maxPoints": 25}, "c1")

    (event,) = diff_coursework(row, work, created_at=NOW)

    assert event.payload["changed"] == ["max_points"]
    assert event.payload["attachments_added"] is False
    assert event.payload["text_changed"] is False


# --------------------------------------------------------------------------
# posted material and announcements
# --------------------------------------------------------------------------

def test_unseen_posted_material_is_new():
    material, _ = parse_coursework_material({"id": "m1", "title": "Slides"}, "c1")
    (event,) = diff_coursework_material(None, material, created_at=NOW)
    assert event.type == "new_material"


def test_unseen_announcement_is_new():
    announcement, _ = parse_announcement({"id": "a1", "text": "Week 3"}, "c1")
    (event,) = diff_announcement(None, announcement, created_at=NOW)
    assert event.type == "new_announcement"


DRIVE = {"driveFile": {"driveFile": {"id": "d1", "title": "slides.pdf"}}}
DRIVE2 = {"driveFile": {"driveFile": {"id": "d2", "title": "exercises.pdf"}}}


def stored_announcement(conn, raw):
    announcement, materials = parse_announcement(raw, "c1")
    store.upsert_announcement(conn, announcement)
    store.upsert_materials(conn, materials)
    row = store.load_rows(conn, "announcements", "c1")[announcement.id]
    return row, {material.id for material in materials}


def stored_posted_material(conn, raw):
    material, attachments = parse_coursework_material(raw, "c1")
    store.upsert_coursework_material(conn, material)
    store.upsert_materials(conn, attachments)
    row = store.load_rows(conn, "coursework_materials", "c1")[material.id]
    return row, {attachment.id for attachment in attachments}


def test_unchanged_announcement_emits_nothing(conn):
    raw = {"id": "a1", "text": "Week 3", "materials": [DRIVE]}
    row, stored_ids = stored_announcement(conn, raw)
    same, live = parse_announcement(raw, "c1")

    assert diff_announcement(
        row, same, created_at=NOW,
        stored_material_ids=stored_ids,
        live_material_ids={m.id for m in live},
    ) == []


def test_announcement_gaining_an_attachment_sets_the_flag(conn):
    """The main way real content reaches me: 211 of 375 attachments are here."""
    row, stored_ids = stored_announcement(conn, {"id": "a1", "text": "Week 3"})
    updated, live = parse_announcement(
        {"id": "a1", "text": "Week 3", "materials": [DRIVE]}, "c1"
    )

    (event,) = diff_announcement(
        row, updated, created_at=NOW,
        stored_material_ids=stored_ids,
        live_material_ids={m.id for m in live},
    )

    assert event.type == "announcement_updated"
    assert event.payload["attachments_added"] is True
    assert event.payload["added_count"] == 1
    assert event.payload["text_changed"] is False


def test_announcement_text_edit_leaves_the_flag_clear(conn):
    """A typo fix is noise; the digest has to be able to tell it apart."""
    row, stored_ids = stored_announcement(
        conn, {"id": "a1", "text": "Week 3", "materials": [DRIVE]}
    )
    updated, live = parse_announcement(
        {"id": "a1", "text": "Week 3 (moved to Friday)", "materials": [DRIVE]}, "c1"
    )

    (event,) = diff_announcement(
        row, updated, created_at=NOW,
        stored_material_ids=stored_ids,
        live_material_ids={m.id for m in live},
    )

    assert event.type == "announcement_updated"
    assert event.payload["attachments_added"] is False
    assert event.payload["added_count"] == 0
    assert event.payload["text_changed"] is True


def test_announcement_losing_an_attachment_is_not_an_addition(conn):
    row, stored_ids = stored_announcement(
        conn, {"id": "a1", "text": "Week 3", "materials": [DRIVE, DRIVE2]}
    )
    updated, live = parse_announcement(
        {"id": "a1", "text": "Week 3", "materials": [DRIVE]}, "c1"
    )

    (event,) = diff_announcement(
        row, updated, created_at=NOW,
        stored_material_ids=stored_ids,
        live_material_ids={m.id for m in live},
    )

    assert event.payload["attachments_added"] is False
    assert event.payload["removed_count"] == 1


def test_announcement_swapping_attachments_counts_both_ways(conn):
    row, stored_ids = stored_announcement(
        conn, {"id": "a1", "text": "Week 3", "materials": [DRIVE]}
    )
    updated, live = parse_announcement(
        {"id": "a1", "text": "Week 3", "materials": [DRIVE2]}, "c1"
    )

    (event,) = diff_announcement(
        row, updated, created_at=NOW,
        stored_material_ids=stored_ids,
        live_material_ids={m.id for m in live},
    )

    assert (event.payload["added_count"], event.payload["removed_count"]) == (1, 1)
    assert event.payload["attachments_added"] is True


def test_new_announcement_reports_its_attachment_count(conn):
    announcement, live = parse_announcement(
        {"id": "a1", "text": "Week 3", "materials": [DRIVE, DRIVE2]}, "c1"
    )

    (event,) = diff_announcement(
        None, announcement, created_at=NOW, live_material_ids={m.id for m in live}
    )

    assert event.type == "new_announcement"
    assert event.payload["attachment_count"] == 2


# -- the same three cases for posted material ------------------------------

def test_unchanged_posted_material_emits_nothing(conn):
    raw = {"id": "m1", "title": "Slides", "materials": [DRIVE]}
    row, stored_ids = stored_posted_material(conn, raw)
    same, live = parse_coursework_material(raw, "c1")

    assert diff_coursework_material(
        row, same, created_at=NOW,
        stored_material_ids=stored_ids,
        live_material_ids={m.id for m in live},
    ) == []


def test_posted_material_gaining_an_attachment_sets_the_flag(conn):
    row, stored_ids = stored_posted_material(conn, {"id": "m1", "title": "Slides"})
    updated, live = parse_coursework_material(
        {"id": "m1", "title": "Slides", "materials": [DRIVE]}, "c1"
    )

    (event,) = diff_coursework_material(
        row, updated, created_at=NOW,
        stored_material_ids=stored_ids,
        live_material_ids={m.id for m in live},
    )

    assert event.type == "material_updated"
    assert event.payload["attachments_added"] is True
    assert event.payload["text_changed"] is False


# --------------------------------------------------------------------------
# the shared contract across all three *_updated types
# --------------------------------------------------------------------------

ATTACHMENT_DELTA_KEYS = {
    "attachments_added",
    "added_count",
    "removed_count",
    "text_changed",
}


def _one_of_each_updated_event(conn):
    """The same change -- one file added, nothing else -- to all three parents."""
    work_row = stored_coursework(conn, {"id": "w1", "title": "TD 3"})
    work, work_live = parse_coursework(
        {"id": "w1", "title": "TD 3", "materials": [DRIVE]}, "c1"
    )
    (coursework_event,) = diff_coursework(
        work_row, work, created_at=NOW, live_material_ids={m.id for m in work_live}
    )

    material_row, material_stored = stored_posted_material(conn, {"id": "m1", "title": "Slides"})
    material, material_live = parse_coursework_material(
        {"id": "m1", "title": "Slides", "materials": [DRIVE]}, "c1"
    )
    (material_event,) = diff_coursework_material(
        material_row, material, created_at=NOW,
        stored_material_ids=material_stored,
        live_material_ids={m.id for m in material_live},
    )

    announcement_row, announcement_stored = stored_announcement(conn, {"id": "a1", "text": "Week 3"})
    announcement, announcement_live = parse_announcement(
        {"id": "a1", "text": "Week 3", "materials": [DRIVE]}, "c1"
    )
    (announcement_event,) = diff_announcement(
        announcement_row, announcement, created_at=NOW,
        stored_material_ids=announcement_stored,
        live_material_ids={m.id for m in announcement_live},
    )

    return coursework_event, material_event, announcement_event


def test_all_updated_events_expose_the_same_attachment_delta_keys(conn):
    """One digest code path for all three, so the key set must not drift."""
    events = _one_of_each_updated_event(conn)

    assert {event.type for event in events} == {
        "coursework_updated",
        "material_updated",
        "announcement_updated",
    }
    for event in events:
        present = ATTACHMENT_DELTA_KEYS & set(event.payload)
        assert present == ATTACHMENT_DELTA_KEYS, f"{event.type} is missing keys"


def test_the_shared_keys_mean_the_same_thing_in_all_three(conn):
    """Same names is not enough -- the same change must read the same way."""
    events = _one_of_each_updated_event(conn)

    for event in events:
        delta = {key: event.payload[key] for key in ATTACHMENT_DELTA_KEYS}
        assert delta == {
            "attachments_added": True,
            "added_count": 1,
            "removed_count": 0,
            "text_changed": False,
        }, event.type


def test_the_shared_keys_agree_on_a_text_only_edit(conn):
    """The other branch: no files moved, only wording."""
    work_row = stored_coursework(conn, {"id": "w1", "title": "TD 3"})
    work, _ = parse_coursework({"id": "w1", "title": "TD 3 fixed"}, "c1")
    (coursework_event,) = diff_coursework(work_row, work, created_at=NOW)

    material_row, material_stored = stored_posted_material(conn, {"id": "m1", "title": "Slides"})
    material, _ = parse_coursework_material({"id": "m1", "title": "Slides fixed"}, "c1")
    (material_event,) = diff_coursework_material(
        material_row, material, created_at=NOW, stored_material_ids=material_stored
    )

    announcement_row, announcement_stored = stored_announcement(conn, {"id": "a1", "text": "Week 3"})
    announcement, _ = parse_announcement({"id": "a1", "text": "Week 3 fixed"}, "c1")
    (announcement_event,) = diff_announcement(
        announcement_row, announcement, created_at=NOW,
        stored_material_ids=announcement_stored,
    )

    for event in (coursework_event, material_event, announcement_event):
        assert event.payload["attachments_added"] is False, event.type
        assert event.payload["added_count"] == 0, event.type
        assert event.payload["text_changed"] is True, event.type


def test_posted_material_title_edit_leaves_the_flag_clear(conn):
    row, stored_ids = stored_posted_material(
        conn, {"id": "m1", "title": "Slides", "materials": [DRIVE]}
    )
    updated, live = parse_coursework_material(
        {"id": "m1", "title": "Slides (week 3)", "materials": [DRIVE]}, "c1"
    )

    (event,) = diff_coursework_material(
        row, updated, created_at=NOW,
        stored_material_ids=stored_ids,
        live_material_ids={m.id for m in live},
    )

    assert event.type == "material_updated"
    assert event.payload["attachments_added"] is False
    assert event.payload["text_changed"] is True


# --------------------------------------------------------------------------
# submissions
# --------------------------------------------------------------------------

def test_grade_null_to_eighty_is_grade_posted(conn):
    row = stored_submission(
        conn, {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN"}
    )
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN",
         "assignedGrade": 80}
    )

    (event,) = diff_submission(row, submission, created_at=NOW)

    assert event.type == "grade_posted"
    assert event.payload["before"] is None
    assert event.payload["after"] == 80


def test_grade_eighty_to_ninety_is_grade_changed(conn):
    row = stored_submission(
        conn, {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "RETURNED",
               "assignedGrade": 80}
    )
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "RETURNED",
         "assignedGrade": 90}
    )

    (event,) = diff_submission(row, submission, created_at=NOW)

    assert event.type == "grade_changed"
    assert (event.payload["before"], event.payload["after"]) == (80, 90)


def test_changed_draft_grade_produces_nothing(conn):
    """draftGrade is invisible to me in Classroom; reacting would leak it."""
    row = stored_submission(
        conn, {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN",
               "draftGrade": 40}
    )
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN",
         "draftGrade": 95}
    )

    assert diff_submission(row, submission, created_at=NOW) == []


def test_state_change_is_reported(conn):
    row = stored_submission(
        conn, {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "CREATED"}
    )
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN"}
    )

    (event,) = diff_submission(row, submission, created_at=NOW)

    assert event.type == "submission_state_changed"
    assert (event.payload["before"], event.payload["after"]) == ("CREATED", "TURNED_IN")


def test_grade_and_state_moving_together_gives_both(conn):
    row = stored_submission(
        conn, {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN"}
    )
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "RETURNED",
         "assignedGrade": 75}
    )

    types = [event.type for event in diff_submission(row, submission, created_at=NOW)]

    assert types == ["grade_posted", "submission_state_changed"]


def test_an_unseen_ungraded_submission_is_silent():
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "CREATED"}
    )
    assert diff_submission(None, submission, created_at=NOW) == []


def test_an_unseen_submission_that_arrives_graded_reports_the_grade():
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "RETURNED",
         "assignedGrade": 61}
    )
    (event,) = diff_submission(None, submission, created_at=NOW)
    assert event.type == "grade_posted"


def test_submission_payload_carries_the_assignment_title(conn):
    row = stored_submission(
        conn, {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN"}
    )
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "TURNED_IN",
         "assignedGrade": 80}
    )

    (event,) = diff_submission(row, submission, created_at=NOW, title="Algebra TD 3")

    assert event.payload["title"] == "Algebra TD 3"
