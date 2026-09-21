"""`agent run`: the whole pipeline, and what happens when a stage fails.

The single property this file exists to protect: the briefing is the point of
the system, and nothing before it may prevent it from being sent. A Drive
outage, a spent LLM quota, a malformed PDF -- all of them cost their own stage
and none of them costs the deadline alert.

Nothing here reaches the network. Every stage is stubbed at the cli._do_* seam.
"""

from __future__ import annotations

import argparse

import pytest

from datetime import date

from agent import cli, scope
from agent.classroom.models import Course, Material, material_id
from agent.gate import scheduler, timetable as tt
from agent.config import Config
from agent.db import store
from agent.files import drive, extract, ocr, packs
from agent.sync import deadlines as deadlines_mod
from agent.sync import poller


@pytest.fixture
def config(tmp_path) -> Config:
    data_dir = tmp_path / "data"
    (data_dir / "library").mkdir(parents=True)
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=data_dir,
        tracked_courses=["c1"],
        ignored_courses=[],
        telegram_chat_id=4242,
    )


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    store.upsert_course(
        connection,
        Course(
            id="c1", name="Operating Systems", section=None, room=None, owner_id=None,
            course_state="ACTIVE", enrollment_code=None, alternate_link=None,
            creation_time=None, update_time=None, content_hash="h",
        ),
    )
    yield connection
    connection.close()


class KeepOpen:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


@pytest.fixture
def pipeline(monkeypatch, conn):
    """Every stage stubbed to succeed, recording the order they ran in.

    Individual tests replace one entry with a failure, which is the only thing
    that differs between them.
    """
    order: list[str] = []
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))

    def record(name, value):
        def stub(*a, **kw):
            order.append(name)
            return value
        return stub

    monkeypatch.setattr(cli, "_do_sync", record("sync", poller.SyncResult()))
    monkeypatch.setattr(cli, "_do_fetch", record("fetch", drive.FetchResult()))
    monkeypatch.setattr(cli, "_do_extract", record("extract", extract.ExtractResult()))
    monkeypatch.setattr(cli, "_do_ocr", record("ocr", ocr.OCRResult()))
    monkeypatch.setattr(cli, "_do_studyitems", record("studyitems", (0, 0)))
    monkeypatch.setattr(cli, "_do_packs", record("packs", packs.PacksResult()))
    monkeypatch.setattr(
        cli, "_do_deadlines", record("deadlines", deadlines_mod.DeadlineScan(events=[]))
    )

    def notify(*a, **kw):
        order.append("notify")
        return 0

    monkeypatch.setattr(cli, "_do_notify", notify)
    return order


