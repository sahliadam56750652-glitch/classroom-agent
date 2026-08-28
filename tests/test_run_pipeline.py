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

from agent import cli
from agent.classroom.models import Course
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
        "sync", "fetch", "extract", "ocr", "packs", "deadlines", "notify",
    ]


@pytest.mark.parametrize(
    "stage,error",
    [
        ("_do_fetch", drive.DriveError("Drive is down")),
        ("_do_extract", extract.ExtractError("PyMuPDF exploded")),
        ("_do_ocr", ocr.OCRError("no provider")),
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

    assert pipeline == ["sync", "extract", "ocr", "packs", "deadlines", "notify"]


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


def test_no_tracked_courses_ends_the_run_early(config, pipeline, monkeypatch):
    """Nothing downstream has anything to work on, and that is not a failure."""
    monkeypatch.setattr(cli, "_do_sync", lambda *a, **kw: None)

    assert cli.cmd_run(config, args()) == 0
    assert pipeline == []


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
