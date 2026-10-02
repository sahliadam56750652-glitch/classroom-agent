"""The half of the quiz that costs a model request: writing the questions.

Split out of gate/quiz.py so that the half which grades can be imported by a
process that must never reach a model. The HTTP API takes quizzes in the browser
-- it starts attempts, records answers and settles them -- and none of that needs
a provider; this module is the only place in gate/ that imports one, and
tests/test_api_guards.py asserts the API process never loads it.

What lives here, and nothing else:

  - the prompt and the response schema,
  - `collect`, which splices one post's text into the material the prompt quotes,
  - `generate`, which serves a cached set or asks the model for a new one,
  - `begin`, the bot's entry point: resume the open attempt, or generate and
    start one,
  - `prepare`, the scheduled pipeline's entry point: write sets ahead of time
    for the items the gate is about to serve, inside a per-run request limit.

Everything a pass depends on -- the attempt, the arithmetic, `settle` and the one
call to `store.verify_study_item` -- stays in gate/quiz.py, which imports no
provider. The model writes questions and that is all it does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import store
from ..files import packs
from ..llm import provider as llm
from . import quiz
from .quiz import QUIZZABLE_STATES, QuizUnavailable, Generated, parse_questions
from datetime import date, timedelta
from typing import Collection

from . import scheduler
from . import timetable as tt
from .scheduler import Item

# Re-exported so callers that only ever wanted the generation half need one
# import, not two.
OPTIONS = quiz.OPTIONS
PROMPT_VERSION = quiz.PROMPT_VERSION

# What the API is told to return. Constrained decoding, so the answer arrives
# as JSON rather than as prose that has to be repaired -- see
# provider.generate_json.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "correct_index": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "source_file": {"type": "string"},
                    "source_page": {"type": "integer"},
                },
                "required": [
                    "question",
                    "options",
                    "correct_index",
                    "explanation",
                    "source_file",
                ],
            },
        },
        "note": {"type": "string"},
    },
    "required": ["questions"],
}

PROMPT = """You are setting a short revision quiz for a student on ONE lecture \
from their own course. The lecture's material is reproduced in full below.

Rules, all of them strict:

1. Every question must be answerable from the material below and from nothing \
else. Do not use outside knowledge, even where you are confident it is correct. \
If the material defines a term unusually, the material is right.
2. Write {count} multiple-choice questions, each with exactly {options} options, \
exactly one of which is correct. The wrong options must be plausible to someone \
who skimmed the lecture and wrong to someone who read it -- not obviously absurd, \
and not so close to correct that two answers could be defended.
3. Passages marked "transcribed from an image" were read out of a diagram, a \
photographed board or a code screenshot by a vision model. They are part of the \
lecture and are fair to ask about.
4. Passages marked as not yet transcribed are holes in what has been read. Never \
write a question that depends on one.
5. Some of the material was read out of images and the reading is not always \
right. Where a passage looks like a transcription error -- malformed notation, \
garbled symbols, mangled mathematics, nonsense tokens, an identifier that is not \
quite a word -- do not build a question on it, and never quote it as an option. \
Prefer passages that read cleanly. Do not try to guess what the broken text was \
meant to say: there is plenty of material here, so move to a passage you can \
trust instead.
6. Name the file and the page each question came from, so a wrong answer can be \
looked up.
7. Give a one-line explanation of the correct answer, in the material's own \
terms.
8. If the material is too thin to write {count} grounded questions -- including \
when too much of it is unreliably transcribed -- write fewer and say why in \
`note`. Fewer honest questions is a better answer than inventing one, and the \
pass mark is a ratio.

Lecture: {title}
Course: {course}

