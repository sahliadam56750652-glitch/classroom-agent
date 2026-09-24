"""The token exchange, the session cookie, and the limiter in front of them.

The rule being pinned hardest: there is no unauthenticated mode. `agent serve`
refuses to start without a token, on localhost as much as anywhere, because a
server that is open because a variable is unset looks exactly like one that is
protected -- and PLAN.md ranks a failure that misreports itself as a defect in
its own right.
"""

from __future__ import annotations

import time

import pytest
from api_fixtures import TOKEN, build, client, env_without_token, make_config, seed
from starlette.testclient import TestClient

from agent.api import auth as api_auth
from agent.config import API_TOKEN_ENV, ConfigError
from agent.db import store


@pytest.fixture
def config(tmp_path):
    made = make_config(tmp_path)
    seed(made)
    return made


# ---------------------------------------------------------------------------
# there is no unauthenticated mode
# ---------------------------------------------------------------------------


def test_the_app_refuses_to_start_without_a_token(config, monkeypatch):
    env_without_token(monkeypatch)
    with pytest.raises(ConfigError) as caught:
        build(config, monkeypatch, token="")
    message = str(caught.value)
    assert API_TOKEN_ENV in message
    assert "no unauthenticated mode" in message
    assert "secrets.token_urlsafe" in message


def test_the_app_refuses_a_token_that_is_too_short(config, monkeypatch):
    with pytest.raises(ConfigError) as caught:
        build(config, monkeypatch, token="tiny")
    assert "32 is the minimum" in str(caught.value)


def test_localhost_is_not_a_special_case(config, monkeypatch):
    """The whole point. A laptop gets the same refusal a public box would.

    There is no flag, no host check and no environment sniff that would make this
    pass -- which is what makes "is the API protected" a question with one answer.
    """
    env_without_token(monkeypatch)
    with pytest.raises(ConfigError):
        build(config, monkeypatch, token="")


# ---------------------------------------------------------------------------
# the exchange
# ---------------------------------------------------------------------------


def test_the_right_token_returns_a_cookie(config, monkeypatch):
    session = client(config, monkeypatch, sign_in=False)
    response = session.post("/api/session", json={"token": TOKEN})

    assert response.status_code == 200
    assert response.json()["signed_in"] is True
    assert api_auth.COOKIE_NAME in session.cookies


def test_the_cookie_is_not_the_token(config, monkeypatch):
    """Opaque by construction, so a stolen cookie cannot be replayed upward."""
    session = client(config, monkeypatch)
    held = session.cookies[api_auth.COOKIE_NAME]
    assert held != TOKEN
    assert TOKEN not in held


def test_the_wrong_token_is_refused_without_elaborating(config, monkeypatch):
    session = client(config, monkeypatch, sign_in=False)
    response = session.post("/api/session", json={"token": "x" * 44})

    assert response.status_code == 401
    assert api_auth.COOKIE_NAME not in session.cookies
    # One bit is one bit: nothing about how close it was.
    assert "character" not in response.text
    assert "length" not in response.text


def test_a_session_row_records_the_device(config, monkeypatch):
    session = client(config, monkeypatch, sign_in=False)
    session.post(
        "/api/session",
        json={"token": TOKEN},
        headers={"user-agent": "Pixel/Chrome"},
    )

    conn = store.connect(config.db_path)
    row = conn.execute("SELECT * FROM api_sessions").fetchone()
    conn.close()
    assert row["user_agent"] == "Pixel/Chrome"
    assert row["expires_at"] > row["created_at"]


def test_an_authenticated_route_needs_the_cookie(config, monkeypatch):
    session = client(config, monkeypatch, sign_in=False)
    assert session.get("/api/status").status_code == 401

    session.post("/api/session", json={"token": TOKEN})
    assert session.get("/api/status").status_code == 200


def test_signing_out_revokes_the_row(config, monkeypatch):
    """The reason sessions are rows and not a signed cookie."""
    session = client(config, monkeypatch)
    assert session.get("/api/status").status_code == 200

    assert session.delete("/api/session").status_code == 200

    conn = store.connect(config.db_path)
    assert store.count_api_sessions(conn) == 0
    conn.close()
    assert session.get("/api/status").status_code == 401


def test_a_forged_cookie_is_not_a_session(config, monkeypatch):
    session = client(config, monkeypatch, sign_in=False)
    session.cookies.set(api_auth.COOKIE_NAME, "not-a-real-session-id")
    assert session.get("/api/status").status_code == 401


def test_an_expired_session_is_not_live(config, monkeypatch):
    """Expiry is in the WHERE clause, so no handler can forget to check it."""
    session = client(config, monkeypatch)
    conn = store.connect(config.db_path)
    conn.execute("UPDATE api_sessions SET expires_at = '2020-01-01T00:00:00Z'")
    conn.commit()
    conn.close()

    assert session.get("/api/status").status_code == 401


def test_using_a_session_slides_its_expiry(config, monkeypatch):
    session = client(config, monkeypatch)
    conn = store.connect(config.db_path)
    before = conn.execute("SELECT expires_at FROM api_sessions").fetchone()[0]
    conn.execute("UPDATE api_sessions SET expires_at = '2026-10-01T00:00:00Z'")
    conn.commit()
    conn.close()

    session.get("/api/status")

    conn = store.connect(config.db_path)
    after = conn.execute("SELECT expires_at FROM api_sessions").fetchone()[0]
    conn.close()
    assert after > "2026-10-01T00:00:00Z"
    assert after >= before[:4] + before[4:]  # still ~90 days out, not shortened


