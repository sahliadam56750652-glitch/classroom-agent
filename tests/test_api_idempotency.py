"""A write the phone sends twice is applied once. Phase 5e, the outbox.

The phone queues read, skip, a milestone, a timetable change and a reading
position while the PC is off, and replays them when it is back. A replay whose
reply was lost is sent again with the same `Idempotency-Key`; the second request
must get the first one's answer and change nothing.
"""

from __future__ import annotations

import pytest
from api_fixtures import COURSE, client, make_config, seed

from agent.api import idempotency
from agent.db import store

MONDAY = "2026-09-21"


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    conn = store.connect(made.db_path)
    for parent in ("p1", "p2"):
        conn.execute(
            "INSERT INTO coursework_materials (id, course_id, title, alternate_link, "
            "creation_time, content_hash, first_seen_at) "
            "VALUES (?, ?, 'Chapter', 'https://classroom/x', '2026-09-01T00:00:00Z', 'h', 'now')",
            (parent, COURSE),
        )
        store.ensure_study_item(conn, course_id=COURSE, entity_type="coursework_material",
                                entity_id=parent)
    conn.commit()
    conn.close()
    return made


def key(value: str) -> dict:
    return {idempotency.HEADER: value}


def rows(config, sql, *args):
    conn = store.connect(config.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_a_replayed_read_returns_the_first_answer(config, monkeypatch):
    session = client(config, monkeypatch)
    first = session.post("/api/study-items/1/read", headers=key("op-read-1"))
    again = session.post("/api/study-items/1/read", headers=key("op-read-1"))
    assert first.status_code == again.status_code == 200
    # Without the key the second Read is honestly "did not move"; with it, the
    # phone gets the answer its lost reply carried.
    assert first.json() == again.json()
    assert first.json()["moved"] is True
    assert again.headers.get("Idempotent-Replayed") == "true"


def test_a_replayed_skip_is_logged_once(config, monkeypatch):
    session = client(config, monkeypatch)
    for _ in range(3):
        response = session.post(
            "/api/study-items/2/skip", json={"reason": "covered in the TD"},
            headers=key("op-skip-1"),
        )
        assert response.status_code == 200
    (state, reason), = rows(config, "SELECT state, skip_reason FROM study_items WHERE id = 2")
    assert state == "skipped"
    # A second application would have appended the floor sentence twice.
    assert reason.count("covered in the TD") == 1


def test_a_different_key_is_a_different_write(config, monkeypatch):
    session = client(config, monkeypatch)
    session.post("/api/study-items/1/read", headers=key("a"))
    second = session.post("/api/study-items/1/read", headers=key("b"))
    assert second.headers.get("Idempotent-Replayed") is None
    assert second.json()["moved"] is False


def test_no_key_is_not_remembered(config, monkeypatch):
    session = client(config, monkeypatch)
    session.post("/api/study-items/1/read")
    assert rows(config, "SELECT COUNT(*) FROM client_ops")[0][0] == 0


def test_a_replayed_adjustment_is_recorded_once(config, monkeypatch):
    session = client(config, monkeypatch)
    body = {"kind": "cancelled", "subject": "Database", "on": MONDAY}
    first = session.post("/api/adjustments", json=body, headers=key("op-adj-1"))
    again = session.post("/api/adjustments", json=body, headers=key("op-adj-1"))
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    assert rows(config, "SELECT COUNT(*) FROM timetable_adjustments")[0][0] == 1


def test_a_refusal_is_not_remembered(config, monkeypatch):
    """Only an applied write has an answer to replay. A refusal is computed
    again, so a retry after the cause is fixed can still land."""
    session = client(config, monkeypatch)
    body = {"kind": "cancelled", "subject": "Database", "on": MONDAY}
    session.post("/api/adjustments", json=body)
    clash = session.post("/api/adjustments", json=body, headers=key("op-adj-2"))
    assert clash.status_code == 409
    assert rows(config, "SELECT COUNT(*) FROM client_ops")[0][0] == 0


def test_a_dry_run_is_not_remembered(config, monkeypatch):
    session = client(config, monkeypatch)
    body = {"kind": "cancelled", "subject": "Database", "on": MONDAY}
    session.post("/api/adjustments?dry_run=true", json=body, headers=key("op-dry"))
    real = session.post("/api/adjustments", json=body, headers=key("op-dry"))
    assert real.status_code == 201
    assert real.json()["id"] is not None


def test_a_replayed_position_and_milestone(config, monkeypatch):
    session = client(config, monkeypatch)
    made = session.post(
        "/api/projects", json={"subject": "Database", "title": "P", "milestones": ["a"]}
    ).json()
    step = made["milestones"][0]["id"]
    first = session.post(f"/api/milestones/{step}/complete", headers=key("op-ms"))
    again = session.post(f"/api/milestones/{step}/complete", headers=key("op-ms"))
    assert first.json() == again.json()

    conn = store.connect(config.db_path)
    store.upsert_extraction(conn, "d1", status="ok", pages=20, chars=0, scan_pages=0,
                            ocr_pages=0, local_path="files/d1.pdf", text_path=None,
                            size_bytes=10, extracted_at="2026-09-02T00:00:00Z")
    conn.commit()
    conn.close()
    put = session.put("/api/documents/d1/position", json={"page_index": 3}, headers=key("op-pos"))
    assert put.status_code == 200
    replay = session.put("/api/documents/d1/position", json={"page_index": 3},
                         headers=key("op-pos"))
    assert replay.json() == put.json()
    assert replay.headers.get("Idempotent-Replayed") == "true"


def test_a_stored_answer_needs_a_session(config, monkeypatch):
    session = client(config, monkeypatch)
    session.post("/api/study-items/1/read", headers=key("op-secret"))
    outsider = client(config, monkeypatch, sign_in=False)
    assert outsider.post("/api/study-items/1/read", headers=key("op-secret")).status_code == 401


def test_client_ops_is_not_backed_up():
    """A queue's receipts: emptying the table loses nothing."""
    from agent import backup
    assert "client_ops" not in {spec.table for spec in backup.SPECS}
