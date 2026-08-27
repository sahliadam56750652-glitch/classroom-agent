"""Parsing raw Classroom payloads: due dates, the materials union, hashes."""

from __future__ import annotations

import pytest

from agent.classroom.models import (
    content_hash,
    material_id,
    parse_announcement,
    parse_course,
    parse_coursework,
    parse_coursework_material,
    parse_due_at,
    parse_materials,
    parse_submission,
)


# --------------------------------------------------------------------------
# parse_due_at -- the four cases
# --------------------------------------------------------------------------

def test_due_date_and_due_time_combine():
    raw = {"dueDate": {"year": 2025, "month": 3, "day": 7},
           "dueTime": {"hours": 14, "minutes": 30}}
    assert parse_due_at(raw) == "2025-03-07T14:30:00Z"


def test_due_date_without_due_time_is_end_of_day():
    """No dueTime key at all means the teacher set a date but no time."""
    raw = {"dueDate": {"year": 2025, "month": 3, "day": 7}}
    assert parse_due_at(raw) == "2025-03-07T23:59:59Z"


def test_empty_due_time_is_midnight_not_end_of_day():
    """The API omits zero-valued fields, so dueTime:{} is a real 00:00 deadline.

    This is the case that silently slips a deadline by a whole day if the empty
    object is mistaken for an absent one.
    """
    raw = {"dueDate": {"year": 2025, "month": 3, "day": 7}, "dueTime": {}}
    assert parse_due_at(raw) == "2025-03-07T00:00:00Z"


def test_no_due_date_is_none():
    """Normal, not exceptional: only 46% of measured coursework has a deadline."""
    assert parse_due_at({}) is None
    assert parse_due_at({"dueTime": {"hours": 9}}) is None


def test_partial_due_time_fills_zeros():
    raw = {"dueDate": {"year": 2025, "month": 12, "day": 31}, "dueTime": {"hours": 9}}
    assert parse_due_at(raw) == "2025-12-31T09:00:00Z"


def test_due_time_with_seconds():
    raw = {"dueDate": {"year": 2025, "month": 1, "day": 1},
           "dueTime": {"hours": 23, "minutes": 59, "seconds": 59}}
    assert parse_due_at(raw) == "2025-01-01T23:59:59Z"


@pytest.mark.parametrize(
    "due_date",
    [{"year": 2025, "month": 2}, {"year": 2025, "month": 13, "day": 1}, {}, None, "nope"],
)
def test_malformed_due_date_is_none_rather_than_a_crash(due_date):
    assert parse_due_at({"dueDate": due_date}) is None


# --------------------------------------------------------------------------
# content_hash
# --------------------------------------------------------------------------

def test_hash_is_stable_across_calls():
    fields = {"title": "Algebra", "materials": ["a", "b"]}
    assert content_hash(fields) == content_hash(dict(fields))


def test_hash_ignores_key_order():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_hash_distinguishes_real_changes():
    assert content_hash({"title": "A"}) != content_hash({"title": "B"})


def test_absent_and_none_collapse_when_parsers_fill_every_key():
    """Parsers always emit the full key set, so a missing field reads as None."""
    assert content_hash({"title": None}) == content_hash({"title": None})
    assert content_hash({"title": None}) != content_hash({})


# --------------------------------------------------------------------------
# materials union
# --------------------------------------------------------------------------

DRIVE = {"driveFile": {"driveFile": {"id": "d1", "title": "lecture.pdf",
                                     "alternateLink": "https://drive/d1"}}}
LINK = {"link": {"url": "https://example.org/x", "title": "Reference"}}
YOUTUBE = {"youtubeVideo": {"id": "yt1", "title": "Lecture 3",
                            "alternateLink": "https://youtu.be/yt1"}}
FORM = {"form": {"formUrl": "https://forms/f1", "title": "Survey"}}


def test_each_union_branch_is_recognised():
    materials = parse_materials("announcement", "a1", "c1", [DRIVE, LINK, YOUTUBE, FORM])
    assert [m.kind for m in materials] == ["driveFile", "link", "youTube", "form"]


def test_drive_file_carries_its_id():
    (material,) = parse_materials("coursework", "w1", "c1", [DRIVE])
    assert material.drive_id == "d1"
    assert material.ref == "d1"
    assert material.title == "lecture.pdf"


def test_non_drive_kinds_have_no_drive_id():
    materials = parse_materials("announcement", "a1", "c1", [LINK, YOUTUBE, FORM])
    assert all(m.drive_id is None for m in materials)


def test_announcement_is_a_first_class_parent():
    """Announcements carry more attachments than the other two parents combined."""
    (material,) = parse_materials("announcement", "a1", "c1", [DRIVE])
    assert material.parent_type == "announcement"
    assert material.parent_id == "a1"
    assert material.course_id == "c1"


