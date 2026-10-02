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
    # Whether this window's `unread` figure means anything.
    #
    # False when OCR has never run over the file at all: then which pages are
    # images is known only in aggregate, so every window reports 0 unread and
    # `ready` true while the document as a whole has untranscribed pages. Those
    # are two different states -- "nothing unread here" and "we do not know where
    # the unread pages are" -- and a client that cannot tell them apart will show
    # the second as the first. Which is the recurring lesson exactly.
    unread_located: bool = True


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
    # The item's attachments, so the default screen can open the reader without
    # a second request. This is the one screen where a round trip is felt.
    files: list[DocumentOut] = Field(default_factory=list)
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
    # Read-only context, and about a DIFFERENT thing from `ItemOut.ready`: a
    # window says which pages an evening covers, while readiness is still about
    # the whole post until 3d stage 2 lands. See routes/library.py.
    windows: list[WindowOut] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# 5d: Read, Skip, and the quiz
# ---------------------------------------------------------------------------


class SkipIn(BaseModel):
    """An optional reason. The record is never empty without one -- see
    gate/actions.py, which writes where the skip came from as its floor."""

    reason: str = Field("", max_length=280)


class ItemActionOut(BaseModel):
    item_id: int
    moved: bool
    state: str


class QuizStatusOut(BaseModel):
    """Whether this item's quiz can be sat, and if not, why and when.

    `kind` is one of: open, ready, requested, not-generated, not-delivered,
    not-readable. `reason` is the sentence to show; `when` says when a requested
    set will exist, measured rather than promised -- whether the bot is
    listening is read from its heartbeat, not assumed.
    """

    item_id: int
    kind: str
    reason: str = ""
    when: str = ""
    attempt_id: int | None = None
    requested_at: str | None = None
    # How the last request closed, when it closed without a set: the refusal's
    # sentence, so the screen can say why instead of waiting forever.
    request_outcome: str | None = None


class AnswerIn(BaseModel):
    index: int = Field(..., ge=0)
    choice: int = Field(..., ge=0)


class FlagIn(BaseModel):
    index: int = Field(..., ge=0)


class QuizQuestionOut(BaseModel):
    """One question as it may be shown WHILE the quiz is open.

    No correct index and no explanation. Those exist only on `QuizReviewOut`,
    which is built from a finished attempt -- so an open attempt cannot leak an
    answer through this model however a route is written.
    """

    index: int
    question: str
    options: list[str]
    chosen: int | None = None
    flagged: bool = False


class QuizReviewOut(BaseModel):
    """One question after the attempt was settled: what was right, and where.

    `drive_id` and `page` point at the source in the reader when the question
    named a file this item holds. `page` is 1-based, as the question gave it.
    """

    index: int
    question: str
    options: list[str]
    chosen: int | None = None
    correct: int
    flagged: bool = False
    right: bool = False
    explanation: str = ""
    source_file: str = ""
    source_page: int | None = None
    drive_id: str | None = None


class QuizResultOut(BaseModel):
    """Counts, never a percentage. "5 of 6", and the bar it had to clear."""

    passed: bool
    verified: bool
    correct: int
    counted: int
    needed: int
    flagged: int
    failures: int = 0
    review: list[QuizReviewOut] = Field(default_factory=list)


class QuizAttemptOut(BaseModel):
    attempt_id: int
    item_id: int
    label: str = ""
    course_id: str = ""
    course_name: str = ""
    total: int
    index: int
    finished: bool
    started_at: str | None = None
    finished_at: str | None = None
    questions: list[QuizQuestionOut] = Field(default_factory=list)
    result: QuizResultOut | None = None


class QuizItemOut(BaseModel):
    item_id: int
    label: str
    course_id: str
    course_name: str = ""
    state: str
    status: QuizStatusOut


class PastAttemptOut(BaseModel):
    attempt_id: int
    item_id: int
    label: str = ""
    course_id: str = ""
    course_name: str = ""
    finished_at: str
    correct: int
    counted: int
    passed: bool


class QuizzesOut(BaseModel):
    """The Quizzes screen: what can be sat now, what is waiting, what was sat."""

    ready: list[QuizItemOut] = Field(default_factory=list)
    waiting: list[QuizItemOut] = Field(default_factory=list)
    attempts: list[PastAttemptOut] = Field(default_factory=list)


class HomeworkOut(BaseModel):
    """One thing to do. `due_at` is an instant and nothing else about time.

    A Classroom item carries its submission state and a link; it cannot be
    submitted from here, because invariant 6 means this project never writes to
    Classroom. A task can be marked done, through POST /api/tasks/{id}/complete.
    """

    kind: str
    id: str
    course_id: str
    course_name: str = ""
    title: str
    due_at: str | None = None
    done: bool = False
    link: str | None = None
    submission_state: str | None = None
    late: bool = False
    grade: float | None = None
    max_points: float | None = None
    task_kind: str | None = None
    done_at: str | None = None
    notes: str | None = None


# NextOut and StudyItemOut both reference DocumentOut before it is defined.
NextOut.model_rebuild()
StudyItemOut.model_rebuild()
