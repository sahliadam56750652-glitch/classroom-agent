"""Everything the API can write, and the one thing it cannot.

The load-bearing test in this file is
`test_verified_stays_zero_after_every_write_route`. It exercises every non-GET
route the app exposes and asserts the count of `verified` study items is still
zero -- the behavioural half of the guarantee whose other halves are the import
guard and the route whitelist. It mirrors the existing Telegram test that presses
every button and asserts the same.

Everything else here is about refusals being the same sentence at a terminal and
over HTTP, which is only true because slice 0 made them exceptions instead of
`print` plus an exit code.
"""

from __future__ import annotations

import io

import pytest
from api_fixtures import COURSE, MANUAL, client, make_config, seed

from agent.api import app as app_mod
from agent.db import store

MONDAY = "2026-09-21"
TUESDAY = "2026-09-22"


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    return made


# A one-page PDF, small enough to inline and real enough to sniff.
PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


# ---------------------------------------------------------------------------
# the guarantee
# ---------------------------------------------------------------------------


def test_verified_stays_zero_after_every_write_route(config, monkeypatch):
    """Every non-GET route, exercised, and `verified` is still zero.

    Enumerated from the app rather than listed by hand, so a write added without
    a thought here still gets pressed. The route whitelist makes adding one a
    deliberate act; this makes adding one that touches study_items a failing test.
    """
    session = client(config, monkeypatch)
    app = session.app if hasattr(session, "app") else None

    # Something for the item-shaped routes to aim at.
    conn = store.connect(config.db_path)
    store.ensure_study_item(
        conn, course_id=COURSE, entity_type="coursework_material", entity_id="p1"
    )
    conn.commit()
    conn.close()

    calls = {
        ("POST", "/api/session"): lambda: session.post("/api/session", json={"token": "x" * 40}),
        ("DELETE", "/api/session"): lambda: None,  # would sign us out; covered elsewhere
        ("PUT", "/api/timetable/file"): lambda: session.put("/api/timetable/file"),
        ("POST", "/api/timetable/file"): lambda: session.post("/api/timetable/file"),
        ("PATCH", "/api/timetable/file"): lambda: session.patch("/api/timetable/file"),
        ("DELETE", "/api/timetable/file"): lambda: session.delete("/api/timetable/file"),
        ("PUT", "/api/documents/{drive_id}/position"): lambda: session.put(
            "/api/documents/d1/position", json={"page_index": 3}
        ),
        ("POST", "/api/adjustments"): lambda: session.post(
            "/api/adjustments",
            json={"kind": "cancelled", "subject": "Database", "on": MONDAY},
        ),
        ("DELETE", "/api/adjustments/{adjustment_id}"): lambda: session.delete(
            "/api/adjustments/1"
        ),
        ("POST", "/api/adjustments/{adjustment_id}/repoint"): lambda: session.post(
            "/api/adjustments/1/repoint", json={"subject": "Database"}
        ),
        ("POST", "/api/projects"): lambda: session.post(
            "/api/projects", json={"subject": "Database", "title": "P", "milestones": ["a"]}
        ),
        ("POST", "/api/projects/{project_id}/milestones"): lambda: session.post(
            "/api/projects/1/milestones", json={"title": "b"}
        ),
        ("POST", "/api/projects/{project_id}/close"): lambda: session.post(
            "/api/projects/1/close"
        ),
        ("POST", "/api/milestones/{milestone_id}/complete"): lambda: session.post(
            "/api/milestones/1/complete"
        ),
        ("POST", "/api/tasks"): lambda: session.post(
            "/api/tasks", json={"subject": "Database", "title": "TD1", "due": "2026-09-30"}
        ),
        ("POST", "/api/tasks/{task_id}/complete"): lambda: session.post(
            "/api/tasks/1/complete"
        ),
        ("POST", "/api/sessions"): lambda: session.post(
            "/api/sessions", json={"subject": "Database", "kind": "LEC", "on": MONDAY}
        ),
        ("POST", "/api/subjects/manual"): lambda: session.post(
            "/api/subjects/manual", json={"subject": "Calculus III"}
        ),
        ("POST", "/api/uploads"): lambda: session.post(
            "/api/uploads",
            files={"file": ("board.pdf", io.BytesIO(PDF), "application/pdf")},
            data={"subject": "Database"},
        ),
    }

    declared = set(app_mod.WRITE_ROUTES)
    assert set(calls) == declared, (
        "this test and WRITE_ROUTES disagree; every write must be exercised here"
    )

    for route, call in calls.items():
        call()

    conn = store.connect(config.db_path)
    states = store.count_study_items_by_state(conn)
    conn.close()
    assert states.get("verified", 0) == 0, states


