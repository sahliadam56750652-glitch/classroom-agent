"""What the API returns, and — as much as anything here — what it refuses to.

Several fields DESIGN.md forbids are absent by construction, and their absence is
the enforcement rather than a note in a review checklist. A client cannot render
a field the server never sends.

**No percentage, anywhere.** Not on a subject, not on a project, not on
"coverage". DESIGN.md forbids a percentage of a deficit, and forbids 0% or 100%
for a subject with no readable material. `SubjectOut` carries counts and a state;
`ProjectOut` carries two integers. Neither carries a ratio, and neither should
gain one.

**No urgency on a deadline.** `DeadlineOut` has `due_at` and nothing else about
time — no severity, no countdown, no hours remaining. DESIGN.md forbids
manufactured urgency, so the API hands over an instant and the client decides
whether it has passed. A `severity` field here would be the app inventing the
alarm it is forbidden to invent.

**No streak, no comparison to last week, no badge count.** There is nowhere for
one to go.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SessionPartOut(BaseModel):
    subject: str
    course_id: str | None = None
    teacher: str | None = None
    room: str | None = None


class SessionOut(BaseModel):
    day: str
    start: str
    end: str
    kind: str
    joint: bool
    parts: list[SessionPartOut]
    # Why this session is on this date, when the pattern does not say so.
    # Empty for a session that is simply the pattern.
    note: str = ""


class ItemOut(BaseModel):
    """One unreviewed post, and whether the agent can actually read it."""

    item_id: int
    entity_type: str
    entity_id: str
    label: str
    state: str
    alternate_link: str | None = None
    creation_time: str | None = None
    files: int
    pages: int
    chars: int
    unread: int
    ready: bool
    # Said out loud, every time, rather than left for the client to infer from
    # `unread`. Quizzing on a lecture nothing has read is the failure the gate
    # exists to prevent, and staying quiet about it is the same failure earlier.
    blocked_reason: str = ""


class WindowOut(BaseModel):
    """One evening-sized run of pages. Read-only context, per 3d stage 1."""

    index: int
    first: int
    last: int
    pages: int
    label: str
    title: str = ""
    topics: int = 1
    snapped: bool = False
    continues: bool = False
    unread: int = 0
    ready: bool = False


class SubjectOut(BaseModel):
    """One subject's standing, in the terms that keep it honest.

    `state` is one of `scheduler.Subject.STATES`. It is the field the client
    branches on, and the reason there is no number to branch on instead.
    """

    name: str
    state: str
    course_id: str | None = None
    course_name: str = ""
    mapped_course: str | None = None
    gated: bool
    manual: bool
    unreviewed: int
    ready: int
    blocked: int
    unread_pages: int
    dead_files: int
    oldest_posted_at: str | None = None
    next_item: ItemOut | None = None
    sessions: list[SessionOut] = Field(default_factory=list)


class NextOut(BaseModel):
    """The default screen: one item, or the honest absence of one.

    `waiting` false is the empty state, and it is modelled as the ABSENCE of an
    item rather than as an empty list. That is `composer.compose()` returning
    None expressed over HTTP: an interface that performs busyness when there is
    nothing to say trains me to stop reading it.
    """

    waiting: bool
    for_date: str
    # One subordinate line, per DESIGN.md: the deficit appears once, above the
    # action, and nowhere else.
    deficit: str = ""
    subject: SubjectOut | None = None
    item: ItemOut | None = None
    windows: list[WindowOut] = Field(default_factory=list)
    # Where to perform the action until 5d lands. 5b is read-only over
    # study_items, so the client sends me to the conversation that can write.
    act_in_telegram: bool = True
    next_session: SessionOut | None = None
    silent_because: str = ""


class GateOut(BaseModel):
    """The whole plan for a date. What `agent gate --dry-run` prints, as data."""

    for_date: str
    version_label: str | None = None
    provisional: bool = False
    worth_sending: bool
    silent_because: str = ""
    total_items: int
    sessions: list[SessionOut] = Field(default_factory=list)
    subjects: list[SubjectOut] = Field(default_factory=list)


class StudyItemOut(BaseModel):
    item_id: int
    course_id: str
    course_name: str = ""
    item: ItemOut
    windows: list[WindowOut] = Field(default_factory=list)
    files: list[DocumentOut] = Field(default_factory=list)


class DocumentOut(BaseModel):
    drive_id: str
    title: str
    url: str | None = None
    status: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    pages: int | None = None
    # What the file is called once it is on the phone -- the Drive title with the
    # extension of the bytes actually served, from the same function Telegram
    # delivery uses, so a document has one name everywhere.
    filename: str | None = None
    readable: bool = False


class DeadlineOut(BaseModel):
    """One thing that is due. `due_at` is the only fact about time.

    No severity, no countdown, no hours remaining -- see this module's docstring.
    """

    entity_type: str
    entity_id: str
    course_id: str | None = None
    title: str
    due_at: str | None = None
    link: str | None = None
    done: bool = False
    label: str = ""
    links_to: str | None = None


class EventOut(BaseModel):
    id: int
    type: str
    entity_type: str
    entity_id: str
    course_id: str | None = None
    payload: dict | None = None
    created_at: str
    notified_at: str | None = None


class OrphanOut(BaseModel):
    """A stored adjustment naming nothing the file still contains.

    Returned on every timetable response, never filtered out, because the
    settled decision is that an orphan is never applied, never silently dropped
    and always printed.
    """

    id: int
    applies_on: str
    kind: str
    subject: str
    session_start: str
    why: str
    renamed_to: str | None = None


class DayOut(BaseModel):
    date: str
    version_label: str | None = None
    provisional: bool = False
    sessions: list[SessionOut] = Field(default_factory=list)
    # Sessions the pattern puts here that no longer happen, each with why.
    # Shown in order to be crossed out: a cancelled lecture that simply vanished
    # would leave the day unexplainably short, and an unexplained difference is
    # indistinguishable from a bug in the resolver.
    departed: list[SessionOut] = Field(default_factory=list)
    exception: str = ""


class TimetableOut(BaseModel):
    path: str
    from_date: str
    to_date: str
    days: list[DayOut]
    orphans: list[OrphanOut] = Field(default_factory=list)
    subjects: dict[str, str | None] = Field(default_factory=dict)


class AdjustmentOut(BaseModel):
    id: int
    applies_on: str
    kind: str
    subject: str
    course_id: str | None = None
    session_start: str
    to_date: str | None = None
    to_start: str | None = None
    to_end: str | None = None
    to_kind: str | None = None
    to_room: str | None = None
    to_teacher: str | None = None
    reason: str | None = None
    created_at: str


class MilestoneOut(BaseModel):
    id: int
    title: str
    position: int
    done_at: str | None = None


class ProjectOut(BaseModel):
    """Progress is two integers. There is no percentage, and that is deliberate.

    A percentage is a feeling typed into a box, and DESIGN.md's closing
    constraint is that nothing may make honesty cost me anything -- a number I
    can quietly revise upward is exactly that. A milestone either happened or it
    did not.
    """

    id: int
    course_id: str
    course_name: str = ""
    title: str
    deliverables: list[str] = Field(default_factory=list)
    team: list[str] = Field(default_factory=list)
    brief_source: str | None = None
    deadline_at: str | None = None
    coursework_id: str | None = None
    closed_at: str | None = None
    created_at: str
    milestones_done: int = 0
    milestones_total: int = 0
    milestones: list[MilestoneOut] = Field(default_factory=list)


class TaskOut(BaseModel):
    id: int
    course_id: str
    course_name: str = ""
    title: str
    kind: str
    due_at: str | None = None
    notes: str | None = None
    source: str | None = None
    done_at: str | None = None
    created_at: str


class HeldSessionOut(BaseModel):
    id: int
    course_id: str
    course_name: str = ""
    held_on: str
    kind: str
    covered: str | None = None
    created_at: str


class PositionOut(BaseModel):
    """Where I stopped, or why that is no longer knowable.

    `changed` true with `page` null is the case DESIGN.md insists is said out
    loud: the anchor is gone because the document was re-uploaded. A silent fall
    back to page one would be a misreport, which this project ranks as a defect
    in its own right.
    """

    drive_id: str
    page: int | None = None
    page_hash: str | None = None
    updated_at: str | None = None
    changed: bool = False
    note: str = ""


class LibraryPostOut(BaseModel):
    entity_type: str
    entity_id: str
    course_id: str
    course_name: str = ""
    title: str
    creation_time: str | None = None
    files: int = 0
    pages: int = 0
    unread: int = 0
    study_item_id: int | None = None
    study_item_state: str | None = None


# StudyItemOut references DocumentOut before it is defined.
StudyItemOut.model_rebuild()
