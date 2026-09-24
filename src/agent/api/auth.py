"""The token exchange, the session cookie, and the limiter in front of both.

One user, one shared secret, no registration and no password reset -- so the
secret IS the account, and everything here is about the two ways that can go
wrong: the secret being guessed, and the cookie outliving the device.

**Why a cookie and not a bearer header.** A browser's own PDF viewer issuing
range requests from an iframe, and any img tag, cannot attach an Authorization
header. A bearer token would force the reader to pull each document whole
through JavaScript in order to attach one, which is the exact failure range
requests exist to prevent. The cookie is load-bearing for the reader, not a
matter of taste.

**Why session rows and not a signed cookie.** Revocation. A signed cookie cannot
be withdrawn: the day the phone is lost, the only remedy is rotating the token,
which signs out every other device and means editing .env on the server. A row
can be deleted. The cookie value is the row id -- random, opaque -- so what the
browser holds says nothing about the token it was traded for.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import Config, api_token
from ..db import store

log = logging.getLogger("agent.api.auth")

COOKIE_NAME = "agent_session"

# Long, and deliberately. This is a phone I open daily; a session expiring at
# 23:00 mid-chapter costs a reading session and buys nothing against a
# single-user tool. Sliding, so daily use never reaches it.
SESSION_DAYS = 90

# The exchange is the only unauthenticated route that does work, so it is the
# only brute-force surface. Logging without a limit is a record of the attack
# rather than a defence.
#
# Two tiers, because they answer different questions. The per-minute burst stops
# a script from trying thousands; the lockout stops one from grinding slowly for
# an hour and staying under the burst. Neither inconveniences a person who
# mistypes: five tries a minute is more than anyone pastes wrong.
BURST_FAILURES = 5
BURST_WINDOW_SECONDS = 60
LOCKOUT_FAILURES = 20
LOCKOUT_WINDOW_SECONDS = 900
LOCKOUT_SECONDS = 900


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expiry_iso(*, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return (moment + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# the limiter
# ---------------------------------------------------------------------------


@dataclass
class _Attempts:
    """One IP's recent failures, and when it is locked out until."""

    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class RateLimiter:
    """Failed exchanges per IP, in memory and per process.

    Not a table, and that is a decision rather than laziness: the state is
    worthless across a restart, a restart is not an attack, and writing a row
    per failed guess would let an attacker grow the database. A dict swept on
    use is the whole mechanism.

    `monotonic` rather than wall clock, so a clock correction cannot hand out a
    lockout that never ends or end one early.
    """

    def __init__(self) -> None:
        self._seen: dict[str, _Attempts] = {}

    def locked_for(self, ip: str, *, now: float | None = None) -> int:
        """Seconds this IP must wait, or 0. The value for Retry-After."""
        moment = now if now is not None else time.monotonic()
        self._sweep(moment)
        record = self._seen.get(ip)
        if record is None:
            return 0

        if record.locked_until > moment:
            return max(1, int(record.locked_until - moment))

        recent = [at for at in record.failures if at > moment - BURST_WINDOW_SECONDS]
        if len(recent) >= BURST_FAILURES:
            return max(1, int(BURST_WINDOW_SECONDS - (moment - min(recent))))
        return 0

    def record_failure(self, ip: str, *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        record = self._seen.setdefault(ip, _Attempts())
        record.failures.append(moment)
        record.failures = [
            at for at in record.failures if at > moment - LOCKOUT_WINDOW_SECONDS
        ]
        if len(record.failures) >= LOCKOUT_FAILURES:
            # Extends on continued attempts rather than resetting, because a
            # lockout that a further guess shortens is not a lockout.
            record.locked_until = moment + LOCKOUT_SECONDS

    def record_success(self, ip: str) -> None:
        """A correct token clears the record. It was me mistyping."""
        self._seen.pop(ip, None)

    def _sweep(self, moment: float) -> None:
        """Drop IPs with nothing recent, so the dict cannot grow without bound.

        The only thing standing between a long-running process and one entry per
        source address ever seen.
        """
        cutoff = moment - LOCKOUT_WINDOW_SECONDS
        stale = [
            ip
            for ip, record in self._seen.items()
            if record.locked_until <= moment
            and not any(at > cutoff for at in record.failures)
        ]
        for ip in stale:
            del self._seen[ip]

    def tracked(self) -> int:
        """How many IPs are being remembered. For the test that pins the sweep."""
        return len(self._seen)


# ---------------------------------------------------------------------------
# the exchange
# ---------------------------------------------------------------------------


class AuthFailed(Exception):
    """The token was wrong. Deliberately says nothing else."""


class RateLimited(Exception):
    """Too many failures from one address."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"too many attempts; retry in {retry_after}s")
        self.retry_after = retry_after


def exchange(
    conn: sqlite3.Connection,
    config: Config,
    presented: str,
    *,
    ip: str,
    user_agent: str | None,
    limiter: RateLimiter,
) -> str:
    """Trade the token for a new session id, or refuse.

    `compare_digest` rather than `==` because the comparison is the whole of the
    authentication, and a length-independent one is free to write.
    """
    waiting = limiter.locked_for(ip)
    if waiting:
        log.warning("api auth: %s is rate limited, %ss remaining", ip, waiting)
        raise RateLimited(waiting)

    expected = api_token(config)
    if not hmac.compare_digest(presented.strip(), expected):
        limiter.record_failure(ip)
        # The rate is the signal on a public box, so every failure is a line.
        log.warning("api auth: wrong token from %s", ip)
        raise AuthFailed("that token is not the one in .env")

    limiter.record_success(ip)
    store.sweep_api_sessions(conn)
    session_id = secrets.token_urlsafe(32)
    store.create_api_session(
        conn, session_id, expires_at=_expiry_iso(), user_agent=user_agent
    )
    log.info("api auth: new session for %s", ip)
    return session_id


def resolve(conn: sqlite3.Connection, session_id: str | None) -> sqlite3.Row | None:
    """The live session behind a cookie, sliding its expiry. None when there is none."""
    if not session_id:
        return None
    return store.touch_api_session(conn, session_id, expires_at=_expiry_iso())


def revoke(conn: sqlite3.Connection, session_id: str | None) -> bool:
    if not session_id:
        return False
    return store.delete_api_session(conn, session_id)


def cookie_arguments(config: Config) -> dict[str, object]:
    """What `set_cookie` is given, in one place so it cannot drift per route.

    SameSite=Lax rather than Strict: Strict would drop the cookie when the app
    is opened from a link in Telegram, which is exactly how 5c's primary action
    is reached in the interim. Lax still blocks the cross-site POST that CSRF
    needs, and the Origin check in `deps.py` covers the rest.
    """
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "secure": config.api_secure_cookie,
        "samesite": "lax",
        "path": "/",
        "max_age": SESSION_DAYS * 24 * 60 * 60,
    }