def args(**overrides):
    base = {"dry_run": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def fails(name, error):
    def stub(*a, **kw):
        raise error
    return stub


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------


def test_the_stages_run_in_dependency_order(config, pipeline):
    """Each feeds the next: bytes before text, text before the pages OCR reads."""
    assert cli.cmd_run(config, args()) == 0

    assert pipeline == [
        "sync", "fetch", "extract", "ocr", "studyitems", "packs", "deadlines",
        "notify",
    ]


@pytest.mark.parametrize(
    "stage,error",
    [
        ("_do_fetch", drive.DriveError("Drive is down")),
        ("_do_extract", extract.ExtractError("PyMuPDF exploded")),
        ("_do_ocr", ocr.OCRError("no provider")),
        ("_do_studyitems", RuntimeError("timetable unreadable")),
        ("_do_packs", packs.PackError("disk full")),
        ("_do_deadlines", RuntimeError("scanner broke")),
    ],
)
def test_no_failing_stage_prevents_the_briefing(config, pipeline, monkeypatch, stage, error):
    """The whole reason `agent run` is safe to schedule."""
    monkeypatch.setattr(cli, stage, fails(stage, error))

    assert cli.cmd_run(config, args()) == 0
    assert "notify" in pipeline


def test_an_unexpected_exception_is_also_survived(config, pipeline, monkeypatch):
    """Tonight it was a socket timeout. It will be something else next time."""
    monkeypatch.setattr(cli, "_do_fetch", fails("fetch", TimeoutError("read timed out")))

    assert cli.cmd_run(config, args()) == 0
    assert "notify" in pipeline


def test_a_failing_stage_does_not_stop_the_ones_after_it(config, pipeline, monkeypatch):
    monkeypatch.setattr(cli, "_do_fetch", fails("fetch", drive.DriveError("down")))

    cli.cmd_run(config, args())

    assert pipeline == [
        "sync", "extract", "ocr", "studyitems", "packs", "deadlines", "notify",
    ]


def test_a_failing_stage_says_so_loudly(config, pipeline, monkeypatch, capsys):
    """A failure that produces no visible output is the worst failure here."""
    monkeypatch.setattr(cli, "_do_fetch", fails("fetch", drive.DriveError("Drive is down")))

    cli.cmd_run(config, args())

    captured = capsys.readouterr()
    assert "fetch failed" in captured.err
    assert "Drive is down" in captured.err


def test_even_a_failing_sync_still_sends_the_briefing(config, pipeline, monkeypatch):
    """Yesterday's unnotified events and an approaching deadline are still news."""
    monkeypatch.setattr(cli, "_do_sync", fails("sync", RuntimeError("Classroom 500")))

    assert cli.cmd_run(config, args()) == 0
    assert pipeline[-1] == "notify"


def test_no_tracked_courses_no_longer_ends_the_run(config, pipeline, monkeypatch):
    """Nothing tracked is not nothing to do -- not since Phase 6.

    A subject can have a library, a backlog and a deadline without Classroom
    knowing it exists, and at the start of a term, before any course is joined,
    that is the NORMAL state rather than an edge case. Stopping here would mean
    an uploaded board is never extracted, never gated, and no briefing is sent
    about an exercise sheet due tomorrow.
    """
    monkeypatch.setattr(cli, "_do_sync", lambda *a, **kw: None)

    assert cli.cmd_run(config, args()) == 0
    assert pipeline == [
        "fetch", "extract", "ocr", "studyitems", "packs", "deadlines", "notify",
    ]


def test_no_tracked_courses_still_says_so(config, pipeline, monkeypatch, capsys):
    """Continuing must not make the advice disappear: a first-run install with
    an empty tracked list still needs to be told how to fill it."""
    monkeypatch.setattr(cli, "_do_sync", lambda *a, **kw: None)

    cli.cmd_run(config, args())
    out = capsys.readouterr().out
    assert "No courses are tracked" in out
    assert "Continuing" in out


# --------------------------------------------------------------------------
# bounding the OCR stage
# --------------------------------------------------------------------------


def test_ocr_is_bounded_by_the_configured_limit(config, pipeline, monkeypatch):
    """A scheduled run must not swallow the whole daily allowance in one go."""
    seen = {}

    def capture(config, conn, **kwargs):
        seen.update(kwargs)
        return ocr.OCRResult()

    monkeypatch.setattr(cli, "_do_ocr", capture)

    cli.cmd_run(config, args())

    assert seen["limit"] == config.ocr_run_limit
    assert seen["limit"] > 0


def test_the_default_limit_leaves_room_for_a_second_run(config):
    """Free tier is about 20 a day and the scheduler fires twice."""
    assert 0 < Config.ocr_run_limit <= 10


def test_a_zero_limit_skips_ocr_entirely(tmp_path, pipeline, monkeypatch, conn, capsys):
    data_dir = tmp_path / "data"
    (data_dir / "library").mkdir(parents=True, exist_ok=True)
    off = Config(
        account="someone@example.com", timezone="Africa/Tunis", data_dir=data_dir,
        tracked_courses=["c1"], ignored_courses=[], ocr_run_limit=0,
    )

    cli.cmd_run(off, args())

    assert "ocr" not in pipeline
    assert "skipped" in capsys.readouterr().out
    assert "notify" in pipeline


def test_the_run_limit_is_configurable(tmp_path):
    from agent.config import load_config

    (tmp_path / "config.yaml").write_text(
        "account: someone@example.com\n"
        "timezone: Africa/Tunis\n"
        f"data_dir: {(tmp_path / 'data').as_posix()}\n"
        "courses:\n  tracked: ['c1']\n  ignored: []\n"
        "ocr:\n  run_limit: 3\n",
        encoding="utf-8",
    )

    assert load_config(tmp_path / "config.yaml").ocr_run_limit == 3


# --------------------------------------------------------------------------
# the studyitems stage
#
# Settled after Phase 6: new material that reaches the gate only if I remember
# a command is a silent failure, and this project treats those as defects. The
# stage is narrower than the command on purpose -- strictly in scope -- because
# it runs unattended twice a day and must never widen the backlog on its own.
# --------------------------------------------------------------------------

REAL_TIMETABLE = """
subjects:
  Calculus III: manual-calculus-iii
  Operating Systems: "c1"

versions:
  - label: S1
    status: provisional
    effective_from: 2026-09-15
    effective_to: 2027-01-23
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC,
          subject: Calculus III }
      - { day: tue, start: "13:00", end: "14:30", kind: LEC,
          subject: Operating Systems }
"""


@pytest.fixture
def real_studyitems(config, conn, monkeypatch, tmp_path):
    """Every stage stubbed EXCEPT studyitems, which runs for real.

    The stages around it need a network or a model; this one needs neither, so
    it is the one worth exercising end to end.
    """
    path = tmp_path / "timetable.yaml"
    path.write_text(REAL_TIMETABLE, encoding="utf-8")
    scoped = Config(
        account=config.account,
        timezone=config.timezone,
        data_dir=config.data_dir,
        tracked_courses=config.tracked_courses,
        ignored_courses=config.ignored_courses,
        telegram_chat_id=config.telegram_chat_id,
        timetable_path_override=path,
    )

    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    for name, value in (
        ("_do_sync", poller.SyncResult()),
        ("_do_fetch", drive.FetchResult()),
        ("_do_extract", extract.ExtractResult()),
        ("_do_ocr", ocr.OCRResult()),
        ("_do_packs", packs.PacksResult()),
        ("_do_deadlines", deadlines_mod.DeadlineScan(events=[])),
    ):
        monkeypatch.setattr(cli, name, lambda *a, _v=value, **kw: _v)
    monkeypatch.setattr(cli, "_do_notify", lambda *a, **kw: 0)
    return scoped


def a_post_with_material(conn, *, course_id, post_id, drive_id, title, posted):
    """A post whose attachment has extracted text -- what `agent studyitems`
    looks for, and all it looks for."""
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, content_hash, "
        "first_seen_at, creation_time) VALUES (?, ?, ?, 'h', ?, ?)",
        (post_id, course_id, title, posted, posted),
    )
    store.upsert_material(conn, Material(
        id=material_id("coursework_material", post_id, "driveFile", drive_id),
        parent_type="coursework_material", parent_id=post_id, course_id=course_id,
        kind="driveFile", ref=drive_id, drive_id=drive_id, title=title,
        url=None, content_hash="h",
    ))
    store.upsert_extraction(
        conn, drive_id, status="ok", mime_type="image/jpeg",
        local_path=f"files/{drive_id}.jpg", text_path=f"text/{drive_id}.txt",
        method="image", pages=1, scan_pages=1, chars=0,
    )
    conn.commit()


