"""Thin wrapper over the Classroom API.

Returns raw dicts exactly as Google sent them -- parsing lives in models.py.
The only things this layer owns are pagination and retry, because getting
either wrong is invisible: a missing page looks like a course with no
coursework, and an unretried 429 looks like an empty sync.

Read-only by construction. Only .list() is ever called from here; see
invariant 6 in CLAUDE.md for why that matters more than usual.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Sequence
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

MAX_PAGE_SIZE = 100
MAX_ATTEMPTS = 5

RETRY_STATUSES = {429, 500, 502, 503, 504}

# A 403 can mean either "slow down" or "you may not do this". Only the first is
# worth retrying; retrying the second just walks into the same wall five times
# and buries the real reason.
RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


def _error_reasons(err: HttpError) -> set[str]:
    """Reason strings from an HttpError body, empty if it cannot be parsed."""
    try:
        payload = json.loads(err.content.decode("utf-8"))
    except (AttributeError, ValueError, UnicodeDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    error = payload.get("error")
    if not isinstance(error, dict):
        return set()
    return {
        item["reason"]
        for item in error.get("errors") or []
        if isinstance(item, dict) and item.get("reason")
    }


def is_retryable(err: HttpError) -> bool:
    status = getattr(err.resp, "status", None)
    if status in RETRY_STATUSES:
        return True
    if status == 403:
        return bool(_error_reasons(err) & RETRYABLE_403_REASONS)
    return False


def _backoff_seconds(attempt: int) -> float:
    """Exponential with jitter, so parallel retries do not resynchronise."""
    return min(32.0, 2.0**attempt) + random.random()


def execute(request, *, sleep=time.sleep) -> dict[str, Any]:
    """Run one API request, retrying only what is worth retrying."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return request.execute()
        except HttpError as err:
            if attempt == MAX_ATTEMPTS or not is_retryable(err):
                raise
            sleep(_backoff_seconds(attempt))
    raise AssertionError("unreachable")  # pragma: no cover


class ClassroomClient:
    """One handle on the Classroom API. Construct it with verified credentials."""

    def __init__(self, credentials, *, sleep=time.sleep):
        self._service = build("classroom", "v1", credentials=credentials, cache_discovery=False)
        self._sleep = sleep

    # -- pagination ------------------------------------------------------

    def _paginate(self, method, params: dict[str, Any], item_key: str) -> list[dict[str, Any]]:
        """Follow nextPageToken to exhaustion.

        Every Classroom list endpoint paginates. A missing loop is not an error,
        it is a shorter list -- which is why this is centralised rather than
        written out at each call site.
        """
        items: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            call = dict(params, pageSize=MAX_PAGE_SIZE)
            if page_token:
                call["pageToken"] = page_token
            response = execute(method(**call), sleep=self._sleep)
            items.extend(response.get(item_key) or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return items

    # -- courses ---------------------------------------------------------

    def list_courses(self, states: Sequence[str]) -> list[dict[str, Any]]:
        """All courses in the given states.

        `states` has no default on purpose. Omitting courseStates makes the API
        return only ACTIVE courses, so last year's archived ones vanish without
        a word -- and 7 of 25 measured courses are archived. Callers must say
        what they want.
        """
        if not states:
            raise ValueError("states must name at least one courseState")
        return self._paginate(
            self._service.courses().list,
            {"courseStates": list(states)},
            "courses",
        )

    # -- per course ------------------------------------------------------

    def list_coursework(self, course_id: str) -> list[dict[str, Any]]:
        return self._paginate(
            self._service.courses().courseWork().list,
            {"courseId": course_id},
            "courseWork",
        )

    def list_coursework_materials(self, course_id: str) -> list[dict[str, Any]]:
        return self._paginate(
            self._service.courses().courseWorkMaterials().list,
            {"courseId": course_id},
            "courseWorkMaterial",
        )

    def list_announcements(self, course_id: str) -> list[dict[str, Any]]:
        return self._paginate(
            self._service.courses().announcements().list,
            {"courseId": course_id},
            "announcements",
        )

    def list_topics(self, course_id: str) -> list[dict[str, Any]]:
        return self._paginate(
            self._service.courses().topics().list,
            {"courseId": course_id},
            "topic",
        )

    def list_submissions(self, course_id: str) -> list[dict[str, Any]]:
        """Every submission of mine in one course, in a single paginated call.

        courseWorkId='-' is the wildcard. One call per assignment instead would
        waste quota and hit rate limits on a full sync.
        """
        return self._paginate(
            self._service.courses().courseWork().studentSubmissions().list,
            {"courseId": course_id, "courseWorkId": "-", "userId": "me"},
            "studentSubmissions",
        )
