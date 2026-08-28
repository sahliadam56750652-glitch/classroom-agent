"""My week, as a hand-edited YAML file.

Deliberately not in SQLite. The timetable is versioned, semester-scoped
configuration that I revise by hand a few times a term, and mirroring it into
the database would add a second source of truth without adding a single query
worth making -- ~20 sessions a week is a list, not a join. See the note where
the table used to be in `db/schema.sql`.

Three properties matter more than anything else here.

**The subject mapping is explicit and never fuzzy.** My timetable says
"Database"; Classroom says "Database GA 2026". Nothing in this module guesses
at that. Every session names a subject that must appear in the `subjects:` map,
and an unknown one is a load error rather than a near-match, because a wrong
match gates the wrong subject -- which is worse than no gate at all, since it
looks like it worked.

**A subject with no course is a first-class case.** Most of my week has no
Classroom presence: only five courses are tracked and Calculus II is not one of
them. Those sessions still belong in the schedule -- a day that silently omits
half its lectures does not read as my day -- so they map to `None` and are
listed but never gated.

**Absence of a timetable is silence, not a guess.** No version covering a date
returns None; a Sunday or a holiday returns no sessions. Every one of those
means the gate sends nothing, which is the right answer and the one a
best-effort guess would get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# Monday to Saturday. Sunday is rejected by name rather than merely absent from
# the map, so a session typed into the wrong day says so instead of failing an
# obscure key lookup.
WEEKDAYS: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5,
}
SUNDAY = "sun"

KINDS = frozenset({"LEC", "TUT", "LAB", "Project"})

STATUSES = frozenset({"provisional", "confirmed"})

# What an exception with no stated reason is called. A bare date in the file is
# usually a holiday I could not be bothered to name, and the caller prints this
# after "no sessions:" -- so it has to read as a cause, not repeat the effect.
DEFAULT_REASON = "listed as an exception"


class TimetableError(Exception):
    """The timetable file is absent, unreadable, or says something impossible."""


@dataclass(frozen=True)
class SessionPart:
    """One subject's half of a session, with the teacher who takes it.

    A teacher belongs to the session, not to the subject: Calculus II has one
    lecturer and a different tutor. A JOINT session has two parts -- two
    subjects, two teachers, and sometimes two rooms -- and every other session
    has exactly one, so nothing downstream has to branch on which kind it is.
    """

    subject: str
    course_id: str | None
    teacher: str | None
    room: str | None

    @property
    def tracked(self) -> bool:
        """Whether this part can ever be gated. False is normal, not an error."""
        return self.course_id is not None


@dataclass(frozen=True)
class Session:
    day: str
    start: str          # 'HH:MM' local wall clock
    end: str            # 'HH:MM' local wall clock
    kind: str
    parts: tuple[SessionPart, ...]

    @property
    def joint(self) -> bool:
        return len(self.parts) > 1

    @property
    def subjects(self) -> tuple[str, ...]:
        return tuple(part.subject for part in self.parts)

    @property
    def course_ids(self) -> tuple[str, ...]:
        return tuple(part.course_id for part in self.parts if part.course_id)


@dataclass(frozen=True)
class Version:
    """One revision of the timetable, and the dates it applies to.

    Provisional is the normal state of a timetable here and is carried through
    to the message rather than hidden: acting on a draft is fine, believing it
    is final is not.
    """

    label: str
    status: str
    effective_from: date
    effective_to: date | None
    sessions: tuple[Session, ...]

    @property
    def provisional(self) -> bool:
        return self.status == "provisional"

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


@dataclass(frozen=True)
class Exception_:
    """A date range with no sessions, whatever the version says.

    Effective dates handle "the timetable was revised". They do not handle "no
    classes this week", and without this the gate cheerfully prepares me for a
    public holiday -- which is how a gate stops being believed.
    """

    start: date
    end: date
    reason: str

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class Timetable:
    subjects: dict[str, str | None]
    exceptions: tuple[Exception_, ...]
    versions: tuple[Version, ...]
    path: Path

    def version_for(self, day: date) -> Version | None:
        """The version in effect on a date, or None.

        None is a real answer -- before the term starts, or after the last
        version expires -- and the caller sends nothing rather than falling
        back to the nearest version. Overlap is impossible here because it is
        rejected at load time.
        """
        for version in self.versions:
            if version.covers(day):
                return version
        return None

    def exception_for(self, day: date) -> Exception_ | None:
        for entry in self.exceptions:
            if entry.covers(day):
                return entry
        return None

    def sessions_on(self, day: date) -> tuple[Session, ...]:
        """Every session on one date, earliest first. Empty is common."""
        if self.exception_for(day) is not None:
            return ()
        version = self.version_for(day)
        if version is None:
            return ()
        # date.weekday() is Monday=0, which is why WEEKDAYS is numbered that
        # way. Sunday is 6 and no session can carry it.
        index = day.weekday()
        found = [session for session in version.sessions if WEEKDAYS[session.day] == index]
        # 'HH:MM' is zero-padded and fixed-width, so a string sort is a time
        # sort. Kept as a string deliberately: these times are for me to read,
        # nothing computes with them, and parsing them into time objects would
        # invite something to start.
        return tuple(sorted(found, key=lambda session: session.start))

    def course_for(self, subject: str) -> str | None:
        return self.subjects.get(subject)

    def subjects_without_course(self) -> list[str]:
        return sorted(name for name, course in self.subjects.items() if course is None)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _where(context: str) -> str:
    return f" ({context})" if context else ""


def _mapping(value: Any, what: str, context: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TimetableError(
            f"{what} must be a mapping, got {type(value).__name__}{_where(context)}."
        )
    return value


def _text(value: Any, what: str, context: str) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        raise TimetableError(f"{what} must be text{_where(context)}, got {value!r}.")
    text = str(value).strip()
    if not text:
        raise TimetableError(f"{what} must not be empty{_where(context)}.")
    return text


def _optional_text(value: Any, what: str, context: str) -> str | None:
    return None if value is None else _text(value, what, context)


def _clock(value: Any, what: str, context: str) -> str:
    """'HH:MM' in 24-hour local time, validated rather than parsed.

    Unquoted times are the nastiest thing in this file, because YAML 1.1 reads
    them as sexagesimal integers -- but only some of them. Measured with the
    PyYAML this project pins: a bare 12:00 becomes 720 and 13:45 becomes 825,
    while 08:30 stays the string "08:30" because the resolver's pattern will
    not start on a zero.

    So a file that quotes the morning sessions and not the afternoon ones looks
    like it works. The int branch below is what turns that into a message
    instead of a session at a time nobody wrote.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        hours, minutes = divmod(value, 60)
        raise TimetableError(
            f"{what} must be quoted{_where(context)}: YAML read it as the "
            f"number {value}. Write it as \"{hours:02d}:{minutes:02d}\"."
        )
    text = _text(value, what, context)
    hours, _, minutes = text.partition(":")
    if not (len(text) == 5 and text[2] == ":" and hours.isdigit() and minutes.isdigit()):
        raise TimetableError(f"{what} must look like \"08:30\"{_where(context)}, got {text!r}.")
    if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
        raise TimetableError(f"{what} is not a real time{_where(context)}: {text!r}.")
    return text


