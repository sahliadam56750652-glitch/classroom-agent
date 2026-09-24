"""Subjects and coverage — DESIGN.md section 3.

Counts, and a state. **No percentage, anywhere**, and its absence is the
enforcement rather than a note in a review: DESIGN.md forbids a percentage of a
deficit, and forbids 0% or 100% for a subject with no readable material. A client
cannot render a field the server never sends.

Every figure comes from `scheduler.standing`, which is what `agent subjects
--standing` prints. That is deliberate: "the API returns what a command already
computes" is only true if a command computes it, and a figure I cannot get at a
terminal is one I cannot check the web client against.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ... import scope as scope_mod
from ...gate import scheduler as gate_scheduler
from .. import convert, schemas
from ..deps import Conf, Db, Session, Table

router = APIRouter()


@router.get("/subjects", response_model=list[schemas.SubjectOut])
def subjects(
    conn: Db, config: Conf, table: Table, session: Session
) -> list[schemas.SubjectOut]:
    """Every subject this semester names, and where each one stands.

    Every subject, including the ones that can never be gated. A subject running
    with no Classroom is a third of my week, and leaving it out would make the
    screen a report on the two-thirds that happen to have an API.
    """
    found = gate_scheduler.standing(conn, scope_mod.local(config, table), table)
    return [convert.subject(value) for value in found]


@router.get("/subjects/{name}", response_model=schemas.SubjectOut)
def subject(
    name: str, conn: Db, config: Conf, table: Table, session: Session
) -> schemas.SubjectOut:
    """One subject by the name the file spells it with.

    Case-insensitive, because it arrives from a URL, and exact otherwise: a
    near-match would report the wrong subject's backlog and look exactly like this
    working. The same rule the subjects map itself follows.
    """
    found = gate_scheduler.standing(conn, scope_mod.local(config, table), table)
    for value in found:
        if value.name.casefold() == name.casefold():
            return convert.subject(value)
    known = ", ".join(sorted(table.subjects)) or "(none)"
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"{name!r} is not a subject in {table.path.name}, and names are never "
            f"matched approximately. Known subjects: {known}"
        ),
    )
