"""The quiz: generation, caching, grading, the flag button, and surviving a kill.

No test reaches a model. Every one of them passes its own stub, and the two
properties worth stating up front are the ones the whole phase rests on:

  * **Nothing reaches `verified` without a passed attempt.** Asserted per path
    and then again as a property over the whole table, because it is the
    guarantee that makes the coverage figure mean anything.
  * **A cached quiz costs zero requests.** The free tier is ~20 a day and OCR
    already claims most of it, so a retry that regenerated would put the
    feature out of reach by the fourth lecture.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.classroom.models import Course, Material
from agent.config import Config
from agent.db import store
from agent.gate import bot, messages, quiz
from agent.gate import scheduler
from agent.gate import timetable as tt
from agent.llm import provider as llm
from agent.notify.telegram import Telegram

TUNIS = ZoneInfo("Africa/Tunis")

TIMETABLE = """
subjects:
  DSA: "c-dsa"
versions:
  - label: S1
    status: confirmed
    effective_from: 2026-09-01
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC, subject: DSA }
"""

MONDAY = date(2026, 9, 14)
EVENING = "2026-09-13T19:00:00Z"
TRACKED = ["c-dsa"]

LECTURE = (
    "A binary search tree keeps its keys ordered so that a lookup can discard "
    "half of the remaining subtree at every step.\n"
    "The worst case is O(n) when the tree degenerates into a list.\n"
    "An AVL tree restores balance with rotations after every insertion.\n"
) * 12


def four_questions(prefix="Q", count=6):
    """A well-formed answer from the model. Correct index differs per question.

    Six by default, matching quiz.DEFAULT_QUESTIONS -- the name is historical
    and the count is not, so the tests that care pass one explicitly.
    """
    return {
        "questions": [
            {
                "question": f"{prefix}{n}: what does the lecture say about step {n}?",
                "options": [f"option {n}a", f"option {n}b", f"option {n}c", f"option {n}d"],
                "correct_index": n % 4,
                "explanation": f"because the lecture says so at {n}",
                "source_file": "Chapter 1.pdf",
                "source_page": n + 1,
            }
            for n in range(count)
        ],
        "note": "",
    }


class StubModel:
    """A provider that answers with what the test queued, and counts the calls."""

    name = "stub:test-model"

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [four_questions()]
        self.calls: list[str] = []

    def generate_json(self, prompt, schema):
        self.calls.append(prompt)
        outcome = self.outcomes.pop(0) if self.outcomes else four_questions()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def transcribe_image(self, image, prompt, *, mime_type="image/png"):
        raise AssertionError("the quiz must never ask for a transcription")


class StubTransport:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, payload, timeout=None):
        self.calls.append((url, payload))
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": []}
        return {"ok": True, "result": {"message_id": 700 + len(self.calls)}}

    def named(self, method):
        return [payload for url, payload in self.calls if url.endswith("/" + method)]

    @property
    def texts(self):
        return [p.get("text", "") for _, p in self.calls]


class StubUploads:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url, body, content_type):
        self.calls.append(content_type)
        return {"ok": True, "result": {"message_id": 99,
                                       "document": {"file_id": "F1", "file_size": 10}}}


@pytest.fixture
def config(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "library" / "files").mkdir(parents=True)
    (data_dir / "library" / "text").mkdir(parents=True)
    (data_dir / "library" / "files" / "d1.pdf").write_bytes(b"%PDF-1.4 fake")
    (data_dir / "library" / "text" / "d1.txt").write_text(LECTURE, encoding="utf-8")
    return Config(
        account="me@example.com", timezone="Africa/Tunis", data_dir=data_dir,
        tracked_courses=list(TRACKED), ignored_courses=[],
        timetable_path_override=tmp_path / "timetable.yaml",
    )


@pytest.fixture
def table(tmp_path):
    (tmp_path / "timetable.yaml").write_text(TIMETABLE, encoding="utf-8")
    return tt.load(tmp_path / "timetable.yaml")


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    store.upsert_course(
        connection,
        Course(id="c-dsa", name="DSA- pi1A", section=None, room=None, owner_id=None,
               course_state="ACTIVE", enrollment_code=None, alternate_link=None,
               creation_time=None, update_time=None, content_hash="h"),
    )
    yield connection
    connection.close()


def post(conn, *, parent_id="p1", drive_id="d1", pages=92, scan=14, ocr=14,
         chars=9000, state="delivered", created="2026-09-01T00:00:00Z"):
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, alternate_link, "
        "creation_time, content_hash, first_seen_at) "
        "VALUES (?, 'c-dsa', ?, 'https://classroom/x', ?, 'h', 'now')",
        (parent_id, f"Lecture {parent_id}", created),
    )
    store.upsert_material(
        conn,
        Material(id=f"coursework_material:{parent_id}:driveFile:{drive_id}",
                 parent_type="coursework_material", parent_id=parent_id,
                 course_id="c-dsa", kind="driveFile", ref=drive_id,
                 drive_id=drive_id, title=f"{drive_id}.pdf",
                 url="https://drive/x", content_hash="h"),
    )
    store.upsert_extraction(
        conn, drive_id, status="ok", pages=pages, chars=chars, scan_pages=scan,
        ocr_pages=ocr, local_path=f"files/{drive_id}.pdf",
        text_path=f"text/{drive_id}.txt", size_bytes=1000,
        extracted_at="2026-09-02T00:00:00Z",
    )
    store.ensure_study_item(
        conn, entity_type="coursework_material", entity_id=parent_id,
        course_id="c-dsa", state=state,
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM study_items WHERE entity_id = ?", (parent_id,)
    ).fetchone()["id"]


def client(transport=None, uploads=None):
    return Telegram("t", 1, transport=transport or StubTransport(),
                    multipart_transport=uploads or StubUploads(),
                    sleep=lambda _: None)


def item_of(conn, item_id):
    return scheduler.item_by_id(conn, item_id)


def press(conn, config, telegram, data, *, provider=None, now=EVENING):
    return bot.handle_callback(
        conn, config, telegram, {"id": "cb", "data": data},
        tz=TUNIS, now=now, provider=provider,
    )


def run_quiz(conn, config, telegram, item_id, *, answers, provider):
    """Start a quiz and answer it, returning the last Handled."""
    press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=provider)
    attempt_id = store.open_quiz_attempt(conn, item_id)["id"]
    result = None
    for index, chosen in enumerate(answers):
        result = press(
            conn, config, telegram,
            messages.encode("q", attempt_id, index, chosen), provider=provider,
        )
    return attempt_id, result


def correct_answers(conn, attempt_id):
    attempt = quiz.attempt_from_row(store.get_quiz_attempt(conn, attempt_id))
    return [question.correct for question in attempt.questions]


# --------------------------------------------------------------- readiness

def test_an_item_with_pending_ocr_refuses_generation(conn, config):
    """The refusal the whole phase turns on. Questions about holes are worse
    than no questions: they would produce a `verified` that means nothing."""
    item_id = post(conn, scan=26, ocr=0, pages=41)
    model = StubModel()

    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert err.value.kind == "not-readable"
    assert "not transcribed yet" in str(err.value)
    assert model.calls == [], "it must refuse before spending a request"


def test_a_partly_transcribed_item_is_still_refused(conn, config):
    """Strict, not proportional. One unread diagram is the page a quiz would
    most want to ask about."""
    item_id = post(conn, scan=26, ocr=25, pages=41)
    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel())
    assert err.value.kind == "not-readable"


def test_a_post_with_almost_no_text_is_refused(conn, config):
    item_id = post(conn, chars=120)
    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel())
    assert err.value.kind == "not-readable"


def test_the_refusal_is_delivered_and_the_item_reaches_reviewed(conn, config):
    """Deliver it, mark it read, and say plainly that verifying needs the OCR."""
    item_id = post(conn, scan=26, ocr=0, pages=41)
    transport = StubTransport()

    result = bot.start_quiz(
        conn, config, client(transport), run_id=1,
        item=item_of(conn, item_id), course_id="c-dsa",
        provider=StubModel(), now=EVENING,
    )

    assert result.kind == "no-quiz"
    assert store.get_study_item(conn, item_id)["state"] == "reviewed"
    assert store.get_study_item(conn, item_id)["verified_at"] is None
    assert any("read, not verified" in text for text in transport.texts)


def test_an_undelivered_item_is_refused_before_the_model(conn, config):
    """A pass on material that was never sent would be evidence of nothing."""
    item_id = post(conn, state="pending")
    model = StubModel()

    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert err.value.kind == "not-delivered"
    assert model.calls == []


def test_a_skipped_item_is_refused_too(conn, config):
    item_id = post(conn, state="pending")
    store.advance_study_item(conn, item_id, "skipped", skip_reason="not now")
    conn.commit()
    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel())
    assert err.value.kind == "not-delivered"


def test_the_quiz_button_on_an_undelivered_item_starts_no_attempt(conn, config):
    item_id = post(conn, state="pending")
    result = press(conn, config, client(), messages.encode("i", 1, item_id, "q"),
                   provider=StubModel())

    assert result.kind == "no-quiz"
    assert result.detail == "not-delivered"
    assert conn.execute("SELECT count(*) AS n FROM quiz_attempts").fetchone()["n"] == 0


def test_an_unready_item_gets_no_quiz_button(conn, config):
    item_id = post(conn, scan=26, ocr=0)
    rows = messages.item_keyboard(1, item_of(conn, item_id))["inline_keyboard"]
    labels = [b["text"] for row in rows for b in row]
    assert messages.QUIZ_LABEL not in labels


def test_a_ready_item_gets_a_quiz_button(conn, config):
    item_id = post(conn)
    rows = messages.item_keyboard(1, item_of(conn, item_id))["inline_keyboard"]
    labels = [b["text"] for row in rows for b in row]
    assert messages.QUIZ_LABEL in labels


# --------------------------------------------------------------- caching

def test_a_cached_quiz_costs_zero_model_calls(conn, config):
    """The whole quota argument, as one assertion."""
    item_id = post(conn)
    model = StubModel()

    first = quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    assert first.cached is False
    assert len(model.calls) == 1

    second = quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    assert second.cached is True
    assert len(model.calls) == 1, "the second look must not reach the model"
    assert [q.question for q in second.questions] == [q.question for q in first.questions]


def test_a_transcription_landing_changes_the_hash_and_regenerates(conn, config):
    """ocr_pages moving means the material genuinely grew, so the questions
    were written over less than the lecture now contains."""
    item_id = post(conn, scan=20, ocr=20)
    model = StubModel(four_questions("A"), four_questions("B"))
    first = quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    store.upsert_extraction(conn, "d1", scan_pages=24, ocr_pages=24)
    conn.commit()
    second = quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert second.source_hash != first.source_hash
    assert second.cached is False
    assert len(model.calls) == 2


def test_an_unchanged_timestamp_does_not_regenerate(conn, config):
    """Invariant 2's instinct: regenerate on the content, not on the clock."""
    item_id = post(conn)
    model = StubModel()
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    assert len(model.calls) == 1


