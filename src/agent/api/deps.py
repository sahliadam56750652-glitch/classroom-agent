"""What every route is handed: a connection, a config, a timetable, a session.

The connection is the important one. Three facts about `db/store.py` decide its
shape, and all three are assumptions of a single-threaded CLI process that this
package is the first thing to violate:

**`sqlite3.connect` defaults to `check_same_thread=True`.** A connection opened
once and reused would raise `ProgrammingError` the first time Starlette's
threadpool ran a route on a second thread. So: one connection per request. Not
`check_same_thread=False` with a shared connection, which trades a loud error for
interleaved transactions on one connection -- worse, and much harder to see.

**`store.connect` applies the whole schema on every call.** Free once per CLI
invocation, once per REQUEST here. `initialise=False` skips it; the app calls
`open_db` once at startup so the version guard still fires loudly, at boot,
rather than on whichever request happens to be first.

**Store writes do not commit; the caller does.** Four `commit()` calls in 2,257
lines, none in any of the write helpers. So the commit lives here, once, and no
route may call it. A route that forgot would return 200 for a write that never
landed -- silent success, which this project ranks as its worst failure mode.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from ..config import Config
from ..db import store
from ..gate import timetable as timetable_mod
from ..gate.timetable import Timetable
from . import auth as api_auth

log = logging.getLogger("agent.api")


def get_config(request: Request) -> Config:
    """The Config loaded once at startup, not per request.

    `load_config` reads .env, parses YAML and mkdirs three directories. That is
    nothing once and wasteful sixty times a minute.
    """
    return request.app.state.config


def get_limiter(request: Request) -> api_auth.RateLimiter:
    return request.app.state.limiter


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection for one request, committed here or rolled back here.

    The commit is unconditional on a clean return rather than conditional on the
    route having written something: a read commits an empty transaction, which
    costs nothing, and the alternative is every write route remembering to say
    so.
    """
    config: Config = request.app.state.config
    conn = store.connect(config.db_path, initialise=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_timetable(request: Request) -> Timetable | None:
    """The timetable, re-read from disk on every request. Deliberately.

    It is 7 KB of hand-edited YAML. Caching it would mean that editing the file
    does nothing until the service restarts -- a silent failure of exactly the
    kind this project keeps writing tests against, bought for a parse that does
    not register.

    None rather than an exception when it will not load, matching `scope.load`:
    a broken timetable must not take the library and the deadlines down with it.
    """
    config: Config = request.app.state.config
    try:
        return timetable_mod.load(config.timetable_path)
    except timetable_mod.TimetableError as err:
        log.warning("api: timetable not read: %s", err)
        return None


def require_timetable(
    table: Annotated[Timetable | None, Depends(get_timetable)],
) -> Timetable:
    """For the routes that cannot answer at all without it.

    A 503 rather than a 500: the service is fine and the file is not, and the
    body says which so the answer is not "something went wrong".
    """
    if table is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "timetable.yaml could not be read, and this answer is built from "
                "it. Run `agent timetable --check` to see why."
            ),
        )
    return table


def require_session(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> sqlite3.Row:
    """Every route but the exchange and the liveness probe depends on this.

    Also the CSRF check, because this is the one place every state-changing
    request passes through. SameSite=Lax already blocks a cross-site form POST;
    the Origin comparison covers what it does not, and costs two lines.
    """
    config: Config = request.app.state.config
    session = api_auth.resolve(conn, request.cookies.get(api_auth.COOKIE_NAME))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="no live session. POST the token to /api/session first.",
        )

    if request.method != "GET" and config.api_origin:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != config.api_origin:
            log.warning("api: rejected %s from origin %s", request.method, origin)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="that request did not come from this app.",
            )
    return session


# The three names routes actually spell.
Db = Annotated[sqlite3.Connection, Depends(get_db)]
Conf = Annotated[Config, Depends(get_config)]
Session = Annotated[sqlite3.Row, Depends(require_session)]
Table = Annotated[Timetable, Depends(require_timetable)]
MaybeTable = Annotated[Timetable | None, Depends(get_timetable)]