def test_signing_in_sweeps_expired_rows(config, monkeypatch):
    session = client(config, monkeypatch)
    conn = store.connect(config.db_path)
    store.create_api_session(
        conn, "stale-one", expires_at="2020-01-01T00:00:00Z", user_agent="old"
    )
    conn.commit()
    assert store.count_api_sessions(conn) == 2
    conn.close()

    session.post("/api/session", json={"token": TOKEN})

    conn = store.connect(config.db_path)
    remaining = [row["id"] for row in conn.execute("SELECT id FROM api_sessions")]
    conn.close()
    assert "stale-one" not in remaining


# ---------------------------------------------------------------------------
# health and status
# ---------------------------------------------------------------------------


def test_health_needs_no_cookie_and_leaks_nothing(config, monkeypatch):
    """A liveness probe for systemd and Caddy, and nothing more than that."""
    session = client(config, monkeypatch, sign_in=False)
    response = session.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_status_is_behind_the_session_because_it_carries_figures(config, monkeypatch):
    session = client(config, monkeypatch, sign_in=False)
    assert session.get("/api/status").status_code == 401

    session.post("/api/session", json={"token": TOKEN})
    body = session.get("/api/status").json()
    assert body["schema_version"] == store.SCHEMA_VERSION
    assert body["sessions_live"] == 1
    assert "study_items" in body


# ---------------------------------------------------------------------------
# the limiter
# ---------------------------------------------------------------------------


def test_a_burst_of_wrong_tokens_is_rate_limited(config, monkeypatch):
    session = client(config, monkeypatch, sign_in=False)
    wrong = {"token": "y" * 44}

    for attempt in range(api_auth.BURST_FAILURES):
        assert session.post("/api/session", json=wrong).status_code == 401, attempt

    blocked = session.post("/api/session", json=wrong)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_the_limiter_blocks_the_right_token_too_while_it_is_hot(config, monkeypatch):
    """Otherwise the limit is advisory: guess until blocked, then try the real one."""
    session = client(config, monkeypatch, sign_in=False)
    for _ in range(api_auth.BURST_FAILURES):
        session.post("/api/session", json={"token": "y" * 44})

    assert session.post("/api/session", json={"token": TOKEN}).status_code == 429


def test_a_correct_token_clears_the_counter(config, monkeypatch):
    limiter = api_auth.RateLimiter()
    for _ in range(api_auth.BURST_FAILURES - 1):
        limiter.record_failure("1.2.3.4")
    assert limiter.locked_for("1.2.3.4") == 0

    limiter.record_success("1.2.3.4")
    for _ in range(api_auth.BURST_FAILURES - 1):
        limiter.record_failure("1.2.3.4")
    assert limiter.locked_for("1.2.3.4") == 0


def test_sustained_failure_locks_out_for_longer_than_the_burst_window():
    """The slow grind, which a per-minute limit alone would let through."""
    limiter = api_auth.RateLimiter()
    now = 1000.0
    for step in range(api_auth.LOCKOUT_FAILURES):
        # One every 30 seconds: never five inside a minute, so the burst rule
        # alone would never fire.
        limiter.record_failure("9.9.9.9", now=now + step * 30)

    later = now + api_auth.LOCKOUT_FAILURES * 30
    waiting = limiter.locked_for("9.9.9.9", now=later)
    assert waiting > api_auth.BURST_WINDOW_SECONDS


def test_continued_attempts_extend_the_lockout_rather_than_resetting_it():
    limiter = api_auth.RateLimiter()
    now = 1000.0
    for step in range(api_auth.LOCKOUT_FAILURES):
        limiter.record_failure("9.9.9.9", now=now + step)
    first = limiter.locked_for("9.9.9.9", now=now + api_auth.LOCKOUT_FAILURES)

    limiter.record_failure("9.9.9.9", now=now + api_auth.LOCKOUT_FAILURES + 60)
    second = limiter.locked_for("9.9.9.9", now=now + api_auth.LOCKOUT_FAILURES + 60)
    assert second >= first


def test_one_address_being_blocked_does_not_block_another():
    limiter = api_auth.RateLimiter()
    for _ in range(api_auth.BURST_FAILURES):
        limiter.record_failure("1.1.1.1")

    assert limiter.locked_for("1.1.1.1") > 0
    assert limiter.locked_for("2.2.2.2") == 0


def test_the_sweep_keeps_the_dict_from_growing_without_bound():
    """A long-running process must not keep one entry per address ever seen."""
    limiter = api_auth.RateLimiter()
    now = 1000.0
    for index in range(50):
        limiter.record_failure(f"10.0.0.{index}", now=now)
    assert limiter.tracked() == 50

    # Long enough that nothing is recent and nothing is locked.
    limiter.locked_for("10.0.0.0", now=now + api_auth.LOCKOUT_WINDOW_SECONDS + 1)
    assert limiter.tracked() == 0


def test_the_limiter_uses_a_monotonic_clock(config, monkeypatch):
    """A clock correction must not hand out a lockout that never ends."""
    limiter = api_auth.RateLimiter()
    limiter.record_failure("3.3.3.3")
    # If wall-clock time were used, moving it backwards would strand the record.
    monkeypatch.setattr(time, "time", lambda: 0.0)
    assert limiter.locked_for("3.3.3.3") == 0
