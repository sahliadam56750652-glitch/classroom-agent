"""Projects: milestones, and the one deadline they share with Classroom.

Two things here are design rather than implementation, and both are tested as
such.

**Progress is milestone-based and never a self-reported percentage.** A
percentage is a feeling typed into a box and I would quietly revise it upward;
DESIGN.md's closing constraint is that nothing may make honesty cost me
anything. There is no percentage column to write, and no output prints one.

**A project that also appears as an assignment has ONE deadline, not two.**
Three project deadlines landed on one day last year with none of them ready --
and the fix for that must not be two alerts per hand-in, which is the fastest
way back to muting the bot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from agent import cli
from agent.classroom.models import parse_course
from agent.config import Config
from agent.db import store
from agent.sync import deadlines

MANUAL = "manual-software-design"
UNIX = "809686876412"

TIMETABLE = """
subjects:
  UNIX: "809686876412"
  Software Design: manual-software-design

versions:
  - label: S1
    status: provisional
    effective_from: 2026-09-15
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC, subject: UNIX }
      - { day: thu, start: "12:15", end: "13:45", kind: Project,
          subject: Software Design }
"""

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class KeepOpen:
    def __init__(self, connection):
        self._conn = connection

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


@pytest.fixture
def config(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "library").mkdir(parents=True)
    path = tmp_path / "timetable.yaml"
    path.write_text(TIMETABLE, encoding="utf-8")
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=data_dir,
        tracked_courses=[UNIX],
        ignored_courses=[],
        timetable_path_override=path,
    )


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    store.upsert_course(connection, parse_course({"id": UNIX, "name": "UNIX"}))
    store.ensure_manual_course(connection, MANUAL, "Software Design")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def wired(conn, monkeypatch):
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    return conn


def a_project(conn, *, due_in_hours=60, course=MANUAL, coursework_id=None,
              title="Compiler front end"):
    return store.add_project(
        conn,
        course_id=course,
        title=title,
        deadline_at=stamp(NOW + timedelta(hours=due_in_hours)),
        coursework_id=coursework_id,
    )


def a_coursework(conn, *, work_id="w1", due_in_hours=60, title="TP Compilateur",
                 state=None):
    conn.execute(
        "INSERT INTO coursework (id, course_id, title, due_at, content_hash, "
        "first_seen_at) VALUES (?, ?, ?, ?, 'h', ?)",
        (work_id, UNIX, title,
         stamp(NOW + timedelta(hours=due_in_hours)) if due_in_hours is not None else None,
         stamp(NOW)),
    )
    if state is not None:
        conn.execute(
            "INSERT INTO submissions (id, course_id, coursework_id, state, "
            "content_hash, first_seen_at) VALUES ('s1', ?, ?, ?, 'h', ?)",
            (UNIX, work_id, state, stamp(NOW)),
        )
    conn.commit()
    return work_id


def scan(conn):
    return deadlines.scan(conn, [UNIX], now=NOW)


# --------------------------------------------------------------------------
# the deadline -- exactly as a Classroom assignment's
# --------------------------------------------------------------------------

def test_a_project_alerts_at_t72_t24_and_t3(conn):
    """The stage's stated done-when."""
    a_project(conn, due_in_hours=80)
    conn.commit()

    fired = []
    for hours_left, expected in ((71, "deadline_t72"), (23, "deadline_t24"),
                                 (2, "deadline_t3")):
        moment = NOW + timedelta(hours=80 - hours_left)
        result = deadlines.scan(conn, [UNIX], now=moment)
        fired += [event.type for event in result.events]
        for event in result.events:
            store.insert_event(conn, event)
        for event in result.suppressed:
            store.insert_event(conn, event, notified_at=stamp(moment))
        conn.commit()

    assert fired == ["deadline_t72", "deadline_t24", "deadline_t3"]


def test_a_project_alert_names_the_project_and_its_kind(conn):
    a_project(conn, due_in_hours=20)
    conn.commit()
    event = scan(conn).events[0]
    assert event.entity_type == "project"
    assert event.payload["title"] == "Compiler front end"
    assert event.payload["kind"] == "project"
    assert event.course_id == MANUAL