def test_a_flagged_set_is_never_served_again(conn, config):
    item_id = post(conn)
    model = StubModel(four_questions("A"), four_questions("B"))
    generated = quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    store.flag_question_set(conn, item_id, generated.source_hash)
    conn.commit()

    again = quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert again.cached is False
    assert again.questions[0].question.startswith("B0")


def test_the_prompt_version_is_part_of_the_cache_key(conn, config, monkeypatch):
    """A question written under different instructions is not the question
    this version of the code would ask."""
    item_id = post(conn)
    model = StubModel(four_questions("A"), four_questions("B"))
    first = quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    monkeypatch.setattr(quiz, "PROMPT_VERSION", quiz.PROMPT_VERSION + 1)
    second = quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert second.source_hash != first.source_hash
    assert second.cached is False


def test_a_different_question_count_is_a_different_cache_entry(conn, config):
    """Changing quiz.question_count must not serve back a set of the old length."""
    item_id = post(conn)
    model = StubModel(four_questions("A"), four_questions("B"))
    quiz.generate(conn, config, item_of(conn, item_id), provider=model, count=6)
    quiz.generate(conn, config, item_of(conn, item_id), provider=model, count=4)
    assert len(model.calls) == 2


# --------------------------------------------------------------- grounding

def test_the_prompt_carries_the_lecture_text(conn, config):
    item_id = post(conn)
    model = StubModel()
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    prompt = model.calls[0]

    assert "binary search tree" in prompt
    assert "from nothing" in prompt, "the grounding instruction must be there"


def test_only_this_post_s_attachments_reach_the_prompt(conn, config):
    """A question about a lecture I have not been given yet is the opposite of
    the gate's job."""
    item_id = post(conn)
    (config.library_dir / "text" / "d2.txt").write_text(
        "THE OTHER LECTURE, about quicksort partitioning.", encoding="utf-8"
    )
    post(conn, parent_id="p2", drive_id="d2")

    model = StubModel()
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert "quicksort" not in model.calls[0]