def _date(value: Any, what: str, context: str) -> date:
    """A YAML date. Written unquoted, so PyYAML already hands back a date."""
    if isinstance(value, date):
        return value
    text = _text(value, what, context)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise TimetableError(
            f"{what} must be a date like 2026-09-14{_where(context)}, got {text!r}."
        ) from None


def _parse_subjects(raw: Any, path: Path) -> dict[str, str | None]:
    section = _mapping(raw, "'subjects'", str(path))
    if not section:
        raise TimetableError(f"{path}: 'subjects' is empty, so no session can name one.")

    subjects: dict[str, str | None] = {}
    for name, course in section.items():
        key = _text(name, "a subject name", str(path))
        if course is None:
            subjects[key] = None
            continue
        if isinstance(course, bool) or not isinstance(course, (str, int)):
            raise TimetableError(
                f"{path}: the course id for {key!r} must be a Classroom course id "
                f"or null, got {course!r}."
            )
        # YAML reads a bare 842149328479 as an int, and every comparison
        # downstream is against a string from the API or from config.yaml.
        subjects[key] = str(course).strip()
    return subjects


def _parse_part(raw: dict[str, Any], subjects: dict[str, str | None],
                context: str, *, room_default: str | None) -> SessionPart:
    subject = _text(raw.get("subject"), "'subject'", context)
    if subject not in subjects:
        known = ", ".join(sorted(subjects)) or "(none)"
        raise TimetableError(
            f"{context}: subject {subject!r} is not in the 'subjects' map, and "
            f"names are never matched approximately -- a wrong match gates the "
            f"wrong subject.\n  Known subjects: {known}"
        )
    room = _optional_text(raw.get("room"), "'room'", context)
    return SessionPart(
        subject=subject,
        course_id=subjects[subject],
        teacher=_optional_text(raw.get("teacher"), "'teacher'", context),
        # A joint session in one room says so once; the second part inherits it
        # rather than repeating it or, worse, silently reading as no room.
        room=room if room is not None else room_default,
    )


