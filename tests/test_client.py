"""Classroom client: pagination and retry classification, both stubbed offline."""

from __future__ import annotations

import json

import pytest
from googleapiclient.errors import HttpError

from agent.classroom import client as client_module
from agent.classroom.client import ClassroomClient, execute, is_retryable


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class FakeService:
    """Self-returning node: courses().courseWork().studentSubmissions().list(...).

    Records every call so tests can assert on pagination parameters.
    """

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[dict] = []

    def __getattr__(self, _name):
        return lambda: self

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self.pages[len(self.calls) - 1])


def make_client(monkeypatch, pages):
    service = FakeService(pages)
    monkeypatch.setattr(client_module, "build", lambda *a, **k: service)
    return ClassroomClient(credentials=None, sleep=lambda _seconds: None), service


def http_error(status, reason=None):
    body = {"error": {"code": status, "message": "nope"}}
    if reason:
        body["error"]["errors"] = [{"reason": reason}]

    class Resp:
        def __init__(self):
            self.status = status
            self.reason = "error"

    return HttpError(Resp(), json.dumps(body).encode("utf-8"))


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------

def test_pagination_follows_next_page_token(monkeypatch):
    """A missing loop is not an error, it is a shorter list -- hence this test."""
    pages = [
        {"courses": [{"id": "1"}, {"id": "2"}], "nextPageToken": "p2"},
        {"courses": [{"id": "3"}], "nextPageToken": "p3"},
        {"courses": [{"id": "4"}]},
    ]
    client, service = make_client(monkeypatch, pages)

    courses = client.list_courses(["ACTIVE"])

    assert [c["id"] for c in courses] == ["1", "2", "3", "4"]
    assert len(service.calls) == 3
    assert "pageToken" not in service.calls[0]
    assert service.calls[1]["pageToken"] == "p2"
    assert service.calls[2]["pageToken"] == "p3"


def test_single_page_makes_one_call(monkeypatch):
    client, service = make_client(monkeypatch, [{"courses": [{"id": "1"}]}])
    assert len(client.list_courses(["ACTIVE"])) == 1
    assert len(service.calls) == 1


def test_empty_response_yields_no_items(monkeypatch):
    """Classroom returns a bare {} rather than an empty list."""
    client, _ = make_client(monkeypatch, [{}])
    assert client.list_courses(["ACTIVE"]) == []


def test_page_size_is_requested(monkeypatch):
    client, service = make_client(monkeypatch, [{"courses": []}])
    client.list_courses(["ACTIVE"])
    assert service.calls[0]["pageSize"] == client_module.MAX_PAGE_SIZE


# --------------------------------------------------------------------------
# per-endpoint parameters
# --------------------------------------------------------------------------

def test_course_states_are_passed_through(monkeypatch):
    client, service = make_client(monkeypatch, [{"courses": []}])
    client.list_courses(["ACTIVE", "ARCHIVED"])
    assert service.calls[0]["courseStates"] == ["ACTIVE", "ARCHIVED"]


def test_states_cannot_be_omitted():
    """Defaulting would silently drop archived courses -- 7 of 25 measured."""
    import inspect

    signature = inspect.signature(ClassroomClient.list_courses)
    assert signature.parameters["states"].default is inspect.Parameter.empty


def test_empty_states_is_rejected(monkeypatch):
    client, _ = make_client(monkeypatch, [{"courses": []}])
    with pytest.raises(ValueError):
        client.list_courses([])


def test_submissions_use_the_wildcard_coursework_id(monkeypatch):
    """One call per course, not one per assignment."""
    client, service = make_client(monkeypatch, [{"studentSubmissions": [{"id": "s1"}]}])

    client.list_submissions("c1")

    assert service.calls[0]["courseWorkId"] == "-"
    assert service.calls[0]["userId"] == "me"
    assert service.calls[0]["courseId"] == "c1"


@pytest.mark.parametrize(
    "method,key",
    [
        ("list_coursework", "courseWork"),
        ("list_coursework_materials", "courseWorkMaterial"),
        ("list_announcements", "announcements"),
        ("list_topics", "topic"),
    ],
)
def test_per_course_endpoints_paginate_and_unwrap(monkeypatch, method, key):
    pages = [{key: [{"id": "1"}], "nextPageToken": "p2"}, {key: [{"id": "2"}]}]
    client, service = make_client(monkeypatch, pages)

    items = getattr(client, method)("c1")

    assert [item["id"] for item in items] == ["1", "2"]
    assert service.calls[0]["courseId"] == "c1"


# --------------------------------------------------------------------------
# retry classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_retry(status):
    assert is_retryable(http_error(status)) is True


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_403_rate_limits_retry(reason):
    assert is_retryable(http_error(403, reason)) is True


@pytest.mark.parametrize(
    "reason",
    ["forbidden", "insufficientPermissions", "accessNotConfigured", None],
)
def test_403_auth_and_policy_failures_do_not_retry(reason):
    """Retrying these just walks into the same wall and buries the real reason."""
    assert is_retryable(http_error(403, reason)) is False


@pytest.mark.parametrize("status", [400, 401, 404, 412])
def test_other_failures_do_not_retry(status):
    assert is_retryable(http_error(status)) is False


def test_unparseable_error_body_does_not_crash_the_classifier():
    class Resp:
        status = 403
        reason = "error"

    assert is_retryable(HttpError(Resp(), b"<html>not json</html>")) is False


# --------------------------------------------------------------------------
# execute()
# --------------------------------------------------------------------------

class FlakyRequest:
    def __init__(self, failures, error):
        self.failures = failures
        self.attempts = 0
        self._error = error

    def execute(self):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self._error
        return {"ok": True}


def test_execute_retries_a_rate_limit_then_succeeds():
    slept: list[float] = []
    request = FlakyRequest(2, http_error(429))

    assert execute(request, sleep=slept.append) == {"ok": True}
    assert request.attempts == 3
    assert len(slept) == 2
    assert slept[0] < slept[1]  # exponential, not flat


def test_execute_raises_immediately_on_a_permission_error():
    slept: list[float] = []
    request = FlakyRequest(1, http_error(403, "insufficientPermissions"))

    with pytest.raises(HttpError):
        execute(request, sleep=slept.append)

    assert request.attempts == 1
    assert slept == []


def test_execute_gives_up_after_the_attempt_limit():
    request = FlakyRequest(99, http_error(503))

    with pytest.raises(HttpError):
        execute(request, sleep=lambda _s: None)

    assert request.attempts == client_module.MAX_ATTEMPTS