--- material begins ---
{material}
--- material ends ---
"""


# One post can carry a whole term of handouts. Past this the prompt is trimmed
# from the end and the trim is recorded, never silent -- a quiz that quietly
# ignored the second half of a lecture would look exactly like one that did not.
MAX_SOURCE_CHARS = 40_000


# --------------------------------------------------------------------------
# the material a question may rest on
# --------------------------------------------------------------------------

@dataclass
class Sources:
    text: str
    fingerprint: str
    files: list[str] = field(default_factory=list)
    truncated: bool = False


def collect(conn, config: Config, item: Item, *, questions: int) -> Sources:
    """One post's extracted text, spliced with its transcriptions.

    The fingerprint is `quiz.fingerprint` -- the identity of every source plus
    the prompt version and the question count, which is everything that would
    change the questions. It is computed there, in the half that imports no
    model, because the API has to answer "is a set ready for this text" without
    being able to write one.
    """
    rows = store.study_item_sources(conn, item.entity_type, item.entity_id)
    fingerprint = quiz.fingerprint_of(rows, questions)

    parts: list[str] = []
    names: list[str] = []
    used = 0
    truncated = False

    for row in rows:
        title = str(row["file_title"] or row["drive_id"])
        path = Path(config.library_dir) / str(row["text_path"])
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            # The row says there is text and the disk disagrees. Skipped rather
            # than guessed at, and the caller notices because `files` is short.
            continue
        body, _, _ = packs.render_pages(
            raw, store.ocr_pages_for(conn, str(row["drive_id"])), int(row["scan_pages"] or 0)
        )
        if not body.strip():
            continue

        room = MAX_SOURCE_CHARS - used
        if room <= 0:
            truncated = True
            break
        names.append(title)
        if len(body) > room:
            body = body[:room]
            truncated = True
        used += len(body)
        parts.append(f"### {title}\n\n{body}")
        if used >= MAX_SOURCE_CHARS:
            truncated = truncated or len(rows) > len(names)
            break

    return Sources(
        text="\n\n".join(parts), fingerprint=fingerprint, files=names, truncated=truncated
    )


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

def generate(
    conn,
    config: Config,
    item: Item,
    *,
    course: str = "",
    provider: llm.LLMProvider | None = None,
    count: int | None = None,
    now: str | None = None,
    require_delivered: bool = True,
) -> Generated:
    """Questions for one item -- from the cache when possible, the model when not.

    Refuses before it spends anything on an item the agent cannot fully read.
    That refusal is the point of the whole phase: `verified` has to mean the
    quiz covered the lecture, and a quiz generated over untranscribed pages
    would be a quiz about the parts that happen to be legible.

    `require_delivered=False` is for `prepare` only. Writing a set ahead of time
    for tomorrow's lecture is not sitting it -- nothing about the item changes --
    so the rule that a pass on undelivered material is evidence of nothing does
    not apply to generating one. Starting an attempt still refuses, in
    quiz.start.
    """
    if require_delivered and item.state not in QUIZZABLE_STATES:
        raise QuizUnavailable(
            f"this item is {item.state} — the material has not been delivered, "
            f"so a pass would not be evidence of anything",
            kind="not-delivered",
        )
    if not item.ready:
        raise QuizUnavailable(item.blocked_reason or "nothing readable here", kind="not-readable")

    wanted = count or config.quiz_question_count
    sources = collect(conn, config, item, questions=wanted)
    if not sources.text.strip():
        raise QuizUnavailable(
            "there is no extracted text on this post to ask about", kind="no-text"
        )

    hit = quiz.cached_set(conn, item.item_id, sources.fingerprint)
    if hit is not None:
        hit.truncated = sources.truncated
        return hit

    try:
        model = provider if provider is not None else llm.from_env()
    except llm.LLMError as err:
        # A missing or malformed key is a configuration fault, but it must not
        # crash a button press: it degrades like every other model failure and
        # says which one it was.
        raise _translate(err) from err

    prompt = PROMPT.format(
        count=wanted,
        options=OPTIONS,
        title=item.label,
        course=course or "(not named)",
        material=sources.text,
    )

    try:
        payload = model.generate_json(prompt, RESPONSE_SCHEMA)
    except llm.LLMError as err:
        raise _translate(err) from err

    questions, note = parse_questions(payload, wanted)
    # Cached even from `agent quiz --dry-run`. The expensive, irreversible thing
    # is the request, not the row: a dry run that threw its answer away and let
    # the real quiz ask again would spend 10% of the day's allowance on looking
    # at the same four questions twice. The command says so on stdout.
    store.save_questions(
        conn,
        item_id=item.item_id,
        source_hash=sources.fingerprint,
        model=model.name,
        questions=json.dumps([q.as_dict() for q in questions]),
        now=now,
    )
    # A request for this item, if one was waiting, is answered by this set.
    store.close_quiz_requests(conn, item.item_id, now=now)
    conn.commit()

    return Generated(
        questions=questions,
        model=model.name,
        source_hash=sources.fingerprint,
        cached=False,
        note=note,
        truncated=sources.truncated,
    )


def _translate(err: llm.LLMError) -> QuizUnavailable:
    """One provider failure, one thing to say about it.

    Seven distinguishable provider failures rather than "the quiz failed". The
    recurring lesson in PLAN.md is that a summary which cannot separate two
    states is a defect of its own -- and here the difference between "come back
    tomorrow", "wait a minute", "the key is wrong" and "the model was retired" is
    the difference between waiting and going to fix something.
    """
    if isinstance(err, llm.LLMQuotaError):
        return QuizUnavailable(
            "the day's model quota is spent, so there is no quiz until tomorrow",
            kind="quota",
        )
    if isinstance(err, llm.LLMRateLimited):
        return QuizUnavailable(
            "the model is rate limited this minute — try again shortly",
            kind="rate-limited",
        )
    if isinstance(err, llm.LLMTimeout):
        return QuizUnavailable("the model did not answer in time", kind="timeout")
    if isinstance(err, llm.LLMModelUnavailable):
        return QuizUnavailable(
            f"the configured model is not available — {err}", kind="model"
        )
    if isinstance(err, llm.LLMAuthError):
        return QuizUnavailable(
            "the model API key is missing or rejected, so no quiz can run at all",
            kind="auth",
        )
    if isinstance(err, llm.LLMRefused):
        return QuizUnavailable(
            f"the model would not write questions for this lecture: {err}",
            kind="refused",
        )
    return QuizUnavailable(f"the model could not be reached: {err}", kind="unavailable")


def begin(
    conn,
    config: Config,
    item: Item,
    *,
    run_id: int = 0,
    course: str = "",
    provider: llm.LLMProvider | None = None,
    now: str | None = None,
) -> tuple[quiz.Attempt, Generated | None]:
    """Resume the open attempt on this item, or generate a set and start one.

    Resuming first is not an optimisation. Telegram redelivers, an old button
    works forever, the bot may have been restarted between the tap that started
    this quiz and the tap that answers it -- and since 5d the attempt may have
    been started in the browser. So "start a quiz" has to mean "make sure a quiz
    is running", or a second tap would silently discard the answers already
    given.
    """
    open_row = store.open_quiz_attempt(conn, item.item_id)
    if open_row is not None:
        return quiz.attempt_from_row(open_row), None

    generated = generate(conn, config, item, course=course, provider=provider, now=now)
    attempt = quiz.start(conn, config, item, generated, run_id=run_id, now=now)
    return attempt, generated


# --------------------------------------------------------------------------
# ahead of time: the scheduled pipeline's half
# --------------------------------------------------------------------------

@dataclass
class Prepared:
    """What one `prepare` pass did, for the run log."""

    written: list[tuple[int, str]] = field(default_factory=list)
    already: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)
    stopped: str = ""

    @property
    def requests(self) -> int:
        return len(self.written)


# Failures that end the pass rather than the item. Every one of them would fail
# the next item identically, and asking again spends quota or time on an answer
# already known.
_STOPPING = frozenset({"quota", "rate-limited", "auth", "model", "unavailable", "timeout"})


def prepare(
    conn,
    config: Config,
    candidates: list[tuple[Item, str]],
    *,
    limit: int,
    provider: llm.LLMProvider | None = None,
    now: str | None = None,
) -> Prepared:
    """Write question sets ahead of time, at most `limit` requests.

    `candidates` arrive in priority order -- items I asked for first, then
    tomorrow's subjects, then everything else the gate could serve -- and each is
    (item, course name). An item with a set already cached costs nothing and does
    not count against the limit; an item that is not fully transcribed is skipped
    with its reason, because generating over holes is the one thing this module
    refuses to do.

    The limit is the whole point. OCR takes 12 of the ~20 free requests a day,
    and every set written here is one fewer the bot has for a quiz I ask for that
    evening -- so the default is small and config.example.yaml shows the sum.
    """
    result = Prepared()
    spent = 0
    for item, course in candidates:
        if spent >= limit:
            break
        if not item.ready:
            result.skipped.append((item.item_id, item.blocked_reason or "not readable"))
            continue
        wanted = config.quiz_question_count
        rows = store.study_item_sources(conn, item.entity_type, item.entity_id)
        if quiz.cached_set(conn, item.item_id, quiz.fingerprint_of(rows, wanted)) is not None:
            result.already += 1
            continue
        # Counted before the call: a request the model refused still spent quota.
        spent += 1
        try:
            generate(
                conn, config, item,
                course=course, provider=provider, now=now, require_delivered=False,
            )
        except QuizUnavailable as err:
            if err.kind in _STOPPING:
                # Left open: tomorrow's quota or a fixed key answers it.
                result.stopped = str(err)
                break
            # A refusal no retry would change. A request for this item closes
            # with the sentence, so the screen can say why instead of waiting.
            store.close_quiz_requests(conn, item.item_id, outcome=str(err), now=now)
            conn.commit()
            result.skipped.append((item.item_id, str(err)))
            continue
        result.written.append((item.item_id, item.label))
    return result


# How far ahead the scheduled pass looks for sessions to prepare for. A week,
# because a set written for Thursday on Monday is still the set Thursday's
# lecture gets -- it is cached against the text, not the date.
LOOKAHEAD_DAYS = 7


def _course_name(conn, course_id: str) -> str:
    course = store.get_course(conn, course_id)
    return str(course["name"]) if course else course_id


def candidates(
    conn,
    config: Config,
    local_courses: Collection[str],
    table: tt.Timetable | None,
    *,
    today: date,
) -> list[tuple[Item, str]]:
    """What to write sets for, most wanted first, each item once.

    1. Quizzes I asked for in the browser, oldest request first. I am waiting.
    2. The next item of each subject the gate will serve tomorrow, in the
       gate's own order -- the quiz most likely to be tapped tonight.
    3. The same for the days after, up to LOOKAHEAD_DAYS.
    4. Anything already read and still unverified, oldest first: those are the
       items "ready to verify" lists, and each is one tap from a quiz.

    Pure reading: nothing here writes or spends.
    """
    seen: set[int] = set()
    found: list[tuple[Item, str]] = []

    def add(item: Item | None, course_id: str) -> None:
        if item is None or item.item_id in seen:
            return
        seen.add(item.item_id)
        found.append((item, _course_name(conn, course_id)))

    for row in store.open_quiz_requests(conn):
        stored = store.get_study_item(conn, int(row["study_item_id"]))
        if stored is not None:
            add(scheduler.item_by_id(conn, int(row["study_item_id"])), str(stored["course_id"]))

    if table is not None:
        for offset in range(1, LOOKAHEAD_DAYS + 1):
            plan = scheduler.plan_for(conn, local_courses, table, today + timedelta(days=offset))
            for subject in plan.actionable:
                if subject.items and subject.course_id:
                    add(subject.items[0], subject.course_id)

    for row in store.read_unverified_items(conn, sorted(local_courses)):
        add(scheduler.item_by_id(conn, int(row["id"])), str(row["course_id"]))

    return found


def serve_requests(
    conn,
    config: Config,
    *,
    limit: int = 1,
    provider: llm.LLMProvider | None = None,
    now: str | None = None,
) -> Prepared:
    """Write the sets the browser asked for, and nothing it did not.

    The bot's half of out-of-process generation: it calls this between long
    polls, so a quiz requested in the app is usually written within a minute or
    two while `agent bot` is running. The scheduled run serves the same queue
    first, so a request is never stranded when the bot is not.
    """
    wanted: list[tuple[Item, str]] = []
    for row in store.open_quiz_requests(conn):
        item_id = int(row["study_item_id"])
        stored = store.get_study_item(conn, item_id)
        item = scheduler.item_by_id(conn, item_id)
        if stored is None or item is None:
            store.close_quiz_requests(
                conn, item_id, outcome="that item is no longer on file", now=now
            )
            conn.commit()
            continue
        wanted.append((item, _course_name(conn, str(stored["course_id"]))))
    return prepare(conn, config, wanted, limit=limit, provider=provider, now=now)