def test_a_transcribed_page_is_offered_as_fair_material(conn, config):
    """Much of this library's real content is diagrams. A question resting on
    one is legitimate; the prompt has to say so."""
    item_id = post(conn)
    store.upsert_ocr_page(
        conn, drive_id="d1", page_index=0, page_hash="h1", status="ok",
        text="Figure 1 shows the rotation of an unbalanced node.",
        model="gemini:vision",
    )
    conn.commit()
    model = StubModel()
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert "transcribed from an image" in model.calls[0]
    assert "fair to ask about" in model.calls[0]


def test_the_prompt_says_not_to_build_on_a_bad_transcription(conn, config):
    """The first real quiz offered "O(N logConst(N))" as an option -- a vision
    model's reading of a complexity expression, not something the lecture says.
    Nothing in code can detect that: a garbled reading is still text. So the
    instruction is to skip such a passage, and never to guess at what it meant."""
    item_id = post(conn)
    model = StubModel()
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    prompt = model.calls[0]

    assert "transcription error" in prompt
    assert "never quote it as an option" in prompt
    assert "Do not try to guess what the broken text was meant to say" in prompt


def test_changing_the_prompt_retires_every_cached_set(conn, config):
    """PROMPT_VERSION is in the cache key precisely so that this fix reaches the
    questions that were already generated under the old instructions."""
    item_id = post(conn)
    model = StubModel(four_questions("A"), four_questions("B"))
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(quiz, "PROMPT_VERSION", quiz.PROMPT_VERSION + 1)
        after = quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert after.cached is False
    assert len(model.calls) == 2


def test_study_item_sources_excludes_a_deleted_attachment(conn, config):
    """Material the teacher removed does not come back as a question. Losing the
    only attachment also empties the readability arithmetic, so the refusal
    arrives one step earlier, as "nothing readable" -- which is the truth."""
    item_id = post(conn)
    conn.execute("UPDATE materials SET deleted_at = 'now' WHERE drive_id = 'd1'")
    conn.commit()

    assert store.study_item_sources(conn, "coursework_material", "p1") == []
    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel())
    assert err.value.kind == "not-readable"


def test_text_the_row_promises_and_the_disk_does_not_have_is_a_refusal(conn, config):
    """extractions says 9000 characters; the .txt is gone. Refused rather than
    quizzed on the empty string."""
    item_id = post(conn)
    (config.library_dir / "text" / "d1.txt").unlink()

    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel())
    assert err.value.kind == "no-text"


# --------------------------------------------------------------- validation

@pytest.mark.parametrize(
    "payload, why",
    [
        ({"questions": []}, "no questions"),
        ({"questions": "four of them"}, "not a list"),
        ("a string", "not an object"),
        ({"questions": [{"question": "", "options": ["a", "b", "c", "d"],
                         "correct_index": 0}]}, "no text"),
        ({"questions": [{"question": "q", "options": ["a", "b", "c"],
                         "correct_index": 0}]}, "three options"),
        ({"questions": [{"question": "q", "options": ["a", "b", "c", "a"],
                         "correct_index": 0}]}, "a repeated option"),
        ({"questions": [{"question": "q", "options": ["a", "b", "c", "d"],
                         "correct_index": 7}]}, "out of range"),
        ({"questions": [{"question": "q", "options": ["a", "b", "c", "d"],
                         "correct_index": "b"}]}, "not an integer"),
        ({"questions": [{"question": "q", "options": ["a", "", "c", "d"],
                         "correct_index": 0}]}, "a blank option"),
    ],
)
def test_an_unusable_answer_is_refused_rather_than_repaired(conn, config, payload, why):
    item_id = post(conn)
    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel(payload))
    assert err.value.kind == "refused", why


def test_a_refused_set_is_not_cached(conn, config):
    item_id = post(conn)
    bad = {"questions": [{"question": "q", "options": ["a", "b"], "correct_index": 0}]}
    with pytest.raises(quiz.QuizUnavailable):
        quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel(bad))
    assert conn.execute("SELECT count(*) AS n FROM quiz_questions").fetchone()["n"] == 0


def test_fewer_honest_questions_are_accepted(conn, config):
    """A model that returns two grounded questions beats one that invents four.
    The pass mark is a ratio, so a short set still works."""
    short = {
        "questions": four_questions()["questions"][:2],
        "note": "the lecture is mostly a reading list",
    }
    item_id = post(conn)
    generated = quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel(short))

    assert len(generated.questions) == 2
    assert "reading list" in generated.note


def test_more_questions_than_asked_for_are_trimmed_to_what_was_asked(conn, config):
    """Keeping the extras would move the denominator the pass mark is measured
    against, which is the one number here that has to mean what config says."""
    many = {"questions": four_questions()["questions"] * 3}
    item_id = post(conn)
    generated = quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel(many))
    assert len(generated.questions) == config.quiz_question_count == 6


# --------------------------------------------------------------- provider errors

@pytest.mark.parametrize(
    "error, kind, says",
    [
        (llm.LLMQuotaError("spent"), "quota", "tomorrow"),
        (llm.LLMRateLimited("slow down"), "rate-limited", "rate limited"),
        (llm.LLMTimeout("too slow"), "timeout", "in time"),
        (llm.LLMModelUnavailable("retired"), "model", "not available"),
        (llm.LLMAuthError("bad key"), "auth", "key"),
        (llm.LLMRefused("nope"), "refused", "would not write"),
        (llm.LLMUnavailable("no network"), "unavailable", "could not be reached"),
    ],
)
def test_every_provider_failure_says_something_different(conn, config, error, kind, says):
    """The recurring lesson: a summary that cannot tell two states apart is a
    defect of its own. Wait a minute, come back tomorrow and go and fix the key
    are three different instructions."""
    item_id = post(conn)
    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel(error))

    assert err.value.kind == kind
    assert says in str(err.value)
    assert messages.no_quiz_line(str(err.value), kind) != messages.no_quiz_line("x", "other")


def test_a_provider_that_raises_on_everything_still_leaves_the_item_reviewed(conn, config):
    """Phase 3's headline: an LLM outage degrades the gate, never suppresses it."""
    item_id = post(conn)
    transport = StubTransport()

    result = bot.start_quiz(
        conn, config, client(transport), run_id=1, item=item_of(conn, item_id),
        course_id="c-dsa", provider=StubModel(llm.LLMQuotaError("spent")), now=EVENING,
    )

    assert result.kind == "no-quiz"
    assert result.detail == "quota"
    row = store.get_study_item(conn, item_id)
    assert row["state"] == "reviewed"
    assert row["verified_at"] is None
    assert transport.calls, "it still had to say something"


