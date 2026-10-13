"""The snapshot: everything small the phone needs, in one answer. Phase 5e.

The backend is this PC and runs only while it is on, so the phone is not a
client that can fall back on a server -- it is the copy I study from. Each sync
replaces the phone's snapshot WHOLE: it is tens of kilobytes, and replacing it
whole means there is no merge logic to get wrong.

**Nothing here computes anything.** Every entry is the answer of the GET route
the client would otherwise have called, invoked in-process with the same
arguments and encoded the same way. A test compares each entry with the real
GET, so the snapshot cannot drift from the API -- the same rule as "the API adds
no business logic", one level up.

The entries are keyed by the path the client would request, so a screen asks
for "/api/subjects" and gets it from the phone whether or not the PC is on.
Three kinds of request do not map one to one, and the client knows them:

  * Today asks for a DATE. The snapshot holds `/api/now?date=D` for today and
    the next seven days; past that, Today says it has no plan rather than
    showing an old one.
  * The timetable is one range, this week's Monday for three weeks. Screens ask
    for a slice of it and the client cuts it.
  * Anything not in the snapshot is not on the phone, and says so.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder

from ...config import display_zone
from ...db import store
from . import deadlines, entries, library, meta, quiz, study, subjects, timetable
from ..deps import Conf, Db, MaybeTable, Session

router = APIRouter()

# Today and the next seven days. A week of the PC being off is the case this
# exists for; past it, Today says the plan ran out instead of guessing.
NOW_DAYS = 8

# This week's Monday, for three weeks: the timetable screen pages a week at a
# time, and three covers this one, next, and the one after.
TIMETABLE_DAYS = 21


def _today(config) -> date:
    return datetime.now(display_zone(config.timezone)).date()


def build(conn, config, table, session) -> dict[str, Any]:
    """The snapshot, as plain JSON. Raises nothing for a missing timetable:
    the entries that need it are listed under `missing` with the reason."""
    today = _today(config)
    entries_: dict[str, Any] = {}
    missing: dict[str, str] = {}

    def put(path: str, work) -> None:
        try:
            entries_[path] = jsonable_encoder(work())
        except HTTPException as err:
            missing[path] = str(err.detail)

    def need_table(work):
        def run():
            if table is None:
                raise HTTPException(
                    status_code=503,
                    detail="timetable.yaml could not be read when this was synced.",
                )
            return work()

        return run

    # Today: one plan per date. Same window size as the screen asks for.
    for offset in range(NOW_DAYS):
        day = (today + timedelta(days=offset)).isoformat()
        put(
            f"/api/now?date={day}",
            need_table(
                lambda day=day: study.now(
                    conn, config, table, session,
                    pages=study.DEFAULT_WINDOW_PAGES, date_=day,
                )
            ),
        )

    put("/api/subjects", need_table(lambda: subjects.subjects(conn, config, table, session)))
    for row in entries_.get("/api/subjects") or []:
        name = row["name"]
        put(
            f"/api/subjects/{name}",
            need_table(lambda name=name: subjects.subject(name, conn, config, table, session)),
        )

    monday = today - timedelta(days=today.weekday())
    last = monday + timedelta(days=TIMETABLE_DAYS - 1)
    put(
        "/api/timetable",
        need_table(
            lambda: timetable.timetable(
                conn, table, session, from_date=monday.isoformat(), to_date=last.isoformat()
            )
        ),
    )

    put("/api/homework", lambda: deadlines.homework(conn, config, table, session, include_done=False))
    put("/api/deadlines", lambda: deadlines.upcoming(conn, config, table, session, include_done=False))
    put("/api/projects", lambda: entries.projects(conn, session, include_closed=False))
    for row in entries_.get("/api/projects") or []:
        put(
            f"/api/projects/{row['id']}",
            lambda pid=row["id"]: entries.project(pid, conn, session),
        )
    put("/api/library", lambda: library.library(conn, config, table, session, course=None, q=None))
    put("/api/quizzes", lambda: quiz.quizzes(conn, config, table, session))
    put("/api/status", lambda: meta.status_report(conn, config, session))

    return {
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": today.isoformat(),
        "now_days": NOW_DAYS,
        "timetable_from": monday.isoformat(),
        "timetable_to": last.isoformat(),
        "schema_version": store.schema_version(conn),
        "entries": entries_,
        "missing": missing,
    }


@router.get("/sync")
def snapshot(conn: Db, config: Conf, table: MaybeTable, session: Session) -> dict[str, Any]:
    """Everything small, in one answer. See the module docstring."""
    return build(conn, config, table, session)
