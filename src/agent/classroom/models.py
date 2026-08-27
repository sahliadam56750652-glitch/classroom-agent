"""Parse raw Classroom API dicts into dataclasses.

Everything that knows about the API's field shapes lives here. The client
returns raw dicts and the store writes rows; this is the only layer that
understands, for example, that dueDate and dueTime are two separate objects.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

ParentType = Literal["coursework", "coursework_material", "announcement"]
MaterialKind = Literal["driveFile", "link", "youTube", "form"]

# The API's union key -> our stored kind. youtubeVideo is renamed for symmetry
# with the other three; everything else keeps its API spelling.
_MATERIAL_KINDS: dict[str, MaterialKind] = {
    "driveFile": "driveFile",
    "link": "link",
    "youtubeVideo": "youTube",
    "form": "form",
}


def iso(moment: datetime) -> str:
    """Render an aware datetime as the UTC ISO-8601 string we store."""
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def content_hash(fields: dict[str, Any]) -> str:
    """Stable hash over the fields worth notifying about.

    Keys are sorted and every key is always present -- absent and None must
    collapse to the same thing, or two syncs of unchanged data would produce
    different hashes and spam a change event.
    """
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# due dates
# --------------------------------------------------------------------------

def parse_due_at(raw: dict[str, Any]) -> str | None:
    """Combine the API's split dueDate/dueTime into one UTC ISO-8601 string.

    Both fields are already UTC. Four cases, and the difference between the
    middle two is easy to get backwards:

      dueDate + dueTime      -> that instant
      dueDate, no dueTime    -> end of day (23:59:59); the field is absent
                                because the teacher set no time at all
      dueDate + dueTime: {}  -> midnight (00:00:00); the API omits zero-valued
                                fields, so an empty object is a real deadline
      no dueDate             -> None, which is normal: only 46% of measured
                                coursework has a deadline at all
    """
    due_date = raw.get("dueDate")
    if not due_date:
        return None

    try:
        year = int(due_date["year"])
        month = int(due_date["month"])
        day = int(due_date["day"])
    except (KeyError, TypeError, ValueError):
        return None

    if "dueTime" in raw:
        due_time = raw.get("dueTime") or {}
        hours = int(due_time.get("hours", 0))
        minutes = int(due_time.get("minutes", 0))
        seconds = int(due_time.get("seconds", 0))
    else:
        hours, minutes, seconds = 23, 59, 59

    try:
        moment = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(
            hours=hours, minutes=minutes, seconds=seconds
        )
    except ValueError:
        return None
    return iso(moment)


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Course:
    id: str
    name: str
    section: str | None
    room: str | None
    owner_id: str | None
    course_state: str
    enrollment_code: str | None
    alternate_link: str | None
    creation_time: str | None
    update_time: str | None
    content_hash: str


@dataclass(frozen=True)
class CourseWork:
    id: str
    course_id: str
    title: str | None
    description: str | None
    state: str | None
    work_type: str | None
    topic_id: str | None
    max_points: float | None
    due_at: str | None
    alternate_link: str | None
    creation_time: str | None
    update_time: str | None
    content_hash: str


@dataclass(frozen=True)
class CourseWorkMaterial:
    id: str
    course_id: str
    title: str | None
    description: str | None
    state: str | None
    topic_id: str | None
    alternate_link: str | None
    creation_time: str | None
    update_time: str | None
    content_hash: str


@dataclass(frozen=True)
class Announcement:
    id: str
    course_id: str
    text: str | None
    state: str | None
    alternate_link: str | None
    creation_time: str | None
    update_time: str | None
    content_hash: str


@dataclass(frozen=True)
class Submission:
    id: str
    course_id: str
    coursework_id: str
    state: str | None
    late: bool
    assigned_grade: float | None
    alternate_link: str | None
    creation_time: str | None
    update_time: str | None
    content_hash: str


@dataclass(frozen=True)
class Material:
    id: str
    parent_type: ParentType
    parent_id: str
    course_id: str
    kind: MaterialKind
    ref: str
    drive_id: str | None
    title: str | None
    url: str | None
    content_hash: str


# --------------------------------------------------------------------------
# materials
# --------------------------------------------------------------------------

def material_id(parent_type: str, parent_id: str, kind: str, ref: str) -> str:
    """Synthetic stable ID: attachments carry no Classroom ID of their own.

    Built from `ref` rather than list position so that removing or reordering a
    sibling attachment does not silently rewrite every other attachment's key.
    """
    return f"{parent_type}:{parent_id}:{kind}:{ref}"


def _describe(entry: dict[str, Any]) -> tuple[MaterialKind, str, str | None, str | None, str | None] | None:
    """(kind, ref, drive_id, title, url) for one materials[] entry."""
    if "driveFile" in entry:
        drive_file = (entry.get("driveFile") or {}).get("driveFile") or {}
        drive_id = drive_file.get("id")
        if not drive_id:
            return None
        return "driveFile", drive_id, drive_id, drive_file.get("title"), drive_file.get("alternateLink")

    if "youtubeVideo" in entry:
        video = entry.get("youtubeVideo") or {}
        ref = video.get("id") or video.get("alternateLink")
        if not ref:
            return None
        return "youTube", ref, None, video.get("title"), video.get("alternateLink")

    if "link" in entry:
        link = entry.get("link") or {}
        url = link.get("url")
        if not url:
            return None
        return "link", url, None, link.get("title"), url

    if "form" in entry:
        form = entry.get("form") or {}
        url = form.get("formUrl")
        if not url:
            return None
        return "form", url, None, form.get("title"), url

    # An unrecognised union member -- Google adding a new attachment type. Skip
    # it rather than guessing; the schema's CHECK would reject it anyway.
    return None


def parse_materials(
    parent_type: ParentType,
    parent_id: str,
    course_id: str,
    entries: list[dict[str, Any]] | None,
) -> list[Material]:
    """One Material per attachment, branching on which union key is present."""
    materials: list[Material] = []
    seen: set[str] = set()

    for entry in entries or []:
        described = _describe(entry or {})
        if described is None:
            continue
        kind, ref, drive_id, title, url = described

        identifier = material_id(parent_type, parent_id, kind, ref)
        if identifier in seen:
            # The same file attached twice to one parent collapses to one row.
            continue
        seen.add(identifier)

        materials.append(
            Material(
                id=identifier,
                parent_type=parent_type,
                parent_id=parent_id,
                course_id=course_id,
                kind=kind,
                ref=ref,
                drive_id=drive_id,
                title=title,
                url=url,
                content_hash=content_hash({"kind": kind, "ref": ref, "title": title, "url": url}),
            )
        )
    return materials


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

def _material_refs(materials: list[Material]) -> list[str]:
    """Sorted attachment IDs, for inclusion in a parent's content hash."""
    return sorted(material.id for material in materials)