def test_a_quota_failure_starts_no_attempt(conn, config):
    item_id = post(conn)
    bot.start_quiz(
        conn, config, client(), run_id=1, item=item_of(conn, item_id),
        course_id="c-dsa", provider=StubModel(llm.LLMQuotaError("spent")), now=EVENING,
    )
    assert conn.execute("SELECT count(*) AS n FROM quiz_attempts").fetchone()["n"] == 0


# --------------------------------------------------------------- grading

def graded(questions, answers, flags=None, ratio=0.75):
    attempt = quiz.Attempt(
        attempt_id=1, item_id=1, questions=questions, answers=list(answers),
        flags=list(flags or [False] * len(questions)), pass_ratio=ratio,
    )
    return attempt


def q(correct=0):
    return quiz.Question(question="?", options=("a", "b", "c", "d"), correct=correct)


def test_three_of_four_passes_at_the_default():
    attempt = graded([q(0), q(1), q(2), q(3)], [0, 1, 2, 0])
    assert attempt.correct == 3
    assert attempt.passed is True


def test_two_of_four_fails():
    attempt = graded([q(0), q(1), q(2), q(3)], [0, 1, 0, 0])
    assert attempt.passed is False


def test_a_flagged_question_leaves_the_denominator_rather_than_counting_wrong():
    """A bad question must not cost me a pass, or flagging one becomes a choice
    between honesty and my own coverage figure."""
    attempt = graded([q(0), q(1), q(2), q(3)], [0, 1, None, 3], flags=[0, 0, 1, 0])
    assert (attempt.correct, attempt.counted) == (3, 3)
    assert attempt.passed is True


def test_flagging_everything_cannot_pass():
    """It is a statement that the set was bad, not a way to be waved through."""
    attempt = graded([q(0), q(1)], [None, None], flags=[1, 1])
    assert attempt.counted == 0
    assert attempt.score == 0.0
    assert attempt.passed is False


def test_the_pass_ratio_is_honoured():
    attempt = graded([q(0), q(1), q(2), q(3)], [0, 1, 0, 0], ratio=0.5)
    assert attempt.passed is True


def test_grading_consults_no_model(conn, config):
    """There is no code path from settle() to a provider, and this pins it."""
    item_id = post(conn)
    model = StubModel()
    attempt, _ = quiz.begin(conn, config, item_of(conn, item_id), provider=model, now=EVENING)
    before = len(model.calls)
    for index, correct in enumerate(correct_answers(conn, attempt.attempt_id)):
        quiz.record_answer(conn, attempt, index, correct)
    quiz.settle(conn, attempt, now=EVENING)
    assert len(model.calls) == before


# --------------------------------------------------------------- verified

def test_a_pass_verifies_the_item(conn, config):
    item_id = post(conn)
    model = StubModel()
    telegram = client()
    attempt_id, _ = run_quiz(conn, config, telegram, item_id, answers=[], provider=model)

    result = None
    for index, correct in enumerate(correct_answers(conn, attempt_id)):
        result = press(conn, config, telegram,
                       messages.encode("q", attempt_id, index, correct), provider=model)

    assert result.kind == "verified"
    row = store.get_study_item(conn, item_id)
    assert row["state"] == "verified"
    assert row["verified_at"] is not None


def test_a_wrong_answer_never_sets_verified(conn, config):
    item_id = post(conn)
    model = StubModel()
    telegram = client()
    attempt_id, _ = run_quiz(conn, config, telegram, item_id, answers=[], provider=model)
    wrong = [(correct + 1) % 4 for correct in correct_answers(conn, attempt_id)]

    result = None
    for index, chosen in enumerate(wrong):
        result = press(conn, config, telegram,
                       messages.encode("q", attempt_id, index, chosen), provider=model)

    assert result.kind == "failed"
    row = store.get_study_item(conn, item_id)
    assert row["state"] == "reviewed"
    assert row["verified_at"] is None
    attempt = store.get_quiz_attempt(conn, attempt_id)
    assert attempt["passed"] == 0
    assert attempt["finished_at"] is not None


def test_a_failure_leaves_the_item_reviewed_not_demoted(conn, config):
    """Never back to pending -- that would erase delivered_at."""
    item_id = post(conn)
    telegram = client()
    model = StubModel()
    attempt_id, _ = run_quiz(conn, config, telegram, item_id, answers=[], provider=model)
    delivered = store.get_study_item(conn, item_id)["delivered_at"]
    for index, correct in enumerate(correct_answers(conn, attempt_id)):
        press(conn, config, telegram,
              messages.encode("q", attempt_id, index, (correct + 1) % 4), provider=model)

    row = store.get_study_item(conn, item_id)
    assert row["state"] == "reviewed"
    assert row["delivered_at"] == delivered


def test_verify_refuses_an_attempt_that_did_not_pass(conn, config):
    """The guarantee is enforced by a query, not by the caller's good manners."""
    item_id = post(conn)
    store.advance_study_item(conn, item_id, "reviewed", now=EVENING)
    attempt_id = store.start_quiz_attempt(conn, item_id=item_id, state="{}", now=EVENING)
    store.finish_quiz_attempt(
        conn, attempt_id, state="{}", score=0.25, passed=False, now=EVENING
    )
    conn.commit()

    with pytest.raises(ValueError) as err:
        store.verify_study_item(conn, item_id, attempt_id)
    assert "did not pass" in str(err.value)
    assert store.get_study_item(conn, item_id)["state"] == "reviewed"


def test_advance_study_item_still_cannot_reach_verified(conn, config):
    item_id = post(conn)
    with pytest.raises(ValueError):
        store.advance_study_item(conn, item_id, "verified")


def test_a_pending_item_cannot_be_verified_even_by_a_passed_attempt(conn, config):
    """The material was never sent. Whatever the attempt says, this did not
    happen."""
    item_id = post(conn, state="pending")
    attempt_id = store.start_quiz_attempt(conn, item_id=item_id, state="{}", now=EVENING)
    store.finish_quiz_attempt(
        conn, attempt_id, state="{}", score=1.0, passed=True, now=EVENING
    )
    conn.commit()

    assert store.verify_study_item(conn, item_id, attempt_id) is False
    assert store.get_study_item(conn, item_id)["state"] == "pending"


