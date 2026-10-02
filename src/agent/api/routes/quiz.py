"""Read, Skip, and the quiz, in the browser. Phase 5d.

Three rules hold this module together, and each is pinned by a test rather than
left to a reviewer.

**`verified` has exactly one writer, and this module reaches it only through
`quiz.settle`.** Settling re-reads nothing on trust: the attempt is rebuilt from
its row before it is graded, grading is an integer compared against an integer,
and `store.verify_study_item` -- which `settle` calls and nothing here does --
re-reads the attempt again and refuses one that did not pass.
tests/test_api_guards.py asserts `settle` appears in this file and in no other
file of the API, and that `verify_study_item` and `advance_study_item` appear in
none. Read and Skip go through gate/actions.py, the same functions the bot's
buttons call.

**No model is called, and none can be.** This imports gate/quiz.py, the half of
the quiz that grades; the half that writes questions is gate/quizgen.py, and the
guard asserts the API process never loads it or the provider. A quiz whose set
is not written yet is a REQUEST, recorded in `quiz_requests` and answered by
`agent bot` between polls or by the next `agent run` -- and the screen says which,
measured from the bot's heartbeat rather than promised.

**No answer leaves the server before it is given.** An open attempt is rendered
through `QuizQuestionOut`, which has no field for the correct option or the
explanation. Those exist only on `QuizReviewOut`, built from a finished attempt.

Every route is `def`, like the rest of the API. `quiz.begin_cached`,
`record_answer` and `record_flag` commit inside themselves, as they do for the
bot; `get_db` commits again at the end, which is harmless.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status

from ... import scope as scope_mod
from ...config import Config, display_zone
from ...db import store
from ...gate import actions, quiz
from ...gate import scheduler as gate_scheduler
from .. import schemas
from ..deps import Conf, Db, MaybeTable, Session

router = APIRouter()

# When `agent run` fires. CLAUDE.md's scheduling table, and the systemd timer
# in deploy/systemd/ -- a copy rather than a reading, because neither Task
# Scheduler nor systemd is something a web request should be asking. If the
# schedule moves, this sentence moves with it.
RUN_TIMES = ("07:30", "19:30")

# How recent the bot's heartbeat must be to say "it is listening". Its long poll
# is 30 seconds, so three minutes is several missed passes, not one slow one.
HEARTBEAT_FRESH = timedelta(minutes=3)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(conn: sqlite3.Connection, item_id: int) -> tuple[sqlite3.Row, gate_scheduler.Item]:
    row = store.get_study_item(conn, item_id)
    item = gate_scheduler.item_by_id(conn, item_id) if row is not None else None
    if row is None or item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"There is no study item {item_id}. It may have been removed.",
        )
    return row, item


def _course_name(conn: sqlite3.Connection, course_id: str) -> str:
    course = store.get_course(conn, course_id)
    return str(course["name"]) if course else course_id


def _bot_listening(conn: sqlite3.Connection) -> bool:
    beat = store.get_bot_state(conn, store.BOT_HEARTBEAT_KEY)
    if not beat:
        return False
    try:
        seen = datetime.strptime(beat, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return _now() - seen <= HEARTBEAT_FRESH


def _next_run(config: Config) -> str:
    """ "at 19:30" or "tomorrow at 07:30" -- the next scheduled `agent run`, locally."""
    local = _now().astimezone(display_zone(config.timezone))
    here = local.strftime("%H:%M")
    for at in RUN_TIMES:
        if at > here:
            return f"at {at}"
    return f"tomorrow at {RUN_TIMES[0]}"


def _when(conn: sqlite3.Connection, config: Config) -> str:
    # Said in DESIGN.md section 5's voice: what will happen and when, and not
    # the plumbing that makes it happen.
    if _bot_listening(conn):
        return "Being written now. Usually ready within a few minutes."
    return f"Will be written at the next sync, {_next_run(config)}."


def _status(conn: sqlite3.Connection, config: Config, item: gate_scheduler.Item) -> schemas.QuizStatusOut:
    found = quiz.readiness(conn, config, item)
    open_row = store.open_quiz_attempt(conn, item.item_id)
    out = schemas.QuizStatusOut(
        item_id=item.item_id,
        kind=found.kind,
        reason=found.reason,
        attempt_id=int(open_row["id"]) if open_row is not None else None,
    )
    if found.kind != "not-generated":
        return out

    request = store.open_quiz_request(conn, item.item_id)
    if request is not None:
        out.kind = "requested"
        out.reason = "Asked for."
        out.requested_at = str(request["requested_at"])
        out.when = _when(conn, config)
        return out

    last = store.last_quiz_request(conn, item.item_id)
    if last is not None and last["outcome"] and last["outcome"] != "written":
        out.request_outcome = str(last["outcome"])
    return out


# ---------------------------------------------------------------------------
# Read and Skip
# ---------------------------------------------------------------------------


@router.post("/study-items/{item_id}/read", response_model=schemas.ItemActionOut)
def mark_read(item_id: int, conn: Db, session: Session) -> schemas.ItemActionOut:
    """I read it. The same function the bot's Read button calls."""
    _item(conn, item_id)
    done = actions.mark_read(conn, item_id)
    return schemas.ItemActionOut(item_id=item_id, moved=done.moved, state=done.state)


