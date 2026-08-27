"""Decide what actually changed, and what is worth saying about it.

The differ compares full live state against full stored state. There is no time
window anywhere in this module and there must never be one: a sync after ten
days offline has to produce exactly the events ten daily syncs would have, no
gaps and no repeats. Any `WHERE updateTime > last_run` is a bug even when the
tests pass.

Two syncs of unchanged data produce zero events. That property is what makes
the notifications worth reading, so it is the thing to protect when changing
anything here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

# The canonical hash lives with the parsers, because that is where the field
# lists live -- re-implementing it here would let the two drift and silently
# change what counts as "unchanged". Re-exported so callers can read it as part
# of the diffing vocabulary.
from ..classroom.models import (
    Announcement,
    CourseWork,
    CourseWorkMaterial,
    Submission,
    content_hash,
)

__all__ = [
    "Event",
    "content_hash",
    "diff_announcement",
    "diff_coursework",
    "diff_coursework_material",
    "diff_submission",
]


@dataclass(frozen=True)
class Event:
    """One real change. Append-only once written; only notified_at ever moves."""

    type: str
    entity_type: str
    entity_id: str
    course_id: str | None
    payload: dict[str, Any]
    created_at: str


# --------------------------------------------------------------------------
# coursework
# --------------------------------------------------------------------------

# Shared by all three *_updated events. The digest branches on the same key
# names with the same meanings whichever kind of thing changed.
def _attachment_delta(
    stored_ids: Collection[str], live_ids: Collection[str]
) -> tuple[set[str], set[str]]:
    stored_set, live_set = set(stored_ids), set(live_ids)
    return live_set - stored_set, stored_set - live_set


# Coursework is compared field by field once the hash says something moved, so
# the event can say which kind of news this is. A due-date move and a title fix
# are not the same message.
def diff_coursework(
    stored: sqlite3.Row | None,
    work: CourseWork,
    *,
    created_at: str,
    stored_material_ids: Collection[str] = (),
    live_material_ids: Collection[str] = (),
) -> list[Event]:
    if stored is None:
        return [
            Event(
                type="new_coursework",
                entity_type="coursework",
                entity_id=work.id,
                course_id=work.course_id,
                payload={
                    "title": work.title,
                    "due_at": work.due_at,
                    "attachment_count": len(set(live_material_ids)),
                },
                created_at=created_at,
            )
        ]

    if stored["content_hash"] == work.content_hash:
        return []

    changed = {
        field
        for field, value in (
            ("title", work.title),
            ("description", work.description),
            ("state", work.state),
            ("max_points", work.max_points),
        )
        if stored[field] != value
    }
    due_changed = stored["due_at"] != work.due_at
    added, removed = _attachment_delta(stored_material_ids, live_material_ids)

    events: list[Event] = []
    if due_changed:
        events.append(
            Event(
                type="due_date_changed",
                entity_type="coursework",
                entity_id=work.id,
                course_id=work.course_id,
                payload={
                    "title": work.title,
                    "before": stored["due_at"],
                    "after": work.due_at,
                },
                created_at=created_at,
            )
        )

    # The hash moved, so something changed. A due-date-only move is already
    # reported above and needs nothing more; anything else lands here. The
    # trailing `not due_changed` keeps a hash change from ever going silent,
    # even if some field we do not compare individually is what moved.
    if changed or added or removed or not due_changed:
        events.append(
            Event(
                type="coursework_updated",
                entity_type="coursework",
                entity_id=work.id,
                course_id=work.course_id,
                payload={
                    "title": work.title,
                    # Coursework-specific detail: which scalar fields moved,
                    # including max_points. Due dates get their own event.
                    "changed": sorted(changed),
                    # The shared attachment-delta contract, identical in name
                    # and meaning to announcement_updated and material_updated
                    # so the digest has one code path for all three.
                    "attachments_added": bool(added),
                    "added_count": len(added),
                    "removed_count": len(removed),
                    "text_changed": (
                        stored["title"] != work.title
                        or stored["description"] != work.description
                    ),
                },
                created_at=created_at,
            )
        )
    return events


# --------------------------------------------------------------------------
# coursework materials and announcements
# --------------------------------------------------------------------------

def diff_coursework_material(
    stored: sqlite3.Row | None,
    material: CourseWorkMaterial,
    *,
    created_at: str,
    stored_material_ids: Collection[str] = (),
    live_material_ids: Collection[str] = (),
) -> list[Event]:
    """A new posting, or an edit to one already seen."""
    if stored is None:
        return [
            Event(
                type="new_material",
                entity_type="coursework_material",
                entity_id=material.id,
                course_id=material.course_id,
                payload={
                    "title": material.title,
                    "attachment_count": len(set(live_material_ids)),
                },
                created_at=created_at,
            )
        ]

    if stored["content_hash"] == material.content_hash:
        return []

    added, removed = _attachment_delta(stored_material_ids, live_material_ids)
    return [
        Event(
            type="material_updated",
            entity_type="coursework_material",
            entity_id=material.id,
            course_id=material.course_id,
            payload={
                "title": material.title,
                # attachments_added is the field the digest branches on: new
                # files are the news, a reworded title is not.
                "attachments_added": bool(added),
                "added_count": len(added),
                "removed_count": len(removed),
                "text_changed": (
                    stored["title"] != material.title
                    or stored["description"] != material.description
                ),
            },
            created_at=created_at,
        )
    ]


def diff_announcement(
    stored: sqlite3.Row | None,
    announcement: Announcement,
    *,
    created_at: str,
    stored_material_ids: Collection[str] = (),
    live_material_ids: Collection[str] = (),
) -> list[Event]:
    """A new announcement, or an edit to one already seen.

    Announcements are the primary way course content actually arrives here: 211
    of 375 measured attachments hang off them, more than coursework and posted
    material combined. A teacher attaching a file to an announcement they posted
    last week is therefore a main delivery path, not an edge case, and it has to
    produce an event.
    """
    if stored is None:
        return [
            Event(
                type="new_announcement",
                entity_type="announcement",
                entity_id=announcement.id,
                course_id=announcement.course_id,
                payload={
                    "text": announcement.text,
                    "attachment_count": len(set(live_material_ids)),
                },
                created_at=created_at,
            )
        ]

    if stored["content_hash"] == announcement.content_hash:
        return []

    added, removed = _attachment_delta(stored_material_ids, live_material_ids)
    return [
        Event(
            type="announcement_updated",
            entity_type="announcement",
            entity_id=announcement.id,
            course_id=announcement.course_id,
            payload={
                "text": announcement.text,
                # The distinction the digest needs without re-querying: files
                # arriving is the thing worth reading, a typo fix is noise.
                "attachments_added": bool(added),
                "added_count": len(added),
                "removed_count": len(removed),
                "text_changed": stored["text"] != announcement.text,
            },
            created_at=created_at,
        )
    ]


# --------------------------------------------------------------------------
# submissions
# --------------------------------------------------------------------------

def diff_submission(
    stored: sqlite3.Row | None,
    submission: Submission,
    *,
    created_at: str,
    title: str | None = None,
) -> list[Event]:
    """Grade and state changes on my own work.

    draftGrade is never consulted and never stored: it is invisible to me in
    Classroom, so reacting to it would announce a grade the teacher has not
    released yet.

    grade_posted also fires when a submission row appears for the first time
    already carrying an assignedGrade. This is deliberate, not an accident of
    the None handling. There is no new_submission event type, and submissions
    arrive alongside their coursework via courseWorkId='-', so an assignment
    that shows up already marked would otherwise be stored silently and the
    grade never reported at all. Treating an absent row as "previous
    assignedGrade was NULL" is what stops a grade being swallowed by the same
    sync that first learns the assignment exists.

    Note that this makes the first non-seeded sync noisy by design: every
    already-graded submission reports. That is exactly what --seed is for.
    """
    before_grade = stored["assigned_grade"] if stored is not None else None
    before_state = stored["state"] if stored is not None else None

    if stored is None:
        # There is no `new_submission` type. A submission row appears when its
        # coursework does, usually ungraded -- but if it arrives already graded
        # that is still a grade nobody has told me about, so an absent row reads
        # as "previous assignedGrade was NULL".
        if submission.assigned_grade is None:
            return []
    elif stored["content_hash"] == submission.content_hash:
        return []

    events: list[Event] = []
    after_grade = submission.assigned_grade

    if before_grade is None and after_grade is not None:
        events.append(
            _grade_event("grade_posted", submission, title, before_grade, after_grade, created_at)
        )
    elif before_grade is not None and after_grade is not None and before_grade != after_grade:
        # A regrade. Always worth hearing about -- it can move either way.
        events.append(
            _grade_event("grade_changed", submission, title, before_grade, after_grade, created_at)
        )

    if stored is not None and before_state != submission.state:
        events.append(
            Event(
                type="submission_state_changed",
                entity_type="submission",
                entity_id=submission.id,
                course_id=submission.course_id,
                payload={
                    "title": title,
                    "coursework_id": submission.coursework_id,
                    "before": before_state,
                    "after": submission.state,
                },
                created_at=created_at,
            )
        )
    return events


def _grade_event(
    event_type: str,
    submission: Submission,
    title: str | None,
    before: float | None,
    after: float | None,
    created_at: str,
) -> Event:
    return Event(
        type=event_type,
        entity_type="submission",
        entity_id=submission.id,
        course_id=submission.course_id,
        payload={
            "title": title,
            "coursework_id": submission.coursework_id,
            "before": before,
            "after": after,
        },
        created_at=created_at,
    )