def test_verifying_twice_stamps_once(conn, config):
    item_id = post(conn)
    store.advance_study_item(conn, item_id, "reviewed", now=EVENING)
    attempt_id = store.start_quiz_attempt(conn, item_id=item_id, state="{}", now=EVENING)
    store.finish_quiz_attempt(conn, attempt_id, state="{}", score=1.0, passed=True)
    conn.commit()

    assert store.verify_study_item(conn, item_id, attempt_id, now=EVENING) is True
    first = store.get_study_item(conn, item_id)["verified_at"]
    assert store.verify_study_item(conn, item_id, attempt_id, now="2027-01-01T00:00:00Z") is False
    assert store.get_study_item(conn, item_id)["verified_at"] == first


def test_no_button_anywhere_reaches_verified_without_answering(conn, config, table):
    """A property over the whole table rather than a claim per handler."""
    item_id = post(conn, state="pending")
    plan = scheduler.plan_for(conn, TRACKED, table, MONDAY)
    run_id = store.create_gate_run(
        conn, for_date=MONDAY.isoformat(), plan=plan.to_json(), version_label="S1"
    )
    store.mark_gate_sent(conn, run_id, 500)
    conn.commit()
    telegram = client()

    for data in (
        messages.encode("g", run_id, "s0"),
        messages.encode("i", run_id, item_id, "r"),
        messages.encode("i", run_id, item_id, "q"),
        messages.encode("i", run_id, item_id, "z"),
    ):
        press(conn, config, telegram, data, provider=StubModel())

    counts = store.count_study_items_by_state(conn)
    assert counts.get("verified", 0) == 0


# --------------------------------------------------------------- the flag

def test_every_question_carries_a_flag_button(conn, config):
    item_id = post(conn)
    attempt, _ = quiz.begin(conn, config, item_of(conn, item_id),
                            provider=StubModel(), now=EVENING)
    for index in range(attempt.total):
        attempt.index = index
        keyboard = messages.question_keyboard(attempt, attempt.questions[index])
        labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
        assert messages.FLAG_LABEL in labels


def test_a_flagged_question_is_recorded_with_its_text(conn, config):
    """Retiring the set is what destroys the evidence, so the question is kept
    verbatim -- a flag with no reader is a button that does nothing."""
    item_id = post(conn)
    telegram = client()
    model = StubModel()
    press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=model)
    attempt_id = store.open_quiz_attempt(conn, item_id)["id"]
    asked = quiz.attempt_from_row(store.get_quiz_attempt(conn, attempt_id)).questions[1]

    press(conn, config, telegram, messages.encode("q", attempt_id, 1, "f"), provider=model)

    rows = store.list_flags(conn)
    assert len(rows) == 1
    kept = json.loads(rows[0]["question"])
    assert kept["question"] == asked.question
    assert kept["options"] == list(asked.options)
    assert rows[0]["model"] == "stub:test-model"
    assert rows[0]["source_file"] == "Chapter 1.pdf"
    assert rows[0]["question_index"] == 1


def test_flagging_retires_the_set_so_the_next_attempt_regenerates(conn, config):
    item_id = post(conn)
    telegram = client()
    model = StubModel(four_questions("A"), four_questions("B"))
    press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=model)
    attempt_id = store.open_quiz_attempt(conn, item_id)["id"]
    source_hash = quiz.attempt_from_row(
        store.get_quiz_attempt(conn, attempt_id)
    ).source_hash

    press(conn, config, telegram, messages.encode("q", attempt_id, 0, "f"), provider=model)

    assert conn.execute("SELECT flagged FROM quiz_questions").fetchone()["flagged"] == 1
    assert store.cached_questions(conn, item_id, source_hash) is None

    fresh = quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    assert fresh.cached is False
    assert fresh.questions[0].question.startswith("B0")


def test_flagging_the_same_question_twice_records_it_once(conn, config):
    item_id = post(conn)
    telegram = client()
    model = StubModel()
    press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=model)
    attempt_id = store.open_quiz_attempt(conn, item_id)["id"]

    press(conn, config, telegram, messages.encode("q", attempt_id, 0, "f"), provider=model)
    repeat = press(conn, config, telegram,
                   messages.encode("q", attempt_id, 0, "f"), provider=model)

    assert repeat.kind == "repeat"
    assert len(store.list_flags(conn)) == 1


def test_the_whole_set_can_be_flagged_from_the_result_screen(conn, config):
    item_id = post(conn)
    telegram = client()
    model = StubModel()
    attempt_id, _ = run_quiz(conn, config, telegram, item_id, answers=[], provider=model)
    for index, correct in enumerate(correct_answers(conn, attempt_id)):
        press(conn, config, telegram,
              messages.encode("q", attempt_id, index, (correct + 1) % 4), provider=model)

    result = press(conn, config, telegram,
                   messages.encode("q", attempt_id, "b"), provider=model)

    assert result.kind == "flagged-set"
    assert len(store.list_flags(conn)) == config.quiz_question_count
    assert conn.execute("SELECT flagged FROM quiz_questions").fetchone()["flagged"] == 1


# --------------------------------------------------------------- restart

def test_a_restart_mid_quiz_resumes_at_the_right_question(conn, config, tmp_path):
    """Nothing here implements a resume. The connection is closed mid-quiz, a
    new one is opened, and the next tap simply works -- because every piece of
    state was already a row."""
    item_id = post(conn)
    telegram = client()
    model = StubModel()
    press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=model)
    attempt_id = store.open_quiz_attempt(conn, item_id)["id"]
    answers = correct_answers(conn, attempt_id)

    press(conn, config, telegram, messages.encode("q", attempt_id, 0, answers[0]),
          provider=model)
    press(conn, config, telegram, messages.encode("q", attempt_id, 1, answers[1]),
          provider=model)
    conn.close()

    fresh = store.connect(config.db_path)
    try:
        resumed = quiz.attempt_from_row(store.get_quiz_attempt(fresh, attempt_id))
        assert resumed.index == 2
        assert resumed.answers[:2] == answers[:2]

        result = None
        for index in range(2, len(answers)):
            result = bot.handle_callback(
                fresh, config, telegram,
                {"id": "cb", "data": messages.encode("q", attempt_id, index, answers[index])},
                tz=TUNIS, now=EVENING, provider=StubModel(),
            )

        assert result.kind == "verified"
        assert fresh.execute(
            "SELECT score FROM quiz_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()["score"] == 1.0
        assert store.get_study_item(fresh, item_id)["state"] == "verified"
    finally:
        fresh.close()


def test_starting_a_quiz_twice_resumes_rather_than_regenerating(conn, config):
    """Telegram redelivers, and an old button works forever. A second tap must
    not discard the answers already given."""
    item_id = post(conn)
    telegram = client()
    model = StubModel(four_questions("A"), four_questions("B"))
    press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=model)
    attempt_id = store.open_quiz_attempt(conn, item_id)["id"]
    answers = correct_answers(conn, attempt_id)
    press(conn, config, telegram, messages.encode("q", attempt_id, 0, answers[0]),
          provider=model)

    press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=model)

    assert store.open_quiz_attempt(conn, item_id)["id"] == attempt_id
    assert len(model.calls) == 1
    resumed = quiz.attempt_from_row(store.get_quiz_attempt(conn, attempt_id))
    assert resumed.index == 1


