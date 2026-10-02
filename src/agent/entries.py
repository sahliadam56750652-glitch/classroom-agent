"""Validation and orchestration for everything entered by hand.

`manual.py` owns identity and minting -- the reserved `manual-` prefix and the
guards that keep a minted id out of the poller. This owns the layer above it:
turning what I typed, or what the web client posted, into the rows the store
writes, with every refusal that keeps a wrong entry out.

Two rules make this module what it is.

**Nothing here prints.** A refusal is an exception or a status on a result
object, never a line on stdout, because the same refusal has to arrive at a
terminal and at an HTTP client and neither one should have to parse the other's
output.

**Nothing here commits.** The caller does, which is the same convention
`db/store.py` already follows. `cli.py` commits at the end of a command; the
API commits in one dependency. A function that committed for itself would make
"the project and its milestones land together or not at all" impossible to
arrange from outside.

Together they are what lets `agent projects --add` and `POST /api/projects`
share one implementation and therefore be unable to disagree -- which is the
whole point, because a disagreement between the two looks exactly like either
one working.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .classroom.models import ISO_FORMAT
from .config import Config, ConfigError, display_zone
from .db import store
from .files import upload as upload_mod
from .gate.timetable import Timetable

# ----------------------------------------------------------------------------
# parsing -- the things typed by hand that have to be rejected, not guessed
# ----------------------------------------------------------------------------


def parse_date(value: Any, flag: str) -> date:
    """An ISO date, or a refusal naming the flag it came from."""
    try:
        return date.fromisoformat(str(value))
    except ValueError as err:
        raise ConfigError(
            f"{flag} must be a date like 2026-09-21, got {value!r}."
        ) from err


def parse_clock(value: Any, flag: str) -> str:
    """'HH:MM', validated rather than parsed.

    These arrive from a command line or a JSON body rather than from YAML, so
    the sexagesimal trap that `timetable._clock` exists for cannot fire here --
    but a typo still can, and a session stored at a time nobody wrote is
    exactly as wrong.
    """
    text = str(value).strip()
    hours, _, minutes = text.partition(":")
    if not (len(text) == 5 and text[2] == ":" and hours.isdigit() and minutes.isdigit()):
        raise ConfigError(f'{flag} must look like "08:30", got {value!r}.')
    if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
        raise ConfigError(f"{flag} is not a real time: {value!r}.")
    return text


def parse_due(config: Config, value: str) -> str:
    """A due date as I would say it, stored as UTC like everything else.

    A date with no time means END OF DAY locally, which is what Classroom means
    by an absent dueTime -- so a sheet due "Friday" is not silently due at
    Friday midnight, twenty-four hours early.
    """
    zone = display_zone(config.timezone)
    text = value.strip().replace("T", " ")
    for pattern, complete in (("%Y-%m-%d %H:%M", None), ("%Y-%m-%d", "eod")):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if complete == "eod":
            parsed = parsed.replace(hour=23, minute=59)
        local = parsed.replace(tzinfo=zone)
        return local.astimezone(timezone.utc).strftime(ISO_FORMAT)
    raise ConfigError(
        f"--due must be YYYY-MM-DD or 'YYYY-MM-DD HH:MM', got {value!r}."
    )


def today(config: Config) -> date:
    """Today, locally. A session was held on a wall-clock day."""
    return datetime.now(display_zone(config.timezone)).date()


def joined(values: list[str] | None) -> str | None:
    """Repeated values as one TEXT column, one per line. Described, never computed."""
    return "\n".join(values) if values else None


def subject_course(table: Timetable, name: str) -> tuple[str, str]:
    """(subject, course id) for a name. Exact, and it says what to do if not.

    Shared by every hand-entry path, so a subject resolves one way everywhere --
    through the timetable's `subjects:` map and never a near-match, because a
    wrong match files work under the wrong subject and looks exactly like the
    feature working.

    Takes a loaded table rather than a Config so that a caller holding one does
    not re-read the file per entry. `upload.resolve_subject` is still the single
    implementation of the refusal.
    """
    return upload_mod.resolve_subject(table, name)


# ----------------------------------------------------------------------------
# outcomes -- the three-way results that are not errors
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What happened to a row that may not have existed or may already be done.

    Three statuses rather than a bool, because "there is no such milestone" and
    "that milestone was already done" are different facts and a caller that
    cannot tell them apart will report one as the other. That is the recurring
    lesson in miniature.
    """

    status: str  # 'done' | 'already' | 'missing'
    row: sqlite3.Row | None = None

    @property
    def ok(self) -> bool:
        """Whether the row is now in the state asked for, however it got there."""
        return self.status in ("done", "already")


