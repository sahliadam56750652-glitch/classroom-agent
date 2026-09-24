"""What to study now — DESIGN.md sections 2 and 4.

The default screen is one item, not a dashboard. I already know I am behind; that
is why I opened the app. What I do not know at 23:00, and what this is uniquely
able to answer, is *which twenty pages*.

> **Computing a plan is not firing the gate.** Nothing here writes a `gate_runs`
> row. If opening the dashboard at 06:00 created one, `gate_runs.for_date` would
> let it silently swallow that evening's real prompt -- the same failure that
> keeps `agent gate` out of `agent run`, arriving through a GET instead of a
> schedule. A test asserts `gate_runs` is untouched by every read here.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status

from ... import scope as scope_mod
from ...config import Config, display_zone
from ...db import store
from ...gate import scheduler as gate_scheduler
from ...gate import sections
from ...gate.timetable import Timetable
from .. import convert, schemas
from ..deps import Conf, Db, Session, Table

router = APIRouter()

# The starting value from 3d, and an argument everywhere rather than a constant
# anywhere: how many pages a session actually covers is a guess until a real
# session happens. Overridable per request so the boundaries stay judgeable by
# eye, exactly as `agent sections --pages N` is.
DEFAULT_WINDOW_PAGES = sections.DEFAULT_BUDGET


def _tomorrow(config: Config) -> date:
    """The next calendar day, locally.

    Local, not UTC: "tomorrow" is a wall-clock fact, and at 20:00 Africa/Tunis
    the two already disagree about which day it is for part of the year.
    """
    return datetime.now(display_zone(config.timezone)).date() + timedelta(days=1)


def _parse_date(value: str | None, config: Config) -> date:
    if value is None:
        return _tomorrow(config)
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"date must look like 2026-09-21, got {value!r}.",
        ) from err


def _windows_for(
    conn: sqlite3.Connection,
    config: Config,
    item: gate_scheduler.Item,
    budget: int,
) -> list[schemas.WindowOut]:
    """Every file of this post, cut into evening-sized runs.

    Read-only context, which is all 3d stage 1 built: nothing acts on a window
    yet, and the client shows the boundary rather than pretending the whole
    92-page post is tonight's ask.

    A file whose text is recorded but missing from disk contributes no windows and
    is not an error here -- `/api/study-items/{id}` names it in `files`, where a
    missing document is visible as itself rather than as an absence of windows.
    """
    found: list[schemas.WindowOut] = []
    for row in store.study_item_sources(conn, item.entity_type, item.entity_id):
        document = sections.read_document(
            row,
            config.library_dir,
            store.ocr_pages_for(conn, str(row["drive_id"])),
            budget=budget,
        )
        if document is None:
            continue
        found.extend(convert.window(value) for value in document.windows)
    return found


@router.get("/now", response_model=schemas.NextOut)
def now(
    conn: Db,
    config: Conf,
    table: Table,
    session: Session,
    pages: int = Query(DEFAULT_WINDOW_PAGES, ge=1, le=500),
) -> schemas.NextOut:
    """The next single action, or the honest absence of one.

    The empty state is `waiting: false` with no item -- the absence of a thing to
    do, not an empty list dressed up as content. That is `composer.compose()`
    returning None expressed over HTTP, and the reason is the same: an interface
    that performs busyness when there is nothing to say trains me to stop reading
    it, and then the screen that mattered goes with it.
    """
    for_date = _tomorrow(config)
    plan = gate_scheduler.plan_for(
        conn, scope_mod.local(config, table), table, for_date
    )

    if not plan.worth_sending:
        upcoming = plan.sessions[0] if plan.sessions else None
        return schemas.NextOut(
            waiting=False,
            for_date=for_date.isoformat(),
            next_session=(
                convert.session(upcoming, plan.note_for(upcoming))
                if upcoming
                else None
            ),
            silent_because=plan.silent_because,
        )

    subject = plan.actionable[0]
    item = subject.next_item
    assert item is not None  # actionable means it has items

    # One subordinate line, and nowhere else. A deficit with no affordance
    # attached is not information.
    deficit = f"{subject.name} · {len(subject.items)} unreviewed"

    return schemas.NextOut(
        waiting=True,
        for_date=for_date.isoformat(),
        deficit=deficit,
        subject=convert.subject(subject),
        item=convert.item(item),
        windows=_windows_for(conn, config, item, pages),
    )


@router.get("/gate", response_model=schemas.GateOut)
def gate(
    conn: Db,
    config: Conf,
    table: Table,
    session: Session,
    date_: str | None = Query(None, alias="date"),
) -> schemas.GateOut:
    """The whole plan for a date. What `agent gate --dry-run` prints, as data.

    Reading this does not record that a prompt was sent, and must not start to.
    """
    for_date = _parse_date(date_, config)
    plan = gate_scheduler.plan_for(
        conn, scope_mod.local(config, table), table, for_date
    )
    return convert.gate_plan(plan)


@router.get("/study-items/{item_id}", response_model=schemas.StudyItemOut)
def study_item(
    item_id: int,
    conn: Db,
    config: Conf,
    session: Session,
    pages: int = Query(DEFAULT_WINDOW_PAGES, ge=1, le=500),
) -> schemas.StudyItemOut:
    """One item, whatever its state, with its files and its window boundaries.

    Read through `store.backlog_item`, which is the same readiness arithmetic
    `gate_backlog` applies -- so an item looks identical here and in the evening
    prompt. `agent quiz --item N` reads it the same way for the same reason.
    """
    row = store.backlog_item(conn, item_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no study item with id {item_id}.",
        )

    item = gate_scheduler.item_by_id(conn, item_id)
    assert item is not None  # backlog_item found the row
    files = store.study_item_files(conn, item.entity_type, item.entity_id)

    return schemas.StudyItemOut(
        item_id=item_id,
        course_id=str(row["course_id"]),
        course_name=str(row["course_name"] or row["course_id"]),
        item=convert.item(item),
        windows=_windows_for(conn, config, item, pages),
        files=[convert.document(found) for found in files],
    )