def test_the_same_answer_arriving_twice_changes_the_row_once(conn, config):
    item_id = post(conn)
    telegram = client()
    model = StubModel()
    press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=model)
    attempt_id = store.open_quiz_attempt(conn, item_id)["id"]

    first = press(conn, config, telegram, messages.encode("q", attempt_id, 0, 1),
                  provider=model)
    second = press(conn, config, telegram, messages.encode("q", attempt_id, 0, 2),
                   provider=model)

    assert first.kind == "answered"
    assert second.kind == "repeat"
    resumed = quiz.attempt_from_row(store.get_quiz_attempt(conn, attempt_id))
    assert resumed.answers[0] == 1


def test_a_button_from_a_finished_quiz_is_answered_not_applied(conn, config):
    item_id = post(conn)
    telegram = client()
    model = StubModel()
    attempt_id, _ = run_quiz(conn, config, telegram, item_id, answers=[], provider=model)
    answers = correct_answers(conn, attempt_id)
    for index, correct in enumerate(answers):
        press(conn, config, telegram, messages.encode("q", attempt_id, index, correct),
              provider=model)

    stale = press(conn, config, telegram, messages.encode("q", attempt_id, 0, 3),
                  provider=model)

    assert stale.kind == "repeat"
    assert store.get_study_item(conn, item_id)["state"] == "verified"


def test_a_callback_for_a_quiz_that_no_longer_exists_is_survivable(conn, config):
    result = press(conn, config, client(), messages.encode("q", 9999, 0, 1))
    assert result.kind == "stale"


# --------------------------------------------------------------- retry

def test_a_retry_starts_a_new_attempt_from_the_cache(conn, config):
    item_id = post(conn)
    telegram = client()
    model = StubModel()
    attempt_id, _ = run_quiz(conn, config, telegram, item_id, answers=[], provider=model)
    for index, correct in enumerate(correct_answers(conn, attempt_id)):
        press(conn, config, telegram,
              messages.encode("q", attempt_id, index, (correct + 1) % 4), provider=model)

    press(conn, config, telegram, messages.encode("q", attempt_id, "n"), provider=model)

    second = store.open_quiz_attempt(conn, item_id)
    assert second is not None and second["id"] != attempt_id
    assert len(model.calls) == 1, "a retry must not spend another request"


def test_three_failures_change_what_the_result_offers(conn, config):
    """A loop I cannot leave is a gate I will mute, so Retry stops being the
    obvious thing and the possibility that the questions are wrong is named."""
    item_id = post(conn)
    telegram = client()
    model = StubModel()

    for _ in range(3):
        press(conn, config, telegram, messages.encode("i", 1, item_id, "q"), provider=model)
        attempt_id = store.open_quiz_attempt(conn, item_id)["id"]
        for index, correct in enumerate(correct_answers(conn, attempt_id)):
            press(conn, config, telegram,
                  messages.encode("q", attempt_id, index, (correct + 1) % 4), provider=model)

    assert store.count_failed_attempts(conn, item_id) == 3
    sent = [p.get("text", "") for _, p in telegram._transport.calls]
    assert any("three failures" in text for text in sent)


def test_the_repeated_failure_keyboard_still_offers_a_way_out(conn, config):
    attempt = graded([q(0)], [1])
    result = quiz.Result(attempt=attempt, passed=False, verified=False,
                         correct=0, counted=1, failures=3)
    labels = [
        b["text"]
        for row in messages.result_keyboard(result)["inline_keyboard"]
        for b in row
    ]
    assert messages.RETRY_LABEL in labels
    assert messages.SKIP_LABEL in labels
    assert any("questions were wrong" in label for label in labels)


# --------------------------------------------------------------- messages

def test_no_callback_in_a_quiz_exceeds_the_limit(conn, config):
    """A 15-digit attempt id and a 12-digit course id both fit, by construction."""
    attempt = quiz.Attempt(
        attempt_id=999999999999999, item_id=806468143345,
        questions=[q(0)], answers=[None], flags=[False], run_id=987654321,
    )
    keyboards = [
        messages.question_keyboard(attempt, attempt.questions[0]),
        messages.result_keyboard(
            quiz.Result(attempt=attempt, passed=False, verified=False,
                        correct=0, counted=1, failures=3)
        ),
    ]
    for keyboard in keyboards:
        for row in keyboard["inline_keyboard"]:
            for pressed in row:
                data = pressed.get("callback_data")
                if data is not None:
                    assert len(data.encode("utf-8")) <= messages.CALLBACK_LIMIT, pressed


def test_a_question_message_escapes_everything_it_interpolates(conn, config):
    """Lecture text in this account contains <, > and &. One missed escape is a
    400 and a question that silently never arrives."""
    attempt = quiz.Attempt(
        attempt_id=1, item_id=1, label="Chapter <1> & 2",
        questions=[quiz.Question(
            question="Is a < b && c > d?",
            options=("a & b", "<tag>", "plain", "x > y"),
            correct=0, source_file="Deck & Notes.pdf", source_page=3,
        )],
        answers=[None], flags=[False],
    )
    text = messages.question_message(attempt, attempt.questions[0])

    assert "&lt;" in text and "&amp;" in text
    assert "<tag>" not in text
    assert "Deck &amp; Notes.pdf" in text


def test_an_enormous_question_is_shortened_not_dropped(conn, config):
    """_fit drops whole sections, which here would drop the question and leave
    four answer buttons under an apology."""
    attempt = quiz.Attempt(
        attempt_id=1, item_id=1, label="Chapter 1",
        questions=[quiz.Question(question="Q " * 3000,
                                 options=("a " * 500, "b", "c", "d"), correct=0)],
        answers=[None], flags=[False],
    )
    text = messages.question_message(attempt, attempt.questions[0])

    assert len(text) <= messages.MESSAGE_LIMIT
    assert "shortened to fit" in text
    assert "question 1 of 1" in text
    assert "omitted" not in text


def test_the_sent_question_never_shows_the_answer(conn, config):
    attempt = quiz.Attempt(
        attempt_id=1, item_id=1,
        questions=[quiz.Question(question="?", options=("a", "b", "c", "d"),
                                 correct=2, explanation="because c")],
        answers=[None], flags=[False],
    )
    text = messages.question_message(attempt, attempt.questions[0])
    assert "because c" not in text