def test_material_ids_are_stable_when_siblings_are_removed():
    """Position-based IDs would rewrite every key when one attachment goes."""
    before = parse_materials("announcement", "a1", "c1", [LINK, DRIVE, FORM])
    after = parse_materials("announcement", "a1", "c1", [DRIVE, FORM])

    kept = {m.id for m in before} & {m.id for m in after}
    assert len(kept) == 2
    assert material_id("announcement", "a1", "driveFile", "d1") in kept


def test_duplicate_attachment_collapses_to_one_row():
    materials = parse_materials("coursework", "w1", "c1", [DRIVE, DRIVE])
    assert len(materials) == 1


def test_unknown_union_member_is_skipped_not_guessed():
    materials = parse_materials("coursework", "w1", "c1", [{"somethingNew": {"id": "x"}}])
    assert materials == []


def test_material_missing_its_identifier_is_skipped():
    assert parse_materials("coursework", "w1", "c1", [{"link": {"title": "no url"}}]) == []


def test_empty_and_absent_material_lists():
    assert parse_materials("coursework", "w1", "c1", []) == []
    assert parse_materials("coursework", "w1", "c1", None) == []


# --------------------------------------------------------------------------
# record parsers
# --------------------------------------------------------------------------

def test_parse_course():
    course = parse_course(
        {"id": "77", "name": "Databases", "section": "G2", "courseState": "ARCHIVED",
         "creationTime": "2024-09-01T08:00:00Z"}
    )
    assert (course.id, course.name, course.section) == ("77", "Databases", "G2")
    assert course.course_state == "ARCHIVED"


def test_parse_course_without_state_does_not_invent_active():
    """Defaulting an unknown state to ACTIVE would be a lie the gate acts on."""
    assert parse_course({"id": "1", "name": "X"}).course_state == "UNKNOWN"


def test_parse_coursework_returns_work_and_materials():
    work, materials = parse_coursework(
        {"id": "w1", "title": "TD 3", "maxPoints": 20, "workType": "ASSIGNMENT",
         "dueDate": {"year": 2025, "month": 5, "day": 4}, "materials": [DRIVE]},
        "c1",
    )
    assert work.due_at == "2025-05-04T23:59:59Z"
    assert work.max_points == 20.0
    assert len(materials) == 1
    assert materials[0].parent_id == "w1"


def test_coursework_hash_changes_when_an_attachment_is_added():
    base = {"id": "w1", "title": "TD 3", "materials": [DRIVE]}
    with_more = {"id": "w1", "title": "TD 3", "materials": [DRIVE, LINK]}

    assert parse_coursework(base, "c1")[0].content_hash != \
        parse_coursework(with_more, "c1")[0].content_hash


def test_coursework_hash_ignores_fields_nobody_needs_telling_about():
    """updateTime churns on trivial teacher edits; it must not reach the hash."""
    quiet = {"id": "w1", "title": "TD 3", "updateTime": "2025-01-01T00:00:00Z"}
    noisy = {"id": "w1", "title": "TD 3", "updateTime": "2025-06-06T12:00:00Z"}

    assert parse_coursework(quiet, "c1")[0].content_hash == \
        parse_coursework(noisy, "c1")[0].content_hash


def test_parse_coursework_material():
    material, attachments = parse_coursework_material(
        {"id": "m1", "title": "Lecture slides", "materials": [DRIVE]}, "c1"
    )
    assert material.title == "Lecture slides"
    assert attachments[0].parent_type == "coursework_material"


def test_parse_announcement():
    announcement, attachments = parse_announcement(
        {"id": "a1", "text": "Slides for week 3", "materials": [DRIVE, LINK]}, "c1"
    )
    assert announcement.text == "Slides for week 3"
    assert len(attachments) == 2


def test_parse_submission():
    submission = parse_submission(
        {"id": "s1", "courseId": "c1", "courseWorkId": "w1", "state": "RETURNED",
         "late": True, "assignedGrade": 17.5}
    )
    assert submission.state == "RETURNED"
    assert submission.late is True
    assert submission.assigned_grade == 17.5


def test_submission_hash_ignores_draft_grade():
    """draftGrade is invisible to the student; reacting to it leaks the grade."""
    without = parse_submission({"id": "s1", "courseId": "c1", "courseWorkId": "w1",
                                "state": "CREATED"})
    with_draft = parse_submission({"id": "s1", "courseId": "c1", "courseWorkId": "w1",
                                   "state": "CREATED", "draftGrade": 12})
    assert without.content_hash == with_draft.content_hash
    assert not hasattr(without, "draft_grade")


def test_submission_late_defaults_to_false():
    submission = parse_submission({"id": "s1", "courseId": "c1", "courseWorkId": "w1"})
    assert submission.late is False
