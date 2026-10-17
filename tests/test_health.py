"""A dead bot or server is said out loud (2026-10-17).

The bot had been down for days and the gate's buttons answered nothing, with
nothing anywhere saying so. Each long-running process now beats into bot_state;
when a beat is stale the gate prompt and the briefing -- both sent without
either process -- carry one plain line, and when both are fine, nothing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from api_fixtures import client, make_config, seed

from agent import health
from agent.db import store
from agent.digest import composer
from agent.gate import messages
from agent.gate.scheduler import GatePlan

TUNIS = ZoneInfo("Africa/Tunis")
NOW = datetime(2026, 10, 17, 19, 0, tzinfo=timezone.utc)  # 20:00 in Tunis


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    yield connection
    connection.close()


def beat(conn, key, ago):
    health.stamp(conn, key, now=NOW - ago)
    conn.commit()


def test_both_alive_says_nothing(conn):
    beat(conn, health.BOT_KEY, timedelta(seconds=40))
    beat(conn, health.SERVE_KEY, timedelta(seconds=50))
    assert health.down_lines(conn, TUNIS, now=NOW) == []


def test_a_stale_bot_is_named_with_what_it_means(conn):
    beat(conn, health.BOT_KEY, timedelta(hours=6))
    beat(conn, health.SERVE_KEY, timedelta(seconds=30))
    (line,) = health.down_lines(conn, TUNIS, now=NOW)
    assert line == ("The bot is not running (last seen 14:00), so the gate's buttons "
                    "will not answer until it is back.")


def test_a_stale_server_is_named_with_what_it_means(conn):
    beat(conn, health.BOT_KEY, timedelta(seconds=30))
    beat(conn, health.SERVE_KEY, timedelta(days=2))
    (line,) = health.down_lines(conn, TUNIS, now=NOW)
    assert line == ("The web app's server is not running (last seen Thu 15 Oct, 20:00), "
                    "so the phone app cannot sync.")


def test_never_seen_is_said_as_such(conn):
    lines = health.down_lines(conn, TUNIS, now=NOW)
    assert len(lines) == 2 and all("never seen running" in line for line in lines)


def test_the_threshold_is_five_minutes(conn):
    beat(conn, health.BOT_KEY, timedelta(minutes=4, seconds=59))
    beat(conn, health.SERVE_KEY, timedelta(minutes=5, seconds=1))
    lines = health.down_lines(conn, TUNIS, now=NOW)
    assert len(lines) == 1 and "server" in lines[0]


def test_the_gate_prompt_carries_the_line_where_it_cannot_be_trimmed():
    plan = GatePlan(for_date=date(2026, 10, 19), version_label="S1", provisional=False)
    text = messages.compose(plan, ["The bot is not running (last seen 14:00), so the gate's buttons will not answer until it is back."])
    header, note = text.split("\n\n")[:2]
    assert "Tomorrow" in header
    assert note.startswith("<i>The bot is not running")
    assert messages.compose(plan) == messages.compose(plan, [])


def test_the_briefing_leads_with_the_line_and_stamps_nothing_for_it(tmp_path, monkeypatch):
    from agent import cli
    from agent.config import Config
    from agent.sync.differ import Event

    data = tmp_path / "data"
    data.mkdir()
    config = Config(account="me@example.com", timezone="Africa/Tunis", data_dir=data,
                    tracked_courses=["c1"], ignored_courses=[], telegram_chat_id=1)
    db = store.connect(config.db_path)
    store.insert_event(db, Event(type="new_material", entity_type="coursework_material",
                                 entity_id="m1", course_id="c1", payload={"title": "Lecture 7"},
                                 created_at="2026-10-17T18:00:00Z"))
    db.commit()
    sent = []

    class Telegram:
        def send_message(self, text):
            sent.append(text)
            return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(cli.telegram_api, "from_config", lambda config: Telegram())
    cli._do_notify(config, db)
    assert sent, "nothing was sent"
    assert sent[0].startswith("<i>The bot is not running")
    assert "Lecture 7" in sent[0]
    assert db.execute("SELECT COUNT(*) FROM events WHERE notified_at IS NULL").fetchone()[0] == 0
    db.close()


def test_status_says_when_the_bot_was_last_seen(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    seed(config)
    db = store.connect(config.db_path)
    health.stamp(db, health.BOT_KEY, now=datetime.now(timezone.utc))
    db.commit()
    db.close()
    body = client(config, monkeypatch).get("/api/status").json()
    assert body["bot_running"] is True and body["bot_seen_at"]
    assert body["serve_seen_at"] is None
