"""The read endpoints, and the DESIGN.md rules that show up as missing fields.

Three things get asserted here that are easy to lose and expensive to notice:

  * `/api/now` returns the ABSENCE of an item when there is nothing waiting, not
    an empty list. That is `composer.compose()` returning None over HTTP.
  * No response anywhere carries a percentage, and no deadline carries a
    severity. A client cannot render a field the server never sends, which is why
    the absence is the enforcement.
  * Reading never fires the gate. `gate_runs` is untouched by every GET.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from api_fixtures import COURSE, MANUAL, client, make_config, seed

from agent.classroom.models import parse_coursework_material
from agent.db import store

# 2026-09-21 is a Monday, so the pattern's Database lecture falls on it.
MONDAY = date(2026, 9, 21)
TUESDAY = date(2026, 9, 22)
WEDNESDAY = date(2026, 9, 23)


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    return made


def add_post(config, *, course=COURSE, post_id="p1", title="Chapter 1, SQL joins",
             posted="2026-09-01T09:00:00Z", state="pending"):
    """One Classroom post with a study item against it."""
    conn = store.connect(config.db_path)
    material, _ = parse_coursework_material(
        {"id": post_id, "title": title, "creationTime": posted, "updateTime": posted},
        course,
    )
    store.upsert_coursework_material(conn, material)
    item_id = store.ensure_study_item(
        conn, course_id=course, entity_type="coursework_material", entity_id=post_id
    )
    if state != "pending" and item_id is not None:
        conn.execute("UPDATE study_items SET state = ? WHERE id = ?", (state, item_id))
    conn.commit()
    found = conn.execute(
        "SELECT id FROM study_items WHERE entity_id = ?", (post_id,)
    ).fetchone()
    conn.close()
    return int(found["id"])


# ---------------------------------------------------------------------------
# /api/now -- the default screen
# ---------------------------------------------------------------------------


def test_nothing_waiting_is_the_absence_of_an_item(config, monkeypatch):
    """The empty state, and it must not be an empty list dressed up as content."""
    session = client(config, monkeypatch)
    body = session.get("/api/now").json()

    assert body["waiting"] is False
    assert body["item"] is None
    assert body["subject"] is None
    assert body["windows"] == []


def test_an_empty_day_says_which_kind_of_empty_it_is(config, monkeypatch):
    """Silence is the right output; an unexplained blank is not."""
    session = client(config, monkeypatch)
    body = session.get("/api/gate", params={"date": "2026-09-27"}).json()

    assert body["worth_sending"] is False
    assert body["silent_because"]


def test_one_unreviewed_post_becomes_the_one_thing_to_do(config, monkeypatch):
    add_post(config)
    session = client(config, monkeypatch)
    body = session.get("/api/gate", params={"date": MONDAY.isoformat()}).json()

    assert body["worth_sending"] is True
    assert body["total_items"] == 1
    behind = [s for s in body["subjects"] if s["state"] == "behind"]
    assert [s["name"] for s in behind] == ["Database"]
    assert behind[0]["next_item"]["label"] == "Chapter 1, SQL joins"


def test_the_deficit_is_one_line_and_names_an_action(config, monkeypatch):
    """DESIGN.md rule 1: no number without an action next to it."""
    add_post(config)
    session = client(config, monkeypatch)
    body = session.get("/api/gate", params={"date": MONDAY.isoformat()}).json()
    subject = next(s for s in body["subjects"] if s["name"] == "Database")

    assert subject["unreviewed"] == 1
    assert subject["next_item"] is not None


def test_an_unreadable_item_says_so_rather_than_offering_a_quiz(config, monkeypatch):
    """`blocked_reason` is carried, always, not left to be inferred."""
    item_id = add_post(config)
    session = client(config, monkeypatch)
    body = session.get(f"/api/study-items/{item_id}").json()

    assert body["item"]["ready"] is False
    assert body["item"]["blocked_reason"]


def test_a_missing_study_item_is_a_404_not_an_empty_body(config, monkeypatch):
    session = client(config, monkeypatch)
    assert session.get("/api/study-items/9999").status_code == 404


# ---------------------------------------------------------------------------
# reading never fires the gate
# ---------------------------------------------------------------------------


def test_no_read_ever_writes_a_gate_run(config, monkeypatch):
    """The sharpest rule in this slice.

    `gate_runs.for_date` means a stray row would let a morning page silently
    swallow that evening's real prompt -- the same failure that keeps `agent gate`
    out of `agent run`, arriving through a GET instead of a schedule.
    """
    add_post(config)
    session = client(config, monkeypatch)

    for path, params in (
        ("/api/now", None),
        ("/api/gate", {"date": MONDAY.isoformat()}),
        ("/api/subjects", None),
        ("/api/deadlines", None),
        ("/api/timetable", {"from": MONDAY.isoformat(), "to": WEDNESDAY.isoformat()}),
    ):
        assert session.get(path, params=params).status_code == 200, path

    conn = store.connect(config.db_path)
    assert store.count_rows(conn, "gate_runs") == 0
    assert store.get_gate_run_for(conn, MONDAY.isoformat()) is None
    conn.close()


def test_no_read_ever_moves_a_study_item(config, monkeypatch):
    item_id = add_post(config)
    session = client(config, monkeypatch)

    session.get("/api/now")
    session.get(f"/api/study-items/{item_id}")

    conn = store.connect(config.db_path)
    row = store.get_study_item(conn, item_id)
    states = store.count_study_items_by_state(conn)
    conn.close()
    assert row["state"] == "pending"
    assert states.get("verified", 0) == 0


# ---------------------------------------------------------------------------
# /api/subjects -- the four states, and no percentage
# ---------------------------------------------------------------------------


def test_every_subject_appears_including_the_ungatable(config, monkeypatch):
    """A third of my week has no Classroom. Leaving it out would be a report on
    the two-thirds that happen to have an API."""
    session = client(config, monkeypatch)
    body = session.get("/api/subjects").json()

    assert {s["name"] for s in body} == {"Calculus III", "Database", "Probability"}


def test_a_subject_awaiting_a_course_is_not_up_to_date(config, monkeypatch):
    """Probability maps to null. It must never read as up to date or as 0."""
    session = client(config, monkeypatch)
    body = session.get("/api/subjects/Probability").json()

    assert body["state"] == "awaiting_course"
    assert body["gated"] is False
    assert body["unreviewed"] == 0


def test_a_manual_subject_with_nothing_entered_says_that(config, monkeypatch):
    """Not "no readable material": nothing is missing and nothing is broken."""
    session = client(config, monkeypatch)
    body = session.get("/api/subjects/Calculus III").json()

    assert body["state"] == "nothing_entered"
    assert body["manual"] is True


def test_a_course_with_no_items_is_not_up_to_date(config, monkeypatch):
    """The Probability & Statistics case: 20 dead attachments, nothing readable."""
    session = client(config, monkeypatch)
    body = session.get("/api/subjects/Database").json()

    assert body["state"] == "no_readable_material"


def test_a_subject_with_an_unreviewed_post_is_behind(config, monkeypatch):
    add_post(config)
    session = client(config, monkeypatch)
    body = session.get("/api/subjects/Database").json()

    assert body["state"] == "behind"
    assert body["unreviewed"] == 1
    assert body["oldest_posted_at"] == "2026-09-01T09:00:00Z"


def test_a_subject_is_matched_exactly_never_approximately(config, monkeypatch):
    """Case-insensitive because it came from a URL; never a near-match."""
    session = client(config, monkeypatch)
    assert session.get("/api/subjects/database").status_code == 200
    assert session.get("/api/subjects/Databse").status_code == 404
    assert "never matched approximately" in session.get("/api/subjects/Databse").text


def test_no_subject_response_carries_a_percentage(config, monkeypatch):
    """DESIGN.md forbids a percentage of a deficit. Absence is the enforcement."""
    add_post(config)
    session = client(config, monkeypatch)
    raw = session.get("/api/subjects").text

    for forbidden in ("percent", "percentage", "coverage", "ratio", "pct"):
        assert forbidden not in raw.lower(), forbidden
    for subject in json.loads(raw):
        assert not any(
            isinstance(value, float) for value in subject.values()
        ), subject


def test_the_standing_the_api_serves_is_the_one_the_cli_prints(config, monkeypatch):
    """One function, two readers. The only way they cannot disagree."""
    add_post(config)
    from agent import scope as scope_mod
    from agent.gate import scheduler, timetable as tt

    session = client(config, monkeypatch)
    body = session.get("/api/subjects").json()

    table = tt.load(config.timetable_path)
    conn = store.connect(config.db_path)
    direct = scheduler.standing(conn, scope_mod.local(config, table), table)
    conn.close()

    assert [s["name"] for s in body] == [s.name for s in direct]
    assert [s["state"] for s in body] == [s.state for s in direct]


# ---------------------------------------------------------------------------
# /api/deadlines -- an instant, and nothing else about time
# ---------------------------------------------------------------------------


def test_a_deadline_carries_no_urgency_of_any_kind(config, monkeypatch):
    """DESIGN.md forbids manufactured urgency. There is nowhere to put one."""
    conn = store.connect(config.db_path)
    store.add_manual_task(
        conn, course_id=MANUAL, title="TD3 Graphes", kind="exercise_sheet",
        due_at="2026-09-25T22:59:00Z",
    )
    conn.commit()
    conn.close()

    session = client(config, monkeypatch)
    body = session.get("/api/deadlines").json()

    assert len(body) == 1
    assert body[0]["due_at"] == "2026-09-25T22:59:00Z"
    for forbidden in ("severity", "urgency", "countdown", "hours_left", "overdue"):
        assert forbidden not in body[0]


def test_an_undated_task_is_not_a_deadline(config, monkeypatch):
    """It is a real row with no deadline, and this endpoint lists deadlines.

    `deadlines._manual_task_candidates` filters `due_at IS NOT NULL`, because
    nothing undated can cross a T-72/24/3 threshold. The row is reachable on
    `/api/tasks`, which lists tasks. Pinned so the asymmetry is deliberate rather
    than discovered: undated COURSEWORK is a candidate with a null `due_at`, and
    an undated manual task is not a candidate at all.
    """
    conn = store.connect(config.db_path)
    store.add_manual_task(conn, course_id=MANUAL, title="No date", kind="exercise_sheet")
    store.add_manual_task(
        conn, course_id=MANUAL, title="Dated", kind="exercise_sheet",
        due_at="2026-09-25T22:59:00Z",
    )
    conn.commit()
    conn.close()

    session = client(config, monkeypatch)
    body = session.get("/api/deadlines").json()

    assert [row["title"] for row in body] == ["Dated"]


def test_undated_coursework_is_a_candidate_with_a_null_due_at(config, monkeypatch):
    """The other half of that asymmetry, so neither can change unnoticed."""
    conn = store.connect(config.db_path)
    from agent.classroom.models import parse_coursework

    work, _ = parse_coursework({"id": "w-undated", "title": "TD with no date"}, COURSE)
    store.upsert_coursework(conn, work)
    conn.commit()
    conn.close()

    session = client(config, monkeypatch)
    body = session.get("/api/deadlines").json()

    undated = [row for row in body if row["title"] == "TD with no date"]
    assert len(undated) == 1
    assert undated[0]["due_at"] is None


def test_the_deadline_screen_never_writes_an_event(config, monkeypatch):
    """The scanner belongs to `agent run`. A GET must not fire T-72/24/3."""
    conn = store.connect(config.db_path)
    store.add_manual_task(
        conn, course_id=MANUAL, title="Due soon", kind="exercise_sheet",
        due_at="2026-09-25T22:59:00Z",
    )
    conn.commit()
    conn.close()

    session = client(config, monkeypatch)
    session.get("/api/deadlines")

    conn = store.connect(config.db_path)
    assert store.count_events(conn) == 0
    conn.close()


# ---------------------------------------------------------------------------
# /api/timetable
# ---------------------------------------------------------------------------


def test_the_timetable_resolves_the_pattern_for_each_day(config, monkeypatch):
    session = client(config, monkeypatch)
    body = session.get(
        "/api/timetable",
        params={"from": MONDAY.isoformat(), "to": WEDNESDAY.isoformat()},
    ).json()

    assert [day["date"] for day in body["days"]] == [
        "2026-09-21", "2026-09-22", "2026-09-23"
    ]
    monday = body["days"][0]
    assert [part["subject"] for part in monday["sessions"][0]["parts"]] == ["Database"]


def test_a_cancelled_session_is_shown_crossed_out_rather_than_vanishing(
    config, monkeypatch
):
    """An unexplained difference is indistinguishable from a bug in the resolver."""
    conn = store.connect(config.db_path)
    store.add_adjustment(
        conn, applies_on=MONDAY.isoformat(), kind="cancelled", subject="Database",
        course_id=COURSE, session_start="08:30", reason="strike",
    )
    conn.commit()
    conn.close()

    session = client(config, monkeypatch)
    body = session.get(
        "/api/timetable", params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    ).json()

    assert body["days"][0]["sessions"] == []
    assert len(body["days"][0]["departed"]) == 1
    assert "cancelled" in body["days"][0]["departed"][0]["note"]


def test_an_orphan_is_always_returned_and_cannot_be_filtered_out(config, monkeypatch):
    """Never applied, never silently dropped, always printed."""
    conn = store.connect(config.db_path)
    store.add_adjustment(
        conn, applies_on=MONDAY.isoformat(), kind="cancelled",
        subject="Renamed Away", course_id=COURSE, session_start="08:30",
    )
    conn.commit()
    conn.close()

    session = client(config, monkeypatch)
    body = session.get("/api/timetable").json()

    assert len(body["orphans"]) == 1
    assert body["orphans"][0]["subject"] == "Renamed Away"
    assert body["orphans"][0]["why"]


def test_the_timetable_file_is_served_as_written(config, monkeypatch):
    """The file I edit, comments and ordering intact -- not a re-emitted parse."""
    session = client(config, monkeypatch)
    response = session.get("/api/timetable/file")

    assert response.status_code == 200
    assert "subjects:" in response.text
    assert response.text == config.timetable_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("method", ["put", "post", "patch", "delete"])
def test_writing_the_timetable_file_is_405_with_the_reason(config, monkeypatch, method):
    """Not 404. A 404 reads as "not built yet" and invites someone to build it."""
    session = client(config, monkeypatch)
    response = getattr(session, method)("/api/timetable/file")

    assert response.status_code == 405
    body = response.text
    assert "one writer" in body
    assert "/api/adjustments" in body


def test_an_unreadable_date_is_refused_by_name(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.get("/api/timetable", params={"from": "last tuesday"})

    assert response.status_code == 400
    assert "2026-09-21" in response.text


def test_a_span_longer_than_the_ceiling_is_refused(config, monkeypatch):
    session = client(config, monkeypatch)
    response = session.get(
        "/api/timetable",
        params={"from": "2026-01-01", "to": "2027-01-01"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# a broken timetable must not take everything down
# ---------------------------------------------------------------------------


def test_an_unreadable_timetable_is_503_with_what_to_run(tmp_path, monkeypatch):
    """A 503 rather than a 500: the service is fine and the file is not."""
    made = make_config(tmp_path, pattern="subjects: [this is not a mapping]\n")
    seed(made)
    session = client(made, monkeypatch)

    response = session.get("/api/now")
    assert response.status_code == 503
    assert "agent timetable --check" in response.text


def test_a_broken_timetable_does_not_break_the_deadline_screen(tmp_path, monkeypatch):
    """Matching `scope.load`: a broken file costs the scoping, not the answer."""
    made = make_config(tmp_path, pattern="subjects: [this is not a mapping]\n")
    seed(made)
    session = client(made, monkeypatch)

    assert session.get("/api/deadlines").status_code == 200
    assert session.get("/api/status").status_code == 200
    assert session.get("/api/health").status_code == 200
