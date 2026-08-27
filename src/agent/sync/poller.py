"""Fetch live Classroom state for the tracked courses, store it, emit events.

Only courses named in config.tracked_courses are touched. courseState is not a
term indicator -- every ACTIVE course on this account is from a finished year --
so the allowlist is the only thing that decides what gets synced.

The sequence per course is fetch -> diff -> write, and the diff always compares
the full live state against the full stored state. Nothing here looks at when
the last run happened.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..classroom.client import ClassroomClient
from ..classroom.models import (
    Material,
    parse_announcement,
    parse_coursework,
    parse_coursework_material,
    parse_submission,
)
from ..config import Config
from ..db import store
from .differ import (
    Event,
    diff_announcement,
    diff_coursework,
    diff_coursework_material,
    diff_submission,
)

RESOURCE_COUNT_KEYS = (
    "courses",
    "coursework",
    "coursework_materials",
    "announcements",
    "submissions",
    "materials",
)


class SeedRefused(Exception):
    """--seed was asked for on a database that already holds events."""


class UnknownCourse(Exception):
    """config.tracked_courses names a course that is not in the database."""


@dataclass
class SyncResult:
    run_id: int | None = None
    courses_synced: list[str] = field(default_factory=list)
    items_seen: dict[str, int] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    events_written: int = 0
    deleted: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False
    seeded: bool = False

    @property
    def event_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.type] = counts.get(event.type, 0) + 1
        return counts


@dataclass
class _CourseState:
    """Everything fetched and parsed for one course, before anything is written."""

    coursework: list = field(default_factory=list)
    coursework_materials: list = field(default_factory=list)
    announcements: list = field(default_factory=list)
    submissions: list = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)


def _fetch_course(client: ClassroomClient, course_id: str) -> _CourseState:
    """One course's live state. Attachments come from all three parents.

    Announcement materials[] matter most here: measured at 211 attachments
    against 131 and 33 for the other two parents, they carry more than half the
    course content, and skipping them loses it without raising anything.
    """
    state = _CourseState()

    for raw in client.list_coursework(course_id):
        work, attachments = parse_coursework(raw, course_id)
        state.coursework.append(work)
        state.materials.extend(attachments)

    for raw in client.list_coursework_materials(course_id):
        material, attachments = parse_coursework_material(raw, course_id)
        state.coursework_materials.append(material)
        state.materials.extend(attachments)

    for raw in client.list_announcements(course_id):
        announcement, attachments = parse_announcement(raw, course_id)
        state.announcements.append(announcement)
        state.materials.extend(attachments)

    # One call for the whole course via courseWorkId='-'.
    state.submissions = [parse_submission(raw) for raw in client.list_submissions(course_id)]
    return state


def sync(
    config: Config,
    db: sqlite3.Connection,
    dry_run: bool = False,
    seed: bool = False,
    *,
    client: ClassroomClient | None = None,
    force: bool = False,
    now: str | None = None,
) -> SyncResult:
    """Poll the tracked courses, diff against stored state, write events.

    dry_run fetches and diffs but writes nothing at all -- not the rows, not the
    events, not even a sync_runs entry.

    seed writes everything and stamps every generated event as already notified.
    The first run spans 25 historical courses and several hundred events, none
    of which are actually news.
    """
    if seed and not force and store.count_events(db) > 0:
        raise SeedRefused(
            f"--seed would mark generated events as already notified, but the "
            f"database already holds {store.count_events(db)} event(s). Seeding "
            f"now would silently bury anything still pending. Pass --force if "
            f"that is genuinely what you want."
        )

    tracked = list(config.tracked_courses)
    result = SyncResult(dry_run=dry_run, seeded=seed)
    result.items_seen = {key: 0 for key in RESOURCE_COUNT_KEYS}
    if not tracked:
        return result

    missing = [course_id for course_id in tracked if store.get_course(db, course_id) is None]
    if missing:
        raise UnknownCourse(
            f"config.courses.tracked names {len(missing)} course(s) not in the "
            f"database: {', '.join(missing)}.\n"
            f"Run `agent courses` first to fetch the course list."
        )

    # One timestamp for the whole run. Every event from this sync shares it,
    # which is what the (type, entity_type, entity_id, created_at) dedupe index
    # keys on.
    created_at = now or store._utc_now_iso()

    if not dry_run:
        result.run_id = store.start_sync_run(db, now=created_at)

    try:
        for course_id in tracked:
            live = _fetch_course(client, course_id)
            result.courses_synced.append(course_id)
            result.events.extend(_diff_course(db, course_id, live, created_at))
            result.items_seen["courses"] += 1
            result.items_seen["coursework"] += len(live.coursework)
            result.items_seen["coursework_materials"] += len(live.coursework_materials)
            result.items_seen["announcements"] += len(live.announcements)
            result.items_seen["submissions"] += len(live.submissions)
            result.items_seen["materials"] += len(live.materials)

            if not dry_run:
                _write_course(db, course_id, live, created_at, result)

        if not dry_run:
            notified_at = created_at if seed else None
            for event in result.events:
                if store.insert_event(db, event, notified_at=notified_at):
                    result.events_written += 1
            db.commit()
    except Exception as err:
        if result.run_id is not None:
            db.rollback()
            store.finish_sync_run(db, result.run_id, status="error", error=repr(err))
        raise

    if result.run_id is not None:
        store.finish_sync_run(
            db,
            result.run_id,
            status="ok",
            items_seen=result.items_seen,
            events_emitted=result.events_written,
        )
    return result


def _attachments_by_parent(materials) -> dict[tuple[str, str], set[str]]:
    grouped: dict[tuple[str, str], set[str]] = {}
    for material in materials:
        grouped.setdefault((material.parent_type, material.parent_id), set()).add(material.id)
    return grouped


def _stored_attachments_by_parent(
    db: sqlite3.Connection, course_id: str
) -> dict[tuple[str, str], set[str]]:
    """Attachment IDs per parent as of the last sync, skipping soft-deleted ones.

    A file removed and later re-attached counts as added again: it is available
    once more, which is the part worth reporting.
    """
    grouped: dict[tuple[str, str], set[str]] = {}
    for row in store.load_rows(db, "materials", course_id).values():
        if row["deleted_at"] is not None:
            continue
        grouped.setdefault((row["parent_type"], row["parent_id"]), set()).add(row["id"])
    return grouped


def _diff_course(
    db: sqlite3.Connection, course_id: str, live: _CourseState, created_at: str
) -> list[Event]:
    """Compare live against stored. Reads only -- writing happens afterwards."""
    events: list[Event] = []

    # Read before anything is written, so these still describe the previous sync.
    live_attachments = _attachments_by_parent(live.materials)
    stored_attachments = _stored_attachments_by_parent(db, course_id)

    stored_work = store.load_rows(db, "coursework", course_id)
    for work in live.coursework:
        key = ("coursework", work.id)
        events.extend(
            diff_coursework(
                stored_work.get(work.id),
                work,
                created_at=created_at,
                stored_material_ids=stored_attachments.get(key, frozenset()),
                live_material_ids=live_attachments.get(key, frozenset()),
            )
        )

    stored_materials = store.load_rows(db, "coursework_materials", course_id)
    for material in live.coursework_materials:
        key = ("coursework_material", material.id)
        events.extend(
            diff_coursework_material(
                stored_materials.get(material.id),
                material,
                created_at=created_at,
                stored_material_ids=stored_attachments.get(key, frozenset()),
                live_material_ids=live_attachments.get(key, frozenset()),
            )
        )

    stored_announcements = store.load_rows(db, "announcements", course_id)
    for announcement in live.announcements:
        key = ("announcement", announcement.id)
        events.extend(
            diff_announcement(
                stored_announcements.get(announcement.id),
                announcement,
                created_at=created_at,
                stored_material_ids=stored_attachments.get(key, frozenset()),
                live_material_ids=live_attachments.get(key, frozenset()),
            )
        )

    # Submission events read better with the assignment's title than with a
    # bare ID, and the parsed coursework is right here.
    titles = {work.id: work.title for work in live.coursework}
    stored_submissions = store.load_rows(db, "submissions", course_id)
    for submission in live.submissions:
        events.extend(
            diff_submission(
                stored_submissions.get(submission.id),
                submission,
                created_at=created_at,
                title=titles.get(submission.coursework_id),
            )
        )
    return events


def _write_course(
    db: sqlite3.Connection,
    course_id: str,
    live: _CourseState,
    created_at: str,
    result: SyncResult,
) -> None:
    """Upsert live state, then soft-delete whatever the live state dropped.

    Coursework goes first: submissions carry a foreign key to it, and they
    arrive together from the courseWorkId='-' call.
    """
    for work in live.coursework:
        store.upsert_coursework(db, work, now=created_at)
    for material in live.coursework_materials:
        store.upsert_coursework_material(db, material, now=created_at)
    for announcement in live.announcements:
        store.upsert_announcement(db, announcement, now=created_at)

    known_work = {work.id for work in live.coursework}
    for submission in live.submissions:
        # A submission whose coursework was not returned would violate the
        # foreign key. Skip rather than abort the whole sync.
        if submission.coursework_id in known_work:
            store.upsert_submission(db, submission, now=created_at)

    store.upsert_materials(db, live.materials, now=created_at)

    for table, live_ids in (
        ("coursework", known_work),
        ("coursework_materials", {m.id for m in live.coursework_materials}),
        ("announcements", {a.id for a in live.announcements}),
        ("submissions", {s.id for s in live.submissions if s.coursework_id in known_work}),
        ("materials", {m.id for m in live.materials}),
    ):
        removed = store.soft_delete_missing(db, table, course_id, live_ids, now=created_at)
        if removed:
            result.deleted[table] = result.deleted.get(table, 0) + len(removed)
