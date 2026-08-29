"""The quiz: the only place a model decides anything, and it decides very little.

This is the one step that turns `reviewed` into `verified`, and the reason that
promotion means something is that the model's contribution stops at writing the
questions. It does not choose the item, does not read the timetable, does not
mark anything, and above all does not grade -- grading is an integer compared
against an integer, so a model that is having a bad day can produce a silly
question but can never hand out a pass.

Three properties hold this together.

**Grounding.** Questions come from the extracted text of ONE post, spliced
exactly as `files/packs.py` splices it, markers and all. A passage the vision
model transcribed from a diagram is fair material -- much of this library's
real content is diagrams -- but a passage nothing has read is not, which is why
generation refuses outright on an item with untranscribed pages. A quiz on
holes is worse than no quiz: it would produce a `verified` that means nothing.

A transcription that landed but landed badly is the third case, and the prompt
handles it rather than the code: a garbled reading of a complexity expression
is still text, so nothing here can detect it, but a question built on one is
unanswerable and teaches me the wrong thing. The instruction is to skip such a
passage and use another, never to guess at what it was meant to say -- 92 pages
is enough material that moving on costs nothing.

**Caching.** The free tier is roughly 20 requests a day and OCR already claims
most of it. A question set is generated once per *version of the text* and then
reused for every retry, every restart, and every second look. The cache key is
a hash of the text's identity, the same instinct as invariant 2: regenerate
when the material actually changed, not when a timestamp moved.

**Honest failure.** Every way this can fail has its own message. Quota spent,
rate limited, timed out, refused, model retired, key wrong, nothing readable --
seven states, seven things to say, because a summary that cannot distinguish
two states is a defect in itself. In all of them the item stays `reviewed`: the
lecture was still delivered and I still said I read it, and suppressing that
because a model was unavailable would break invariant 4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..classroom.models import content_hash
from ..config import Config
from ..db import store
from ..files import packs
from ..llm import provider as llm
from .scheduler import Item

# Bumped when the prompt or the shape of a question changes. It is part of the
# cache key, so raising it retires every stored set -- which is the point: a
# question generated under different instructions is not the question this
# version of the code would ask, and serving it would make the prompt untestable.
#
# 2: rule 4 gained the instruction to avoid passages that look like transcription
#    errors. The first real quiz offered "O(N logConst(N))" as an option, which
#    is a vision model's reading of a complexity expression and not something
#    the lecture says.
# 3: the default question count went from 4 to 6. Without this bump a stored
#    4-question set would keep being served against a 6-question config, and a
#    quiz that quietly ignores its own configuration is worse than one that
#    costs a request.
PROMPT_VERSION = 3

# Exactly four, always. The pass threshold is a ratio, so the count can vary
# without breaking anything, but the OPTIONS count is load-bearing: the answer
# buttons are built from it and a five-option question would silently lose one.
OPTIONS = 4

# Six, because four is not enough evidence. With four options a question is
# worth 0.25 to a guesser, and the pass mark is a ratio -- see DEFAULT_PASS_RATIO
# below for the arithmetic that makes six the length where the threshold starts
# meaning something. One request buys the whole set whatever its length, so the
# cost of the extra two is my time, not quota.
DEFAULT_QUESTIONS = 6

# Below three the ratio is too coarse to express a useful threshold. Above ten a
# quiz taken one question at a time on a phone stops being something I finish
# walking to a lecture, which is the only test that matters for whether I keep
# using it.
MIN_QUESTIONS = 3
MAX_QUESTIONS = 10

# What fraction of the countable questions has to be right. A ratio rather than
# a count, because the denominator moves: the model may return fewer than asked
# for, and a flagged question leaves the count entirely.
#
# 0.75 against six questions is FIVE of six, because 4/6 is 0.667 and does not
# clear the bar. That is the intended reading and it is worth the arithmetic:
# guessing at random through four options, six questions gives
#
#     3 of 6   16.9%       (and 32.0% if one option can be eliminated)
#     4 of 6    3.8%       (10.0%)
#     5 of 6    0.5%       ( 1.8%)
#
# Four of six is a one-in-ten walk-through for someone who half-remembers the
# lecture well enough to discard one distractor, which is exactly the state this
# gate exists to catch. Five of six is not.
#
# What the same ratio demands at other lengths, since a flagged question or a
# short set changes the denominator: 3 of 4, 4 of 5, 5 of 6, 6 of 7, 6 of 8. The
# harsh end is a three-question set, which needs 3 of 3 -- rare, and the honest
# answer there is that three questions is not much evidence either.
DEFAULT_PASS_RATIO = 0.75

# One post can carry a whole term of handouts. Past this the prompt is trimmed
# from the end and the trim is recorded, never silent -- a quiz that quietly
# ignored the second half of a lecture would look exactly like one that did not.
MAX_SOURCE_CHARS = 40_000

# After this many failed attempts the result message stops leading with Retry
# and starts suggesting the questions themselves may be wrong. A loop I cannot
# leave is a gate I will mute.
REPEATED_FAILURES = 3

# States an item can be quizzed from. `pending` is excluded on purpose: the
# material was never sent, so there is nothing a pass could be evidence of --
# and a quiz that runs but can never count is worse than one that refuses.
QUIZZABLE_STATES = frozenset({"delivered", "reviewed", "verified"})

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


class QuizError(Exception):
    """Something is wrong with the quiz itself, not with the interaction."""


class QuizUnavailable(QuizError):
    """No quiz for this item right now, and `kind` says why.

    Never fatal to the interaction. The caller reports the reason and leaves
    the item `reviewed`: the material was delivered and I said I read it, and
    an unreachable model must not be able to take that back.
    """

    def __init__(self, message: str, *, kind: str):
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------
# what a question is
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Question:
    question: str
    options: tuple[str, ...]
    correct: int
    explanation: str = ""
    source_file: str = ""
    source_page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": list(self.options),
            "correct": self.correct,
            "explanation": self.explanation,
            "source_file": self.source_file,
            "source_page": self.source_page,
        }

    @property
    def where(self) -> str:
        if self.source_file and self.source_page:
            return f"{self.source_file}, page {self.source_page}"
        return self.source_file or ""


def _question_from(raw: Any, index: int) -> Question:
    """One question, or a refusal naming what was wrong with it.

    Validated for meaning rather than for shape: responseSchema already
    guarantees a list of strings and an integer. What it cannot guarantee is
    that there are four of them, that the index points at one, or that two of
    them are not the same string -- and a set with two identical options has
    two correct answers whatever `correct_index` says.
    """
    if not isinstance(raw, dict):
        raise QuizUnavailable(
            f"question {index + 1} came back as {type(raw).__name__}, not an object",
            kind="refused",
        )

    text = str(raw.get("question") or "").strip()
    if not text:
        raise QuizUnavailable(f"question {index + 1} has no text", kind="refused")

    options = [str(option).strip() for option in (raw.get("options") or [])]
    if len(options) != OPTIONS:
        raise QuizUnavailable(
            f"question {index + 1} has {len(options)} options, not {OPTIONS}",
            kind="refused",
        )
    if any(not option for option in options):
        raise QuizUnavailable(f"question {index + 1} has a blank option", kind="refused")
    if len({option.casefold() for option in options}) != OPTIONS:
        # Two identical options are two correct answers, or two wrong ones. Either
        # way one of them can be picked and graded against the other.
        raise QuizUnavailable(
            f"question {index + 1} repeats an option, so more than one answer "
            f"could be right",
            kind="refused",
        )

    correct = raw.get("correct_index")
    if not isinstance(correct, int) or isinstance(correct, bool):
        raise QuizUnavailable(
            f"question {index + 1} has no usable correct_index", kind="refused"
        )
    if not 0 <= correct < OPTIONS:
        raise QuizUnavailable(
            f"question {index + 1} says the answer is option {correct}, which is "
            f"not one of the {OPTIONS} it offered",
            kind="refused",
        )

    page = raw.get("source_page")
    return Question(
        question=text,
        options=tuple(options),
        correct=correct,
        explanation=str(raw.get("explanation") or "").strip(),
        source_file=str(raw.get("source_file") or "").strip(),
        source_page=int(page) if isinstance(page, int) and not isinstance(page, bool) else None,
    )


def parse_questions(payload: Any, wanted: int = MAX_QUESTIONS) -> tuple[list[Question], str]:
    """(questions, the model's note). Raises QuizUnavailable on anything unusable.

    A model that returns more than it was asked for is trimmed to the number
    asked for. Keeping the extras would quietly change the denominator the pass
    mark is computed against, which is the one number in this file that has to
    mean what the configuration says it means.
    """
    if not isinstance(payload, dict):
        raise QuizUnavailable(
            f"the model returned {type(payload).__name__}, not an object", kind="refused"
        )
    raw = payload.get("questions")
    if not isinstance(raw, list) or not raw:
        raise QuizUnavailable("the model returned no questions", kind="refused")

    questions = [_question_from(item, index) for index, item in enumerate(raw)]
    return questions[:wanted], str(payload.get("note") or "").strip()


def _load_questions(stored: str) -> list[Question]:
    """Rehydrate a cached or in-progress set, which was validated before storage."""
    return [
        Question(
            question=str(raw["question"]),
            options=tuple(str(option) for option in raw["options"]),
            correct=int(raw["correct"]),
            explanation=str(raw.get("explanation") or ""),
            source_file=str(raw.get("source_file") or ""),
            source_page=raw.get("source_page"),
        )
        for raw in json.loads(stored)
    ]


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

    The fingerprint covers the identity of every source plus the prompt version
    and the question count -- everything that would change the questions. It
    does NOT cover the text itself, which would mean reading every file to
    decide whether to read every file; `chars`, `ocr_pages` and `extracted_at`
    already move whenever the text does.
    """
    rows = store.study_item_sources(conn, item.entity_type, item.entity_id)
    fingerprint = content_hash(
        {
            "prompt": PROMPT_VERSION,
            "questions": questions,
            "sources": [
                {
                    "drive_id": row["drive_id"],
                    "chars": row["chars"],
                    "pages": row["pages"],
                    "ocr_pages": row["ocr_pages"],
                    "scan_pages": row["scan_pages"],
                    "extracted_at": row["extracted_at"],
                }
                for row in rows
            ],
        }
    )

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

@dataclass
class Generated:
    questions: list[Question]
    model: str
    source_hash: str
    cached: bool
    note: str = ""
    truncated: bool = False


def generate(
    conn,
    config: Config,
    item: Item,
    *,
    course: str = "",
    provider: llm.LLMProvider | None = None,
    count: int | None = None,
    now: str | None = None,
) -> Generated:
    """Questions for one item -- from the cache when possible, the model when not.

    Refuses before it spends anything on an item the agent cannot fully read.
    That refusal is the point of the whole phase: `verified` has to mean the
    quiz covered the lecture, and a quiz generated over untranscribed pages
    would be a quiz about the parts that happen to be legible.
    """
    if item.state not in QUIZZABLE_STATES:
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

    hit = store.cached_questions(conn, item.item_id, sources.fingerprint)
    if hit is not None:
        return Generated(
            questions=_load_questions(str(hit["questions"])),
            model=str(hit["model"]),
            source_hash=sources.fingerprint,
            cached=True,
            truncated=sources.truncated,
        )

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
    recurring
    lesson in PLAN.md is that a summary which cannot separate two states is a
    defect of its own -- and here the difference between "come back tomorrow",
    "wait a minute", "the key is wrong" and "the model was retired" is the
    difference between waiting and going to fix something.
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


# --------------------------------------------------------------------------
# one attempt, and the arithmetic that grades it
# --------------------------------------------------------------------------

@dataclass
class Attempt:
    """One quiz being sat. Every field of this lives in a row, never in memory."""

    attempt_id: int
    item_id: int
    questions: list[Question]
    answers: list[int | None]
    flags: list[bool]
    index: int = 0
    message_id: int | None = None
    run_id: int = 0
    model: str = ""
    source_hash: str = ""
    # config.yaml's quiz.pass_threshold, copied onto the attempt when it starts.
    # Stored rather than read live so that a quiz keeps the rules it began
    # under: changing the threshold mid-quiz would move the bar under a quiz
    # already half answered.
    pass_ratio: float = DEFAULT_PASS_RATIO
    label: str = ""

    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def complete(self) -> bool:
        return self.index >= self.total

    @property
    def current(self) -> Question | None:
        return self.questions[self.index] if not self.complete else None

    @property
    def counted(self) -> int:
        """The denominator: every question I did not flag.

        A flagged question leaves the denominator rather than counting as
        wrong. A bad question must not cost me a pass -- if it did, flagging
        one would be a choice between honesty and my own coverage figure, and
        I would stop flagging them.
        """
        return sum(1 for flagged in self.flags if not flagged)

    @property
    def correct(self) -> int:
        return sum(
            1
            for index, question in enumerate(self.questions)
            if not self.flags[index] and self.answers[index] == question.correct
        )

    @property
    def wrong(self) -> list[int]:
        return [
            index
            for index, question in enumerate(self.questions)
            if not self.flags[index]
            and self.answers[index] is not None
            and self.answers[index] != question.correct
        ]

    @property
    def score(self) -> float:
        return self.correct / self.counted if self.counted else 0.0

    @property
    def passed(self) -> bool:
        """A quiz with nothing left to count cannot pass.

        Flagging every question is a statement that the whole set was bad, not
        a way to be waved through. The set is retired and regenerated instead.
        """
        return self.counted > 0 and self.score >= self.pass_ratio

    def to_json(self) -> str:
        return json.dumps(
            {
                "model": self.model,
                "source_hash": self.source_hash,
                "run_id": self.run_id,
                "message_id": self.message_id,
                "index": self.index,
                "pass_ratio": self.pass_ratio,
                "label": self.label,
                "questions": [question.as_dict() for question in self.questions],
                "answers": self.answers,
                "flags": [1 if flag else 0 for flag in self.flags],
            }
        )


def attempt_from_row(row) -> Attempt:
    """Rebuild an attempt from its row. This is the whole of restart recovery."""
    state = json.loads(str(row["questions"]) or "{}")
    questions = _load_questions(json.dumps(state.get("questions") or []))
    answers = list(state.get("answers") or [None] * len(questions))
    flags = [bool(flag) for flag in (state.get("flags") or [0] * len(questions))]
    return Attempt(
        attempt_id=int(row["id"]),
        item_id=int(row["study_item_id"]),
        questions=questions,
        answers=answers,
        flags=flags,
        index=int(state.get("index") or 0),
        message_id=state.get("message_id"),
        run_id=int(state.get("run_id") or 0),
        model=str(state.get("model") or ""),
        source_hash=str(state.get("source_hash") or ""),
        pass_ratio=float(state.get("pass_ratio") or DEFAULT_PASS_RATIO),
        label=str(state.get("label") or ""),
    )


def begin(
    conn,
    config: Config,
    item: Item,
    *,
    run_id: int = 0,
    course: str = "",
    provider: llm.LLMProvider | None = None,
    now: str | None = None,
) -> tuple[Attempt, Generated | None]:
    """Resume the open attempt on this item, or start a new one.

    Resuming first is not an optimisation. Telegram redelivers, an old button
    works forever, and the bot may have been restarted between the tap that
    started this quiz and the tap that answers it -- so "start a quiz" has to
    mean "make sure a quiz is running", or a second tap would silently discard
    the answers already given.
    """
    open_row = store.open_quiz_attempt(conn, item.item_id)
    if open_row is not None:
        return attempt_from_row(open_row), None

    generated = generate(conn, config, item, course=course, provider=provider, now=now)

    attempt = Attempt(
        attempt_id=0,
        item_id=item.item_id,
        questions=generated.questions,
        answers=[None] * len(generated.questions),
        flags=[False] * len(generated.questions),
        run_id=run_id,
        model=generated.model,
        source_hash=generated.source_hash,
        pass_ratio=config.quiz_pass_threshold,
        label=item.label,
    )
    attempt.attempt_id = store.start_quiz_attempt(
        conn, item_id=item.item_id, state=attempt.to_json(), now=now
    )
    # Starting a quiz is reading it, whatever the quiz then says. The item was
    # already `delivered`; this is the other legal way into `reviewed`.
    store.advance_study_item(conn, item.item_id, "reviewed", now=now)
    conn.commit()
    return attempt, generated


def record_answer(conn, attempt: Attempt, index: int, chosen: int) -> bool:
    """Store one answer. False when this question was already dealt with.

    Idempotent by inspection rather than by hope: the same callback arriving
    twice must not overwrite an answer or advance past a question I have not
    seen.
    """
    if not 0 <= index < attempt.total:
        return False
    if attempt.answers[index] is not None or attempt.flags[index]:
        return False
    if not 0 <= chosen < len(attempt.questions[index].options):
        return False

    attempt.answers[index] = chosen
    attempt.index = max(attempt.index, index + 1)
    store.update_quiz_attempt(conn, attempt.attempt_id, state=attempt.to_json())
    conn.commit()
    return True


def record_flag(conn, attempt: Attempt, index: int, *, now: str | None = None) -> bool:
    """Mark one question as bad, keep it verbatim, and move on.

    Three things happen together and all three matter: the question leaves the
    denominator, the set is retired so it is never served again, and the
    question itself is copied into `quiz_flags` -- because retiring the set is
    what destroys the evidence, and a flag button with no reader is a button
    that does nothing.
    """
    if not 0 <= index < attempt.total:
        return False
    if attempt.flags[index]:
        return False

    question = attempt.questions[index]
    attempt.flags[index] = True
    attempt.answers[index] = None
    attempt.index = max(attempt.index, index + 1)

    store.record_flag(
        conn,
        item_id=attempt.item_id,
        attempt_id=attempt.attempt_id,
        source_hash=attempt.source_hash,
        model=attempt.model,
        question_index=index,
        question=json.dumps(question.as_dict()),
        source_file=question.source_file or None,
        source_page=question.source_page,
        now=now,
    )
    if attempt.source_hash:
        store.flag_question_set(conn, attempt.item_id, attempt.source_hash)
    store.update_quiz_attempt(conn, attempt.attempt_id, state=attempt.to_json(), flagged=True)
    conn.commit()
    return True


def flag_whole_set(conn, attempt: Attempt, *, now: str | None = None) -> int:
    """Flag every question in this set at once, from the result screen.

    Offered after repeated failures, where the honest possibility is that the
    questions are wrong rather than that I am.
    """
    flagged = 0
    for index in range(attempt.total):
        if attempt.flags[index]:
            continue
        question = attempt.questions[index]
        attempt.flags[index] = True
        store.record_flag(
            conn,
            item_id=attempt.item_id,
            attempt_id=attempt.attempt_id,
            source_hash=attempt.source_hash,
            model=attempt.model,
            question_index=index,
            question=json.dumps(question.as_dict()),
            source_file=question.source_file or None,
            source_page=question.source_page,
            now=now,
        )
        flagged += 1
    if flagged and attempt.source_hash:
        store.flag_question_set(conn, attempt.item_id, attempt.source_hash)
    if flagged:
        store.update_quiz_attempt(
            conn, attempt.attempt_id, state=attempt.to_json(), flagged=True
        )
        conn.commit()
    return flagged


@dataclass
class Result:
    attempt: Attempt
    passed: bool
    verified: bool
    correct: int
    counted: int
    failures: int = 0

    @property
    def score(self) -> float:
        return self.attempt.score

    @property
    def repeated(self) -> bool:
        return self.failures >= REPEATED_FAILURES


def settle(conn, attempt: Attempt, *, now: str | None = None) -> Result:
    """Grade the attempt, close it, and promote the item only if it passed.

    Grading is `answers[i] == questions[i].correct` and nothing else. No model
    is consulted here and none can be -- there is no code path from this
    function to a provider, which is what makes the pass threshold a fact
    rather than an opinion.
    """
    passed = attempt.passed
    store.finish_quiz_attempt(
        conn,
        attempt.attempt_id,
        state=attempt.to_json(),
        score=attempt.score,
        passed=passed,
        now=now,
    )

    verified = False
    if passed:
        # store.verify_study_item re-reads the row it was just handed and
        # refuses on an attempt that did not pass. Belt and braces on purpose:
        # this is the only promotion in the system that cannot be undone by
        # anything short of --reopen.
        verified = store.verify_study_item(conn, attempt.item_id, attempt.attempt_id, now=now)
    conn.commit()

    return Result(
        attempt=attempt,
        passed=passed,
        verified=verified,
        correct=attempt.correct,
        counted=attempt.counted,
        failures=store.count_failed_attempts(conn, attempt.item_id),
    )
