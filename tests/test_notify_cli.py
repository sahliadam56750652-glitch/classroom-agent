"""The notify command: dry runs stamp nothing, and `run` chains the stages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import cli
from agent.classroom.models import parse_course, parse_coursework
from agent.config import Config
from agent.db import store
from agent.notify.telegram import Telegram, _ApiError
from agent.sync import poller
from agent.sync.differ import Event


@pytest.fixture
def config(tmp_path) -> Config:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
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
    store.upsert_course(connection, parse_course({"id": "c1", "name": "Databases"}))
    connection.commit()
    yield connection
    connection.close()


class StubTransport:
    def __init__(self, failures=None):
        self.calls: list[dict] = []
        self.failures = failures or {}

    def __call__(self, url, payload):
        index = len(self.calls)
        self.calls.append(payload)
        if index in self.failures:
            raise self.failures[index]
        return {"ok": True, "result": {"message_id": index}}


class KeepOpen:
    """A connection whose close() does nothing.

    cmd_run closes the connection it was handed, which would leave the test
    unable to inspect the database afterwards. sqlite3.Connection.close is
    read-only and cannot be patched, so it gets wrapped instead.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def stub_client(monkeypatch, transport):
    """Replace the real client factory so no test can reach Telegram."""
    client = Telegram("t", 1, transport=transport, sleep=lambda _: None)
    monkeypatch.setattr(cli.telegram_api, "from_config", lambda config, **kw: client)
    return client