def test_no_write_route_ever_creates_a_gate_run(config, monkeypatch):
    session = client(config, monkeypatch)
    session.post(
        "/api/adjustments",
        json={"kind": "cancelled", "subject": "Database", "on": MONDAY},
    )
    session.post("/api/tasks", json={"subject": "Database", "title": "TD1"})

    conn = store.connect(config.db_path)
    assert store.count_rows(conn, "gate_runs") == 0
    conn.close()


# ---------------------------------------------------------------------------
# adjustments
# ---------------------------------------------------------------------------


def test_cancelling_a_session_records_a_dated_fact(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.post(
        "/api/adjustments",
        json={"kind": "cancelled", "subject": "Database", "on": MONDAY,
              "reason": "strike"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "cancelled"
    assert body["session_start"] == "08:30"
    assert "unchanged" in body["note"]


def test_the_file_is_never_written(config, monkeypatch):
    """The constraint the whole adjustments layer exists to respect."""
    before = config.timetable_path.read_text(encoding="utf-8")
    session = client(config, monkeypatch)
    session.post(
        "/api/adjustments",
        json={"kind": "cancelled", "subject": "Database", "on": MONDAY},
    )
    assert config.timetable_path.read_text(encoding="utf-8") == before


def test_moving_a_session_says_which_evening_now_gates_it(config, monkeypatch):
    """Falls out of resolving by date rather than by pattern."""
    session = client(config, monkeypatch)
    body = session.post(
        "/api/adjustments",
        json={"kind": "moved", "subject": "Database", "on": MONDAY,
              "to_date": "2026-09-23", "to_time": "14:00"},
    ).json()

    assert body["lands_on"] == "2026-09-23"
    assert body["gate_moves"] is True
    assert body["gate_evening"] == "2026-09-22"


def test_a_second_adjustment_on_one_session_is_refused_not_merged(config, monkeypatch):
    """Which of them wins is not a question anything here can answer."""
    session = client(config, monkeypatch)
    first = session.post(
        "/api/adjustments",
        json={"kind": "cancelled", "subject": "Database", "on": MONDAY},
    )
    assert first.status_code == 201

    second = session.post(
        "/api/adjustments",
        json={"kind": "moved", "subject": "Database", "on": MONDAY, "to_time": "14:00"},
    )
    assert second.status_code == 409
    assert "already adjusted" in second.text
    assert "neither is guessed" in second.text


def test_a_subject_that_is_not_in_the_file_is_refused_exactly(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.post(
        "/api/adjustments",
        json={"kind": "cancelled", "subject": "Databse", "on": MONDAY},
    )

    assert response.status_code == 400
    assert "never matched approximately" in response.text


def test_a_session_the_pattern_does_not_have_is_refused_with_what_it_does(
    config, monkeypatch
):
    session = client(config, monkeypatch)
    response = session.post(
        "/api/adjustments",
        json={"kind": "cancelled", "subject": "Database", "on": TUESDAY},
    )

    assert response.status_code == 400
    assert "--extra" in response.text


def test_a_dry_run_resolves_everything_and_writes_nothing(config, monkeypatch):
    session = client(config, monkeypatch)
    body = session.post(
        "/api/adjustments",
        params={"dry_run": "true"},
        json={"kind": "cancelled", "subject": "Database", "on": MONDAY},
    ).json()

    assert body["written"] is False
    assert body["session_start"] == "08:30"

    conn = store.connect(config.db_path)
    assert store.count_rows(conn, "timetable_adjustments") == 0
    conn.close()


def test_an_adjustment_can_be_removed_and_the_pattern_returns(config, monkeypatch):
    session = client(config, monkeypatch)
    made = session.post(
        "/api/adjustments",
        json={"kind": "cancelled", "subject": "Database", "on": MONDAY},
    ).json()

    removed = session.delete(f"/api/adjustments/{made['id']}")
    assert removed.status_code == 200
    assert "reasserts itself" in removed.text

    day = session.get(
        "/api/timetable", params={"from": MONDAY, "to": MONDAY}
    ).json()["days"][0]
    assert len(day["sessions"]) == 1


def test_the_api_and_the_cli_record_an_identical_row(config, monkeypatch):
    """One implementation, so the two cannot disagree. The point of slice 0."""
    from agent.gate import adjustments as adjust, timetable as tt

    session = client(config, monkeypatch)
    session.post(
        "/api/adjustments",
        json={"kind": "cancelled", "subject": "Database", "on": MONDAY,
              "reason": "strike"},
    )

    table = tt.load(config.timetable_path)
    plan = adjust.plan(
        table,
        adjust.AdjustmentSpec(
            kind="cancelled", subject="Database", on=MONDAY, reason="strike"
        ),
    )
    conn = store.connect(config.db_path)
    row = adjust.find_clash(conn, plan)
    conn.close()

    assert row is not None
    assert str(row["kind"]) == "cancelled"
    assert str(row["session_start"]) == plan.fields["session_start"]


# ---------------------------------------------------------------------------
# projects -- two integers, never a percentage
# ---------------------------------------------------------------------------


def test_a_project_and_its_milestones_land_together(config, monkeypatch):
    session = client(config, monkeypatch)
    body = session.post(
        "/api/projects",
        json={"subject": "Database", "title": "Normalisation report",
              "deadline": "2026-10-15", "milestones": ["outline", "draft", "submit"]},
    ).json()

    assert body["milestones_total"] == 3
    assert body["milestones_done"] == 0
    assert body["deadline_at"].startswith("2026-10-15")


def test_project_progress_is_two_integers_and_no_percentage(config, monkeypatch):
    """A number I can quietly revise upward is what DESIGN.md forbids."""
    session = client(config, monkeypatch)
    made = session.post(
        "/api/projects",
        json={"subject": "Database", "title": "P", "milestones": ["a", "b"]},
    ).json()

    for forbidden in ("percent", "progress_pct", "completion", "ratio"):
        assert forbidden not in made

    session.post(f"/api/milestones/{made['milestones'][0]['id']}/complete")
    after = session.get(f"/api/projects/{made['id']}").json()
    assert (after["milestones_done"], after["milestones_total"]) == (1, 2)


def test_a_duplicate_project_name_in_one_subject_is_refused(config, monkeypatch):
    session = client(config, monkeypatch)
    first = session.post("/api/projects", json={"subject": "Database", "title": "P"})
    assert first.status_code == 201

    second = session.post("/api/projects", json={"subject": "Database", "title": "P"})
    assert second.status_code == 409
    assert "two deadlines for one piece of work" in second.text


def test_completing_a_milestone_twice_is_not_an_error(config, monkeypatch):
    session = client(config, monkeypatch)
    made = session.post(
        "/api/projects", json={"subject": "Database", "title": "P", "milestones": ["a"]}
    ).json()
    step = made["milestones"][0]["id"]

    assert session.post(f"/api/milestones/{step}/complete").status_code == 200
    again = session.post(f"/api/milestones/{step}/complete")
    assert again.status_code == 200
    assert again.json()["done_at"]


def test_a_project_deadline_joins_the_existing_scanner(config, monkeypatch):
    """Not a parallel scanner: a due date on a row is all the scanner needs."""
    session = client(config, monkeypatch)
    session.post(
        "/api/projects",
        json={"subject": "Database", "title": "Report", "deadline": "2026-10-15"},
    )

    body = session.get("/api/deadlines").json()
    assert [row["label"] for row in body] == ["project"]


def test_a_closed_project_stops_being_chased(config, monkeypatch):
    session = client(config, monkeypatch)
    made = session.post(
        "/api/projects",
        json={"subject": "Database", "title": "Report", "deadline": "2026-10-15"},
    ).json()

    session.post(f"/api/projects/{made['id']}/close")
    assert session.get("/api/deadlines").json() == []


# ---------------------------------------------------------------------------
# tasks and sessions
# ---------------------------------------------------------------------------


def test_a_task_is_recorded_and_joins_the_scanner(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.post(
        "/api/tasks",
        json={"subject": "Calculus III", "title": "TD3 Graphes", "due": "2026-09-30"},
    )

    assert response.status_code == 201
    assert response.json()["due_at"].startswith("2026-09-30")
    assert len(session.get("/api/deadlines").json()) == 1


def test_a_dated_task_is_due_at_end_of_day_locally(config, monkeypatch):
    """What Classroom means by an absent dueTime -- not midnight, 24 h early."""
    session = client(config, monkeypatch)
    body = session.post(
        "/api/tasks",
        json={"subject": "Calculus III", "title": "TD3", "due": "2026-09-30"},
    ).json()

    # 23:59 Africa/Tunis is 22:59 UTC.
    assert body["due_at"] == "2026-09-30T22:59:00Z"


def test_an_identical_task_is_refused_rather_than_duplicated(config, monkeypatch):
    session = client(config, monkeypatch)
    payload = {"subject": "Calculus III", "title": "TD3", "due": "2026-09-30"}
    assert session.post("/api/tasks", json=payload).status_code == 201

    again = session.post("/api/tasks", json=payload)
    assert again.status_code == 409
    assert "two alerts for one sheet" in again.text


def test_an_undated_task_is_recorded_and_visible_on_tasks(config, monkeypatch):
    """Where `/api/deadlines` deliberately does not carry it."""
    session = client(config, monkeypatch)
    session.post("/api/tasks", json={"subject": "Calculus III", "title": "Someday"})

    assert [row["title"] for row in session.get("/api/tasks").json()] == ["Someday"]
    assert session.get("/api/deadlines").json() == []


def test_a_held_session_is_logged_with_what_was_covered(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.post(
        "/api/sessions",
        json={"subject": "Calculus III", "kind": "TUT", "on": MONDAY,
              "covered": "integration by parts"},
    )

    assert response.status_code == 201
    assert response.json()["covered"] == "integration by parts"
    assert len(session.get("/api/sessions").json()) == 1


def test_a_subject_not_in_the_file_is_refused_for_every_entry(config, monkeypatch):
    """One resolution, one refusal, everywhere."""
    session = client(config, monkeypatch)
    for path, payload in (
        ("/api/tasks", {"subject": "Nope", "title": "x"}),
        ("/api/sessions", {"subject": "Nope", "kind": "LEC"}),
        ("/api/projects", {"subject": "Nope", "title": "x"}),
    ):
        response = session.post(path, json=payload)
        assert response.status_code == 400, path
        assert "Nope" in response.text


# ---------------------------------------------------------------------------
# manual subjects
# ---------------------------------------------------------------------------


def test_minting_a_manual_subject_returns_the_line_to_paste(config, monkeypatch):
    """It does the database half and hands back the half that needs the file."""
    session = client(config, monkeypatch)
    body = session.post(
        "/api/subjects/manual", json={"subject": "Calculus III"}
    ).json()

    assert body["course_id"] == MANUAL
    assert body["paste"].strip() == f"Calculus III: {MANUAL}"


def test_a_subject_with_a_classroom_course_cannot_be_made_manual(config, monkeypatch):
    """A subject has one identity."""
    session = client(config, monkeypatch)
    response = session.post("/api/subjects/manual", json={"subject": "Database"})

    assert response.status_code == 409
    assert "one identity" in response.text


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


def test_an_upload_writes_the_three_rows_fetch_would_have(config, monkeypatch):
    """Download is the only stage an upload skips."""
    session = client(config, monkeypatch)
    response = session.post(
        "/api/uploads",
        files={"file": ("board.pdf", io.BytesIO(PDF), "application/pdf")},
        data={"subject": "Database", "title": "Tuesday's board"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["written"] is True
    assert body["drive_id"].startswith("manual-")

    conn = store.connect(config.db_path)
    assert store.get_extraction(conn, body["drive_id"]) is not None
    post = conn.execute(
        "SELECT title FROM coursework_materials WHERE id = ?", (body["post_id"],)
    ).fetchone()
    conn.close()
    assert post["title"] == "Tuesday's board"


def test_an_uploaded_file_lands_in_the_library(config, monkeypatch):
    session = client(config, monkeypatch)
    body = session.post(
        "/api/uploads",
        files={"file": ("board.pdf", io.BytesIO(PDF), "application/pdf")},
        data={"subject": "Database"},
    ).json()

    conn = store.connect(config.db_path)
    row = store.get_extraction(conn, body["drive_id"])
    conn.close()
    assert (config.library_dir / str(row["local_path"])).read_bytes() == PDF


def test_an_upload_dry_run_resolves_everything_and_writes_nothing(config, monkeypatch):
    """The same resolution and the same refusals, which is what makes it useful."""
    session = client(config, monkeypatch)
    body = session.post(
        "/api/uploads",
        params={"dry_run": "true"},
        files={"file": ("board.pdf", io.BytesIO(PDF), "application/pdf")},
        data={"subject": "Database"},
    ).json()

    assert body["written"] is False
    assert body["course_id"] == COURSE

    conn = store.connect(config.db_path)
    assert store.count_rows(conn, "extractions") == 0
    conn.close()


def test_a_renamed_file_is_refused_because_renaming_is_not_converting(
    config, monkeypatch
):
    """Said here, where the answer is known -- not three stages later."""
    session = client(config, monkeypatch)
    response = session.post(
        "/api/uploads",
        files={"file": ("board.pdf", io.BytesIO(b"not a pdf at all"), "application/pdf")},
        data={"subject": "Database"},
    )

    assert response.status_code == 400
    assert "does not convert it" in response.text


def test_an_unsupported_format_names_what_is_accepted(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.post(
        "/api/uploads",
        files={"file": ("notes.xyz", io.BytesIO(b"whatever"), "application/octet-stream")},
        data={"subject": "Database"},
    )

    assert response.status_code == 400
    assert ".pdf" in response.text


def test_an_empty_file_is_refused(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.post(
        "/api/uploads",
        files={"file": ("board.pdf", io.BytesIO(b""), "application/pdf")},
        data={"subject": "Database"},
    )

    assert response.status_code == 400


def test_nothing_is_left_in_the_spool_directory(config, monkeypatch):
    """Under DATA_DIR, per invariant 5, and cleaned up on every path."""
    session = client(config, monkeypatch)
    session.post(
        "/api/uploads",
        files={"file": ("board.pdf", io.BytesIO(PDF), "application/pdf")},
        data={"subject": "Database"},
    )
    session.post(
        "/api/uploads",
        files={"file": ("bad.pdf", io.BytesIO(b"nope"), "application/pdf")},
        data={"subject": "Database"},
    )

    spool = config.data_dir / "tmp"
    assert not spool.exists() or not list(spool.iterdir())


def test_an_upload_for_a_tracked_subject_is_still_a_manual_row(config, monkeypatch):
    """The case the prefix guards exist for: a manual row inside a course the
    sync does reconcile."""
    session = client(config, monkeypatch)
    body = session.post(
        "/api/uploads",
        files={"file": ("board.pdf", io.BytesIO(PDF), "application/pdf")},
        data={"subject": "Database"},
    ).json()

    assert body["course_id"] == COURSE
    assert body["post_id"].startswith("manual-")

    conn = store.connect(config.db_path)
    stamped = store.soft_delete_missing(conn, "coursework_materials", COURSE, [])
    conn.close()
    assert body["post_id"] not in stamped


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_a_write_from_another_origin_is_refused_when_one_is_configured(
    tmp_path, monkeypatch
):
    made = make_config(tmp_path, api_origin="https://classroom.example.org")
    seed(made)
    session = client(made, monkeypatch)

    allowed = session.post(
        "/api/tasks",
        json={"subject": "Database", "title": "ok"},
        headers={"Origin": "https://classroom.example.org"},
    )
    assert allowed.status_code == 201

    refused = session.post(
        "/api/tasks",
        json={"subject": "Database", "title": "not ok"},
        headers={"Origin": "https://evil.example"},
    )
    assert refused.status_code == 403
    assert "did not come from this app" in refused.text


def test_a_read_from_another_origin_is_fine(tmp_path, monkeypatch):
    """SameSite=Lax governs the cookie; the Origin check is for state changes."""
    made = make_config(tmp_path, api_origin="https://classroom.example.org")
    seed(made)
    session = client(made, monkeypatch)

    response = session.get("/api/subjects", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
