"""The weekly pattern, plus what actually happened to this particular date.

`timetable.yaml` holds the stable pattern and is read and never written --
Phase 3a settled that, and an app that wrote sessions back would give one
source of truth two writers. A professor moving Tuesday's lecture is not a
change to the pattern; it is one dated fact about one Tuesday. So the file
stays as it is and this module resolves the two together:

    pattern for a date  →  minus what was cancelled
                        →  relocated where something moved
                        →  plus what arrived from elsewhere or was added

Three rules that are not arbitrary:

**A move changes which evening gates it.** The gate fires the night before a
session, so a lecture moved from Monday to Wednesday must be prompted on
Tuesday evening and not on Sunday. That falls out of resolving by date rather
than by pattern: the session simply is not on Monday any more.

**An adjustment beats an `exceptions:` entry.** A session moved onto -- or
added to -- a date the file calls a holiday still happens, because a makeup
class during a reading week is a real thing and the adjustment is both more
specific and more recent than the blanket rule. The same goes for a date no
version covers: the professor scheduled it there, and a resolution that
silently dropped it would be the failure this whole layer exists to prevent,
one step further along.

**Cancelling a joint session cancels the session.** A JOINT is one session, in
one room, at one time, serving two subjects. The adjustment names one of its
subjects in order to identify it; the thing that does not happen is the
session. Callers say so out loud when they record one, because "I cancelled
Database" reading as "and OS with it" is worth a sentence.

**An adjustment whose subject no longer exists in the file is an ORPHAN.** It
is never applied, never silently dropped, and always reported -- see `orphans`.
Re-binding it by course id would be a second matching path, and a wrong match
adjusts the wrong session and looks exactly like it working, which is the
reason PLAN.md forbids approximate subject matching in the first place.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import date, timedelta

from ..db import store
from . import timetable as tt

_WEEKDAY_NAMES = {index: name for name, index in tt.WEEKDAYS.items()}


class AdjustmentError(Exception):
    """An adjustment cannot be built or applied, for a stated reason."""


@dataclass(frozen=True)
class Adjustment:
    """One dated change, as stored. A row, not a session."""

    id: int
    applies_on: date
    kind: str
    subject: str
    course_id: str | None
    session_start: str
    to_date: date | None = None
    to_start: str | None = None
    to_end: str | None = None
    to_kind: str | None = None
    to_room: str | None = None
    to_teacher: str | None = None
    reason: str | None = None

    @property
    def lands_on(self) -> date:
        """The date this session actually happens on. Its own, unless moved."""
        return self.to_date or self.applies_on

    @property
    def moves_date(self) -> bool:
        return self.kind == "moved" and self.to_date is not None and (
            self.to_date != self.applies_on
        )


@dataclass(frozen=True)
class Resolved:
    """One session on one date, and why it is there if not simply the pattern."""

    session: tt.Session
    adjustment: Adjustment | None = None
    moved_from: date | None = None

    @property
    def adjusted(self) -> bool:
        return self.adjustment is not None

    @property
    def note(self) -> str:
        """Why this day differs from the pattern, in one phrase or none."""
        if self.adjustment is None:
            return ""
        if self.adjustment.kind == "extra":
            return "added"
        if self.moved_from is not None:
            return (
                f"moved from {self.moved_from:%a %d %b} "
                f"{self.adjustment.session_start}"
            )
        return f"moved from {self.adjustment.session_start}"


@dataclass(frozen=True)
class Orphan:
    """A stored adjustment that names nothing the file still contains."""

    adjustment: Adjustment
    why: str
    # What the course it pointed at is called now, when that can be worked out.
    renamed_to: str | None = None


def from_row(row: sqlite3.Row) -> Adjustment:
    return Adjustment(
        id=int(row["id"]),
        applies_on=date.fromisoformat(str(row["applies_on"])),
        kind=str(row["kind"]),
        subject=str(row["subject"]),
        course_id=row["course_id"],
        session_start=str(row["session_start"]),
        to_date=date.fromisoformat(str(row["to_date"])) if row["to_date"] else None,
        to_start=row["to_start"],
        to_end=row["to_end"],
        to_kind=row["to_kind"],
        to_room=row["to_room"],
        to_teacher=row["to_teacher"],
        reason=row["reason"],
    )


def _minutes(clock: str) -> int:
    hours, _, minutes = clock.partition(":")
    return int(hours) * 60 + int(minutes)


def _clock(total: int) -> str:
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def pattern_session(
    table: tt.Timetable, day: date, subject: str, start: str
) -> tt.Session | None:
    """The session the file puts on this date at this time for this subject.

    A JOINT session is found by either of its subjects, because it is one
    session and either name identifies it.
    """
    for session in table.sessions_on(day):
        if session.start == start and subject in session.subjects:
            return session
    return None


def sessions_for_subject(table: tt.Timetable, day: date, subject: str) -> list[tt.Session]:
    """Every session on this date that involves a subject. Usually one."""
    return [
        session for session in table.sessions_on(day) if subject in session.subjects
    ]


def _relocate(session: tt.Session, change: Adjustment) -> tt.Session:
    """The same session, where it actually took place.

    Only what the adjustment states is changed. An end time it does not state
    is derived by keeping the session's own length -- a lecture moved from
    08:30 to 13:00 is still ninety minutes, and making me retype that is
    friction with no answer attached.
    """
    start = change.to_start or session.start
    if change.to_end:
        end = change.to_end
    elif change.to_start:
        length = _minutes(session.end) - _minutes(session.start)
        end = _clock(_minutes(start) + length)
    else:
        end = session.end

    parts = session.parts
    if change.to_room:
        parts = tuple(replace(part, room=change.to_room) for part in parts)

    day_name = session.day
    if change.to_date is not None:
        day_name = _WEEKDAY_NAMES.get(change.to_date.weekday(), session.day)

    return replace(
        session,
        day=day_name,
        start=start,
        end=end,
        kind=change.to_kind or session.kind,
        parts=parts,
    )


def _build_extra(table: tt.Timetable, change: Adjustment) -> tt.Session | None:
    """A session the pattern does not contain at all. None if unbuildable.

    Single-subject only. An extra JOINT session is two teachers agreeing to
    share a slot that does not exist in the file, which has not happened and
    would need a shape this table does not have -- recorded as an open
    question rather than half-built.
    """
    if change.subject not in table.subjects:
        return None
    part = tt.SessionPart(
        subject=change.subject,
        course_id=table.subjects[change.subject],
        teacher=change.to_teacher,
        room=change.to_room,
    )
    start = change.to_start or change.session_start
    end = change.to_end or _clock(_minutes(start) + 90)
    landing = change.lands_on
    return tt.Session(
        day=_WEEKDAY_NAMES.get(landing.weekday(), "mon"),
        start=start,
        end=end,
        kind=change.to_kind or "LEC",
        parts=(part,),
    )


def sessions_on(
    conn: sqlite3.Connection, table: tt.Timetable, day: date
) -> tuple[Resolved, ...]:
    """What actually happens on one date: the pattern, adjusted.

    Pure with respect to the clock -- it answers for the date it is asked
    about and reads nothing about today, so the gate, `agent timetable --on`
    and a test all get the same answer.
    """
    changes = [from_row(row) for row in store.adjustments_for(conn, day.isoformat())]
    by_here = {
        (change.session_start, change.subject): change
        for change in changes
        if change.applies_on == day
    }

    resolved: list[Resolved] = []

    # 1. The pattern, minus and as modified by what was adjusted about this date.
    for session in table.sessions_on(day):
        change = next(
            (
                found
                for (start, subject), found in by_here.items()
                if start == session.start and subject in session.subjects
            ),
            None,
        )
        if change is None:
            resolved.append(Resolved(session=session))
            continue
        if change.kind == "cancelled":
            continue
        if change.kind == "moved" and not change.moves_date:
            resolved.append(Resolved(session=_relocate(session, change), adjustment=change))
        # A move to another date is handled by that date, in step 2.

    # 2. What arrives on this date from somewhere else, or was added outright.
    #    Neither is filtered by version coverage or by `exceptions:` -- see the
    #    module docstring. The professor scheduled it here.
    for change in changes:
        if change.kind == "extra" and change.applies_on == day:
            extra = _build_extra(table, change)
            if extra is not None:
                resolved.append(Resolved(session=extra, adjustment=change))
        elif change.kind == "moved" and change.moves_date and change.to_date == day:
            origin = pattern_session(
                table, change.applies_on, change.subject, change.session_start
            )
            if origin is not None:
                resolved.append(
                    Resolved(
                        session=_relocate(origin, change),
                        adjustment=change,
                        moved_from=change.applies_on,
                    )
                )

    # Same ordering rule as the file: 'HH:MM' is zero-padded, so a string sort
    # is a time sort.
    return tuple(sorted(resolved, key=lambda item: (item.session.start, item.session.kind)))


def departures(
    conn: sqlite3.Connection, table: tt.Timetable, day: date
) -> tuple[Resolved, ...]:
    """What the pattern puts on this date but no longer happens on it.

    `sessions_on` answers "what is happening", which is what the gate needs.
    This answers "why is this day shorter than the printed timetable", which is
    what I need when I am looking at the day and counting. A cancelled lecture
    that simply vanishes leaves the difference unexplained, and an unexplained
    difference is indistinguishable from a bug in the resolver.

    The session returned is the PATTERN's, not the relocated one -- it is being
    shown in order to be crossed out.
    """
    changes = {
        (change.session_start, change.subject): change
        for change in (
            from_row(row) for row in store.adjustments_for(conn, day.isoformat())
        )
        if change.applies_on == day and change.kind in ("cancelled", "moved")
    }
    gone: list[Resolved] = []
    for session in table.sessions_on(day):
        change = next(
            (
                found
                for (start, subject), found in changes.items()
                if start == session.start and subject in session.subjects
            ),
            None,
        )
        if change is None:
            continue
        if change.kind == "cancelled" or change.moves_date:
            gone.append(Resolved(session=session, adjustment=change))
    return tuple(sorted(gone, key=lambda item: item.session.start))


def departure_note(resolved: Resolved) -> str:
    """How a session that is no longer here should read. One phrase."""
    change = resolved.adjustment
    if change is None:
        return ""
    if change.kind == "cancelled":
        return f"cancelled{f' -- {change.reason}' if change.reason else ''}"
    landing = change.lands_on
    return f"moved to {landing:%a %d %b} {change.to_start or resolved.session.start}"


def orphans(conn: sqlite3.Connection, table: tt.Timetable) -> list[Orphan]:
    """Stored adjustments that name nothing the file still contains.

    The honest half of refusing to match approximately. An orphan is never
    applied and never silently dropped -- it is listed here so the row can be
    repointed or removed, which is a decision only I can take.

    Where the subject is gone but its course id is still in the map under a new
    name, that name is reported, because "Calculus III is now called Calculus
    3" is the whole answer and making me work it out is a waste of the fact.
    """
    by_course = {
        course: name for name, course in table.subjects.items() if course is not None
    }
    found: list[Orphan] = []
    for row in store.list_adjustments(conn, limit=10_000):
        change = from_row(row)
        if change.subject in table.subjects:
            if change.kind == "extra":
                continue
            if pattern_session(
                table, change.applies_on, change.subject, change.session_start
            ) is not None:
                continue
            found.append(
                Orphan(
                    adjustment=change,
                    why=(
                        f"{table.path.name} has no {change.subject} session at "
                        f"{change.session_start} on {change.applies_on:%a %d %b %Y}"
                    ),
                )
            )
            continue

        renamed = by_course.get(change.course_id) if change.course_id else None
        found.append(
            Orphan(
                adjustment=change,
                why=f"{table.path.name} no longer has a subject called {change.subject!r}",
                renamed_to=renamed,
            )
        )
    return found


# ----------------------------------------------------------------------------
# recording one -- resolution, refusal, and the write
# ----------------------------------------------------------------------------
#
# This half used to live in cli.py, shaped around an argparse Namespace and
# print(). It moved down when the HTTP API needed the same resolution, because
# two implementations of "which session does this name" is how the CLI and the
# API come to disagree -- and a disagreement between them looks exactly like
# either one working.
#
# Nothing here prints and nothing here commits. `plan` validates and resolves,
# `record` writes, and the caller renders and commits. What a caller needs in
# order to narrate a recorded adjustment is carried on AdjustmentPlan as facts
# rather than as sentences.


def exact_subject(table: tt.Timetable, name: str) -> str:
    """The subject as the file spells it, or a refusal listing what it knows.

    Case-insensitive because I type it, exact otherwise because a wrong match
    adjusts the wrong session and looks exactly like this working -- the same
    rule as the subjects map itself. Unlike `entries.subject_course` this allows
    a subject mapped to null: a manual subject's sessions move too.
    """
    matches = [known for known in table.subjects if known.casefold() == name.casefold()]
    if not matches:
        known = ", ".join(sorted(table.subjects)) or "(none)"
        raise AdjustmentError(
            f"{name!r} is not a subject in {table.path.name}, and names "
            f"are never matched approximately -- a wrong match adjusts the wrong "
            f"session and looks exactly like this working.\n"
            f"  Known subjects: {known}"
        )
    return matches[0]


def resolve_one_session(
    table: tt.Timetable, day: date, subject: str, at: str | None
) -> tt.Session:
    """Which session on that date, refusing rather than guessing.

    A subject meeting twice on one day is two sessions, and adjusting the wrong
    one is a failure that looks exactly like success -- the same reason subject
    names are never matched approximately.
    """
    found = sessions_for_subject(table, day, subject)
    if at is not None:
        exact = [session for session in found if session.start == at]
        if not exact:
            times = ", ".join(session.start for session in found) or "none"
            raise AdjustmentError(
                f"{table.path.name} has no {subject} session at {at} on "
                f"{day:%a %d %b %Y}. That day has: {times}"
            )
        return exact[0]
    if not found:
        raise AdjustmentError(
            f"{table.path.name} has no {subject} session on {day:%a %d %b %Y}.\n"
            f"  For a session the pattern does not contain, use --extra."
        )
    if len(found) > 1:
        times = ", ".join(session.start for session in found)
        raise AdjustmentError(
            f"{subject} meets {len(found)} times on {day:%a %d %b %Y} ({times}). "
            f"Say which with --at."
        )
    return found[0]


@dataclass(frozen=True)
class AdjustmentSpec:
    """A dated change as asked for, before anything has been resolved.

    The dates and times arrive as strings because that is how a command line and
    a JSON body both carry them, and refusing a bad one is part of resolution
    rather than something every caller does first.
    """

    kind: str  # 'cancelled' | 'moved' | 'extra'
    subject: str
    on: str
    at: str | None = None
    to_date: str | None = None
    to_time: str | None = None
    end: str | None = None
    to_kind: str | None = None
    room: str | None = None
    teacher: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AdjustmentPlan:
    """A resolved adjustment, with nothing written yet.

    `fields` is exactly what `store.add_adjustment` takes. Everything beside it
    is a fact a caller needs in order to say what this does -- which session
    matched, whether it is joint, where it lands -- so that the narration is
    built from the resolution rather than repeating it.
    """

    fields: dict[str, object]
    kind: str
    subject: str
    day: date
    start: str
    session_kind: str | None = None
    joint_subjects: tuple[str, ...] = ()
    landing: date | None = None

    @property
    def joint(self) -> bool:
        return bool(self.joint_subjects)

    @property
    def gate_moves(self) -> bool:
        """Whether the evening that prompts for this session changes.

        Falls out of resolving by date rather than by pattern: a session moved
        onto another day is gated the evening before THAT day.
        """
        return self.landing is not None and self.landing != self.day

    @property
    def gate_evening(self) -> date | None:
        return self.landing - timedelta(days=1) if self.gate_moves else None


def plan(table: tt.Timetable, spec: AdjustmentSpec) -> AdjustmentPlan:
    """Resolve a spec against the file, refusing anything ambiguous.

    Raises `AdjustmentError` for everything a person has to fix. Writes nothing
    and reads no database -- a clash with an existing row is `record`'s answer,
    because only the database knows about one.
    """
    if not spec.subject or not spec.on:
        raise AdjustmentError(f"--{spec.kind} needs --subject and --on.")

    day = _date_or_refuse(spec.on, "--on")
    subject = exact_subject(table, spec.subject)

    at = _clock_or_refuse(spec.at, "--at") if spec.at else None
    to_time = _clock_or_refuse(spec.to_time, "--to-time") if spec.to_time else None
    end = _clock_or_refuse(spec.end, "--end") if spec.end else None

    fields: dict[str, object] = {
        "applies_on": day.isoformat(),
        "kind": spec.kind,
        "subject": subject,
        "course_id": table.subjects[subject],
        "reason": spec.reason,
    }

    if spec.kind == "extra":
        # An extra session is not in the pattern, so there is nothing to resolve
        # against -- only a start time to insist on.
        if to_time is None and at is None:
            raise AdjustmentError("--extra needs --to-time (when it starts).")
        start = to_time or at
        assert start is not None  # narrowed by the check above
        fields.update(
            session_start=start,
            to_date=day.isoformat(),
            to_start=start,
            to_end=end,
            to_kind=spec.to_kind or "LEC",
            to_room=spec.room,
            to_teacher=spec.teacher,
        )
        return AdjustmentPlan(
            fields=fields, kind=spec.kind, subject=subject, day=day, start=start
        )

    session = resolve_one_session(table, day, subject, at)
    fields["session_start"] = session.start
    joint_subjects = tuple(session.subjects) if session.joint else ()

    if spec.kind == "cancelled":
        return AdjustmentPlan(
            fields=fields,
            kind=spec.kind,
            subject=subject,
            day=day,
            start=session.start,
            session_kind=session.kind,
            joint_subjects=joint_subjects,
        )

    if to_time is None and spec.to_date is None:
        raise AdjustmentError(
            "--move needs --to (a new date) or --to-time (a new time)."
        )
    landing = _date_or_refuse(spec.to_date, "--to") if spec.to_date else day
    fields.update(
        to_date=landing.isoformat(),
        to_start=to_time,
        to_end=end,
        to_kind=spec.to_kind,
        to_room=spec.room,
    )
    return AdjustmentPlan(
        fields=fields,
        kind=spec.kind,
        subject=subject,
        day=day,
        start=session.start,
        session_kind=session.kind,
        joint_subjects=joint_subjects,
        landing=landing,
    )


def record(conn: sqlite3.Connection, adjustment: AdjustmentPlan) -> int | None:
    """Write the row. None when this session on this date already has one.

    Refused rather than merged, and None rather than an exception, because which
    of two adjustments wins is not a question anything here can answer and
    guessing silently discards one. The caller names the clash with `find_clash`.
    """
    return store.add_adjustment(conn, **adjustment.fields)


def find_clash(
    conn: sqlite3.Connection, adjustment: AdjustmentPlan
) -> sqlite3.Row | None:
    """The row that made `record` return None, so a caller can name it."""
    return store.find_adjustment(
        conn,
        applies_on=str(adjustment.fields["applies_on"]),
        subject=adjustment.subject,
        session_start=str(adjustment.fields["session_start"]),
    )


def repoint(
    conn: sqlite3.Connection, table: tt.Timetable, adjustment_id: int, subject: str
) -> tuple[str, str] | None:
    """Move an orphan onto a subject the file still has.

    Returns (old subject, new subject), or None when there is no such row. The
    only way an orphan ever moves: matching approximately is what adjusts the
    wrong session while looking like it worked.
    """
    resolved = exact_subject(table, subject)
    row = store.get_adjustment(conn, adjustment_id)
    if row is None:
        return None
    store.repoint_adjustment(
        conn, adjustment_id, subject=resolved, course_id=table.subjects[resolved]
    )
    return str(row["subject"]), resolved


def remove(conn: sqlite3.Connection, adjustment_id: int) -> sqlite3.Row | None:
    """Delete one, returning what it was. None when there is no such row.

    The pattern reasserts itself afterwards, because nothing in the file ever
    moved.
    """
    row = store.get_adjustment(conn, adjustment_id)
    if row is None:
        return None
    store.delete_adjustment(conn, adjustment_id)
    return row


def _date_or_refuse(value: object, flag: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as err:
        raise AdjustmentError(
            f"{flag} must be a date like 2026-09-21, got {value!r}."
        ) from err


def _clock_or_refuse(value: object, flag: str) -> str:
    text = str(value).strip()
    hours, _, minutes = text.partition(":")
    if not (len(text) == 5 and text[2] == ":" and hours.isdigit() and minutes.isdigit()):
        raise AdjustmentError(f'{flag} must look like "08:30", got {value!r}.')
    if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
        raise AdjustmentError(f"{flag} is not a real time: {value!r}.")
    return text