def add_event(conn, entity_id="m1", title="Lecture 7"):
    store.insert_event(
        conn,
        Event(
            type="new_material",
            entity_type="coursework_material",
            entity_id=entity_id,
            course_id="c1",
            payload={"title": title},
            created_at=f"2026-08-27T12:00:00Z",
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------

def test_dry_run_prints_the_digest(config, conn, capsys):
    add_event(conn)

    assert cli._do_notify(config, conn, dry_run=True) == 0

    out = capsys.readouterr().out
    assert "<b>Databases</b>" in out
    assert "Lecture 7" in out
    assert "dry run" in out


def test_dry_run_stamps_nothing(config, conn):
    add_event(conn)

    cli._do_notify(config, conn, dry_run=True)

    assert store.count_pending_events(conn) == 1


def test_dry_run_sends_nothing(config, conn, monkeypatch):
    add_event(conn)
    transport = StubTransport()
    stub_client(monkeypatch, transport)

    cli._do_notify(config, conn, dry_run=True)

    assert transport.calls == []


def test_dry_run_needs_no_bot_token(config, conn, monkeypatch):
    """A dry run must work on a machine that never configured a bot."""
    add_event(conn)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("a dry run must not build a Telegram client")

    monkeypatch.setattr(cli.telegram_api, "from_config", explode)

    assert cli._do_notify(config, conn, dry_run=True) == 0


# --------------------------------------------------------------------------
# real delivery
# --------------------------------------------------------------------------

def test_a_successful_notify_stamps_and_reports(config, conn, monkeypatch, capsys):
    add_event(conn)
    transport = StubTransport()
    stub_client(monkeypatch, transport)

    assert cli._do_notify(config, conn) == 0

    assert len(transport.calls) == 1
    assert store.count_pending_events(conn) == 0
    assert "sent 1 message" in capsys.readouterr().out


def test_a_failed_notify_exits_nonzero_and_says_so(config, conn, monkeypatch, capsys):
    add_event(conn)
    transport = StubTransport({0: _ApiError("bad request", status=400, retry_after=None)})
    stub_client(monkeypatch, transport)

    assert cli._do_notify(config, conn) == 1

    captured = capsys.readouterr()
    assert "send failed" in captured.err
    assert "retry" in captured.err
    # The whole point: nothing was stamped, so nothing was lost.
    assert store.count_pending_events(conn) == 1


def test_nothing_pending_sends_nothing_and_succeeds(config, conn, monkeypatch, capsys):
    """Silence is the correct output for a quiet day."""
    transport = StubTransport()
    stub_client(monkeypatch, transport)

    assert cli._do_notify(config, conn) == 0

    assert transport.calls == []
    out = capsys.readouterr().out
    assert "nothing pending" in out
    # Never a cheerful "nothing new today" -- that goes to stdout, not Telegram.
    assert "no briefing sent" in out


def test_a_second_notify_sends_nothing(config, conn, monkeypatch):
    add_event(conn)
    first = StubTransport()
    stub_client(monkeypatch, first)
    cli._do_notify(config, conn)
    assert len(first.calls) == 1

    second = StubTransport()
    stub_client(monkeypatch, second)
    cli._do_notify(config, conn)

    assert second.calls == []


# --------------------------------------------------------------------------
# deadlines command
# --------------------------------------------------------------------------

def add_due_soon(conn, work_id="w1"):
    """Coursework due in an hour, so every threshold has genuinely been crossed."""
    due = datetime.now(timezone.utc) + timedelta(hours=1)
    work, _ = parse_coursework(
        {
            "id": work_id,
            "title": "TD 3",
            "dueDate": {"year": due.year, "month": due.month, "day": due.day},
            "dueTime": {
                "hours": due.hour,
                "minutes": due.minute,
                "seconds": due.second,
            },
        },
        "c1",
    )
    store.upsert_coursework(conn, work)
    conn.commit()
    return work


def test_deadline_dry_run_records_nothing(config, conn):
    add_due_soon(conn)

    result = cli._do_deadlines(config, conn, dry_run=True)

    # Due in an hour with no history, so all three thresholds are crossed and
    # only the nearest is worth sending.
    assert [event.type for event in result.events] == ["deadline_t3"]
    assert len(result.suppressed) == 2
    # ...and none of it was written.
    assert result.events_written == 0
    assert result.suppressed_written == 0
    assert store.count_events(conn) == 0


def test_deadline_scan_records_and_is_idempotent(config, conn):
    add_due_soon(conn)

    first = cli._do_deadlines(config, conn)

    assert first.events_written == 1
    assert first.suppressed_written == 2
    # Three rows, but only the t3 alert is ever sent.
    assert store.count_events(conn) == 3
    assert store.count_pending_events(conn) == 1

    second = cli._do_deadlines(config, conn)

    assert second.events == []
    assert second.suppressed == []
    assert store.count_events(conn) == 3


# --------------------------------------------------------------------------
# the run command
# --------------------------------------------------------------------------

def test_run_chains_sync_then_deadlines_then_notify(config, conn, monkeypatch, capsys):
    """`agent run` is what the scheduler calls; the order is what makes it work.

    The sync stores the coursework the deadline scan reads, and both write the
    events notify sends. Stubbed at the sync boundary so this needs no network.
    """
    called: list[str] = []

    def fake_sync(cfg, connection, **kwargs):
        called.append("sync")
        add_due_soon(connection)
        return poller.SyncResult(items_seen={"courses": 1}, dry_run=kwargs.get("dry_run", False))

    monkeypatch.setattr(cli, "_do_sync", fake_sync)
    transport = StubTransport()
    stub_client(monkeypatch, transport)
    monkeypatch.setattr(cli.store, "open_db", lambda cfg: KeepOpen(conn))

    args = cli._build_parser().parse_args(["run"])
    assert cli.cmd_run(config, args) == 0

    out = capsys.readouterr().out
    assert called == ["sync"]
    assert out.index("== sync ==") < out.index("== deadlines ==") < out.index("== notify ==")

    # The deadline scan saw what the sync stored, and notify sent what it wrote.
    assert len(transport.calls) == 1
    assert "TD 3" in transport.calls[0]["text"]
    assert store.count_pending_events(conn) == 0


def test_run_dry_run_writes_nothing_and_sends_nothing(config, conn, monkeypatch, capsys):
    def fake_sync(cfg, connection, **kwargs):
        add_due_soon(connection)
        return poller.SyncResult(items_seen={"courses": 1}, dry_run=True)

    monkeypatch.setattr(cli, "_do_sync", fake_sync)
    transport = StubTransport()
    stub_client(monkeypatch, transport)
    monkeypatch.setattr(cli.store, "open_db", lambda cfg: KeepOpen(conn))

    args = cli._build_parser().parse_args(["run", "--dry-run"])
    assert cli.cmd_run(config, args) == 0

    assert transport.calls == []
    assert store.count_events(conn) == 0


def test_run_stops_early_when_nothing_is_tracked(config, conn, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_do_sync", lambda cfg, connection, **kw: None)
    monkeypatch.setattr(cli.store, "open_db", lambda cfg: KeepOpen(conn))

    args = cli._build_parser().parse_args(["run"])
    assert cli.cmd_run(config, args) == 0

    out = capsys.readouterr().out
    assert "No courses are tracked" in out
    assert "== notify ==" not in out
