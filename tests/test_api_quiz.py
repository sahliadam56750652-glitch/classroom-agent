"""Read, Skip and the quiz over HTTP -- Phase 5d.

The other side of test_api_writes.py's guarantee. That file shows no write route
reaches `verified` without a passed quiz; this one shows the quiz route does
reach it, only on a pass, only through quiz.settle, and never lets an answer out
before it is given.

Question sets are written in these tests with gate/quizgen.py and a stub model,
because that is the half of the quiz the API cannot reach -- the same way the
scheduled run or the bot would have written them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from api_fixtures import COURSE, client, make_config, seed

from agent.classroom.models import Material
from agent.db import store
from agent.gate import quiz, quizgen, scheduler

LECTURE = (
    "A relation is a set of tuples. A natural join keeps rows whose shared "
    "attributes are equal. A left outer join keeps every row of the left table.\n"
) * 20


class StubModel:
    name = "stub:test-model"

    def __init__(self, count=6):
        self.count = count
        self.calls = 0

    def generate_json(self, prompt, schema):
        self.calls += 1
        return {
            "questions": [
                {
                    "question": f"Q{n}: what does the lecture say?",
                    "options": [f"{n}a", f"{n}b", f"{n}c", f"{n}d"],
                    "correct_index": n % 4,
                    "explanation": f"because of line {n}",
                    "source_file": "ch4.pdf",
                    "source_page": n + 3,
                }
                for n in range(self.count)
            ]
        }


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    (made.library_dir / "files").mkdir(parents=True, exist_ok=True)
    (made.library_dir / "text").mkdir(parents=True, exist_ok=True)
    (made.library_dir / "files" / "d1.pdf").write_bytes(b"%PDF-1.4 fake")
    (made.library_dir / "text" / "d1.txt").write_text(LECTURE, encoding="utf-8")
    return made


def make_item(config, *, parent="p1", state="delivered", scan=0, ocr=0):
    conn = store.connect(config.db_path)
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, alternate_link, "
        "creation_time, content_hash, first_seen_at) "
        "VALUES (?, ?, 'Chapter 4', 'https://classroom/x', '2026-09-01T00:00:00Z', 'h', 'now')",
        (parent, COURSE),
    )
    store.upsert_material(
        conn,
        Material(id=f"coursework_material:{parent}:driveFile:d1", parent_type="coursework_material",
                 parent_id=parent, course_id=COURSE, kind="driveFile", ref="d1",
                 drive_id="d1", title="ch4.pdf", url="https://drive/x", content_hash="h"),
    )
    store.upsert_extraction(
        conn, "d1", status="ok", pages=20, chars=len(LECTURE), scan_pages=scan,
        ocr_pages=ocr, local_path="files/d1.pdf", text_path="text/d1.txt",
        size_bytes=1000, extracted_at="2026-09-02T00:00:00Z",
    )
    store.ensure_study_item(conn, entity_type="coursework_material", entity_id=parent,
                            course_id=COURSE, state=state)
    conn.commit()
    item_id = conn.execute("SELECT id FROM study_items WHERE entity_id = ?", (parent,)).fetchone()[0]
    conn.close()
    return item_id


def write_set(config, item_id, *, count=6):
    conn = store.connect(config.db_path)
    item = scheduler.item_by_id(conn, item_id)
    quizgen.generate(conn, config, item, provider=StubModel(count), require_delivered=False)
    conn.close()


def state_of(config, item_id):
    conn = store.connect(config.db_path)
    row = store.get_study_item(conn, item_id)
    conn.close()
    return row["state"], row["skip_reason"]


def answer_all(session, attempt, *, right):
    """Answer every question, correctly or not, from what the server sent."""
    body = attempt
    for question in attempt["questions"]:
        # The correct index is n % 4 by construction of StubModel; the server
        # never sent it, which is asserted elsewhere.
        n = question["index"]
        choice = n % 4 if right else (n + 1) % 4
        body = session.post(
            f"/api/quiz-attempts/{attempt['attempt_id']}/answers",
            json={"index": n, "choice": choice},
        ).json()
    return body


# ---------------------------------------------------------------------------
# Read and Skip
# ---------------------------------------------------------------------------


def test_read_moves_a_pending_item_to_reviewed_and_no_further(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config, state="pending")
    body = session.post(f"/api/study-items/{item_id}/read").json()
    assert body == {"item_id": item_id, "moved": True, "state": "reviewed"}
    assert state_of(config, item_id)[0] == "reviewed"


def test_read_twice_is_one_move(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    session.post(f"/api/study-items/{item_id}/read")
    assert session.post(f"/api/study-items/{item_id}/read").json()["moved"] is False


def test_skip_is_logged_with_my_reason_and_never_verified(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    body = session.post(f"/api/study-items/{item_id}/skip", json={"reason": "covered in TD"}).json()
    assert body["state"] == "skipped"
    assert state_of(config, item_id) == ("skipped", "skipped in the web app: covered in TD")


def test_skip_needs_no_reason(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    session.post(f"/api/study-items/{item_id}/skip", json={})
    assert state_of(config, item_id) == ("skipped", "skipped in the web app")


def test_an_unknown_item_is_a_plain_404(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.post("/api/study-items/999/read")
    assert response.status_code == 404
    assert "no study item 999" in response.json()["detail"]


# ---------------------------------------------------------------------------
# whether a quiz is ready, and asking for one
# ---------------------------------------------------------------------------


def test_untranscribed_pages_are_unquizzable_and_say_why(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config, scan=6, ocr=0)
    status = session.get(f"/api/study-items/{item_id}/quiz").json()
    assert status["kind"] == "not-readable"
    assert "not transcribed" in status["reason"]
    refused = session.post(f"/api/study-items/{item_id}/quiz/request")
    assert refused.status_code == 409
    assert "not transcribed" in refused.json()["detail"]


def test_no_set_yet_says_so_and_a_request_says_when(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    assert session.get(f"/api/study-items/{item_id}/quiz").json()["kind"] == "not-generated"

    refused = session.post(f"/api/study-items/{item_id}/quiz")
    assert refused.status_code == 409
    assert "not been written" in refused.json()["detail"]

    asked = session.post(f"/api/study-items/{item_id}/quiz/request").json()
    assert asked["kind"] == "requested"
    # No heartbeat: the bot is not listening, so the next scheduled run does it.
    assert "at the next sync" in asked["when"]


def test_a_fresh_heartbeat_says_the_bot_will_write_it(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    conn = store.connect(config.db_path)
    beat = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.set_bot_state(conn, store.BOT_HEARTBEAT_KEY, beat)
    conn.commit()
    conn.close()
    asked = session.post(f"/api/study-items/{item_id}/quiz/request").json()
    assert "within a few minutes" in asked["when"]


def test_a_stale_heartbeat_is_not_a_listening_bot(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    conn = store.connect(config.db_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.set_bot_state(conn, store.BOT_HEARTBEAT_KEY, old)
    conn.commit()
    conn.close()
    asked = session.post(f"/api/study-items/{item_id}/quiz/request").json()
    assert "at the next sync" in asked["when"]


def test_asking_spends_nothing(config, monkeypatch):
    """The API records a request; it never writes a set itself."""
    session = client(config, monkeypatch)
    item_id = make_item(config)
    session.post(f"/api/study-items/{item_id}/quiz/request")
    conn = store.connect(config.db_path)
    sets = conn.execute("SELECT count(*) FROM quiz_questions").fetchone()[0]
    conn.close()
    assert sets == 0


# ---------------------------------------------------------------------------
# sitting it
# ---------------------------------------------------------------------------


def test_an_open_attempt_never_carries_the_answers(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    write_set(config, item_id)
    attempt = session.post(f"/api/study-items/{item_id}/quiz").json()
    assert attempt["finished"] is False
    assert attempt["result"] is None
    for question in attempt["questions"]:
        assert set(question) == {"index", "question", "options", "chosen", "flagged"}
    text = str(attempt)
    assert "because of line" not in text  # no explanation
    assert "correct" not in text


def test_answers_mid_quiz_reveal_nothing(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    write_set(config, item_id)
    attempt = session.post(f"/api/study-items/{item_id}/quiz").json()
    after = session.post(
        f"/api/quiz-attempts/{attempt['attempt_id']}/answers", json={"index": 0, "choice": 2}
    ).json()
    assert after["questions"][0]["chosen"] == 2
    assert "correct" not in str(after) and "because of line" not in str(after)


def test_a_passed_quiz_verifies_through_settle(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    write_set(config, item_id)

    calls = []
    real = quiz.settle

    def spy(conn, attempt, **kwargs):
        calls.append(attempt.attempt_id)
        return real(conn, attempt, **kwargs)

    monkeypatch.setattr(quiz, "settle", spy)

    attempt = session.post(f"/api/study-items/{item_id}/quiz").json()
    done = answer_all(session, attempt, right=True)
    assert done["finished"] is True
    assert done["result"]["passed"] is True
    assert done["result"]["verified"] is True
    assert done["result"]["correct"] == 6 and done["result"]["counted"] == 6
    assert done["result"]["needed"] == 5
    assert calls == [attempt["attempt_id"]]
    assert state_of(config, item_id)[0] == "verified"


def test_a_failed_quiz_leaves_the_item_reviewed_and_shows_what_was_missed(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    write_set(config, item_id)
    attempt = session.post(f"/api/study-items/{item_id}/quiz").json()
    done = answer_all(session, attempt, right=False)
    result = done["result"]
    assert result["passed"] is False and result["verified"] is False
    assert state_of(config, item_id)[0] == "reviewed"
    missed = [q for q in result["review"] if not q["right"]]
    assert len(missed) == 6
    # Each missed question points at its source page in the reader.
    assert missed[0]["drive_id"] == "d1"
    assert missed[0]["source_page"] == 3
    assert missed[0]["explanation"] == "because of line 0"


def test_a_flag_leaves_the_denominator(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    write_set(config, item_id)
    attempt = session.post(f"/api/study-items/{item_id}/quiz").json()
    attempt_id = attempt["attempt_id"]
    session.post(f"/api/quiz-attempts/{attempt_id}/flags", json={"index": 0})
    for n in range(1, 6):
        body = session.post(
            f"/api/quiz-attempts/{attempt_id}/answers", json={"index": n, "choice": n % 4}
        ).json()
    result = body["result"]
    assert result["counted"] == 5 and result["correct"] == 5 and result["flagged"] == 1
    assert result["passed"] is True


def test_a_finished_quiz_refuses_more_answers(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config)
    write_set(config, item_id)
    attempt = session.post(f"/api/study-items/{item_id}/quiz").json()
    answer_all(session, attempt, right=False)
    again = session.post(
        f"/api/quiz-attempts/{attempt['attempt_id']}/answers", json={"index": 0, "choice": 0}
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "That quiz is already finished."


def test_one_attempt_is_shared_with_telegram(config, monkeypatch):
    """Started in the bot, resumed in the browser at the same question."""
    session = client(config, monkeypatch)
    item_id = make_item(config)
    conn = store.connect(config.db_path)
    started, _ = quizgen.begin(conn, config, scheduler.item_by_id(conn, item_id), provider=StubModel())
    quiz.record_answer(conn, started, 0, 0)
    conn.close()

    resumed = session.post(f"/api/study-items/{item_id}/quiz").json()
    assert resumed["attempt_id"] == started.attempt_id
    assert resumed["index"] == 1
    assert resumed["questions"][0]["chosen"] == 0
    assert session.get(f"/api/study-items/{item_id}/quiz").json()["kind"] == "open"


def test_the_quizzes_screen_lists_ready_waiting_and_past(config, monkeypatch):
    session = client(config, monkeypatch)
    ready = make_item(config, parent="p1", state="reviewed")
    write_set(config, ready)
    waiting = make_item(config, parent="p2", state="reviewed")
    session.post(f"/api/study-items/{waiting}/quiz/request")

    body = session.get("/api/quizzes").json()
    assert [entry["item_id"] for entry in body["ready"]] == [ready]
    assert [entry["item_id"] for entry in body["waiting"]] == [waiting]
    assert body["waiting"][0]["status"]["kind"] == "requested"

    attempt = session.post(f"/api/study-items/{ready}/quiz").json()
    answer_all(session, attempt, right=False)
    past = session.get("/api/quizzes").json()["attempts"]
    assert past[0]["attempt_id"] == attempt["attempt_id"]
    assert past[0]["correct"] == 0 and past[0]["counted"] == 6
    # Counts, never a percentage.
    assert "score" not in past[0]


def test_a_quiz_needs_reading_first(config, monkeypatch):
    session = client(config, monkeypatch)
    item_id = make_item(config, state="pending")
    write_set(config, item_id)
    status = session.get(f"/api/study-items/{item_id}/quiz").json()
    assert status["kind"] == "not-delivered"
    assert session.post(f"/api/study-items/{item_id}/quiz").status_code == 409
    session.post(f"/api/study-items/{item_id}/read")
    assert session.get(f"/api/study-items/{item_id}/quiz").json()["kind"] == "ready"