def _parse_session(raw: Any, subjects: dict[str, str | None], context: str) -> Session:
    entry = _mapping(raw, "a session", context)

    day = _text(entry.get("day"), "'day'", context).lower()
    if day == SUNDAY:
        raise TimetableError(
            f"{context}: 'day' is {SUNDAY!r}, but the week here runs Monday to "
            f"Saturday. If a session really is on a Sunday, this file is the "
            f"wrong place to say so."
        )
    if day not in WEEKDAYS:
        raise TimetableError(
            f"{context}: 'day' must be one of {', '.join(WEEKDAYS)}, got {day!r}."
        )

    start = _clock(entry.get("start"), "'start'", context)
    end = _clock(entry.get("end"), "'end'", context)
    if end <= start:
        raise TimetableError(f"{context}: 'end' ({end}) is not after 'start' ({start}).")

    kind = _text(entry.get("kind"), "'kind'", context)
    if kind not in KINDS:
        raise TimetableError(
            f"{context}: 'kind' must be one of {', '.join(sorted(KINDS))}, got {kind!r}."
        )

    room = _optional_text(entry.get("room"), "'room'", context)
    parts = [_parse_part(entry, subjects, context, room_default=room)]

    for index, extra in enumerate(entry.get("also") or [], start=1):
        parts.append(
            _parse_part(
                _mapping(extra, "an 'also' entry", context),
                subjects,
                f"{context}, also[{index}]",
                room_default=room,
            )
        )

    seen = [part.subject for part in parts]
    if len(set(seen)) != len(seen):
        raise TimetableError(f"{context}: the same subject appears twice in one session.")

    return Session(day=day, start=start, end=end, kind=kind, parts=tuple(parts))


def _parse_version(raw: Any, subjects: dict[str, str | None], path: Path, index: int) -> Version:
    entry = _mapping(raw, f"versions[{index}]", str(path))
    label = _text(entry.get("label"), "'label'", f"{path}: versions[{index}]")
    context = f"{path}: version {label!r}"

    status = _text(entry.get("status"), "'status'", context)
    if status not in STATUSES:
        raise TimetableError(
            f"{context}: 'status' must be one of {', '.join(sorted(STATUSES))}, "
            f"got {status!r}."
        )

    effective_from = _date(entry.get("effective_from"), "'effective_from'", context)
    raw_to = entry.get("effective_to")
    effective_to = None if raw_to is None else _date(raw_to, "'effective_to'", context)
    if effective_to is not None and effective_to < effective_from:
        raise TimetableError(
            f"{context}: 'effective_to' ({effective_to}) is before "
            f"'effective_from' ({effective_from})."
        )

    raw_sessions = entry.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise TimetableError(f"{context}: 'sessions' must be a non-empty list.")

    sessions = tuple(
        _parse_session(session, subjects, f"{context}, sessions[{number}]")
        for number, session in enumerate(raw_sessions)
    )
    return Version(
        label=label,
        status=status,
        effective_from=effective_from,
        effective_to=effective_to,
        sessions=sessions,
    )


