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

from fastapi import APIRouter, HTTPException, Query, Response, status

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
