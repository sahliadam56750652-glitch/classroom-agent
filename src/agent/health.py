"""Whether the two long-running processes are alive, said where I will see it.

`agent bot` answers the gate's buttons; `agent serve` is what the phone syncs
from. Each writes a heartbeat into `bot_state` -- the bot on every pass of its
long poll, the server once a minute from a background thread -- and a process
that has stopped stops writing. Found 2026-10-17: the bot had been dead for
days, the gate's buttons answered nothing, and nothing anywhere said so. A
failure that produces no visible output is the worst failure mode this project
has.

So: when a heartbeat is stale, the gate prompt and the briefing -- both sent
straight through the Bot API by `agent gate` and `agent run`, which need
neither process -- carry ONE plain line naming which one is down and what that
means. When both are fine there is no line at all: a line that is always there
is a line I stop reading.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import store

BOT_KEY = store.BOT_HEARTBEAT_KEY
SERVE_KEY = "serve_heartbeat_at"

# How often each beats, and how late a beat may be before the process is called
# down. The bot beats every pass: a 30 s long poll, or a backoff of at most 60 s
# while it cannot reach Telegram, plus whatever a tapped button took. The server
# beats every SERVE_EVERY_SECONDS.
SERVE_EVERY_SECONDS = 60
STALE_AFTER = timedelta(minutes=5)


@dataclass(frozen=True)
class Beat:
    name: str
    seen_at: datetime | None
    alive: bool


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def beat(conn: sqlite3.Connection, key: str, *, now: datetime | None = None) -> Beat:
    seen = _parse(store.get_bot_state(conn, key))
    moment = now or datetime.now(timezone.utc)
    return Beat(key, seen, seen is not None and moment - seen <= STALE_AFTER)


def stamp(conn: sqlite3.Connection, key: str, *, now: datetime | None = None) -> None:
    """Write one heartbeat. The caller commits."""
    moment = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.set_bot_state(conn, key, moment)


def _when(seen: datetime | None, zone, now: datetime) -> str:
    if seen is None:
        return "never seen running"
    local = seen.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return f"last seen {local:%H:%M}"
    return f"last seen {local:%a %d %b, %H:%M}"


def down_lines(conn: sqlite3.Connection, zone, *, now: datetime | None = None) -> list[str]:
    """Plain sentences for each process that is down. Empty when both are up."""
    moment = now or datetime.now(timezone.utc)
    lines = []
    bot = beat(conn, BOT_KEY, now=moment)
    if not bot.alive:
        lines.append(
            f"The bot is not running ({_when(bot.seen_at, zone, moment)}), "
            f"so the gate's buttons will not answer until it is back."
        )
    serve = beat(conn, SERVE_KEY, now=moment)
    if not serve.alive:
        lines.append(
            f"The web app's server is not running ({_when(serve.seen_at, zone, moment)}), "
            f"so the phone app cannot sync."
        )
    return lines