def _parse_exceptions(raw: Any, path: Path) -> tuple[Exception_, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TimetableError(
            f"{path}: 'exceptions' must be a list of dates or ranges, "
            f"got {type(raw).__name__}."
        )

    entries: list[Exception_] = []
    for index, item in enumerate(raw):
        context = f"{path}: exceptions[{index}]"
        if isinstance(item, dict):
            start = _date(item.get("from"), "'from'", context)
            end = _date(item.get("to"), "'to'", context)
            if end < start:
                raise TimetableError(f"{context}: 'to' ({end}) is before 'from' ({start}).")
            reason = _optional_text(item.get("reason"), "'reason'", context) or DEFAULT_REASON
            entries.append(Exception_(start=start, end=end, reason=reason))
            continue
        day = _date(item, "an exception date", context)
        entries.append(Exception_(start=day, end=day, reason=DEFAULT_REASON))
    return tuple(entries)


def _check_no_overlap(versions: tuple[Version, ...], path: Path) -> None:
    """Two versions covering one day is ambiguity, and ambiguity gates wrongly.

    Last-one-wins would be a silent answer to a question the file does not
    actually settle, and the failure it produces -- revising for the version I
    replaced -- looks exactly like the gate working.
    """
    ordered = sorted(versions, key=lambda version: version.effective_from)
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.effective_to is None or earlier.effective_to >= later.effective_from:
            ends = "open-ended" if earlier.effective_to is None else str(earlier.effective_to)
            raise TimetableError(
                f"{path}: versions {earlier.label!r} ({ends}) and {later.label!r} "
                f"(from {later.effective_from}) both cover "
                f"{later.effective_from}. Give the earlier one an "
                f"'effective_to' before that date."
            )


def load(path: Path) -> Timetable:
    """Read and validate the timetable, or raise an error that names the line."""
    path = Path(path)
    if not path.is_file():
        raise TimetableError(
            f"No timetable at {path}.\n"
            f"Copy timetable.example.yaml to {path.name} and fill it in, or "
            f"point 'timetable_path' in config.yaml somewhere else."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise TimetableError(f"{path} is not valid YAML: {err}") from err
    except OSError as err:
        raise TimetableError(f"{path} could not be read: {err}") from err

    if raw is None:
        raise TimetableError(f"{path} is empty.")
    top = _mapping(raw, f"{path}", "")

    subjects = _parse_subjects(top.get("subjects"), path)

    raw_versions = top.get("versions")
    if not isinstance(raw_versions, list) or not raw_versions:
        raise TimetableError(f"{path}: 'versions' must be a non-empty list.")
    versions = tuple(
        _parse_version(version, subjects, path, index)
        for index, version in enumerate(raw_versions)
    )
    _check_no_overlap(versions, path)

    return Timetable(
        subjects=subjects,
        exceptions=_parse_exceptions(top.get("exceptions"), path),
        # Ordered so version_for() returns the same answer whatever order they
        # were written in. Non-overlap makes "first match" unambiguous.
        versions=tuple(sorted(versions, key=lambda version: version.effective_from)),
        path=path,
    )


# --------------------------------------------------------------------------
# warnings
# --------------------------------------------------------------------------

def warnings(timetable: Timetable, tracked_courses: list[str]) -> list[str]:
    """Things worth saying about a file that is nonetheless valid.

    Both of these are the normal state of my timetable rather than mistakes --
    six of my eleven subjects have no Classroom course at all -- so neither is
    an error. They are printed because the alternative is discovering months
    later that a subject was never gated and never said so.
    """
    notes: list[str] = []

    unmapped = timetable.subjects_without_course()
    if unmapped:
        notes.append(
            f"{len(unmapped)} subject(s) have no Classroom course, so they appear "
            f"in the schedule but are never gated: {', '.join(unmapped)}"
        )

    tracked = set(tracked_courses)
    untracked = sorted(
        f"{name} ({course})"
        for name, course in timetable.subjects.items()
        if course is not None and course not in tracked
    )
    if untracked:
        notes.append(
            f"{len(untracked)} subject(s) map to a course that is not in "
            f"courses.tracked, so nothing is ever synced for them and the gate "
            f"cannot open: {', '.join(untracked)}"
        )

    used = {subject for version in timetable.versions
            for session in version.sessions for subject in session.subjects}
    unused = sorted(set(timetable.subjects) - used)
    if unused:
        notes.append(
            f"{len(unused)} subject(s) are mapped but never meet in any version: "
            f"{', '.join(unused)}"
        )

    return notes