def parse_course(raw: dict[str, Any]) -> Course:
    return Course(
        id=str(raw["id"]),
        name=raw.get("name") or "",
        section=raw.get("section"),
        room=raw.get("room"),
        owner_id=raw.get("ownerId"),
        course_state=raw.get("courseState") or "UNKNOWN",
        enrollment_code=raw.get("enrollmentCode"),
        alternate_link=raw.get("alternateLink"),
        creation_time=raw.get("creationTime"),
        update_time=raw.get("updateTime"),
        content_hash=content_hash(
            {
                "name": raw.get("name"),
                "section": raw.get("section"),
                "room": raw.get("room"),
                "course_state": raw.get("courseState"),
            }
        ),
    )


def parse_coursework(raw: dict[str, Any], course_id: str) -> tuple[CourseWork, list[Material]]:
    identifier = str(raw["id"])
    materials = parse_materials("coursework", identifier, course_id, raw.get("materials"))
    due_at = parse_due_at(raw)
    max_points = raw.get("maxPoints")

    return (
        CourseWork(
            id=identifier,
            course_id=course_id,
            title=raw.get("title"),
            description=raw.get("description"),
            state=raw.get("state"),
            work_type=raw.get("workType"),
            topic_id=raw.get("topicId"),
            max_points=float(max_points) if max_points is not None else None,
            due_at=due_at,
            alternate_link=raw.get("alternateLink"),
            creation_time=raw.get("creationTime"),
            update_time=raw.get("updateTime"),
            content_hash=content_hash(
                {
                    "title": raw.get("title"),
                    "description": raw.get("description"),
                    "due_at": due_at,
                    "max_points": max_points,
                    "state": raw.get("state"),
                    "materials": _material_refs(materials),
                }
            ),
        ),
        materials,
    )


def parse_coursework_material(
    raw: dict[str, Any], course_id: str
) -> tuple[CourseWorkMaterial, list[Material]]:
    identifier = str(raw["id"])
    materials = parse_materials("coursework_material", identifier, course_id, raw.get("materials"))

    return (
        CourseWorkMaterial(
            id=identifier,
            course_id=course_id,
            title=raw.get("title"),
            description=raw.get("description"),
            state=raw.get("state"),
            topic_id=raw.get("topicId"),
            alternate_link=raw.get("alternateLink"),
            creation_time=raw.get("creationTime"),
            update_time=raw.get("updateTime"),
            content_hash=content_hash(
                {
                    "title": raw.get("title"),
                    "description": raw.get("description"),
                    "materials": _material_refs(materials),
                }
            ),
        ),
        materials,
    )


def parse_announcement(raw: dict[str, Any], course_id: str) -> tuple[Announcement, list[Material]]:
    identifier = str(raw["id"])
    materials = parse_materials("announcement", identifier, course_id, raw.get("materials"))

    return (
        Announcement(
            id=identifier,
            course_id=course_id,
            text=raw.get("text"),
            state=raw.get("state"),
            alternate_link=raw.get("alternateLink"),
            creation_time=raw.get("creationTime"),
            update_time=raw.get("updateTime"),
            content_hash=content_hash(
                {"text": raw.get("text"), "materials": _material_refs(materials)}
            ),
        ),
        materials,
    )


def parse_submission(raw: dict[str, Any]) -> Submission:
    assigned = raw.get("assignedGrade")
    late = bool(raw.get("late", False))
    state = raw.get("state")

    return Submission(
        id=str(raw["id"]),
        course_id=str(raw["courseId"]),
        coursework_id=str(raw["courseWorkId"]),
        state=state,
        late=late,
        assigned_grade=float(assigned) if assigned is not None else None,
        alternate_link=raw.get("alternateLink"),
        creation_time=raw.get("creationTime"),
        update_time=raw.get("updateTime"),
        # draftGrade is deliberately excluded: it is not visible to the student,
        # so hashing it would fire an event about a grade nobody has released.
        content_hash=content_hash(
            {"state": state, "late": late, "assigned_grade": assigned}
        ),
    )