# ----------------------------------------------------------------------------
# projects
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectSpec:
    """A project as asked for, before anything has been resolved or validated."""

    subject: str
    title: str
    deadline: str | None = None
    deliverables: list[str] = field(default_factory=list)
    team: list[str] = field(default_factory=list)
    milestones: list[str] = field(default_factory=list)
    brief: str | None = None
    coursework: str | None = None


@dataclass(frozen=True)
class ProjectPlan:
    """Everything a project needs written, with nothing written yet.

    Separated from the write so a dry run resolves the subject, parses the
    deadline and hits the same refusals as a real one -- a dry run that takes a
    different path proves nothing.
    """

    subject: str
    course_id: str
    title: str
    deadline_at: str | None
    deliverables: list[str]
    team: list[str]
    milestones: list[str]
    brief: str | None
    coursework: str | None


def plan_project(config: Config, table: Timetable, spec: ProjectSpec) -> ProjectPlan:
    """Resolve and validate a project. Raises rather than returning a code."""
    if not spec.subject or not spec.title:
        raise ConfigError("a project needs a subject and a title.")
    subject, course_id = subject_course(table, spec.subject)
    return ProjectPlan(
        subject=subject,
        course_id=course_id,
        title=spec.title,
        deadline_at=parse_due(config, spec.deadline) if spec.deadline else None,
        deliverables=list(spec.deliverables),
        team=list(spec.team),
        milestones=list(spec.milestones),
        brief=spec.brief,
        coursework=spec.coursework,
    )


def create_project(conn: sqlite3.Connection, plan: ProjectPlan) -> int | None:
    """Write the project and its milestones. None when that name is taken.

    None is not an error: the UNIQUE key did its job, and two projects with one
    name in one subject would be two deadlines for one piece of work.
    """
    project_id = store.add_project(
        conn,
        course_id=plan.course_id,
        title=plan.title,
        deadline_at=plan.deadline_at,
        deliverables=joined(plan.deliverables),
        team=joined(plan.team),
        brief_source=plan.brief,
        coursework_id=plan.coursework,
    )
    if project_id is None:
        return None
    for name in plan.milestones:
        store.add_milestone(conn, project_id, name)
    return project_id


def complete_milestone(conn: sqlite3.Connection, milestone_id: int) -> Outcome:
    """Mark a milestone done. The only way project progress moves."""
    row = store.get_milestone(conn, milestone_id)
    if row is None:
        return Outcome("missing")
    if row["done_at"]:
        return Outcome("already", row)
    store.complete_milestone(conn, milestone_id)
    return Outcome("done", row)


def close_project(conn: sqlite3.Connection, project_id: int) -> Outcome:
    """Stop chasing a project for a deadline."""
    row = store.get_project(conn, project_id)
    if row is None:
        return Outcome("missing")
    if row["closed_at"]:
        return Outcome("already", row)
    store.close_project(conn, project_id)
    return Outcome("done", row)


# ----------------------------------------------------------------------------
# tasks
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSpec:
    subject: str
    title: str
    kind: str
    due: str | None = None
    notes: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class TaskPlan:
    subject: str
    course_id: str
    title: str
    kind: str
    due_at: str | None
    notes: str | None
    source: str | None

    @property
    def unscanned(self) -> bool:
        """Recorded, and invisible to the only thing that would have chased it.

        Worth a property rather than a check at each call site: an undated task
        is a legitimate entry and also a silent one, and every interface has to
        say so.
        """
        return self.due_at is None


def plan_task(config: Config, table: Timetable, spec: TaskSpec) -> TaskPlan:
    subject, course_id = subject_course(table, spec.subject)
    return TaskPlan(
        subject=subject,
        course_id=course_id,
        title=spec.title,
        kind=spec.kind,
        due_at=parse_due(config, spec.due) if spec.due else None,
        notes=spec.notes,
        source=spec.source,
    )


def create_task(conn: sqlite3.Connection, plan: TaskPlan) -> int | None:
    """Record a task. None when the same subject, title and due date exist.

    None is not an error, for the same reason as a duplicate project: two
    identical rows would mean two alerts for one sheet.
    """
    return store.add_manual_task(
        conn,
        course_id=plan.course_id,
        title=plan.title,
        kind=plan.kind,
        due_at=plan.due_at,
        notes=plan.notes,
        source=plan.source,
    )


