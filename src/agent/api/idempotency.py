"""A write sent twice is applied once. Phase 5e, the phone's outbox.

Writes made while the PC is off wait on the phone and are replayed when it is
back (web/outbox.js). A replay can lose its reply -- the PC applied the write and
the connection dropped before the answer arrived -- and then the phone, rightly,
sends it again. Each queued write carries an `Idempotency-Key`, a UUID made on
the phone; the first time a key is seen its answer is stored in `client_ops`,
and every later request with that key gets the stored answer and changes
nothing.

Only the routes the outbox uses are wrapped: read, skip, marking a task or a
milestone done, recording a timetable change, and saving where I stopped
reading.
Creating a task or a project is never queued -- creation is where a silent loss
would hurt most, so it needs the PC and says so.

The session check runs before any of this (it is a dependency of the route), so
a stored answer is only ever given to a signed-in request.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from datetime import datetime, timezone

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

HEADER = "Idempotency-Key"
MAX_KEY = 100


def _key(request: Request) -> str | None:
    value = (request.headers.get(HEADER) or "").strip()
    return value[:MAX_KEY] or None


def replayed(conn: sqlite3.Connection, request: Request) -> JSONResponse | None:
    key = _key(request)
    if key is None:
        return None
    row = conn.execute("SELECT status, body FROM client_ops WHERE op_id = ?", (key,)).fetchone()
    if row is None:
        return None
    return JSONResponse(
        json.loads(row["body"]), status_code=int(row["status"]),
        headers={"Idempotent-Replayed": "true"},
    )


def remember(conn: sqlite3.Connection, request: Request, result, status: int) -> None:
    key = _key(request)
    if key is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO client_ops (op_id, route, status, body, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            key,
            f"{request.method} {request.url.path}",
            status,
            json.dumps(jsonable_encoder(result)),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )


def idempotent(status: int = 200):
    """Wrap a route that takes `request` and `conn` so a repeated key replays."""

    def wrap(route):
        @functools.wraps(route)
        def wrapper(*args, **kwargs):
            request = kwargs["request"]
            conn = kwargs["conn"]
            if kwargs.get("dry_run"):
                # Writes nothing, so there is nothing to apply only once.
                return route(*args, **kwargs)
            hit = replayed(conn, request)
            if hit is not None:
                return hit
            result = route(*args, **kwargs)
            remember(conn, request, result, status)
            return result

        # Read by tests/test_web_assets.py, which holds the wrapped routes and
        # the routes the phone queues to be the same list.
        wrapper.idempotent = True
        return wrapper

    return wrap
