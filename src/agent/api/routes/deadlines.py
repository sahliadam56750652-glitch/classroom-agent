"""Deadlines, and the events already sent about them.

`due_at` is a UTC instant and there is no other field about time. No severity, no
countdown, no hours remaining. DESIGN.md forbids manufactured urgency, so the API
hands over the instant and the client decides whether it has passed -- and the one
red in this app is a deadline already gone.

The scanner is deliberately not reachable from here. `deadlines.candidates` is the
reading half; `deadlines.scan` decides which thresholds are owed an event and is
the writing half, and it belongs to `agent run`. A GET that emitted an event would
mean the T-72/24/3 alerts fired when I happened to open a page.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ... import scope as scope_mod
from ...db import store
from ...sync import deadlines as deadlines_mod
from .. import convert, schemas
from ..deps import Conf, Db, MaybeTable, Session

router = APIRouter()


@router.get("/deadlines", response_model=list[schemas.DeadlineOut])
def upcoming(
    conn: Db,
    config: Conf,
    table: MaybeTable,
    session: Session,
    include_done: bool = Query(False),
) -> list[schemas.DeadlineOut]:
    """Everything due, merged across coursework, manual tasks and projects.

    One list from one function, because `sync/deadlines.py` already merges the
    three and links a project to the coursework it was eventually posted as. A
    second merge here would be a second answer to "how many deadlines are there".

    What carries no due date is where the two halves of the corpus differ, and
    this reports what the scanner sees rather than smoothing it over. Coursework
    with no `dueDate` is still a candidate -- 25 of 46 measured items have none, so
    it is the majority case and `due_at` comes back null. A manual task with no
    `--due` is NOT, because `_manual_task_candidates` filters it out: nothing
    undated can ever cross a T-72/24/3 threshold, so the scanner has no use for
    it.

    That asymmetry is the scanner's, not this route's, and it is not smoothed over
    here. An undated task is a real row and belongs on `/api/tasks`, which lists
    tasks; this lists deadlines, and a task with no date does not have one.
    """
    local = scope_mod.local(config, table)
    found = deadlines_mod.candidates(conn, sorted(local))
    if not include_done:
        found = [value for value in found if not value.done]
    # Undated last, then soonest first. A stable order so the screen does not
    # reshuffle between reads.
    found.sort(key=lambda value: (value.due_at is None, value.due_at or "", value.title))
    return [convert.deadline(value) for value in found]


@router.get("/events", response_model=list[schemas.EventOut])
def events(
    conn: Db,
    session: Session,
    limit: int = Query(50, ge=1, le=500),
    include_notified: bool = Query(True),
) -> list[schemas.EventOut]:
    """What has been reported, and what is still waiting to be.

    Here because DESIGN.md forbids a state the app counts but will not show me.
    `notified_at` is the whole of invariant 3, and a figure resting on it that I
    cannot inspect is a figure I will not trust.
    """
    rows = store.list_events(conn, include_notified=include_notified, limit=limit)
    return [convert.event(row) for row in rows]
