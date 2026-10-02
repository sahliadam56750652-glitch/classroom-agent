"""The two halves of the quiz, the shared Read and Skip, and the request queue.

Phase 5d split gate/quiz.py so that the half which grades can be loaded by a
process that must never reach a model. These tests pin the split itself (the pure
half imports no provider), what the pure half can do on its own (say whether a
quiz is ready, start one from a cached set, refuse when there is none), and the
generation half's two new jobs: writing sets ahead of time within a limit, and
answering the requests the browser leaves for it.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys

import pytest

from agent.db import store
from agent.gate import actions, bot, quiz, quizgen
from agent.llm import provider as llm

from test_quiz import (  # noqa: F401 -- fixtures, used by name
    MONDAY,
    StubModel,
    config,
    conn,
    four_questions,
    item_of,
    post,
    table,
)

GATE = pathlib.Path(quiz.__file__).parent


# --------------------------------------------------------------------------
# the split
# --------------------------------------------------------------------------

def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
            for alias in node.names:
                found.add(f"{'.' * node.level}{node.module or ''}.{alias.name}")
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_grading_half_imports_no_model_provider():
    imports = _imports(GATE / "quiz.py")
    assert not any("llm" in name for name in imports), imports
    assert not any("quizgen" in name for name in imports), imports


def test_that_check_sees_a_real_import():
    """Control: the generation half DOES import the provider, and the scan says so."""
    assert any("llm" in name for name in _imports(GATE / "quizgen.py"))


def test_loading_the_grading_half_loads_no_provider():
    """Transitively, in a clean interpreter -- the property the API relies on."""
    code = (
        "import sys, json; import agent.gate.quiz; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('agent.'))))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))
    assert "agent.gate.quiz" in loaded
    assert "agent.llm.provider" not in loaded
    assert "agent.gate.quizgen" not in loaded


def test_settle_is_still_in_the_grading_half():
    """The one path to `verified` must live where the API can reach it."""
    assert hasattr(quiz, "settle")
    assert not hasattr(quizgen, "settle")


# --------------------------------------------------------------------------
# readiness and starting from the cache
# --------------------------------------------------------------------------

def test_an_item_with_no_set_is_not_generated_yet(conn, config):
    item_id = post(conn)
    found = quiz.readiness(conn, config, item_of(conn, item_id))
    assert found.kind == "not-generated"
    assert not found.can_start


def test_an_item_with_pending_ocr_says_why_before_anything_else(conn, config):
    item_id = post(conn, scan=26, ocr=0, pages=41)
    found = quiz.readiness(conn, config, item_of(conn, item_id))
    assert found.kind == "not-readable"
    assert "not transcribed" in found.reason


def test_an_undelivered_item_asks_to_be_read_first(conn, config):
    item_id = post(conn, state="pending")
    found = quiz.readiness(conn, config, item_of(conn, item_id))
    assert found.kind == "not-delivered"
    assert "Mark it read" in found.reason


def test_a_cached_set_makes_it_ready_and_begin_cached_starts_it(conn, config):
    item_id = post(conn)
    quizgen.generate(conn, config, item_of(conn, item_id), provider=StubModel())
    item = item_of(conn, item_id)
    assert quiz.readiness(conn, config, item).kind == "ready"

    attempt = quiz.begin_cached(conn, config, item)
    assert attempt.attempt_id
    assert attempt.total == 6
    # Starting is reading.
    assert store.get_study_item(conn, item_id)["state"] == "reviewed"


def test_begin_cached_refuses_without_a_set_and_spends_nothing(conn, config):
    item_id = post(conn)
    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.begin_cached(conn, config, item_of(conn, item_id))
    assert err.value.kind == "not-generated"
    assert store.open_quiz_attempt(conn, item_id) is None


def test_one_open_attempt_is_shared_by_both_surfaces(conn, config):
    """Started by the bot, resumed by the browser: the same row, the same answers."""
    item_id = post(conn)
    first, _ = quizgen.begin(conn, config, item_of(conn, item_id), provider=StubModel())
    quiz.record_answer(conn, first, 0, 1)

    resumed = quiz.begin_cached(conn, config, item_of(conn, item_id))
    assert resumed.attempt_id == first.attempt_id
    assert resumed.answers[0] == 1
    assert resumed.index == 1

    # And the other way round: the bot resumes what the browser holds.
    again, generated = quizgen.begin(conn, config, item_of(conn, item_id), provider=StubModel())
    assert again.attempt_id == first.attempt_id
    assert generated is None


def test_sitting_a_set_written_ahead_still_refuses_undelivered_material(conn, config):
    item_id = post(conn, state="pending")
    quizgen.generate(
        conn, config, item_of(conn, item_id), provider=StubModel(), require_delivered=False
    )
    with pytest.raises(quiz.QuizUnavailable) as err:
        quiz.begin_cached(conn, config, item_of(conn, item_id))
    assert err.value.kind == "not-delivered"


# --------------------------------------------------------------------------
# writing ahead of time
# --------------------------------------------------------------------------

def test_prepare_spends_at_most_its_limit(conn, config):
    ids = [post(conn, parent_id=f"p{n}", drive_id="d1") for n in range(4)]
    model = StubModel()
    done = quizgen.prepare(
        conn, config, [(item_of(conn, i), "DSA") for i in ids], limit=2, provider=model
    )
    assert len(model.calls) == 2
    assert done.requests == 2


def test_prepare_skips_untranscribed_items_without_spending(conn, config):
    blocked = post(conn, parent_id="p1", scan=26, ocr=0, pages=41)
    model = StubModel()
    done = quizgen.prepare(conn, config, [(item_of(conn, blocked), "DSA")], limit=2, provider=model)
    assert model.calls == []
    assert done.skipped and "not transcribed" in done.skipped[0][1]


def test_prepare_counts_a_cached_set_as_free(conn, config):
    item_id = post(conn)
    quizgen.generate(conn, config, item_of(conn, item_id), provider=StubModel())
    model = StubModel()
    done = quizgen.prepare(conn, config, [(item_of(conn, item_id), "DSA")], limit=1, provider=model)
    assert model.calls == []
    assert done.already == 1


def test_prepare_stops_at_a_spent_quota_and_leaves_the_request_open(conn, config):
    item_id = post(conn)
    store.request_quiz(conn, item_id)
    model = StubModel(llm.LLMQuotaError("spent"))
    done = quizgen.prepare(conn, config, [(item_of(conn, item_id), "DSA")], limit=3, provider=model)
    assert "quota" in done.stopped
    assert store.open_quiz_request(conn, item_id) is not None


def test_a_refusal_closes_the_request_with_its_sentence(conn, config):
    item_id = post(conn)
    store.request_quiz(conn, item_id)
    model = StubModel({"questions": []})
    quizgen.prepare(conn, config, [(item_of(conn, item_id), "DSA")], limit=1, provider=model)
    assert store.open_quiz_request(conn, item_id) is None
    assert "no questions" in store.last_quiz_request(conn, item_id)["outcome"]


def test_a_written_set_closes_the_request(conn, config):
    item_id = post(conn)
    store.request_quiz(conn, item_id)
    done = quizgen.serve_requests(conn, config, limit=1, provider=StubModel())
    assert done.written
    assert store.open_quiz_request(conn, item_id) is None
    assert store.last_quiz_request(conn, item_id)["outcome"] == "written"


def test_asking_twice_is_one_request(conn, config):
    item_id = post(conn)
    assert store.request_quiz(conn, item_id) is True
    assert store.request_quiz(conn, item_id) is False
    assert len(store.open_quiz_requests(conn)) == 1


def test_candidates_put_requests_before_tomorrows_subjects(conn, config, table):
    """What I asked for comes first; then the gate's next item for each day."""
    tomorrow = post(conn, parent_id="p1", created="2026-09-01T00:00:00Z", state="pending")
    asked = post(conn, parent_id="p2", created="2026-09-05T00:00:00Z")
    store.request_quiz(conn, asked)
    found = quizgen.candidates(conn, config, {"c-dsa"}, table, today=MONDAY.replace(day=13))
    order = [item.item_id for item, _ in found]
    assert order[0] == asked
    assert tomorrow in order
    assert len(order) == len(set(order))


