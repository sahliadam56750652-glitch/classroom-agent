"""Send a composed briefing and stamp what actually went out.

This is the module that makes "notify exactly once" true, so the ordering here
is the whole point:

    send the message  ->  it succeeded  ->  stamp its events  ->  commit

A send that fails leaves notified_at NULL and the next run picks the same
events up again. Delivered twice is a bug; delivered late is not.

Stamping is per message, not per digest. A briefing that spans four messages
and fails on the third has genuinely delivered the first two, and stamping
nothing would resend them -- so each message carries the ids of the events it
accounts for and those are stamped the moment it lands.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..db import store
from ..digest.composer import Block
from .telegram import MESSAGE_LIMIT, Telegram, TelegramError, split_message


@dataclass
class DeliveryResult:
    messages_sent: int = 0
    events_notified: int = 0
    # Set when a send failed. Whatever was sent before it stays sent and
    # stamped; everything after it stays pending for the next run.
    error: str | None = None
    courses: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.error is not None


def pack(blocks: list[Block], limit: int = MESSAGE_LIMIT) -> list[tuple[str, list[int]]]:
    """Group course blocks into messages, each with the event ids it covers.

    Whole courses are kept together wherever they fit, because that is the
    boundary a reader scans by. A single course too large for one message is
    split, and its event ids ride on the *last* piece only -- so a failure
    partway through that course stamps none of it and the next run sends the
    whole course again rather than half of it twice.
    """
    messages: list[tuple[str, list[int]]] = []
    text = ""
    ids: list[int] = []

    for block in blocks:
        candidate = f"{text}\n\n{block.html}" if text else block.html
        if len(candidate) <= limit:
            text = candidate
            ids.extend(block.event_ids)
            continue

        if text:
            messages.append((text, ids))
            text, ids = "", []

        if len(block.html) <= limit:
            text, ids = block.html, list(block.event_ids)
            continue

        chunks = split_message(block.html, limit)
        for chunk in chunks[:-1]:
            messages.append((chunk, []))
        text, ids = chunks[-1], list(block.event_ids)

    if text:
        messages.append((text, ids))
    return messages


def deliver(
    db: sqlite3.Connection,
    blocks: list[Block],
    telegram: Telegram,
    *,
    now: str | None = None,
) -> DeliveryResult:
    """Send the briefing, stamping each message's events as it succeeds.

    Returns rather than raises on a send failure: a partial delivery is a real
    outcome that the caller has to report accurately, not an exception to be
    swallowed somewhere up the stack. A run that produces no visible output is
    the worst failure mode this project has.
    """
    result = DeliveryResult(courses=[block.course_name for block in blocks])

    for text, event_ids in pack(blocks):
        try:
            telegram.send_message(text)
        except TelegramError as err:
            result.error = str(err)
            return result

        result.messages_sent += 1
        # Only now, and only for this message. If the process dies between the
        # send and this line the events stay pending and will be sent again --
        # the one window where "exactly once" degrades to "at least once", and
        # the only alternative is losing messages instead, which is worse.
        result.events_notified += store.mark_notified(db, event_ids, now=now)

    return result
