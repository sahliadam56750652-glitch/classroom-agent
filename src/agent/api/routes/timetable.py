"""The weekly pattern resolved against what actually happened to each date.

Two rules from PLAN.md are visible in the shape of this module.

**The file holds the pattern and keeps one writer.** `GET /api/timetable/file`
serves it and there is no PUT. A `PUT` returns 405 with a body saying why and what
to do instead -- a 404 would read as a missing feature and invite someone to add
it, which is how a second source of truth gets introduced by accident.

**An orphan is never applied, never silently dropped, and always printed.** So
`orphans` is on every response from this router and is not filterable. A stored
adjustment that names nothing the file still contains is a fact I need in front of
me, not a row to tidy away.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel

from ...db import store
from ...gate import adjustments as adjust
from .. import convert, schemas
from ..deps import Db, Session, Table

router = APIRouter()

# A fortnight at a time. Long enough to answer "what does next week look like",
# short enough that resolving every date's adjustments stays one query per day.
DEFAULT_DAYS = 14
MAX_DAYS = 120


def _parse(value: str | None, fallback: date, flag: str) -> date:
    if value is None:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{flag} must look like 2026-09-21, got {value!r}.",
        ) from err


@router.get("/timetable", response_model=schemas.TimetableOut)
def timetable(
    conn: Db,
    table: Table,
    session: Session,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
) -> schemas.TimetableOut:
    """Each day's real sessions, each with why it is there if not the pattern.

    Both what is happening and what is not: `sessions` is `adjustments.sessions_on`
    and `departed` is `adjustments.departures`, which answers "why is this day
    shorter than the printed timetable". A cancelled lecture that simply vanished
    would leave the difference unexplained, and an unexplained difference is
    indistinguishable from a bug in the resolver.
    """
    start = _parse(from_date, date.today(), "from")
    end = _parse(to_date, start + timedelta(days=DEFAULT_DAYS - 1), "to")
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'to' is before 'from'.",
        )
    span = (end - start).days + 1
    if span > MAX_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"that is {span} days; {MAX_DAYS} is the most this returns at once.",
        )

    days = []
    for offset in range(span):
        day = start + timedelta(days=offset)
        version = table.version_for(day)
        excused = table.exception_for(day)
        resolved = adjust.sessions_on(conn, table, day)
        gone = adjust.departures(conn, table, day)
        days.append(
            schemas.DayOut(
                date=day.isoformat(),
                version_label=version.label if version else None,
                provisional=version.provisional if version else False,
                sessions=[
                    convert.session(item.session, item.note) for item in resolved
                ],
                departed=[
                    convert.session(item.session, adjust.departure_note(item))
                    for item in gone
                ],
                exception=excused.reason if excused else "",
            )
        )

    return schemas.TimetableOut(
        path=str(table.path),
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        days=days,
        orphans=[convert.orphan(value) for value in adjust.orphans(conn, table)],
        subjects=dict(table.subjects),
    )


@router.get("/timetable/file")
def timetable_file(table: Table, session: Session) -> Response:
    """The pattern as it is on disk, read-only.

    Served as text rather than parsed, because what I want to see is the file I
    edit -- comments, ordering and all. Parsing and re-emitting it would show me
    something that merely means the same thing.
    """
    try:
        body = table.path.read_text(encoding="utf-8")
    except OSError as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{table.path.name} could not be read: {err}",
        ) from err
    return Response(content=body, media_type="text/yaml; charset=utf-8")


@router.api_route("/timetable/file", methods=["PUT", "POST", "PATCH", "DELETE"])
def timetable_file_is_read_only() -> Response:
    """405, with the reason and the alternative.

    Deliberately not a 404. The file holds the weekly PATTERN and keeps exactly
    one writer -- me, in an editor -- which is what Phase 3a settled when it
    removed the `timetable` table. A one-off change is not a change to the
    pattern: it is one dated fact about one date, and it belongs in
    `timetable_adjustments`.

    A 404 here would read as "not built yet" and invite someone to build it,
    which is precisely how one source of truth acquires a second writer.
    """
    raise HTTPException(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        detail=(
            "timetable.yaml is never written by this project. It holds the weekly "
            "pattern and keeps one writer, which is what keeps it a single source "
            "of truth.\n"
            "  A professor moving one Tuesday is not a change to the pattern -- it "
            "is one dated fact about one Tuesday. POST /api/adjustments.\n"
            "  A permanent change IS a change to the pattern: add a version with "
            "an effective_from, by hand, in the file."
        ),
    )


# ---------------------------------------------------------------------------
# adjustments -- one dated fact about one date
# ---------------------------------------------------------------------------


class AdjustmentIn(BaseModel):
    """A dated change as asked for. The dates and times arrive as strings.

    `kind` is the stored spelling, not the flag: `agent adjust --move` records
    'moved', and deriving one from the other by string surgery turned into a crash
    once already.
    """

    kind: Literal["cancelled", "moved", "extra"]
    subject: str
    on: str
    at: str | None = None
    to_date: str | None = None
    to_time: str | None = None
    end: str | None = None
    to_kind: str | None = None
    room: str | None = None
    teacher: str | None = None
    reason: str | None = None


class RepointIn(BaseModel):
    subject: str


class RecordedOut(BaseModel):
    """What was recorded, and what it does, as facts rather than as a sentence."""

    id: int | None
    kind: str
    subject: str
    applies_on: str
    session_start: str
    joint_subjects: list[str] = []
    lands_on: str | None = None
    gate_moves: bool = False
    gate_evening: str | None = None
    written: bool = True
    note: str = ""


@router.get("/adjustments", response_model=list[schemas.AdjustmentOut])
def adjustments(
    conn: Db,
    session: Session,
    include_past: bool = Query(False),
) -> list[schemas.AdjustmentOut]:
    rows = store.list_adjustments(conn, include_past=include_past)
    return [convert.adjustment(row) for row in rows]


@router.post("/adjustments", response_model=RecordedOut, status_code=201)
def record_adjustment(
    body: AdjustmentIn,
    conn: Db,
    table: Table,
    session: Session,
    dry_run: bool = Query(False),
) -> RecordedOut:
    """Record what a professor did to one session on one date.

    `timetable.yaml` is never written. This records the dated facts the pattern
    cannot express, and the gate resolves the two together.

    A clash is REFUSED, not merged, and that is a 409 rather than a silent
    overwrite: which of two adjustments on one session on one date wins is not a
    question anything here can answer, and guessing discards one of them.
    """
    try:
        plan = adjust.plan(
            table,
            adjust.AdjustmentSpec(
                kind=body.kind,
                subject=body.subject,
                on=body.on,
                at=body.at,
                to_date=body.to_date,
                to_time=body.to_time,
                end=body.end,
                to_kind=body.to_kind,
                room=body.room,
                teacher=body.teacher,
                reason=body.reason,
            ),
        )
    except adjust.AdjustmentError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)
        ) from err

    shared = (
        f"this session is {' + '.join(plan.joint_subjects)} -- both are affected. "
        if plan.joint
        else ""
    )
    if dry_run:
        return RecordedOut(
            id=None,
            kind=plan.kind,
            subject=plan.subject,
            applies_on=plan.day.isoformat(),
            session_start=plan.start,
            joint_subjects=list(plan.joint_subjects),
            lands_on=plan.landing.isoformat() if plan.landing else None,
            gate_moves=plan.gate_moves,
            gate_evening=plan.gate_evening.isoformat() if plan.gate_evening else None,
            written=False,
            note=f"{shared}dry run -- nothing written",
        )

    adjustment_id = adjust.record(conn, plan)
    if adjustment_id is None:
        clash = adjust.find_clash(conn, plan)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"already adjusted: #{clash['id']} {clash['kind']} this session on "
                f"that date. Remove it first if this replaces it -- which of two "
                f"wins is not a question anything here can answer, so neither is "
                f"guessed."
            ),
        )

    moved = (
        f"The prompt for it moves to {plan.gate_evening:%a %d %b} evening. "
        if plan.gate_moves and plan.gate_evening
        else ""
    )
    return RecordedOut(
        id=adjustment_id,
        kind=plan.kind,
        subject=plan.subject,
        applies_on=plan.day.isoformat(),
        session_start=plan.start,
        joint_subjects=list(plan.joint_subjects),
        lands_on=plan.landing.isoformat() if plan.landing else None,
        gate_moves=plan.gate_moves,
        gate_evening=plan.gate_evening.isoformat() if plan.gate_evening else None,
        note=f"{shared}{moved}{table.path.name} is unchanged.",
    )


@router.delete("/adjustments/{adjustment_id}", response_model=RecordedOut)
def remove_adjustment(
    adjustment_id: int, conn: Db, table: Table, session: Session
) -> RecordedOut:
    """Drop one. The pattern reasserts itself -- nothing in the file ever moved."""
    row = adjust.remove(conn, adjustment_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no adjustment with id {adjustment_id}.",
        )
    return RecordedOut(
        id=adjustment_id,
        kind=str(row["kind"]),
        subject=str(row["subject"]),
        applies_on=str(row["applies_on"]),
        session_start=str(row["session_start"]),
        note="removed. The pattern reasserts itself -- nothing in the file ever moved.",
    )


@router.post("/adjustments/{adjustment_id}/repoint", response_model=RecordedOut)
def repoint_adjustment(
    adjustment_id: int,
    body: RepointIn,
    conn: Db,
    table: Table,
    session: Session,
) -> RecordedOut:
    """Move an orphan onto a subject the file still has.

    The only way an orphan ever moves. Matching approximately is what adjusts the
    wrong session while looking like it worked, so the new name is resolved
    exactly or refused.
    """
    try:
        moved = adjust.repoint(conn, table, adjustment_id, body.subject)
    except adjust.AdjustmentError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)
        ) from err
    if moved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no adjustment with id {adjustment_id}.",
        )

    was, now = moved
    row = store.get_adjustment(conn, adjustment_id)
    assert row is not None
    return RecordedOut(
        id=adjustment_id,
        kind=str(row["kind"]),
        subject=now,
        applies_on=str(row["applies_on"]),
        session_start=str(row["session_start"]),
        note=f"repointed {adjustment_id}: {was!r} -> {now!r}",
    )
