"""The snapshot is the API, gathered -- never a second answer.

Every entry of GET /api/sync must equal what the client would have got by
calling that GET itself. If the two ever disagreed, the phone would show one
thing offline and another online, and neither would look wrong.
"""

from __future__ import annotations

import pytest
from api_fixtures import COURSE, client, make_config, seed

from agent.db import store


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    conn = store.connect(made.db_path)
    store.ensure_study_item(conn, course_id=COURSE, entity_type="coursework_material", entity_id="p1")
    store.add_manual_task(conn, course_id="manual-calculus-iii", title="TD1",
                          kind="exercise_sheet", due_at="2026-12-01T21:59:00Z")
    project = store.add_project(conn, course_id=COURSE, title="P", deadline_at=None,
                                deliverables=None, team=None)
    store.add_milestone(conn, project, "first")
    conn.commit()
    conn.close()
    return made


def test_every_entry_equals_its_get(config, monkeypatch):
    session = client(config, monkeypatch)
    body = session.get("/api/sync").json()
    entries = body["entries"]

    # The ones every screen needs are there.
    for path in ("/api/subjects", "/api/homework", "/api/projects", "/api/library",
                 "/api/quizzes", "/api/status", "/api/timetable", "/api/deadlines"):
        assert path in entries, path
    assert any(path.startswith("/api/now?date=") for path in entries)
    assert any(path.startswith("/api/projects/") for path in entries)
    assert any(path.startswith("/api/subjects/") for path in entries)

    for path, value in entries.items():
        if path == "/api/timetable":
            live = session.get(
                path, params={"from": body["timetable_from"], "to": body["timetable_to"]}
            ).json()
        else:
            live = session.get(path).json()
        if path == "/api/status":
            # Signed-in device count and the sync that was running can move
            # between two requests; everything else must match exactly.
            for key in ("sessions_live",):
                live.pop(key, None)
                value = {k: v for k, v in value.items() if k != key}
        assert value == live, path


def test_the_plans_cover_today_and_a_week(config, monkeypatch):
    body = client(config, monkeypatch).get("/api/sync").json()
    dates = sorted(p.split("=", 1)[1] for p in body["entries"] if p.startswith("/api/now?date="))
    assert len(dates) == body["now_days"] == 8
    assert dates[0] == body["today"]


def test_a_broken_timetable_says_which_entries_are_missing(config, monkeypatch):
    config.timetable_path.write_text("not: [valid", encoding="utf-8")
    body = client(config, monkeypatch).get("/api/sync").json()
    assert "/api/subjects" in body["missing"]
    assert "timetable.yaml" in body["missing"]["/api/subjects"]
    # What does not need the timetable is still synced.
    assert "/api/projects" in body["entries"]


def test_the_snapshot_needs_a_session(config, monkeypatch):
    assert client(config, monkeypatch, sign_in=False).get("/api/sync").status_code == 401


def test_only_this_semesters_courses_are_offered_for_the_phone(tmp_path, monkeypatch):
    """A course still tracked but not in the timetable -- last year's -- never has
    its documents copied to the phone."""
    made = make_config(tmp_path, tracked_courses=[COURSE, "999-archived"])
    seed(made)
    body = client(made, monkeypatch).get("/api/sync").json()
    assert COURSE in body["offline_courses"]
    assert "999-archived" not in body["offline_courses"]
