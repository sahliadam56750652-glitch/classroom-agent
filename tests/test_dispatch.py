"""Delivery: notified_at is stamped only for what actually went out.

Delivered twice is a bug; delivered late is not. Every test here is about one
of those two halves.

No test in this file may reach the network: the Telegram client is always built
with a stub transport.
"""

from __future__ import annotations

import json

import pytest

from agent.classroom.models import parse_course
from agent.db import store
from agent.digest.composer import Block, compose_blocks
from agent.notify import dispatch
from agent.notify.telegram import MESSAGE_LIMIT, Telegram, TelegramError, _ApiError
from agent.sync.differ import Event

NOW = "2026-08-27T12:00:00Z"


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    store.upsert_course(connection, parse_course({"id": "c1", "name": "Databases"}))
    store.upsert_course(connection, parse_course({"id": "c2", "name": "Operating Systems"}))
    yield connection
    connection.close()


class StubTransport:
    """Replies OK, or raises whatever the test queued for that call."""

    def __init__(self, failures=None):
        self.calls: list[dict] = []
        # {call index (0-based) -> exception to raise}
        self.failures = failures or {}

    def __call__(self, url, payload):
        index = len(self.calls)
        self.calls.append(payload)
        if index in self.failures:
            raise self.failures[index]
        return {"ok": True, "result": {"message_id": index}}


def client(transport):
    return Telegram("t", 1, transport=transport, sleep=lambda _: None)


def add_event(conn, event_id_hint, event_type="new_material", course_id="c1", title="X"):
    event = Event(
        type=event_type,
        entity_type="coursework_material",
        entity_id=f"m{event_id_hint}",
        course_id=course_id,
        payload={"title": title},
        created_at=f"2026-08-27T12:00:{event_id_hint:02d}Z",
    )
    store.insert_event(conn, event)
    conn.commit()


def pending_ids(conn):
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM events WHERE notified_at IS NULL ORDER BY id"
        ).fetchall()
    ]


def notified_ids(conn):
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM events WHERE notified_at IS NOT NULL ORDER BY id"
        ).fetchall()
    ]


def blocks_for(conn):
    rows = store.list_events(conn, include_notified=False)
    return compose_blocks(rows, timezone_name="Africa/Tunis")


# --------------------------------------------------------------------------
# the notified_at contract
# --------------------------------------------------------------------------

def test_a_successful_send_stamps_notified_at(conn):
    add_event(conn, 1)
    add_event(conn, 2)
    transport = StubTransport()

    result = dispatch.deliver(conn, blocks_for(conn), client(transport), now=NOW)

    assert result.failed is False
    assert result.messages_sent == 1
    assert result.events_notified == 2
    assert pending_ids(conn) == []
    assert len(notified_ids(conn)) == 2


def test_a_failed_send_leaves_notified_at_null(conn):
    """The required case: a failure must leave everything retryable."""
    add_event(conn, 1)
    add_event(conn, 2)
    transport = StubTransport({0: _ApiError("boom", status=400, retry_after=None)})

    result = dispatch.deliver(conn, blocks_for(conn), client(transport), now=NOW)

    assert result.failed is True
    assert result.events_notified == 0
    assert len(pending_ids(conn)) == 2
    assert notified_ids(conn) == []


def test_the_next_run_retries_after_a_failure(conn):
    add_event(conn, 1)
    add_event(conn, 2)

    # Telegram stays down through every retry, so the send genuinely fails.
    failing = StubTransport(
        {index: _ApiError("down", status=503, retry_after=None) for index in range(5)}
    )
    first = dispatch.deliver(conn, blocks_for(conn), client(failing), now=NOW)
    assert first.failed is True
    assert len(failing.calls) == 5, "a 5xx is retried before it is given up on"
    assert len(pending_ids(conn)) == 2

    working = StubTransport()
    second = dispatch.deliver(conn, blocks_for(conn), client(working), now=NOW)

    assert second.failed is False
    assert second.events_notified == 2
    assert pending_ids(conn) == []


def test_an_already_notified_event_is_never_sent_again(conn):
    """Invariant 3: a non-null notified_at is never selected again."""
    add_event(conn, 1)
    transport = StubTransport()
    dispatch.deliver(conn, blocks_for(conn), client(transport), now=NOW)
    assert len(transport.calls) == 1

    again = StubTransport()
    result = dispatch.deliver(conn, blocks_for(conn), client(again), now=NOW)

    assert blocks_for(conn) == []
    assert again.calls == []
    assert result.messages_sent == 0