def test_a_failure_message_shows_what_was_missed(conn, config):
    attempt = quiz.Attempt(
        attempt_id=1, item_id=1, label="Chapter 1",
        questions=[quiz.Question(question="What is the worst case?",
                                 options=("O(1)", "O(n)", "O(n^2)", "O(log n)"),
                                 correct=1, explanation="a degenerate tree is a list",
                                 source_file="Chapter 1.pdf", source_page=7)],
        answers=[0], flags=[False],
    )
    result = quiz.Result(attempt=attempt, passed=False, verified=False,
                         correct=0, counted=1)
    text = messages.result_message(result)

    assert "Not passed" in text
    assert "O(n)" in text
    assert "degenerate tree" in text
    assert "Chapter 1.pdf, page 7" in text
    assert "read, not verified" in text


def test_a_pass_message_names_what_is_next(conn, config):
    item_id = post(conn)
    attempt = quiz.Attempt(attempt_id=1, item_id=item_id, label="Chapter 1",
                           questions=[q(0)], answers=[0], flags=[False], run_id=4)
    result = quiz.Result(attempt=attempt, passed=True, verified=True,
                         correct=1, counted=1)
    text = messages.result_message(result, remaining=2, subject="DSA")

    assert "Passed" in text
    assert "verified" in text
    assert "2 more waiting in DSA" in text


def test_the_next_button_addresses_an_item_by_id(conn, config):
    item_id = post(conn)
    item = item_of(conn, item_id)
    attempt = quiz.Attempt(attempt_id=1, item_id=item_id, questions=[q(0)],
                           answers=[0], flags=[False], run_id=4)
    result = quiz.Result(attempt=attempt, passed=True, verified=True,
                         correct=1, counted=1)
    rows = messages.result_keyboard(result, next_item=item)["inline_keyboard"]

    assert rows[0][0]["callback_data"] == messages.encode("i", 4, item_id, "d")


def test_the_next_button_delivers_that_item(conn, config):
    post(conn)
    second = post(conn, parent_id="p2", drive_id="d2", created="2026-09-05T00:00:00Z")
    (config.library_dir / "text" / "d2.txt").write_text(LECTURE, encoding="utf-8")
    (config.library_dir / "files" / "d2.pdf").write_bytes(b"%PDF-1.4 fake")
    store.advance_study_item(conn, second, "delivered", now=EVENING)
    conn.execute("UPDATE study_items SET state = 'pending' WHERE id = ?", (second,))
    conn.commit()

    result = press(conn, config, client(), messages.encode("i", 4, second, "d"))

    assert result.kind == "delivered"
    assert store.get_study_item(conn, second)["state"] == "delivered"


# --------------------------------------------------------------- the CLI

def test_the_dry_run_prints_the_answers_and_starts_nothing(conn, config, capsys, monkeypatch):
    from agent import cli

    item_id = post(conn)
    monkeypatch.setattr(cli.store, "open_db", lambda _config: conn)
    monkeypatch.setattr(
        cli.gate_quiz, "generate",
        lambda *a, **kw: quiz.Generated(
            questions=[quiz.Question(question="What is the worst case?",
                                     options=("O(1)", "O(n)", "O(n^2)", "O(log n)"),
                                     correct=1, explanation="a degenerate tree",
                                     source_file="Chapter 1.pdf", source_page=7)],
            model="stub:test-model", source_hash="abc123", cached=False,
        ),
    )

    code = cli.cmd_quiz(config, type("Args", (), {"item": item_id, "dry_run": True})())
    out = capsys.readouterr().out

    assert code == 0
    assert "* B. O(n)" in out
    assert "a degenerate tree" in out

    # cmd_quiz closes the connection it was handed, so read the result back
    # through a fresh one.
    after = store.connect(config.db_path)
    try:
        assert after.execute("SELECT count(*) AS n FROM quiz_attempts").fetchone()["n"] == 0
        assert store.get_study_item(after, item_id)["state"] == "delivered"
    finally:
        after.close()


def test_the_cli_refuses_an_unreadable_item_before_spending_anything(
    conn, config, capsys, monkeypatch
):
    from agent import cli

    item_id = post(conn, scan=26, ocr=0, pages=41)
    monkeypatch.setattr(cli.store, "open_db", lambda _config: conn)

    code = cli.cmd_quiz(config, type("Args", (), {"item": item_id, "dry_run": True})())
    out = capsys.readouterr().out

    assert code == 1
    assert "26 of 41 page(s) not transcribed yet" in out
    assert "agent ocr" in out


def test_the_cli_refuses_an_undelivered_item(conn, config, capsys, monkeypatch):
    from agent import cli

    item_id = post(conn, state="pending")
    monkeypatch.setattr(cli.store, "open_db", lambda _config: conn)

    code = cli.cmd_quiz(config, type("Args", (), {"item": item_id, "dry_run": True})())
    out = capsys.readouterr().out

    assert code == 1
    assert "this item is pending" in out
    assert "agent gate" in out


def test_agent_flagged_prints_what_was_flagged(conn, config, capsys, monkeypatch):
    from agent import cli

    item_id = post(conn)
    store.record_flag(
        conn, item_id=item_id, attempt_id=None, source_hash="h", model="stub:test-model",
        question_index=0,
        question=json.dumps({"question": "A badly worded one",
                             "options": ["a", "b", "c", "d"], "correct": 0}),
        source_file="Chapter 1.pdf", source_page=7, now=EVENING,
    )
    conn.commit()
    monkeypatch.setattr(cli.store, "open_db", lambda _config: conn)

    code = cli.cmd_flagged(config, type("Args", (), {})())
    out = capsys.readouterr().out

    assert code == 0
    assert "A badly worded one" in out
    assert "Chapter 1.pdf, page 7" in out
    assert "stub:test-model" in out


# --------------------------------------------------------------------------
# what the model is not allowed to touch
# --------------------------------------------------------------------------

GATE = Path(__file__).resolve().parents[1] / "src" / "agent" / "gate"


@pytest.mark.parametrize("module", ["scheduler", "timetable"])
def test_the_deciding_modules_cannot_reach_a_model(module):
    """Structural, not a promise. Which subjects are gated, what the backlog is
    and when a session runs are decided by code with no import path to a
    provider -- so an outage cannot change any of them, however it fails."""
    source = (GATE / f"{module}.py").read_text(encoding="utf-8")
    assert "llm" not in source
    assert "provider" not in source


