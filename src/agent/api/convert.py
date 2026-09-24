"""Domain objects to response models, in one place.

Every route converts through here rather than building a dict inline, for the
same reason `messages.py` has one `_subject_line`: two routes describing one
subject slightly differently is a bug nobody notices until the two screens
disagree, and by then neither can be trusted.

Nothing here queries anything. It takes what a route has already fetched and
reshapes it, so the conversions stay testable without a database and a route's
cost stays visible where the route is.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..filenames import document_filename
from ..gate import adjustments as adjust
from ..gate import scheduler as gate_scheduler
from ..gate import sections
from ..gate import timetable as tt
from ..sync import deadlines
from . import schemas


def session(value: tt.Session, note: str = "") -> schemas.SessionOut:
    return schemas.SessionOut(
        day=value.day,
        start=value.start,
        end=value.end,
        kind=value.kind,
        joint=value.joint,
        note=note,
        parts=[
            schemas.SessionPartOut(
                subject=part.subject,
                course_id=part.course_id,
                teacher=part.teacher,
                room=part.room,
            )
            for part in value.parts
        ],
    )


def item(value: gate_scheduler.Item) -> schemas.ItemOut:
    return schemas.ItemOut(
        item_id=value.item_id,
        entity_type=value.entity_type,
        entity_id=value.entity_id,
        label=value.label,
        state=value.state,
        alternate_link=value.alternate_link,
        creation_time=value.creation_time,
        files=value.files,
        pages=value.pages,
        chars=value.chars,
        unread=value.unread,
        ready=value.ready,
        blocked_reason=value.blocked_reason,
    )


def subject(
    value: gate_scheduler.Subject, *, notes: dict[int, str] | None = None
) -> schemas.SubjectOut:
    """One subject's standing.

    `state` comes from `Subject.state`, which `messages._subject_line` also reads.
    Neither this nor the Telegram renderer decides the classification for itself,
    so a subject cannot read "up to date" on one surface and "6 unreviewed" on the
    other.
    """
    notes = notes or {}
    oldest = min(
        (found.creation_time for found in value.items if found.creation_time),
        default=None,
    )
    return schemas.SubjectOut(
        name=value.name,
        state=value.state,
        course_id=value.course_id,
        course_name=value.course_name,
        mapped_course=value.mapped_course,
        gated=value.gated,
        manual=value.manual,
        unreviewed=len(value.items),
        ready=value.ready_count,
        blocked=value.blocked_count,
        unread_pages=value.unread_pages,
        dead_files=value.dead_files,
        oldest_posted_at=oldest,
        next_item=item(value.next_item) if value.next_item else None,
        sessions=[
            session(found, notes.get(id(found), "")) for found in value.sessions
        ],
    )


def gate_plan(plan: gate_scheduler.GatePlan) -> schemas.GateOut:
    notes = {id(found): plan.note_for(found) for found in plan.sessions}
    return schemas.GateOut(
        for_date=plan.for_date.isoformat(),
        version_label=plan.version_label,
        provisional=plan.provisional,
        worth_sending=plan.worth_sending,
        silent_because=plan.silent_because,
        total_items=plan.total_items,
        sessions=[session(found, notes.get(id(found), "")) for found in plan.sessions],
        subjects=[subject(found, notes=notes) for found in plan.subjects],
    )


def window(value: sections.Window) -> schemas.WindowOut:
    return schemas.WindowOut(
        index=value.index,
        first=value.first,
        last=value.last,
        pages=value.pages,
        label=value.label,
        title=value.title,
        topics=value.topics,
        snapped=value.snapped,
        continues=value.continues,
        unread=value.unread,
        ready=value.ready,
    )


def document(row: sqlite3.Row) -> schemas.DocumentOut:
    """One attachment, as `store.study_item_files` returns it.

    The filename is `filenames.document_filename`, which Telegram delivery uses
    too -- so a document arrives under the same name on both, and changing that
    rule stays a decision about one function.
    """
    local = row["local_path"] if "local_path" in row.keys() else None
    title = str(row["title"] or row["drive_id"])
    return schemas.DocumentOut(
        drive_id=str(row["drive_id"]),
        title=title,
        url=row["url"] if "url" in row.keys() else None,
        status=row["status"] if "status" in row.keys() else None,
        mime_type=row["mime_type"] if "mime_type" in row.keys() else None,
        size_bytes=row["size_bytes"] if "size_bytes" in row.keys() else None,
        pages=row["pages"] if "pages" in row.keys() else None,
        filename=document_filename(title, local) if local else None,
        readable=bool(local) and str(row["status"] or "") == "ok",
    )


def deadline(value: deadlines.Candidate) -> schemas.DeadlineOut:
    return schemas.DeadlineOut(
        entity_type=value.entity_type,
        entity_id=value.entity_id,
        course_id=value.course_id,
        title=value.title,
        due_at=value.due_at,
        link=value.link,
        done=value.done,
        label=value.label,
        links_to=value.links_to,
    )


def event(row: sqlite3.Row) -> schemas.EventOut:
    raw = row["payload"] if "payload" in row.keys() else None
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        payload = None
    return schemas.EventOut(
        id=int(row["id"]),
        type=str(row["type"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        course_id=row["course_id"],
        payload=payload if isinstance(payload, dict) else None,
        created_at=str(row["created_at"]),
        notified_at=row["notified_at"],
    )


def orphan(value: adjust.Orphan) -> schemas.OrphanOut:
    return schemas.OrphanOut(
        id=value.adjustment.id,
        applies_on=value.adjustment.applies_on.isoformat(),
        kind=value.adjustment.kind,
        subject=value.adjustment.subject,
        session_start=value.adjustment.session_start,
        why=value.why,
        renamed_to=value.renamed_to,
    )


def adjustment(row: sqlite3.Row) -> schemas.AdjustmentOut:
    return schemas.AdjustmentOut(
        id=int(row["id"]),
        applies_on=str(row["applies_on"]),
        kind=str(row["kind"]),
        subject=str(row["subject"]),
        course_id=row["course_id"],
        session_start=str(row["session_start"]),
        to_date=row["to_date"],
        to_start=row["to_start"],
        to_end=row["to_end"],
        to_kind=row["to_kind"],
        to_room=row["to_room"],
        to_teacher=row["to_teacher"],
        reason=row["reason"],
        created_at=str(row["created_at"]),
    )


def _split(value: Any) -> list[str]:
    """A TEXT column holding one value per line, back into a list."""
    return str(value).splitlines() if value else []


def project(
    row: sqlite3.Row, milestones: list[sqlite3.Row] | None = None
) -> schemas.ProjectOut:
    steps = milestones or []
    return schemas.ProjectOut(
        id=int(row["id"]),
        course_id=str(row["course_id"]),
        course_name=str(row["course_name"] or "") if "course_name" in row.keys() else "",
        title=str(row["title"]),
        deliverables=_split(row["deliverables"]),
        team=_split(row["team"]),
        brief_source=row["brief_source"],
        deadline_at=row["deadline_at"],
        coursework_id=row["coursework_id"],
        closed_at=row["closed_at"],
        created_at=str(row["created_at"]),
        milestones_done=sum(1 for step in steps if step["done_at"]),
        milestones_total=len(steps),
        milestones=[
            schemas.MilestoneOut(
                id=int(step["id"]),
                title=str(step["title"]),
                position=int(step["position"]),
                done_at=step["done_at"],
            )
            for step in steps
        ],
    )


def task(row: sqlite3.Row) -> schemas.TaskOut:
    return schemas.TaskOut(
        id=int(row["id"]),
        course_id=str(row["course_id"]),
        course_name=str(row["course_name"] or "") if "course_name" in row.keys() else "",
        title=str(row["title"]),
        kind=str(row["kind"]),
        due_at=row["due_at"],
        notes=row["notes"],
        source=row["source"],
        done_at=row["done_at"],
        created_at=str(row["created_at"]),
    )


def held_session(row: sqlite3.Row) -> schemas.HeldSessionOut:
    return schemas.HeldSessionOut(
        id=int(row["id"]),
        course_id=str(row["course_id"]),
        course_name=str(row["course_name"] or "") if "course_name" in row.keys() else "",
        held_on=str(row["held_on"]),
        kind=str(row["kind"]),
        covered=row["covered"],
        created_at=str(row["created_at"]),
    )
