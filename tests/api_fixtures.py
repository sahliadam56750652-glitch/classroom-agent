"""Shared scaffolding for the API tests.

A module rather than a conftest, because every other test file in this project
carries its own fixtures and one convention is easier to follow than two.
Imported by name from each `test_api_*.py`.

There is no `monkeypatch.setattr(store, "open_db", ...)` here, and that absence
is the point: the API opens a connection per request from `config.db_path`, so
pointing the config at a temp file is the whole of the isolation. A test that had
to intercept `open_db` would be testing something other than what runs.
"""

from __future__ import annotations

import os

from agent.api.app import create_app
from agent.classroom.models import parse_course
from agent.config import API_TOKEN_ENV, Config
from agent.db import store

TOKEN = "test-token-" + "z" * 32
COURSE = "842149328479"
MANUAL = "manual-calculus-iii"

PATTERN = """
subjects:
  Calculus III: manual-calculus-iii
  Database: "842149328479"
  Probability: null

versions:
  - label: S1 2026-27
    status: confirmed
    effective_from: 2026-09-15
    effective_to: 2027-01-23
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC,
          subject: Database, teacher: Gharbi, room: Lab3 }
      - { day: tue, start: "13:00", end: "14:30", kind: TUT,
          subject: Calculus III, teacher: Yassine }
      - { day: wed, start: "10:30", end: "12:00", kind: LEC,
          subject: Probability, teacher: Slim }
"""


def make_config(tmp_path, *, pattern: str = PATTERN, **overrides) -> Config:
    """A Config pointing entirely inside tmp_path. Invariant 5, as a fixture."""
    data_dir = tmp_path / "data"
    (data_dir / "library").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    timetable = tmp_path / "timetable.yaml"
    timetable.write_text(pattern, encoding="utf-8")
    fields = {
        "account": "someone@example.com",
        "timezone": "Africa/Tunis",
        "data_dir": data_dir,
        "tracked_courses": [COURSE],
        "ignored_courses": [],
        "timetable_path_override": timetable,
        # Off by default in tests: the TestClient speaks http, and a Secure
        # cookie over http is dropped by the client, which would make every
        # authenticated test fail for a reason that has nothing to do with it.
        "api_secure_cookie": False,
    }
    fields.update(overrides)
    return Config(**fields)


def seed(config: Config) -> None:
    """The two courses every screen expects to exist."""
    conn = store.connect(config.db_path)
    store.upsert_course(conn, parse_course({"id": COURSE, "name": "Database GA 2026"}))
    store.ensure_manual_course(conn, MANUAL, "Calculus III")
    conn.commit()
    conn.close()


def build(config: Config, monkeypatch, *, token: str = TOKEN):
    """The app, with the token in the environment where `api_token` reads it."""
    monkeypatch.setenv(API_TOKEN_ENV, token)
    return create_app(config)


def client(config, monkeypatch, *, token: str = TOKEN, sign_in: bool = True):
    """A TestClient, signed in unless asked not to be."""
    from starlette.testclient import TestClient

    app = build(config, monkeypatch, token=token)
    session = TestClient(app)
    if sign_in:
        response = session.post("/api/session", json={"token": token})
        assert response.status_code == 200, response.text
    return session


def env_without_token(monkeypatch) -> None:
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    os.environ.pop(API_TOKEN_ENV, None)