def test_stamping_is_idempotent(conn):
    add_event(conn, 1)
    ids = pending_ids(conn)

    assert store.mark_notified(conn, ids, now=NOW) == 1
    # The IS NULL guard means a second stamp changes nothing.
    assert store.mark_notified(conn, ids, now="2026-09-09T09:09:09Z") == 0

    row = conn.execute("SELECT notified_at FROM events WHERE id = ?", (ids[0],)).fetchone()
    assert row["notified_at"] == NOW


def test_stamping_nothing_is_a_no_op(conn):
    assert store.mark_notified(conn, [], now=NOW) == 0


# --------------------------------------------------------------------------
# partial delivery
# --------------------------------------------------------------------------

def test_a_failure_partway_stamps_only_what_was_sent(conn):
    """Two courses, two messages, the second fails: the first stays sent."""
    big = "y" * 3000
    add_event(conn, 1, course_id="c1", title=big)
    add_event(conn, 2, course_id="c2", title=big)

    blocks = blocks_for(conn)
    messages = dispatch.pack(blocks)
    assert len(messages) == 2, "the two courses must not fit in one message"

    transport = StubTransport({1: _ApiError("boom", status=400, retry_after=None)})
    result = dispatch.deliver(conn, blocks, client(transport), now=NOW)

    assert result.failed is True
    assert result.messages_sent == 1
    assert result.events_notified == 1
    # Exactly one event delivered and stamped, exactly one still pending.
    assert len(notified_ids(conn)) == 1
    assert len(pending_ids(conn)) == 1


def test_the_retry_after_a_partial_failure_sends_only_the_remainder(conn):
    """No duplicate: the course that already landed is not sent a second time."""
    big = "y" * 3000
    add_event(conn, 1, course_id="c1", title=big)
    add_event(conn, 2, course_id="c2", title=big)

    failing = StubTransport({1: _ApiError("boom", status=400, retry_after=None)})
    dispatch.deliver(conn, blocks_for(conn), client(failing), now=NOW)
    first_text = failing.calls[0]["text"]

    working = StubTransport()
    result = dispatch.deliver(conn, blocks_for(conn), client(working), now=NOW)

    assert result.failed is False
    assert len(working.calls) == 1
    assert working.calls[0]["text"] != first_text
    assert pending_ids(conn) == []


# --------------------------------------------------------------------------
# packing
# --------------------------------------------------------------------------

def test_small_courses_share_one_message():
    blocks = [
        Block("Databases", "<b>Databases</b>\nitem", [1]),
        Block("Operating Systems", "<b>Operating Systems</b>\nitem", [2]),
    ]

    messages = dispatch.pack(blocks)

    assert len(messages) == 1
    text, ids = messages[0]
    assert ids == [1, 2]
    assert "\n\n" in text


def test_packing_never_exceeds_the_limit():
    blocks = [Block(f"C{i}", f"<b>C{i}</b>\n" + "x" * 2000, [i]) for i in range(6)]

    messages = dispatch.pack(blocks)

    assert all(len(text) <= MESSAGE_LIMIT for text, _ in messages)
    assert sorted(i for _, ids in messages for i in ids) == list(range(6))


def test_an_oversized_course_carries_its_ids_on_the_last_piece():
    """A half-sent course must stamp nothing, so the retry resends all of it."""
    lines = ["<b>Databases</b>"] + [f"item {i} " + "z" * 100 for i in range(60)]
    blocks = [Block("Databases", "\n".join(lines), [1, 2, 3])]

    messages = dispatch.pack(blocks)

    assert len(messages) > 1
    assert [ids for _, ids in messages[:-1]] == [[] for _ in messages[:-1]]
    assert messages[-1][1] == [1, 2, 3]


def test_pack_of_nothing_is_nothing():
    assert dispatch.pack([]) == []


# --------------------------------------------------------------------------
# what gets sent
# --------------------------------------------------------------------------

def test_the_message_is_html_with_the_course_header(conn):
    add_event(conn, 1, title="Lecture 7")
    transport = StubTransport()

    dispatch.deliver(conn, blocks_for(conn), client(transport), now=NOW)

    payload = transport.calls[0]
    assert payload["parse_mode"] == "HTML"
    assert "<b>Databases</b>" in payload["text"]
    assert "Lecture 7" in payload["text"]


def test_nothing_is_sent_when_there_is_nothing_pending(conn):
    transport = StubTransport()

    result = dispatch.deliver(conn, blocks_for(conn), client(transport), now=NOW)

    assert transport.calls == []
    assert result.messages_sent == 0
    assert result.failed is False