def test_an_upload_reaches_the_gate_through_agent_run_alone(real_studyitems, conn):
    """The reason this stage exists.

    A board photographed on Tuesday is extracted and transcribed unattended by
    the 19:30 run. Before this stage it then sat there: no study item, so
    nothing for the gate to serve, until `agent studyitems` was typed by hand.
    New material that reaches the gate only if I remember a command is a silent
    failure.
    """
    store.ensure_manual_course(conn, "manual-calculus-iii", "Calculus III")
    a_post_with_material(
        conn, course_id="manual-calculus-iii", post_id="manual-post-abc",
        drive_id="manual-file-abc", title="Board, 21 Sep",
        posted="2026-09-21T12:00:00Z",
    )

    assert cli.cmd_run(real_studyitems, args()) == 0

    # The item exists, unprompted, and it is pending rather than skipped.
    row = conn.execute(
        "SELECT state FROM study_items WHERE course_id = 'manual-calculus-iii'"
    ).fetchone()
    assert row is not None, "the run did not create a study item for the upload"
    assert row["state"] == "pending"

    # And the gate serves it: Monday 21 Sep 2026 is a Calculus III lecture.
    table = tt.load(real_studyitems.timetable_path)
    plan = scheduler.plan_for(
        conn, scope.local(real_studyitems, table), table, date(2026, 9, 21)
    )
    calculus = next(s for s in plan.subjects if s.name == "Calculus III")
    assert calculus.gated
    assert [item.label for item in calculus.items] == ["Board, 21 Sep"]
    assert plan.worth_sending