def complete_task(conn: sqlite3.Connection, task_id: int) -> Outcome:
    row = store.get_manual_task(conn, task_id)
    if row is None:
        return Outcome("missing")
    if row["done_at"]:
        return Outcome("already", row)
    store.complete_manual_task(conn, task_id)
    return Outcome("done", row)


# ----------------------------------------------------------------------------
# sessions held
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSpec:
    subject: str
    kind: str
    on: str | None = None
    covered: str | None = None


@dataclass(frozen=True)
class SessionPlan:
    subject: str
    course_id: str
    held_on: str
    kind: str
    covered: str | None


def plan_session(config: Config, table: Timetable, spec: SessionSpec) -> SessionPlan:
    """The timetable says a session was SCHEDULED; this says one took place."""
    subject, course_id = subject_course(table, spec.subject)
    held_on = (
        today(config).isoformat()
        if spec.on is None
        else parse_date(spec.on, "--on").isoformat()
    )
    return SessionPlan(
        subject=subject,
        course_id=course_id,
        held_on=held_on,
        kind=spec.kind,
        covered=spec.covered,
    )


def log_session(conn: sqlite3.Connection, plan: SessionPlan) -> int:
    return store.log_manual_session(
        conn,
        course_id=plan.course_id,
        held_on=plan.held_on,
        kind=plan.kind,
        covered=plan.covered,
    )


# ----------------------------------------------------------------------------
# homework -- Classroom coursework and the tasks entered by hand, in one list
# ----------------------------------------------------------------------------

# Handed in, or handed back. The same set sync/deadlines.py uses to decide an
# alert would be noise; RECLAIMED_BY_STUDENT is NOT here, because pulling work
# back makes it outstanding again.
DONE_SUBMISSIONS = frozenset({"TURNED_IN", "RETURNED"})


@dataclass(frozen=True)
class Homework:
    """One thing I have to do, from Classroom or typed in by hand.

    `kind` is "classroom" or "task", and it decides what can be done about it
    here: a Classroom item can only be OPENED in Classroom (invariant 6 -- this
    project never writes to Classroom, and turning work in is a write), and a
    task can be marked done, because the row is mine.

    `due_at` is the stored UTC instant and nothing else about time. No days
    left, no severity: DESIGN.md forbids manufactured urgency.
    """

    kind: str
    id: str
    course_id: str
    course_name: str
    title: str
    due_at: str | None
    done: bool
    link: str | None = None
    # Classroom only.
    submission_state: str | None = None
    late: bool = False
    grade: float | None = None
    max_points: float | None = None
    # Task only.
    task_kind: str | None = None
    done_at: str | None = None
    notes: str | None = None


def homework(
    conn: sqlite3.Connection,
    course_ids: list[str],
    *,
    include_done: bool = False,
) -> list[Homework]:
    """Everything due, Classroom and hand-entered, soonest first, undated last.

    Coursework is read for `course_ids` (the caller passes `scope.local`).
    Manual tasks are read whatever their course, for the reason
    sync/deadlines.py gives: a task exists only because I typed it in against a
    subject, so there is no allowlist for it to fall outside.

    Undated items are included. Unlike the deadline list -- which only wants
    what can cross a threshold -- this is the list of what I have to do, and
    25 of 46 measured assignments carry no due date at all.
    """
    found: list[Homework] = []
    for row in store.coursework_with_submissions(conn, course_ids):
        state = row["submission_state"]
        done = (state or "") in DONE_SUBMISSIONS
        if done and not include_done:
            continue
        found.append(
            Homework(
                kind="classroom",
                id=str(row["id"]),
                course_id=str(row["course_id"]),
                course_name=str(row["course_name"] or row["course_id"]),
                title=str(row["title"] or "(untitled)"),
                due_at=row["due_at"],
                done=done,
                link=row["submission_link"] or row["alternate_link"],
                submission_state=state,
                late=bool(row["late"]),
                grade=row["assigned_grade"],
                max_points=row["max_points"],
            )
        )
    for row in store.manual_tasks(conn, include_done=include_done):
        found.append(
            Homework(
                kind="task",
                id=str(row["id"]),
                course_id=str(row["course_id"]),
                course_name=str(row["course_name"] or row["course_id"]),
                title=str(row["title"]),
                due_at=row["due_at"],
                done=row["done_at"] is not None,
                task_kind=str(row["kind"] or "other"),
                done_at=row["done_at"],
                notes=row["notes"],
            )
        )
    found.sort(key=lambda value: (value.due_at is None, value.due_at or "", value.title, value.id))
    return found
