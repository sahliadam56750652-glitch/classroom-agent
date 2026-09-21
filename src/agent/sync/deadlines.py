"""Derive deadline events from everything with a due date. Fetches nothing.

Stateless by design: every run recomputes every candidate from scratch and
leans on the events table to know what has already been said. There is no
"last scanned" timestamp anywhere in here and there must never be one -- the
laptop is closed for days at a time, and a scanner that only looks at the
window since it last ran drops every threshold crossed while it was off.

Running this twice in a row produces events the first time and nothing the
second. That is the property to protect when changing anything here.

Phase 6 added two more kinds of thing that are due -- a tutorial or exercise
sheet entered by hand, and a project -- and they arrive here as extra CANDIDATE
SOURCES rather than as a second scanner. That is the whole reason they are
cheap: a due date on a row is all the logic below needs, so they inherit
catch-up safety, the events-table dedupe and the most-urgent-speaks-for-all
rule for nothing. A parallel scanner would have had to re-earn every one of
them, and would have drifted the first time one of the two was changed.

At most one alert per assignment per scan. Under normal operation -- a scan
twice a day -- each threshold is crossed on its own and fires on its own. It is
only after downtime that several are crossed at once, and then the most urgent
one speaks for all of them: being told "72 hours left" about something due in
three is worse than not being told twice.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from ..classroom.models import ISO_FORMAT, iso
from .differ import Event

# Hours before the due datetime, and the event type each one emits. Ordered
# far-to-near so a scan that is catching up emits them in the order they would
# have arrived had it been running all along.
THRESHOLDS: tuple[tuple[str, int], ...] = (
    ("deadline_t72", 72),
    ("deadline_t24", 24),
    ("deadline_t3", 3),
)

# Handed in or already marked. Both mean the work is done and an alert would be
# noise; RECLAIMED_BY_STUDENT deliberately is not in this set, because pulling
# work back means it is outstanding again.
DONE_STATES = frozenset({"TURNED_IN", "RETURNED"})


@dataclass(frozen=True)
class Candidate:
    """One thing that is due, whatever kind of thing it is.

    The scanner below works on these and nothing else, so adding a kind of
    deadline means adding a function that yields Candidates -- never touching
    the threshold logic, the dedupe, or the suppression rule.

    `entity_type` is what the events table is keyed on alongside `entity_id`,
    and that column has no CHECK and no foreign key, so a new value there needs
    no schema change at all.
    """

    entity_type: str
    entity_id: str
    course_id: str | None
    title: str
    due_at: str | None
    link: str | None = None
    done: bool = False
    # Carried into the payload so the digest can say what kind of thing it is
    # without looking anything up.
    label: str = ""
    # For a project: the Classroom coursework it is the same work as, when it
    # was eventually posted as one. See `_merge_linked_projects`.
    links_to: str | None = None


@dataclass
class DeadlineScan:
    # Alerts to actually send: at most one per assignment per scan.
    events: list[Event]
    # Thresholds that were crossed while the scanner was not running, and that
    # a more urgent alert in `events` now speaks for. They must still be
    # written -- stamped as already notified -- or the next scan would find
    # them uncrossed and fire them late.
    suppressed: list[Event] = field(default_factory=list)
    events_written: int = 0
    suppressed_written: int = 0
    # Coursework with no due_at at all. Counted for the run log, never warned
    # about: 25 of 46 measured items have no due date, so this is the normal
    # majority case and not a data problem to report.
    without_due_date: int = 0
    considered: int = 0

    @property
    def event_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.type] = counts.get(event.type, 0) + 1
        return counts


def _parse_iso(value: str) -> datetime | None:
    """Read one stored UTC timestamp, or None if it is not one."""
    try:
        return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _coursework_candidates(
    db: sqlite3.Connection, course_ids: list[str]
) -> list[Candidate]:
    """Live coursework in the tracked courses, with its submission state.

    Soft-deleted coursework is excluded: a teacher who removed the assignment
    is not expecting it handed in. The submission join is LEFT because an
    assignment with no submission row is still outstanding -- that is precisely
    the not-started case a deadline alert exists for.
    """
    if not course_ids:
        return []

    placeholders = ", ".join("?" for _ in course_ids)
    rows = db.execute(
        f"""
        SELECT cw.id, cw.course_id, cw.title, cw.due_at, cw.alternate_link,
               s.state AS submission_state
        FROM coursework cw
        LEFT JOIN submissions s
               ON s.coursework_id = cw.id AND s.deleted_at IS NULL
        WHERE cw.course_id IN ({placeholders})
          AND cw.deleted_at IS NULL
        """,
        course_ids,
    ).fetchall()
    return [
        Candidate(
            entity_type="coursework",
            entity_id=str(row["id"]),
            course_id=row["course_id"],
            title=row["title"],
            due_at=row["due_at"],
            link=row["alternate_link"],
            done=(row["submission_state"] or "") in DONE_STATES,
        )
        for row in rows
    ]


def _manual_task_candidates(db: sqlite3.Connection) -> list[Candidate]:
    """Tutorials and exercise sheets entered by hand.

    Deliberately not filtered by the course list. A manual task only ever
    exists because I typed it in against a subject myself, so there is no
    allowlist to respect -- and filtering by `courses.tracked` would silently
    drop every task belonging to a subject with no Classroom, which is most of
    what this table is for.
    """
    rows = db.execute(
        "SELECT t.id, t.course_id, t.title, t.kind, t.due_at, t.done_at "
        "  FROM manual_tasks t "
        " WHERE t.due_at IS NOT NULL"
    ).fetchall()
    return [
        Candidate(
            entity_type="manual_task",
            entity_id=str(row["id"]),
            course_id=row["course_id"],
            title=row["title"],
            due_at=row["due_at"],
            done=row["done_at"] is not None,
            label=str(row["kind"] or "tutorial").replace("_", " "),
        )
        for row in rows
    ]


def _project_candidates(db: sqlite3.Connection) -> list[Candidate]:
    """Open projects. Not filtered by a course list, for the same reason tasks
    are not: a project exists because I typed it in, and most of them belong to
    subjects Classroom knows nothing about."""
    rows = db.execute(
        "SELECT id, course_id, title, deadline_at, coursework_id "
        "  FROM projects WHERE closed_at IS NULL"
    ).fetchall()
    return [
        Candidate(
            entity_type="project",
            entity_id=str(row["id"]),
            course_id=row["course_id"],
            title=row["title"],
            due_at=row["deadline_at"],
            label="project",
            links_to=row["coursework_id"],
        )
        for row in rows
    ]


def _merge_linked_projects(
    coursework: list[Candidate], projects: list[Candidate]
) -> list[Candidate]:
    """ONE deadline for work that is both a project and a posted assignment.

    A project that names a `coursework_id` is the same work as that assignment,
    so two candidates would mean two alerts at every threshold for one hand-in.
    The rule:

      * The surviving candidate is keyed on the COURSEWORK. That matters for
        more than tidiness -- events already written for that assignment,
        possibly months before the project was recorded, keep deduplicating it.
      * The professor's posted due date wins, because it is the fact; the one
        I typed is a memory of a verbal brief. 54% of measured coursework
        carries no dueDate at all, and in exactly that case the project's own
        deadline is used -- which is the common case for a project, and the
        reason the link is worth having.
      * The event names the PROJECT, because that is what I call the work.
      * A linked assignment that is TURNED_IN or RETURNED is already `done`,
        so the project falls silent with it. That needs no code here.

    A project whose link names an assignment the sync has not seen yet keeps
    its own candidate: the brief usually arrives weeks before the post does.
    """
    by_id = {candidate.entity_id: candidate for candidate in coursework}
    merged = dict(by_id)
    standalone: list[Candidate] = []

    for project in projects:
        linked = by_id.get(project.links_to) if project.links_to else None
        if linked is None:
            standalone.append(project)
            continue
        merged[linked.entity_id] = replace(
            linked,
            title=project.title,
            due_at=linked.due_at or project.due_at,
            label="project",
        )

    return list(merged.values()) + standalone


def _candidates(db: sqlite3.Connection, course_ids: list[str]) -> list[Candidate]:
    """Everything that is due, from every source that has due dates."""
    return (
        _merge_linked_projects(
            _coursework_candidates(db, course_ids), _project_candidates(db)
        )
        + _manual_task_candidates(db)
    )


def scan(
    db: sqlite3.Connection,
    course_ids: list[str],
    *,
    now: datetime | None = None,
) -> DeadlineScan:
    """Every deadline event that is due to be emitted and has not been yet.

    Reads only. The caller writes, so a dry run is the same code path.
    """
    moment = now or datetime.now(timezone.utc)
    result = DeadlineScan(events=[])

    for row in _candidates(db, course_ids):
        result.considered += 1

        # No due date is the common case, not an anomaly: only 46% of measured
        # coursework carries one, and the other 54% are silently skipped.
        # Warning about them would mean a warning per sync per item forever.
        if not row.due_at:
            result.without_due_date += 1
            continue

        due_at = _parse_iso(row.due_at)
        if due_at is None:
            result.without_due_date += 1
            continue

        # Handed in, marked, or ticked off -- nothing left to chase.
        if row.done:
            continue

        # Already past. Without this the scanner announces at 03:00 that
        # something was due last month, every single run, forever.
        if moment >= due_at:
            continue

        crossed = []
        for event_type, hours in THRESHOLDS:
            threshold = due_at - timedelta(hours=hours)
            if moment < threshold:
                continue
            if _already_emitted(db, event_type, row.entity_type, row.entity_id):
                continue
            crossed.append(_event(event_type, hours, row, due_at, threshold))

        if not crossed:
            continue

        # Running normally -- twice a day -- exactly one threshold is newly
        # crossed and this is a list of one. After downtime it can hold two or
        # three, and only the most urgent is worth sending: three alerts about
        # one assignment is noise, and the first one read would say "72 hours"
        # when the true answer is three. THRESHOLDS runs far-to-near, so the
        # last entry is always the most urgent one.
        result.events.append(crossed[-1])
        # The rest are recorded anyway, stamped as already notified by the
        # caller. Dropping them instead would leave them uncrossed in the
        # events table, and the next scan would fire "72 hours left" for a
        # deadline that by then is an hour away.
        result.suppressed.extend(crossed[:-1])

    return result


def _already_emitted(
    db: sqlite3.Connection, event_type: str, entity_type: str, entity_id: str
) -> bool:
    """Has this threshold ever been recorded for this thing?

    Deliberately ignores notified_at, and both values matter here. A NULL means
    the alert is written but still queued, and emitting a second one would put
    it in the digest twice. A stamped one means it was either sent or
    suppressed in favour of a more urgent threshold -- and a suppressed
    threshold must stay suppressed, or it fires late on the next scan.
    """
    row = db.execute(
        "SELECT 1 FROM events WHERE type = ? AND entity_type = ? "
        "AND entity_id = ? LIMIT 1",
        (event_type, entity_type, entity_id),
    ).fetchone()
    return row is not None


def _event(
    event_type: str,
    hours: int,
    row: Candidate,
    due_at: datetime,
    threshold: datetime,
) -> Event:
    """One deadline event, timestamped at the threshold rather than at now.

    created_at is the moment the threshold was crossed, not the moment this
    scan happened to run. That matters: the unique index is on (type,
    entity_type, entity_id, created_at), so a wall-clock created_at would give
    the same alert a different key on every run and the index would stop
    deduplicating. Anchoring it to the deadline makes the key stable, which is
    what lets a stateless re-scan be safe. _already_emitted is the primary
    guard; this makes the index a real backstop rather than a decoration.
    """
    payload: dict[str, Any] = {
        "title": row.title,
        "due_at": iso(due_at),
        "hours_before": hours,
        "alternate_link": row.link,
    }
    if row.label:
        # So the digest can say "exercise sheet" rather than guessing from the
        # entity type. Absent for coursework, where there is nothing to add.
        payload["kind"] = row.label
    return Event(
        type=event_type,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        course_id=row.course_id,
        payload=payload,
        created_at=iso(threshold),
    )
