"""`agent bot` outlives the network (2026-10-17).

It used to make one getUpdates call per pass and let any failure out of the
loop: a laptop waking from sleep, Wi-Fi not up at logon -- and the bot exited,
silently under pythonw, so the gate's buttons answered nothing for days.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from agent.config import Config
from agent.db import store
from agent.gate import bot
from agent.notify.telegram import TelegramError

TUNIS = ZoneInfo("Africa/Tunis")


class Stop(BaseException):
    """Ends the endless loop from inside a test. Not an Exception, on purpose."""


class ScriptedTelegram:
    """get_updates plays a script: a list is returned, an exception raised."""

    def __init__(self, script):
        self.script = list(script)
        self.polls = 0

    def get_updates(self, offset, *, timeout=30):
        self.polls += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.fixture
def config(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    return Config(account="me@example.com", timezone="Africa/Tunis", data_dir=data,
                  tracked_courses=[], ignored_courses=[])


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    yield connection
    connection.close()


def run(conn, config, telegram, **kwargs):
    problems, slept = [], []
    with pytest.raises(Stop):
        bot.poll(conn, config, telegram, tz=TUNIS, timeout=0, on_problem=problems.append,
                 sleep=slept.append, **kwargs)
    return problems, slept


def test_a_network_failure_waits_and_polls_again(conn, config):
    telegram = ScriptedTelegram([
        TelegramError("could not reach the Telegram API: getaddrinfo failed"),
        TelegramError("could not reach the Telegram API: timed out"),
        [],
        Stop(),
    ])
    beats = []
    problems, slept = run(conn, config, telegram, between=lambda: beats.append(1))
    assert telegram.polls == 4
    assert slept == [2, 4]
    assert any("getaddrinfo" in text for text in problems)
    assert any("polling again" in text for text in problems)
    # Alive while cut off: the heartbeat work ran on every pass.
    assert len(beats) == 4


def test_the_wait_never_exceeds_a_minute(conn, config):
    telegram = ScriptedTelegram([TelegramError("down")] * 9 + [Stop()])
    _, slept = run(conn, config, telegram)
    assert max(slept) == 60 and slept[:3] == [2, 4, 8]


def test_a_handler_that_raises_is_retried_then_skipped(conn, config, monkeypatch):
    update = {"update_id": 77, "callback_query": {"id": "q", "data": "r:1"}}
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bot, "handle_callback", broken)
    telegram = ScriptedTelegram([[update], [update], [update], Stop()])
    problems, _ = run(conn, config, telegram)
    assert len(calls) == 3
    assert any("skipped" in text for text in problems)
    # Acknowledged in the end, so Telegram stops offering it.
    assert store.get_bot_state(conn, bot.OFFSET_KEY) == "77"


def test_work_between_polls_never_stops_the_listener(conn, config):
    def broken():
        raise RuntimeError("quota")

    telegram = ScriptedTelegram([[], Stop()])
    problems, _ = run(conn, config, telegram, between=broken)
    assert telegram.polls == 2
    assert any("quota" in text for text in problems)


def test_once_still_raises_so_a_single_poll_reports_failure(conn, config):
    telegram = ScriptedTelegram([TelegramError("down")])
    with pytest.raises(TelegramError):
        bot.poll(conn, config, telegram, tz=TUNIS, timeout=0, once=True)