def test_a_second_scan_says_nothing(conn):
    a_project(conn, due_in_hours=60)
    conn.commit()
    result = scan(conn)
    for event in result.events:
        store.insert_event(conn, event)
    conn.commit()
    assert scan(conn).events == []


def test_a_closed_project_is_no_longer_chased(conn):
    project_id = a_project(conn, due_in_hours=60)
    store.close_project(conn, project_id)
    conn.commit()
    assert scan(conn).events == []


def test_a_project_with_no_deadline_alerts_on_nothing(conn):
    store.add_project(conn, course_id=MANUAL, title="Open ended")
    conn.commit()
    assert scan(conn).events == []


# --------------------------------------------------------------------------
# ONE deadline, not two
# --------------------------------------------------------------------------

def test_a_linked_project_produces_one_event_carrying_the_project_name(conn):
    """Two candidates would mean two alerts at every threshold for one hand-in,
    which is the fastest way back to muting the bot."""
    a_coursework(conn, due_in_hours=60)
    a_project(conn, due_in_hours=60, coursework_id="w1")
    conn.commit()

    events = scan(conn).events
    assert len(events) == 1
    event = events[0]
    # Keyed on the coursework, so events written months before the project was
    # recorded keep deduplicating it.
    assert (event.entity_type, event.entity_id) == ("coursework", "w1")
    # But named for the work as I call it.
    assert event.payload["title"] == "Compiler front end"
    assert event.payload["kind"] == "project"


def test_the_professors_due_date_wins_over_the_one_i_typed(conn):
    """The posted date is the fact; the one I typed is a memory of a verbal
    brief."""
    a_coursework(conn, due_in_hours=20)
    a_project(conn, due_in_hours=60, coursework_id="w1")
    conn.commit()

    events = scan(conn).events
    assert len(events) == 1
    assert events[0].payload["due_at"] == stamp(NOW + timedelta(hours=20))
    assert events[0].payload["hours_before"] == 24


def test_a_linked_assignment_with_no_due_date_uses_the_projects_own(conn):
    """54% of measured coursework carries no dueDate at all, and that is the
    common case for a project -- it is the reason the link is worth having."""
    a_coursework(conn, due_in_hours=None)
    a_project(conn, due_in_hours=60, coursework_id="w1")
    conn.commit()

    events = scan(conn).events
    assert len(events) == 1
    assert events[0].payload["due_at"] == stamp(NOW + timedelta(hours=60))
    assert events[0].payload["title"] == "Compiler front end"


def test_handing_the_assignment_in_silences_the_project_too(conn):
    a_coursework(conn, due_in_hours=60, state="TURNED_IN")
    a_project(conn, due_in_hours=60, coursework_id="w1")
    conn.commit()
    assert scan(conn).events == []


def test_a_link_to_an_assignment_the_sync_has_not_seen_keeps_its_own_deadline(conn):
    """The brief usually arrives weeks before the post does."""
    a_project(conn, due_in_hours=60, coursework_id="not-synced-yet")
    conn.commit()

    events = scan(conn).events
    assert len(events) == 1
    assert events[0].entity_type == "project"


def test_an_unlinked_project_and_an_unrelated_assignment_both_alert(conn):
    a_coursework(conn, due_in_hours=60, title="TP 2")
    a_project(conn, due_in_hours=60)
    conn.commit()

    events = scan(conn).events
    assert sorted(event.entity_type for event in events) == ["coursework", "project"]


def test_a_closed_linked_project_leaves_the_assignment_alone(conn):
    """Closing my own record is not a statement about the professor's."""
    a_coursework(conn, due_in_hours=60, title="TP Compilateur")
    project_id = a_project(conn, due_in_hours=60, coursework_id="w1")
    store.close_project(conn, project_id)
    conn.commit()

    events = scan(conn).events
    assert len(events) == 1
    assert events[0].payload["title"] == "TP Compilateur"


# --------------------------------------------------------------------------
# milestones, and the number that is never printed
# --------------------------------------------------------------------------