def test_archived_material_creates_nothing(real_studyitems, conn):
    """The stage is strictly in scope, and this is what that buys.

    An archived or ignored course still has material in the library from
    earlier phases. Turning it into pending items twice a day would make the
    gate claim a backlog I do not have, and Phase 4's coverage figure a lie.
    """
    store.upsert_course(conn, Course(
        id="archived", name="Probability & Statistics 2025-26", section=None,
        room=None, owner_id=None, course_state="ARCHIVED", enrollment_code=None,
        alternate_link=None, creation_time=None, update_time=None, content_hash="h",
    ))
    a_post_with_material(
        conn, course_id="archived", post_id="arch1", drive_id="1ArchiveDriveId",
        title="Chapter 9", posted="2026-05-01T10:00:00Z",
    )

    assert cli.cmd_run(real_studyitems, args()) == 0
    assert store.count_rows(conn, "study_items") == 0


def test_a_tracked_course_off_the_timetable_creates_nothing_either(
    real_studyitems, conn
):
    """`courses.tracked` is a fetching decision, not a revising one.

    A course I track to pull its files from, but which does not meet this
    semester, is not something the gate should start asking about. That is the
    difference between the stage's `in scope` and the command's `local`, and it
    is the whole reason the stage takes a narrower set.
    """
    store.upsert_course(conn, Course(
        id="c2", name="Tracked but not taught", section=None, room=None,
        owner_id=None, course_state="ACTIVE", enrollment_code=None,
        alternate_link=None, creation_time=None, update_time=None, content_hash="h",
    ))
    real_studyitems.tracked_courses.append("c2")
    a_post_with_material(
        conn, course_id="c2", post_id="p2", drive_id="1OtherDriveId",
        title="Slides", posted="2026-09-20T10:00:00Z",
    )

    assert cli.cmd_run(real_studyitems, args()) == 0
    assert store.count_rows(conn, "study_items") == 0

    # But typing the command by hand still reaches it -- that is a deliberate
    # act, and it is the difference the two callers exist for.
    created, _seen = cli._do_studyitems(real_studyitems, conn)
    assert created == 1


def test_the_stage_is_idempotent(real_studyitems, conn):
    """It runs twice a day forever. `ensure_study_item` is INSERT .. DO NOTHING,
    so a second run is silent rather than duplicating."""
    store.ensure_manual_course(conn, "manual-calculus-iii", "Calculus III")
    a_post_with_material(
        conn, course_id="manual-calculus-iii", post_id="manual-post-abc",
        drive_id="manual-file-abc", title="Board", posted="2026-09-21T12:00:00Z",
    )

    cli.cmd_run(real_studyitems, args())
    cli.cmd_run(real_studyitems, args())
    assert store.count_rows(conn, "study_items") == 1


def test_the_stage_never_seeds(real_studyitems, conn):
    """--seed records everything as already skipped. It is a first-run tool and
    an unattended pipeline must never reach for it."""
    store.ensure_manual_course(conn, "manual-calculus-iii", "Calculus III")
    a_post_with_material(
        conn, course_id="manual-calculus-iii", post_id="manual-post-abc",
        drive_id="manual-file-abc", title="Board", posted="2026-09-21T12:00:00Z",
    )

    cli.cmd_run(real_studyitems, args())
    row = conn.execute("SELECT state, skip_source FROM study_items").fetchone()
    assert (row["state"], row["skip_source"]) == ("pending", None)


def test_a_dry_run_creates_nothing(real_studyitems, conn, capsys):
    store.ensure_manual_course(conn, "manual-calculus-iii", "Calculus III")
    a_post_with_material(
        conn, course_id="manual-calculus-iii", post_id="manual-post-abc",
        drive_id="manual-file-abc", title="Board", posted="2026-09-21T12:00:00Z",
    )

    cli.cmd_run(real_studyitems, args(dry_run=True))
    assert store.count_rows(conn, "study_items") == 0
    assert "would create 1 study item(s)" in capsys.readouterr().out


def test_an_unreadable_timetable_creates_nothing_rather_than_everything(
    real_studyitems, conn, capsys
):
    """The stage fails SAFE. `scope.in_scope(None)` is empty, so a broken file
    costs the stage rather than filing the whole library under the wrong set."""
    real_studyitems.timetable_path.write_text("versions: [", encoding="utf-8")
    store.ensure_manual_course(conn, "manual-calculus-iii", "Calculus III")
    a_post_with_material(
        conn, course_id="manual-calculus-iii", post_id="manual-post-abc",
        drive_id="manual-file-abc", title="Board", posted="2026-09-21T12:00:00Z",
    )

    assert cli.cmd_run(real_studyitems, args()) == 0
    assert store.count_rows(conn, "study_items") == 0
    assert "created 0 study item(s)" in capsys.readouterr().out