# --------------------------------------------------------------------------
# Read and Skip, one path for both surfaces
# --------------------------------------------------------------------------

def test_read_from_pending_passes_through_delivered(conn):
    item_id = post(conn, state="pending")
    done = actions.mark_read(conn, item_id, now="2026-09-13T19:00:00Z")
    row = store.get_study_item(conn, item_id)
    assert done.moved and row["state"] == "reviewed"
    assert row["delivered_at"] and row["reviewed_at"]


def test_read_twice_moves_once(conn):
    item_id = post(conn)
    assert actions.mark_read(conn, item_id).moved is True
    assert actions.mark_read(conn, item_id).moved is False


def test_read_never_reaches_verified(conn):
    item_id = post(conn)
    actions.mark_read(conn, item_id)
    assert store.get_study_item(conn, item_id)["state"] != "verified"


def test_a_skip_with_no_reason_still_records_where_it_came_from(conn):
    item_id = post(conn)
    actions.skip_item(conn, item_id, source="web")
    row = store.get_study_item(conn, item_id)
    assert row["state"] == "skipped"
    assert row["skip_reason"] == "skipped in the web app"


def test_a_skip_keeps_my_reason_after_the_floor(conn):
    item_id = post(conn)
    actions.skip_item(conn, item_id, source="web", reason="  covered in the TD  ")
    assert store.get_study_item(conn, item_id)["skip_reason"] == (
        "skipped in the web app: covered in the TD"
    )


def test_the_bot_skip_records_what_it_always_did(conn):
    item_id = post(conn)
    actions.skip_item(conn, item_id, source="telegram", context="gate run 41")
    assert store.get_study_item(conn, item_id)["skip_reason"] == (
        "skipped at delivery, gate run 41"
    )


def test_the_bot_read_and_skip_buttons_go_through_actions():
    """No second path: the bot's handlers call the shared functions."""
    source = (GATE / "bot.py").read_text(encoding="utf-8")
    assert "actions.mark_read(" in source
    assert "actions.skip_item(" in source
    body = source[source.index('if verb == "r":'):source.index('if verb == "z":')]
    assert "advance_study_item" not in body