@router.post("/study-items/{item_id}/skip", response_model=schemas.ItemActionOut)
def skip(item_id: int, body: schemas.SkipIn, conn: Db, session: Session) -> schemas.ItemActionOut:
    """Skipped, logged, never verified. No confirmation: DESIGN.md section 4."""
    _item(conn, item_id)
    done = actions.skip_item(conn, item_id, source="web", reason=body.reason)
    return schemas.ItemActionOut(item_id=item_id, moved=done.moved, state=done.state)


# ---------------------------------------------------------------------------
# starting, or asking for, a quiz
# ---------------------------------------------------------------------------


@router.get("/study-items/{item_id}/quiz", response_model=schemas.QuizStatusOut)
def quiz_status(item_id: int, conn: Db, config: Conf, session: Session) -> schemas.QuizStatusOut:
    _, item = _item(conn, item_id)
    return _status(conn, config, item)


@router.post("/study-items/{item_id}/quiz", response_model=schemas.QuizAttemptOut)
def start_quiz(item_id: int, conn: Db, config: Conf, session: Session) -> schemas.QuizAttemptOut:
    """Resume the open attempt, or start one from a set already written.

    Never writes a set. With none to start from this refuses, and the client
    offers to ask for one.
    """
    _, item = _item(conn, item_id)
    try:
        attempt = quiz.begin_cached(conn, config, item, now=_stamp(_now()))
    except quiz.QuizUnavailable as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(err)) from err
    return _attempt_out(conn, config, store.get_quiz_attempt(conn, attempt.attempt_id))


@router.post("/study-items/{item_id}/quiz/request", response_model=schemas.QuizStatusOut)
def request_quiz(item_id: int, conn: Db, config: Conf, session: Session) -> schemas.QuizStatusOut:
    """Ask for this item's questions to be written, by a process that may.

    Refused for an item no set could be written for yet -- untranscribed pages
    are a hole a question might land in, and that refusal is the point of the
    whole quiz -- and a no-op when a quiz can already be sat.
    """
    _, item = _item(conn, item_id)
    found = quiz.readiness(conn, config, item)
    if found.kind == "not-readable":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=found.reason)
    if found.kind == "not-generated":
        store.request_quiz(conn, item_id, now=_stamp(_now()))
    return _status(conn, config, item)


# ---------------------------------------------------------------------------
# sitting it
# ---------------------------------------------------------------------------


def _needed(counted: int, ratio: float) -> int:
    """The smallest score that clears the bar, counted the way Attempt.passed is."""
    return next((k for k in range(counted + 1) if counted and k / counted >= ratio), counted)


def _source_of(files: list[sqlite3.Row], named: str) -> str | None:
    """The drive id of the file a question named, when it names one this item holds.

    Matched on the title, then on the title without its extension, then -- for a
    post with a single readable file -- that file. A question that names nothing
    recognisable gets no link rather than a link to the wrong document.
    """
    readable = [row for row in files if row["status"] == "ok" and row["local_path"]]
    wanted = (named or "").strip().casefold()
    if wanted:
        for row in readable:
            title = str(row["title"] or "").strip().casefold()
            stem = title.rsplit(".", 1)[0]
            if wanted in (title, stem) or wanted.rsplit(".", 1)[0] == stem:
                return str(row["drive_id"])
    if len(readable) == 1:
        return str(readable[0]["drive_id"])
    return None


