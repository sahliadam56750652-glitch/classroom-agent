"""Homework: Classroom coursework and hand-entered tasks, in one list.

One function, `entries.homework`, behind both `agent homework` and
`GET /api/homework` -- so the terminal and the screen cannot disagree about what
is outstanding.
"""

from __future__ import annotations

import pytest
from api_fixtures import COURSE, MANUAL, client, make_config, seed

from agent import entries
from agent.db import store


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    return made


def coursework(conn, cw_id, *, title, due=None, state=None, late=0, deleted=None):
    conn.execute(
        "INSERT INTO coursework (id, course_id, title, due_at, alternate_link, "
        "content_hash, first_seen_at, deleted_at) VALUES (?, ?, ?, ?, ?, 'h', 'now', ?)",
        (cw_id, COURSE, title, due, f"https://classroom/{cw_id}", deleted),
    )
    if state is not None:
        conn.execute(
            "INSERT INTO submissions (id, course_id, coursework_id, state, late, "
            "alternate_link, content_hash, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, 'h', 'now')",
            (f"s-{cw_id}", COURSE, cw_id, state, late, f"https://classroom/s/{cw_id}"),
        )


def task(conn, title, *, due=None, done=None, course=MANUAL):
    conn.execute(
        "INSERT INTO manual_tasks (course_id, title, kind, due_at, done_at, created_at) "
        "VALUES (?, ?, 'exercise_sheet', ?, ?, 'now')",
        (course, title, due, done),
    )


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    coursework(connection, "cw1", title="Lab 2", due="2026-10-07T20:59:00Z", state="CREATED")
    coursework(connection, "cw2", title="Lab 1", due="2026-10-01T20:59:00Z", state="TURNED_IN")
    coursework(connection, "cw3", title="Quiz prep", state=None)
    coursework(connection, "cw4", title="Reclaimed", due="2026-10-09T20:59:00Z",
               state="RECLAIMED_BY_STUDENT")
    coursework(connection, "cw5", title="Removed", due="2026-10-05T20:59:00Z", deleted="x")
    task(connection, "TD 3", due="2026-10-05T21:59:00Z")
    task(connection, "TD 2", due="2026-09-28T21:59:00Z", done="2026-09-27T10:00:00Z")
    task(connection, "Read chapter 2")
    connection.commit()
    yield connection
    connection.close()


def titles(found):
    return [value.title for value in found]


def test_one_list_soonest_first_undated_last(conn):
    found = entries.homework(conn, [COURSE, MANUAL])
    assert titles(found) == ["TD 3", "Lab 2", "Reclaimed", "Quiz prep", "Read chapter 2"]


def test_handed_in_and_done_are_left_out_unless_asked(conn):
    found = entries.homework(conn, [COURSE, MANUAL], include_done=True)
    assert "Lab 1" in titles(found) and "TD 2" in titles(found)
    done = {value.title: value.done for value in found}
    assert done["Lab 1"] is True and done["TD 2"] is True and done["Lab 2"] is False


def test_reclaimed_work_is_outstanding_again(conn):
    found = {value.title: value for value in entries.homework(conn, [COURSE])}
    assert found["Reclaimed"].done is False
    assert found["Reclaimed"].submission_state == "RECLAIMED_BY_STUDENT"


def test_removed_coursework_is_not_mine_to_do(conn):
    assert "Removed" not in titles(entries.homework(conn, [COURSE, MANUAL]))


def test_tasks_come_whatever_the_course_list(conn):
    """A task exists because I typed it in; there is no allowlist for it."""
    assert "TD 3" in titles(entries.homework(conn, []))


def test_classroom_items_link_to_my_submission(conn):
    lab = next(v for v in entries.homework(conn, [COURSE]) if v.title == "Lab 2")
    assert lab.kind == "classroom"
    assert lab.link == "https://classroom/s/cw1"


def test_the_api_returns_the_same_list(config, monkeypatch, conn):
    session = client(config, monkeypatch)
    body = session.get("/api/homework").json()
    assert [row["title"] for row in body] == titles(entries.homework(conn, [COURSE, MANUAL]))
    # An instant, and nothing else about time.
    assert set(body[0]) >= {"due_at", "kind", "done", "link"}
    assert not any(key in body[0] for key in ("days_left", "severity", "urgent"))


def test_a_task_marked_done_leaves_the_list(config, monkeypatch, conn):
    session = client(config, monkeypatch)
    task_id = next(v for v in entries.homework(conn, []) if v.title == "TD 3").id
    assert session.post(f"/api/tasks/{task_id}/complete").status_code == 200
    assert "TD 3" not in [row["title"] for row in session.get("/api/homework").json()]


def test_the_command_prints_it(config, conn, capsys, monkeypatch):
    from agent import cli

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    assert cli.main(["homework"]) == 0
    out = capsys.readouterr().out
    assert "Lab 2" in out and "not handed in" in out
    assert "taken back, not handed in" in out
    assert "no date" in out
