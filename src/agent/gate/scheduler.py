"""What tomorrow needs, worked out from the timetable and the backlog.

Entirely deterministic. Nothing here calls a model, and nothing here decides
anything by wall-clock elapsed time -- the two ways a gate stops being
believable are quizzing me on material it has never read, and telling me about
a lecture that already happened.

The batching rule is the whole design. My week is ~20 sessions across ~11
subjects, so a prompt per session would arrive three times a day and be muted
inside a fortnight. One message covers the whole of tomorrow, and a subject
that meets twice tomorrow is ONE entry in it -- I revise the subject, not the
timeslot.

Three ways tomorrow produces nothing, all of them correct and all of them
silent: no timetable version covers the date, the date is an exception (a
holiday or a break), or the day simply has no sessions. Silence when there is
nothing to say is a feature -- a bot that says "nothing today" every evening
trains me to swipe it away, and then the one that mattered goes with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..db import store
from ..files import packs
from . import timetable as tt

# Below this, a post has no text worth calling readable -- a title slide, or a
# link with a one-line description. Distinct from `unread`, which is material
# the agent knows it cannot see; this is material that is simply not there.
MIN_READABLE_CHARS = 400

# The keyboard gets one button per subject plus snooze and skip, and the
# interface guidance is eight buttons at most. Tomorrow realistically holds
# three or four subjects; this is the ceiling that keeps a pathological day
# usable rather than a limit I expect to meet.
MAX_SUBJECT_BUTTONS = 6


@dataclass(frozen=True)
class Item:
    """One unreviewed post, and whether the agent can actually read it."""

    item_id: int
    entity_type: str
    entity_id: str
    label: str
    state: str
    alternate_link: str | None
    creation_time: str | None
    files: int
    pages: int
    chars: int
    unread: int

    @property
    def ready(self) -> bool:
        """Whether a quiz on this would be a quiz on what I was actually given.

        Strict: one untranscribed page is enough to block it. The pages that
        need OCR here are diagrams, equations and photographed boards -- the
        parts of a lecture a quiz would most want to ask about -- so "most of
        it is readable" is not the reassurance it sounds like.
        """
        return self.unread == 0 and self.chars >= MIN_READABLE_CHARS

    @property
    def blocked_reason(self) -> str:
        if self.unread:
            return f"{self.unread} of {self.pages} page(s) not transcribed yet"
        if self.chars < MIN_READABLE_CHARS:
            return "almost no readable text in this post"
        return ""


@dataclass(frozen=True)
class Subject:
    """One subject of tomorrow's, however many times it meets.

    `sessions` holds every session it appears in, so a subject taught twice
    tomorrow shows both times and still gates once.
    """

    name: str
    course_id: str | None
    course_name: str
    sessions: tuple[tt.Session, ...]
    items: tuple[Item, ...] = ()
    # Set when the course has no study items at all, which is not the same as
    # being up to date. Probability & Statistics is the live example: 20 of 20
    # attachments are gone from Drive, so nothing was ever readable.
    dead_files: int = 0
    has_items: bool = True

    @property
    def gated(self) -> bool:
        return self.course_id is not None

    @property
    def first_start(self) -> str:
        return min(session.start for session in self.sessions)

    @property
    def ready_count(self) -> int:
        return sum(1 for item in self.items if item.ready)

    @property
    def blocked_count(self) -> int:
        return sum(1 for item in self.items if not item.ready)

    @property
    def next_item(self) -> Item | None:
        """What "start catch-up" would serve: the oldest unreviewed post.

        Not the oldest READY one. Skipping ahead to whatever happens to be
        transcribed would mean revising out of order so that a quiz becomes
        possible, which is the tail wagging the dog -- and it would hide the
        backlog that OCR has not caught up with instead of reporting it.
        """
        return self.items[0] if self.items else None

    @property
    def unread_pages(self) -> int:
        return sum(item.unread for item in self.items)


@dataclass
class GatePlan:
    for_date: date
    version_label: str | None
    provisional: bool
    sessions: tuple[tt.Session, ...] = ()
    subjects: tuple[Subject, ...] = ()
    # Why there is nothing to send, when there is nothing to send. Carried so
    # `agent gate --dry-run` can say which of the three it was rather than
    # printing an unexplained blank.
    silent_because: str = ""

    @property
    def gated_subjects(self) -> tuple[Subject, ...]:
        return tuple(subject for subject in self.subjects if subject.gated)

    @property
    def actionable(self) -> tuple[Subject, ...]:
        """Subjects with something for me to actually do tonight."""
        return tuple(subject for subject in self.gated_subjects if subject.items)

    @property
    def total_items(self) -> int:
        return sum(len(subject.items) for subject in self.actionable)

    @property
    def worth_sending(self) -> bool:
        """Whether anything here justifies a notification.

        A day full of sessions I am completely up to date on is not news. The
        prompt exists to tell me what I have not revised, and sending "you are
        fine" every evening is how it gets muted.
        """
        return bool(self.actionable)

    def subject_at(self, index: int) -> Subject | None:
        """Resolve a keyboard index. Out of range is normal on a stale button."""
        gated = self.actionable
        return gated[index] if 0 <= index < len(gated) else None

    def to_json(self) -> str:
        """The plan as stored on the gate_runs row.

        Stored rather than recomputed because callback_data carries a subject
        INDEX into `actionable`, and a button tapped tomorrow morning has to
        resolve to the subject it named last night even though delivering an
        item has since changed the backlog underneath it.
        """
        return json.dumps(
            {
                "for_date": self.for_date.isoformat(),
                "version_label": self.version_label,
                "provisional": self.provisional,
                "sessions": [
                    {
                        "start": session.start,
                        "end": session.end,
                        "kind": session.kind,
                        "parts": [
                            {
                                "subject": part.subject,
                                "course_id": part.course_id,
                                "teacher": part.teacher,
                                "room": part.room,
                            }
                            for part in session.parts
                        ],
                    }
                    for session in self.sessions
                ],
                "subjects": [
                    {
                        "name": subject.name,
                        "course_id": subject.course_id,
                        "course_name": subject.course_name,
                        "item_ids": [item.item_id for item in subject.items],
                    }
                    for subject in self.actionable
                ],
            },
            sort_keys=True,
        )


def stored_subjects(plan_json: str) -> list[dict[str, Any]]:
    """The subject list a stored plan was sent with, in keyboard order."""
    try:
        decoded = json.loads(plan_json)
    except (TypeError, ValueError):
        return []
    subjects = decoded.get("subjects")
    return subjects if isinstance(subjects, list) else []


def _label(row: Any) -> str:
    """The best human name for a post.

    Announcements carry no title, only a body -- 10 of the 67 study items in
    this database are announcements -- so packs._label's fallback to the first
    line is reused rather than written a second time and drifting.
    """
    return packs.label(
        {
            "parent_title": row["parent_title"],
            "parent_body": row["parent_body"],
            "entity_type": row["entity_type"],
        }
    )


def _item(row: Any) -> Item:
    return Item(
        item_id=int(row["item_id"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        label=_label(row),
        state=str(row["state"]),
        alternate_link=row["alternate_link"],
        creation_time=row["creation_time"],
        files=int(row["files"] or 0),
        pages=int(row["pages"] or 0),
        chars=int(row["chars"] or 0),
        unread=int(row["unread"] or 0),
    )


def items_for(conn, course_id: str) -> tuple[Item, ...]:
    """One course's unreviewed backlog, oldest post first.

    The bot re-reads this when a button is tapped rather than trusting the item
    ids stored on the run: something may have been skipped or reviewed since
    the prompt was sent last night, and serving a lecture I have already dealt
    with is how an interface stops feeling like it knows what is going on.
    """
    return tuple(_item(row) for row in store.gate_backlog(conn, [course_id]))


def item_by_id(conn, item_id: int) -> Item | None:
    """One item by its id, with its readiness, whatever state it is in.

    The buttons that name a single item -- quiz me, deliver this next one --
    and `agent quiz --item N` all resolve through here rather than through the
    course backlog, because a verified or skipped item still has to be
    describable. Same query, same readiness arithmetic, no state filter.
    """
    row = store.backlog_item(conn, item_id)
    return _item(row) if row is not None else None


def plan_for(
    conn,
    tracked_courses: list[str],
    table: tt.Timetable,
    for_date: date,
) -> GatePlan:
    """Everything tomorrow needs, or a plan that says why it needs nothing."""
    version = table.version_for(for_date)
    if version is None:
        return GatePlan(
            for_date=for_date,
            version_label=None,
            provisional=False,
            silent_because="no timetable version covers this date",
        )

    excused = table.exception_for(for_date)
    if excused is not None:
        return GatePlan(
            for_date=for_date,
            version_label=version.label,
            provisional=version.provisional,
            silent_because=f"no sessions: {excused.reason}",
        )

    sessions = table.sessions_on(for_date)
    if not sessions:
        return GatePlan(
            for_date=for_date,
            version_label=version.label,
            provisional=version.provisional,
            silent_because=f"no sessions on a {for_date:%A}",
        )

    # One entry per SUBJECT, not per session -- a subject meeting twice
    # tomorrow is one thing to revise. A joint session contributes every one of
    # its parts, so both its subjects are gated.
    by_subject: dict[str, list[tt.Session]] = {}
    course_of: dict[str, str | None] = {}
    for session in sessions:
        for part in session.parts:
            by_subject.setdefault(part.subject, []).append(session)
            course_of[part.subject] = part.course_id

    wanted = [
        course_of[name]
        for name in by_subject
        if course_of[name] is not None and course_of[name] in tracked_courses
    ]
    backlog: dict[str, list[Item]] = {}
    names: dict[str, str] = {}
    for row in store.gate_backlog(conn, sorted(set(wanted))):
        backlog.setdefault(str(row["course_id"]), []).append(_item(row))
        names[str(row["course_id"])] = str(row["course_name"] or row["course_id"])

    item_counts = store.study_item_counts(conn)
    dead = store.dead_reference_counts(conn)
    # From the courses table rather than only from backlog rows: a subject that
    # is fully up to date has no backlog rows and still has a name.
    for course in store.list_courses(conn):
        names.setdefault(str(course["id"]), str(course["name"]))

    subjects = []
    for name, met in by_subject.items():
        course_id = course_of[name]
        # A course mapped in the timetable but absent from courses.tracked can
        # never be gated: nothing syncs it, so its backlog would always read as
        # empty. Treated as untracked rather than as up to date.
        gated = course_id if course_id in tracked_courses else None
        subjects.append(
            Subject(
                name=name,
                course_id=gated,
                course_name=names.get(gated or "", gated or "") if gated else "",
                sessions=tuple(met),
                items=tuple(backlog.get(gated or "", ())),
                dead_files=dead.get(gated or "", 0),
                has_items=bool(item_counts.get(gated or "", 0)) if gated else True,
            )
        )

    # Ordered by when the subject first meets tomorrow, so the message reads
    # down the day and the keyboard matches it.
    subjects.sort(key=lambda subject: (subject.first_start, subject.name))

    return GatePlan(
        for_date=for_date,
        version_label=version.label,
        provisional=version.provisional,
        sessions=sessions,
        subjects=tuple(subjects),
    )