def _attempt_out(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> schemas.QuizAttemptOut:
    attempt = quiz.attempt_from_row(row)
    stored = store.get_study_item(conn, attempt.item_id)
    course_id = str(stored["course_id"]) if stored is not None else ""
    finished = row["finished_at"] is not None

    out = schemas.QuizAttemptOut(
        attempt_id=attempt.attempt_id,
        item_id=attempt.item_id,
        label=attempt.label,
        course_id=course_id,
        course_name=_course_name(conn, course_id) if course_id else "",
        total=attempt.total,
        index=attempt.index,
        finished=finished,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
    if not finished:
        out.questions = [
            schemas.QuizQuestionOut(
                index=index,
                question=question.question,
                options=list(question.options),
                chosen=attempt.answers[index],
                flagged=attempt.flags[index],
            )
            for index, question in enumerate(attempt.questions)
        ]
        return out

    item = gate_scheduler.item_by_id(conn, attempt.item_id)
    files = store.study_item_files(conn, item.entity_type, item.entity_id) if item else []
    review = [
        schemas.QuizReviewOut(
            index=index,
            question=question.question,
            options=list(question.options),
            chosen=attempt.answers[index],
            correct=question.correct,
            flagged=attempt.flags[index],
            right=not attempt.flags[index] and attempt.answers[index] == question.correct,
            explanation=question.explanation,
            source_file=question.source_file,
            source_page=question.source_page,
            drive_id=_source_of(files, question.source_file),
        )
        for index, question in enumerate(attempt.questions)
    ]
    passed = bool(row["passed"])
    out.result = schemas.QuizResultOut(
        passed=passed,
        verified=passed and stored is not None and stored["state"] == "verified",
        correct=attempt.correct,
        counted=attempt.counted,
        needed=_needed(attempt.counted, attempt.pass_ratio),
        flagged=sum(1 for flag in attempt.flags if flag),
        failures=store.count_failed_attempts(conn, attempt.item_id),
        review=review,
    )
    return out


def _attempt_row(conn: sqlite3.Connection, attempt_id: int) -> sqlite3.Row:
    row = store.get_quiz_attempt(conn, attempt_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That quiz is no longer on file.",
        )
    return row


def _settle_if_complete(conn: sqlite3.Connection, attempt_id: int) -> None:
    """Grade an attempt once its last question is dealt with.

    The attempt is rebuilt from its ROW, not from whatever this request holds:
    Telegram may have answered a question since this request began, and grading
    a stale copy would grade answers that are not the stored ones. This is the
    API's only path to `verified`, and it is `quiz.settle`'s.
    """
    row = _attempt_row(conn, attempt_id)
    if row["finished_at"] is not None:
        return
    attempt = quiz.attempt_from_row(row)
    if attempt.complete:
        quiz.settle(conn, attempt, now=_stamp(_now()))


@router.get("/quiz-attempts/{attempt_id}", response_model=schemas.QuizAttemptOut)
def get_attempt(attempt_id: int, conn: Db, config: Conf, session: Session) -> schemas.QuizAttemptOut:
    return _attempt_out(conn, config, _attempt_row(conn, attempt_id))


@router.post("/quiz-attempts/{attempt_id}/answers", response_model=schemas.QuizAttemptOut)
def answer(
    attempt_id: int, body: schemas.AnswerIn, conn: Db, config: Conf, session: Session
) -> schemas.QuizAttemptOut:
    """One answer. A repeat is not an error: the attempt comes back as it stands."""
    row = _attempt_row(conn, attempt_id)
    if row["finished_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That quiz is already finished."
        )
    attempt = quiz.attempt_from_row(row)
    if body.index >= attempt.total or body.choice >= quiz.OPTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is not one of this quiz's questions or options.",
        )
    quiz.record_answer(conn, attempt, body.index, body.choice)
    _settle_if_complete(conn, attempt_id)
    return _attempt_out(conn, config, _attempt_row(conn, attempt_id))


@router.post("/quiz-attempts/{attempt_id}/flags", response_model=schemas.QuizAttemptOut)
def flag(
    attempt_id: int, body: schemas.FlagIn, conn: Db, config: Conf, session: Session
) -> schemas.QuizAttemptOut:
    """A bad question: it leaves the denominator, and the set is retired."""
    row = _attempt_row(conn, attempt_id)
    if row["finished_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That quiz is already finished."
        )
    attempt = quiz.attempt_from_row(row)
    if body.index >= attempt.total:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is not one of this quiz's questions.",
        )
    quiz.record_flag(conn, attempt, body.index, now=_stamp(_now()))
    _settle_if_complete(conn, attempt_id)
    return _attempt_out(conn, config, _attempt_row(conn, attempt_id))


# ---------------------------------------------------------------------------
# the Quizzes screen
# ---------------------------------------------------------------------------


@router.get("/quizzes", response_model=schemas.QuizzesOut)
def quizzes(conn: Db, config: Conf, table: MaybeTable, session: Session) -> schemas.QuizzesOut:
    """Items read and not yet verified, split by whether a quiz can be sat now,
    and every attempt already finished, newest first."""
    out = schemas.QuizzesOut()
    local = sorted(scope_mod.local(config, table))
    for row in store.read_unverified_items(conn, local):
        item = gate_scheduler.item_by_id(conn, int(row["id"]))
        if item is None:
            continue
        course_id = str(row["course_id"])
        entry = schemas.QuizItemOut(
            item_id=item.item_id,
            label=item.label,
            course_id=course_id,
            course_name=_course_name(conn, course_id),
            state=item.state,
            status=_status(conn, config, item),
        )
        (out.ready if entry.status.kind in ("ready", "open") else out.waiting).append(entry)

    for row in store.finished_quiz_attempts(conn):
        attempt = quiz.attempt_from_row(row)
        out.attempts.append(
            schemas.PastAttemptOut(
                attempt_id=attempt.attempt_id,
                item_id=attempt.item_id,
                label=attempt.label,
                course_id=str(row["course_id"]),
                course_name=str(row["course_name"] or row["course_id"]),
                finished_at=str(row["finished_at"]),
                correct=attempt.correct,
                counted=attempt.counted,
                passed=bool(row["passed"]),
            )
        )
    return out