def test_progress_is_counted_from_milestones_and_stored_nowhere(conn):
    project_id = a_project(conn)
    for name in ("Lexer", "Parser", "Type checker"):
        store.add_milestone(conn, project_id, name)
    conn.commit()

    row = store.projects(conn)[0]
    assert (row["milestones"], row["milestones_done"]) == (3, 0)

    first = store.project_milestones(conn, project_id)[0]
    assert store.complete_milestone(conn, first["id"]) is True
    conn.commit()
    assert store.projects(conn)[0]["milestones_done"] == 1

    # There is no column that could hold a number I revised upward.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    assert not columns & {"progress", "percent", "percentage", "complete"}


def test_a_milestone_cannot_be_completed_twice(conn):
    project_id = a_project(conn)
    milestone_id = store.add_milestone(conn, project_id, "Lexer")
    conn.commit()
    assert store.complete_milestone(conn, milestone_id) is True
    assert store.complete_milestone(conn, milestone_id) is False


def test_milestones_keep_the_order_they_were_given(conn):
    project_id = a_project(conn)
    for name in ("Lexer", "Parser", "Type checker"):
        store.add_milestone(conn, project_id, name)
    conn.commit()
    assert [row["title"] for row in store.project_milestones(conn, project_id)] == [
        "Lexer", "Parser", "Type checker",
    ]


def test_deleting_a_project_takes_its_milestones_with_it(conn):
    project_id = a_project(conn)
    store.add_milestone(conn, project_id, "Lexer")
    conn.commit()
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    assert store.project_milestones(conn, project_id) == []


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def project_args(**kwargs):
    return argparse.Namespace(**{
        "add": False, "subject": None, "title": None, "deadline": None,
        "deliverables": None, "milestones": None, "team": None, "brief": None,
        "coursework": None, "show": None, "done": None, "close": None,
        "include_closed": False, "dry_run": False, **kwargs,
    })


def test_adding_a_project_records_everything_the_brief_had(config, wired, capsys):
    assert cli.cmd_projects(config, project_args(
        add=True, subject="Software Design", title="Compiler front end",
        deadline="2026-12-15", deliverables=["Report", "Demo"],
        milestones=["Lexer", "Parser"], team=["me", "Sami"],
        brief="verbal, 15 Sep lecture", coursework="w1",
    )) == 0
    assert "recorded as project" in capsys.readouterr().out

    row = store.projects(wired)[0]
    assert row["title"] == "Compiler front end"
    assert row["deliverables"] == "Report\nDemo"
    assert row["team"] == "me\nSami"
    assert row["brief_source"] == "verbal, 15 Sep lecture"
    assert row["coursework_id"] == "w1"
    assert (row["milestones"], row["milestones_done"]) == (2, 0)


def test_the_listing_shows_a_count_and_never_a_percentage(config, wired, capsys):
    cli.cmd_projects(config, project_args(
        add=True, subject="Software Design", title="Compiler front end",
        deadline="2026-12-15", milestones=["Lexer", "Parser", "Types"]))
    milestone = store.project_milestones(wired, store.projects(wired)[0]["id"])[0]
    cli.cmd_projects(config, project_args(done=milestone["id"]))
    capsys.readouterr()

    assert cli.cmd_projects(config, project_args()) == 0
    out = capsys.readouterr().out
    assert "1 of 3" in out
    assert "%" not in out
    assert "33" not in out


def test_show_lists_the_milestones_with_what_is_done(config, wired, capsys):
    cli.cmd_projects(config, project_args(
        add=True, subject="Software Design", title="Compiler front end",
        deadline="2026-12-15", deliverables=["Report"],
        milestones=["Lexer", "Parser"]))
    project_id = store.projects(wired)[0]["id"]
    milestone = store.project_milestones(wired, project_id)[0]
    cli.cmd_projects(config, project_args(done=milestone["id"]))
    capsys.readouterr()

    assert cli.cmd_projects(config, project_args(show=project_id)) == 0
    out = capsys.readouterr().out
    assert "milestones -- 1 of 2" in out
    assert "[x]" in out and "[ ]" in out
    assert "Report" in out
    assert "%" not in out


