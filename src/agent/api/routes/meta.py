"""The session exchange, liveness, and the figures behind everything else.

`health` and `status` are two routes rather than one, deliberately. A liveness
probe that needs a cookie is awkward for systemd and for Caddy; a liveness probe
that reports the backlog leaks it to anyone who can reach the port. So health
says `{"ok": true}` and nothing whatever, and every figure lives behind the
session.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ...db import store
from .. import auth as api_auth
from ..deps import Conf, Db, Session

router = APIRouter()


class TokenIn(BaseModel):
    token: str = Field(min_length=1)


class SessionOut(BaseModel):
    signed_in: bool
    expires_at: str


class HealthOut(BaseModel):
    ok: bool


class StatusOut(BaseModel):
    schema_version: int
    study_items: dict[str, int]
    extractions: dict[str, int]
    ocr_pages: dict[str, int]
    events_total: int
    events_pending: int
    sessions_live: int
    last_sync: dict[str, object] | None
    ocr_errors: list[dict[str, object]]


@router.post("/session", response_model=SessionOut)
def sign_in(
    body: TokenIn, request: Request, response: Response, config: Conf, conn: Db
) -> SessionOut:
    """Trade the token for a cookie. The only unauthenticated route that works.

    The refusal is deliberately incurious: a wrong token gets 401 and a sentence,
    and a rate-limited one gets 429 with Retry-After. Neither says whether the
    token was close, because there is nothing useful to say and one bit is one
    bit.
    """
    limiter = request.app.state.limiter
    client = request.client.host if request.client else "unknown"
    try:
        session_id = api_auth.exchange(
            conn,
            config,
            body.token,
            ip=client,
            user_agent=request.headers.get("user-agent"),
            limiter=limiter,
        )
    except api_auth.RateLimited as err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(err),
            headers={"Retry-After": str(err.retry_after)},
        ) from err
    except api_auth.AuthFailed as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(err)
        ) from err

    response.set_cookie(value=session_id, **api_auth.cookie_arguments(config))
    row = store.touch_api_session(
        conn, session_id, expires_at=api_auth._expiry_iso()
    )
    assert row is not None  # just created
    return SessionOut(signed_in=True, expires_at=str(row["expires_at"]))


@router.delete("/session", response_model=SessionOut)
def sign_out(request: Request, response: Response, config: Conf, conn: Db) -> SessionOut:
    """Revoke this browser's row. The reason sessions are rows at all."""
    api_auth.revoke(conn, request.cookies.get(api_auth.COOKIE_NAME))
    response.delete_cookie(key=api_auth.COOKIE_NAME, path="/")
    return SessionOut(signed_in=False, expires_at="")


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Is the process up. Says nothing else, and needs no cookie to say it."""
    return HealthOut(ok=True)


@router.get("/status", response_model=StatusOut)
def status_report(conn: Db, session: Session) -> StatusOut:
    """Every figure `agent run` prints, for the screen that audits the rest.

    Here because DESIGN.md forbids "a state the app counts but will not show me".
    A coverage figure I cannot take apart is one I will not trust.
    """
    runs = store.recent_sync_runs(conn, limit=1)
    return StatusOut(
        schema_version=store.schema_version(conn),
        study_items=store.count_study_items_by_state(conn),
        extractions=store.count_extractions_by_status(conn),
        ocr_pages=store.count_ocr_pages_by_status(conn),
        events_total=store.count_events(conn),
        events_pending=store.count_pending_events(conn),
        sessions_live=store.count_api_sessions(conn),
        last_sync=dict(runs[0]) if runs else None,
        ocr_errors=[
            {"error": name, "pages": count}
            for name, count in store.ocr_error_counts(conn)
        ],
    )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return dict(row) if row is not None else None
