"""The two things I can say about an item: I read it, or I am skipping it.

One module, two callers. `agent bot` reaches these from a Telegram button and
the HTTP API from a button in the browser, and both go through exactly these
functions -- so "Read" cannot mean one thing on the phone's chat and another in
the app, and a skip cannot be logged with a reason in one place and without one
in the other.

Neither function can reach `verified`. Both move an item through
`store.advance_study_item`, whose `_TRANSITIONS` does not contain `verified` at
all; the only way there is a passed quiz, through `quiz.settle`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..db import store

# What a skip records when I give no reason. A skip with no reason at all is the
# one thing store.advance_study_item refuses -- a year later it would be
# indistinguishable from the seeded backlog -- so the surface it came from is the
# floor.
SKIP_FLOOR = {
    "telegram": "skipped at delivery",
    "web": "skipped in the web app",
}

# Long enough for a sentence, short enough that it stays a reason rather than a
# diary entry.
MAX_REASON = 280


@dataclass(frozen=True)
class Outcome:
    """What a tap did. `moved` False is normal: the same tap arriving twice."""

    moved: bool
    state: str


def _state(conn: sqlite3.Connection, item_id: int) -> str | None:
    row = conn.execute("SELECT state FROM study_items WHERE id = ?", (item_id,)).fetchone()
    return str(row["state"]) if row is not None else None


def mark_read(conn: sqlite3.Connection, item_id: int, *, now: str | None = None) -> Outcome:
    """I read it. Moves the item to `reviewed`; never further.

    From `pending` it passes through `delivered` first, and that step is honest
    rather than a shortcut: in Telegram, delivery is the bot sending the files;
    in the browser, the files ARE delivered -- they are on the screen, one tap
    from the reader. Saying "I read it" about material the app put in front of
    me is the same claim either way.

    Does not commit. The caller's transaction does, which is the API's single
    commit in `get_db` and the bot's commit after the tap.
    """
    before = _state(conn, item_id)
    if before is None:
        raise LookupError(f"no study item {item_id}")
    if before == "pending":
        store.advance_study_item(conn, item_id, "delivered", now=now)
    moved = store.advance_study_item(conn, item_id, "reviewed", now=now)
    return Outcome(moved=moved or before == "pending", state=_state(conn, item_id) or before)


def skip_item(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    source: str,
    reason: str = "",
    context: str = "",
    now: str | None = None,
) -> Outcome:
    """I am not doing this one. Logged as `skipped`, never as `verified`.

    `reason` is mine and optional; `source` and `context` say where the skip
    came from ("web", "telegram"; "gate run 41"), so the record is never empty
    even when I give no reason -- and a reason I did give is kept verbatim after
    that floor rather than replacing it.
    """
    before = _state(conn, item_id)
    if before is None:
        raise LookupError(f"no study item {item_id}")
    floor = SKIP_FLOOR.get(source, f"skipped from {source}")
    if context:
        floor = f"{floor}, {context}"
    said = " ".join(reason.split())[:MAX_REASON]
    recorded = f"{floor}: {said}" if said else floor
    moved = store.advance_study_item(
        conn, item_id, "skipped", skip_reason=recorded, now=now
    )
    return Outcome(moved=moved, state=_state(conn, item_id) or before)