def test_a_project_with_no_milestones_says_it_has_nothing_to_report(
    config, wired, capsys
):
    """Rather than rendering as 0% or as complete -- the same rule as a subject
    with no readable material."""
    cli.cmd_projects(config, project_args(
        add=True, subject="Software Design", title="Open ended"))
    project_id = store.projects(wired)[0]["id"]
    capsys.readouterr()

    cli.cmd_projects(config, project_args(show=project_id))
    out = capsys.readouterr().out
    assert "no milestones set" in out
    assert "0%" not in out


def test_adding_the_same_project_twice_is_refused(config, wired, capsys):
    same = dict(add=True, subject="Software Design", title="Compiler front end")
    cli.cmd_projects(config, project_args(**same))
    capsys.readouterr()
    assert cli.cmd_projects(config, project_args(**same)) == 0
    assert "already has a project called" in capsys.readouterr().out
    assert len(store.projects(wired)) == 1


def test_a_project_with_no_deadline_says_it_will_not_be_alerted_on(
    config, wired, capsys
):
    cli.cmd_projects(config, project_args(
        add=True, subject="Software Design", title="Open ended"))
    assert "the scanner will never alert" in capsys.readouterr().out


def test_marking_a_milestone_done_twice_says_which_happened(config, wired, capsys):
    cli.cmd_projects(config, project_args(
        add=True, subject="Software Design", title="P", milestones=["Lexer"]))
    milestone = store.project_milestones(wired, store.projects(wired)[0]["id"])[0]
    capsys.readouterr()

    assert cli.cmd_projects(config, project_args(done=milestone["id"])) == 0
    assert "done: Lexer" in capsys.readouterr().out
    assert cli.cmd_projects(config, project_args(done=milestone["id"])) == 0
    assert "was already done" in capsys.readouterr().out


def test_an_unknown_milestone_says_where_to_find_the_ids(config, wired, capsys):
    assert cli.cmd_projects(config, project_args(done=999)) == 1
    assert "agent projects --show" in capsys.readouterr().err


def test_closing_a_project_is_reported_and_idempotent(config, wired, capsys):
    cli.cmd_projects(config, project_args(
        add=True, subject="Software Design", title="P", deadline="2026-12-15"))
    project_id = store.projects(wired)[0]["id"]
    capsys.readouterr()

    assert cli.cmd_projects(config, project_args(close=project_id)) == 0
    assert "no longer chased" in capsys.readouterr().out
    assert cli.cmd_projects(config, project_args(close=project_id)) == 0
    assert "was already closed" in capsys.readouterr().out
    assert store.projects(wired) == []


def test_dry_run_writes_nothing(config, wired, capsys):
    cli.cmd_projects(config, project_args(
        add=True, subject="Software Design", title="P",
        deadline="2026-12-15", milestones=["Lexer"], dry_run=True))
    assert "dry run -- nothing written" in capsys.readouterr().out
    assert store.projects(wired) == []


def test_add_without_a_subject_is_refused(config, wired, capsys):
    assert cli.cmd_projects(config, project_args(add=True, title="P")) == 1
    assert "needs --subject" in capsys.readouterr().err


# --------------------------------------------------------------------------
# and a sync does nothing to any of it
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, courses):
        self.courses = courses

    def _for(self, course_id, key):
        return list(self.courses.get(course_id, {}).get(key, []))

    def list_coursework(self, course_id):
        return self._for(course_id, "coursework")

    def list_coursework_materials(self, course_id):
        return self._for(course_id, "coursework_materials")

    def list_announcements(self, course_id):
        return self._for(course_id, "announcements")

    def list_submissions(self, course_id):
        return self._for(course_id, "submissions")


def test_projects_and_milestones_survive_a_full_sync(conn):
    from agent.sync.poller import sync

    project_id = a_project(conn, coursework_id="w1")
    store.add_milestone(conn, project_id, "Lexer")
    # One against a TRACKED subject too, which is where the reconciler runs.
    a_project(conn, course=UNIX, title="Shell project")
    conn.commit()

    class FakeConfig:
        tracked_courses = [UNIX]
        ignored_courses: list[str] = []

    sync(FakeConfig(), conn, client=FakeClient({UNIX: {}}))
    sync(FakeConfig(), conn, client=FakeClient({UNIX: {}}))

    assert len(store.projects(conn)) == 2
    assert len(store.project_milestones(conn, project_id)) == 1
