"""The application factory, and the one place that says what may be written.

`WRITE_ROUTES` is not documentation. A test asserts that the set of non-GET
routes the app actually exposes equals it exactly, which makes adding a write a
deliberate act with a failing test in front of it. That is how "the API cannot
reach `verified`" and "the API never fires the gate" stop being conventions and
become checks -- the same instinct as `_TRANSITIONS` not containing `verified` at
all.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from ..config import Config, api_token
from ..db import store
from . import auth as api_auth
from .routes import deadlines, library, meta, study, subjects, timetable

log = logging.getLogger("agent.api")

# Every route this API exposes that is not a GET, as (method, path). The test
# that compares this to the live app is the guard; this is the declaration.
#
# Deliberately absent, and each for a stated reason:
#   * anything touching study_items -- 5b is read-only over them, and `verified`
#     has exactly one writer which is not reachable from here
#   * anything creating a gate_runs row -- computing a plan is not firing the
#     gate, and conflating them lets a morning page swallow the evening prompt
#   * PUT on timetable.yaml -- the file holds the pattern and keeps one writer
#   * any pipeline stage -- no sync, no OCR, no model call
WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/session"),
        ("DELETE", "/api/session"),
        # These four write nothing. They exist to REFUSE with a body that says
        # why timetable.yaml is never written and what to do instead, because
        # FastAPI's automatic 405 says "Method Not Allowed", which reads as "not
        # built yet" and invites someone to build it. Declared here because the
        # guard's question is "is every non-GET route accounted for", and the
        # honest answer for these is yes, on purpose.
        # The one write in 5b that is not a hand-entered record: where I stopped
        # reading. It stores a content hash, never an index alone.
        ("PUT", "/api/documents/{drive_id}/position"),
        ("PUT", "/api/timetable/file"),
        ("POST", "/api/timetable/file"),
        ("PATCH", "/api/timetable/file"),
        ("DELETE", "/api/timetable/file"),
    }
)


def create_app(config: Config) -> FastAPI:
    """Build the app around an already-loaded Config.

    Two things happen here rather than on the first request, and both are about
    failing at the right moment:

    `api_token` is called at startup, so a missing or short secret stops the
    service before it binds a port. A server that is open because a variable is
    unset looks exactly like one that is protected, and the only defence against
    that is refusing to start.

    `store.open_db` is called once, so schema.sql is applied and the version
    guard runs at boot -- loudly, in the log, next to the unit that started it,
    rather than as a 500 on whichever request arrived first.
    """
    api_token(config)  # raises ConfigError, before anything is served

    opened = store.open_db(config)
    version = store.schema_version(opened)
    opened.close()
    log.info("api: database ready at schema version %s", version)

    app = FastAPI(
        title="classroom-agent",
        description="The HTTP API behind the web client. Read-only over study items.",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        # Everything this process serves lives under /api, so that 5c can mount
        # the static client at / without the two ever arguing about a path.
        # FastAPI would otherwise register /redoc and /docs/oauth2-redirect at
        # the root on its own.
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.config = config
    app.state.limiter = api_auth.RateLimiter()

    app.include_router(meta.router, prefix="/api", tags=["meta"])
    app.include_router(study.router, prefix="/api", tags=["study"])
    app.include_router(subjects.router, prefix="/api", tags=["subjects"])
    app.include_router(library.router, prefix="/api", tags=["library"])
    app.include_router(deadlines.router, prefix="/api", tags=["deadlines"])
    app.include_router(timetable.router, prefix="/api", tags=["timetable"])
    return app


def all_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Every (method, path) the app exposes, reads included.

    Walks nested routers rather than reading `app.routes` flat, because FastAPI
    1.7 keeps an included router wrapped -- `app.routes` holds one opaque entry
    whose real routes carry paths relative to the router and whose prefix lives
    on the include context. A flat read returns the four FastAPI built-ins and
    none of ours, which would make the whitelist below vacuously true. That is
    why `test_the_walker_finds_the_real_routes` asserts it found something before
    anything asserts what it found.

    Written against both shapes, so a FastAPI that goes back to flattening does
    not silently empty the guard.
    """
    found: set[tuple[str, str]] = set()

    def walk(routes, prefix: str) -> None:
        for route in routes:
            methods = getattr(route, "methods", None)
            path = getattr(route, "path", None)
            if methods and path is not None:
                for method in methods:
                    found.add((method, prefix + path))
                continue

            # An included router: its prefix is on the include context, and its
            # own routes are relative to it.
            nested = getattr(route, "original_router", None)
            context = getattr(route, "include_context", None)
            if nested is not None:
                inner = getattr(context, "prefix", "") or ""
                walk(getattr(nested, "routes", []), prefix + inner)
                continue

            inner_routes = getattr(route, "routes", None)
            if inner_routes:
                walk(inner_routes, prefix + (getattr(route, "prefix", "") or ""))

    walk(app.routes, "")
    return found


def write_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Every (method, path) the app exposes that is not a read.

    HEAD and OPTIONS are excluded: Starlette adds them alongside GET and neither
    changes anything.
    """
    return {
        (method, path)
        for method, path in all_routes(app)
        if method not in ("GET", "HEAD", "OPTIONS")
    }