def test_the_scheduler_decides_the_item_without_asking_anything(conn, config, table):
    """Oldest post first, and nothing is consulted to pick it."""
    post(conn, parent_id="p-old", drive_id="d-old", created="2026-01-01T00:00:00Z",
         state="pending")
    post(conn, parent_id="p-new", drive_id="d-new", created="2026-06-01T00:00:00Z",
         state="pending")
    (config.library_dir / "text" / "d-old.txt").write_text(LECTURE, encoding="utf-8")
    (config.library_dir / "text" / "d-new.txt").write_text(LECTURE, encoding="utf-8")

    plan = scheduler.plan_for(conn, TRACKED, table, MONDAY)

    assert plan.subjects[0].next_item.entity_id == "p-old"


def test_generation_never_asks_for_a_transcription(conn, config):
    """StubModel raises on transcribe_image. A quiz that reached for the vision
    path would fail loudly rather than quietly spending the OCR allowance."""
    item_id = post(conn)
    quiz.generate(conn, config, item_of(conn, item_id), provider=StubModel())


def test_the_gate_prompt_costs_no_model_call(conn, config, table):
    """Invariant 4 at its most literal: a briefing that depended on a model
    would be a briefing a quota outage could suppress."""
    post(conn, state="pending")
    model = StubModel()
    plan = scheduler.plan_for(conn, TRACKED, table, MONDAY)
    messages.compose(plan)
    messages.keyboard(plan, 1)
    assert model.calls == []


# --------------------------------------------------------------------------
# six questions, and what a pass is
# --------------------------------------------------------------------------

def test_the_default_quiz_is_six_questions(conn, config):
    item_id = post(conn)
    model = StubModel()
    generated = quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert len(generated.questions) == 6
    assert "Write 6 multiple-choice questions" in model.calls[0]


def test_six_questions_still_cost_one_request(conn, config):
    """The point of the change: the length is paid for in my time, not quota."""
    item_id = post(conn)
    model = StubModel()
    quiz.generate(conn, config, item_of(conn, item_id), provider=model)
    assert len(model.calls) == 1


def test_the_configured_count_reaches_the_prompt(conn, config):
    item_id = post(conn)
    model = StubModel(four_questions(count=3))
    generated = quiz.generate(
        conn, config, item_of(conn, item_id), provider=model, count=3
    )

    assert "Write 3 multiple-choice questions" in model.calls[0]
    assert len(generated.questions) == 3


@pytest.mark.parametrize(
    "correct, passes",
    [(6, True), (5, True), (4, False), (3, False), (0, False)],
)
def test_five_of_six_passes_and_four_does_not(correct, passes):
    """The decision, pinned. Four of six is a one-in-ten walk-through for
    someone who can eliminate one distractor; five of six is under two."""
    questions = [q(n % 4) for n in range(6)]
    answers = [
        question.correct if index < correct else (question.correct + 1) % 4
        for index, question in enumerate(questions)
    ]
    assert graded(questions, answers).passed is passes


@pytest.mark.parametrize(
    "total, need",
    [(3, 3), (4, 3), (5, 4), (6, 5), (7, 6), (8, 6), (10, 8)],
)
def test_what_the_default_threshold_demands_at_each_length(total, need):
    """The denominator moves when the model returns short or when I flag a
    question, so the table is worth pinning rather than rederiving."""
    questions = [q(0) for _ in range(total)]

    just_under = [0] * (need - 1) + [1] * (total - need + 1)
    exactly = [0] * need + [1] * (total - need)

    assert graded(questions, just_under).passed is False
    assert graded(questions, exactly).passed is True


def test_flagging_one_of_six_leaves_five_and_four_of_five_passes():
    """A flagged question leaves the denominator, so the bar moves with it."""
    questions = [q(0) for _ in range(6)]
    answers = [0, 0, 0, 0, 1, None]
    attempt = graded(questions, answers, flags=[0, 0, 0, 0, 0, 1])

    assert (attempt.correct, attempt.counted) == (4, 5)
    assert attempt.passed is True


def test_the_threshold_is_read_from_config_when_a_quiz_starts(conn, config):
    from dataclasses import replace

    strict = replace(config, quiz_pass_threshold=1.0)
    item_id = post(conn)
    attempt, _ = quiz.begin(
        conn, strict, item_of(conn, item_id), provider=StubModel(), now=EVENING
    )
    assert attempt.pass_ratio == 1.0


def test_a_quiz_keeps_the_threshold_it_started_under(conn, config):
    """Changing config mid-quiz must not move the bar under a half-answered one."""
    item_id = post(conn)
    attempt, _ = quiz.begin(
        conn, config, item_of(conn, item_id), provider=StubModel(), now=EVENING
    )
    reloaded = quiz.attempt_from_row(store.get_quiz_attempt(conn, attempt.attempt_id))
    assert reloaded.pass_ratio == config.quiz_pass_threshold


def test_the_prompt_version_bump_retires_the_four_question_sets(conn, config):
    """A stored 4-question set must not be served against a 6-question config."""
    assert quiz.PROMPT_VERSION >= 3

    item_id = post(conn)
    model = StubModel(four_questions("A", count=4), four_questions("B"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(quiz, "PROMPT_VERSION", 2)
        old = quiz.generate(conn, config, item_of(conn, item_id), provider=model, count=4)
    assert len(old.questions) == 4

    fresh = quiz.generate(conn, config, item_of(conn, item_id), provider=model)

    assert fresh.cached is False
    assert len(fresh.questions) == 6
    assert fresh.source_hash != old.source_hash


def test_six_questions_fit_one_message_each(conn, config):
    item_id = post(conn)
    attempt, _ = quiz.begin(
        conn, config, item_of(conn, item_id), provider=StubModel(), now=EVENING
    )
    for index in range(attempt.total):
        attempt.index = index
        text = messages.question_message(attempt, attempt.questions[index])
        assert len(text) <= messages.MESSAGE_LIMIT
        assert f"question {index + 1} of 6" in text


def test_a_failure_on_six_lists_every_miss(conn, config):
    questions = [
        quiz.Question(question=f"Question {n}", options=("a", "b", "c", "d"),
                      correct=0, explanation=f"reason {n}")
        for n in range(6)
    ]
    attempt = graded(questions, [0, 1, 1, 1, 1, 1])
    result = quiz.Result(attempt=attempt, passed=False, verified=False,
                         correct=1, counted=6)
    text = messages.result_message(result)

    assert "1 of 6" in text
    assert len(text) <= messages.MESSAGE_LIMIT
    for n in range(1, 6):
        assert f"reason {n}" in text
