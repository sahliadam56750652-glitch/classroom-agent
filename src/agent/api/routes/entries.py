"""Everything entered by hand, over HTTP.

Every route here calls `entries.py` or `files/upload.py` and nothing else. The
validation, the refusals and the orchestration are the same functions
`agent projects`, `agent tasks`, `agent sessions` and `agent upload` call -- which
is the point of slice 0 having lifted them out of `cli.py`. Two implementations of
"which subject is this" is how the CLI and the API come to disagree, and a
disagreement between them looks exactly like either one working.

Nothing here commits. The `get_db` dependency does, once, on a clean return.
"""

from __future__ import annotations

import secrets
import shutil

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from ... import entries as entries_mod
from ... import manual
from ...config import ConfigError
from ...db import store
from ...files import upload as upload_mod
from ...filenames import safe_filename
from ...gate import timetable as timetable_mod
from .. import convert, schemas
from ..deps import Conf, Db, Session, Table

router = APIRouter()


def _refuse(err: Exception) -> HTTPException:
    """A validation failure as a 400 carrying the message the CLI would print.

    The same sentence in both places, which is the whole reason these refusals
    are exceptions rather than `print` plus an exit code.
    """
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


class ProjectIn(BaseModel):
    subject: str
    title: str
    deadline: str | None = None
    deliverables: list[str] = Field(default_factory=list)
    team: list[str] = Field(default_factory=list)
    milestones: list[str] = Field(default_factory=list)
    brief: str | None = None
    coursework: str | None = None


class MilestoneIn(BaseModel):
    title: str


@router.get("/projects", response_model=list[schemas.ProjectOut])
def projects(
    conn: Db, session: Session, include_closed: bool = Query(False)
) -> list[schemas.ProjectOut]:
    rows = store.projects(conn, include_closed=include_closed)
    return [
        convert.project(row, store.project_milestones(conn, int(row["id"])))
        for row in rows
    ]


@router.get("/projects/{project_id}", response_model=schemas.ProjectOut)
def project(project_id: int, conn: Db, session: Session) -> schemas.ProjectOut:
    row = store.get_project(conn, project_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no project with id {project_id}.",
        )
    return convert.project(row, store.project_milestones(conn, project_id))


@router.post("/projects", response_model=schemas.ProjectOut, status_code=201)
def create_project(
    body: ProjectIn, conn: Db, config: Conf, table: Table, session: Session
) -> schemas.ProjectOut:
    """Record a project and its milestones, together or not at all.

    `with conn:` because this is one insert plus N more: the default
    isolation_level would let a failure part-way leave a project with three of
    five milestones, and a project whose milestone list is wrong reports progress
    that never happened.
    """
    try:
        plan = entries_mod.plan_project(
            config,
            table,
            entries_mod.ProjectSpec(
                subject=body.subject,
                title=body.title,
                deadline=body.deadline,
                deliverables=body.deliverables,
                team=body.team,
                milestones=body.milestones,
                brief=body.brief,
                coursework=body.coursework,
            ),
        )
    except (ConfigError, upload_mod.UploadError) as err:
        raise _refuse(err) from err

    with conn:
        project_id = entries_mod.create_project(conn, plan)

    if project_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{plan.subject} already has a project called {plan.title!r}. "
                f"Nothing written -- two projects with one name in one subject "
                f"would be two deadlines for one piece of work."
            ),
        )
    row = store.get_project(conn, project_id)
    assert row is not None  # just written
    return convert.project(row, store.project_milestones(conn, project_id))


@router.post(
    "/projects/{project_id}/milestones",
    response_model=schemas.ProjectOut,
    status_code=201,
)
def add_milestone(
    project_id: int, body: MilestoneIn, conn: Db, session: Session
) -> schemas.ProjectOut:
    """Append one milestone.

    `add_milestone` reads MAX(position) and then inserts, which two concurrent
    writers can both do with the same answer. It fails SAFE -- UNIQUE
    (project_id, position) turns the loser into an IntegrityError rather than two
    milestones at position 4 -- so the retry here is the whole handling, and a 409
    is the answer if even that loses.
    """
    if store.get_project(conn, project_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no project with id {project_id}.",
        )

    import sqlite3

    for _ in range(3):
        try:
            store.add_milestone(conn, project_id, body.title)
            break
        except sqlite3.IntegrityError:
            continue
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that milestone position is taken; try again.",
        )

    row = store.get_project(conn, project_id)
    assert row is not None
    return convert.project(row, store.project_milestones(conn, project_id))


@router.post("/milestones/{milestone_id}/complete", response_model=schemas.MilestoneOut)
def complete_milestone(
    milestone_id: int, conn: Db, session: Session
) -> schemas.MilestoneOut:
    """The only way project progress moves. There is no percentage to write."""
    outcome = entries_mod.complete_milestone(conn, milestone_id)
    if outcome.status == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no milestone with id {milestone_id}.",
        )
    row = store.get_milestone(conn, milestone_id)
    assert row is not None
    return schemas.MilestoneOut(
        id=int(row["id"]),
        title=str(row["title"]),
        position=int(row["position"]),
        done_at=row["done_at"],
    )


@router.post("/projects/{project_id}/close", response_model=schemas.ProjectOut)
def close_project(project_id: int, conn: Db, session: Session) -> schemas.ProjectOut:
    outcome = entries_mod.close_project(conn, project_id)
    if outcome.status == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no project with id {project_id}.",
        )
    row = store.get_project(conn, project_id)
    assert row is not None
    return convert.project(row, store.project_milestones(conn, project_id))


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


class TaskIn(BaseModel):
    subject: str
    title: str
    kind: str = "exercise_sheet"
    due: str | None = None
    notes: str | None = None
    source: str | None = None


@router.get("/tasks", response_model=list[schemas.TaskOut])
def tasks(
    conn: Db, session: Session, include_done: bool = Query(False)
) -> list[schemas.TaskOut]:
    """Every task, dated or not.

    This is where an undated task lives. `/api/deadlines` lists deadlines and a
    task with no date does not have one -- the scanner filters it out because
    nothing undated can cross a T-72/24/3 threshold.
    """
    return [
        convert.task(row)
        for row in store.manual_tasks(conn, include_done=include_done)
    ]


@router.post("/tasks", response_model=schemas.TaskOut, status_code=201)
def create_task(
    body: TaskIn, conn: Db, config: Conf, table: Table, session: Session
) -> schemas.TaskOut:
    try:
        plan = entries_mod.plan_task(
            config,
            table,
            entries_mod.TaskSpec(
                subject=body.subject,
                title=body.title,
                kind=body.kind,
                due=body.due,
                notes=body.notes,
                source=body.source,
            ),
        )
    except (ConfigError, upload_mod.UploadError) as err:
        raise _refuse(err) from err

    task_id = entries_mod.create_task(conn, plan)
    if task_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "already recorded -- same subject, title and due date. Two "
                "identical rows would mean two alerts for one sheet."
            ),
        )
    row = store.get_manual_task(conn, task_id)
    assert row is not None
    return convert.task(row)


@router.post("/tasks/{task_id}/complete", response_model=schemas.TaskOut)
def complete_task(task_id: int, conn: Db, session: Session) -> schemas.TaskOut:
    outcome = entries_mod.complete_task(conn, task_id)
    if outcome.status == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no task with id {task_id}."
        )
    row = store.get_manual_task(conn, task_id)
    assert row is not None
    return convert.task(row)


# ---------------------------------------------------------------------------
# sessions held
# ---------------------------------------------------------------------------


class HeldSessionIn(BaseModel):
    subject: str
    kind: str = "LEC"
    on: str | None = None
    covered: str | None = None


@router.get("/sessions", response_model=list[schemas.HeldSessionOut])
def held_sessions(
    conn: Db, session: Session, limit: int = Query(50, ge=1, le=500)
) -> list[schemas.HeldSessionOut]:
    return [
        convert.held_session(row) for row in store.manual_sessions(conn, limit=limit)
    ]


@router.post("/sessions", response_model=schemas.HeldSessionOut, status_code=201)
def log_session(
    body: HeldSessionIn, conn: Db, config: Conf, table: Table, session: Session
) -> schemas.HeldSessionOut:
    """The timetable says a session was SCHEDULED; this says one took place."""
    try:
        plan = entries_mod.plan_session(
            config,
            table,
            entries_mod.SessionSpec(
                subject=body.subject,
                kind=body.kind,
                on=body.on,
                covered=body.covered,
            ),
        )
    except (ConfigError, upload_mod.UploadError) as err:
        raise _refuse(err) from err

    session_id = entries_mod.log_session(conn, plan)
    row = conn.execute(
        "SELECT s.*, c.name AS course_name FROM manual_sessions s "
        "  LEFT JOIN courses c ON c.id = s.course_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None
    return convert.held_session(row)


# ---------------------------------------------------------------------------
# manual subjects
# ---------------------------------------------------------------------------


class ManualSubjectIn(BaseModel):
    subject: str


class ManualSubjectOut(BaseModel):
    subject: str
    course_id: str
    created: bool
    already_mapped: bool
    # What to paste into timetable.yaml, because this never writes that file.
    paste: str
    note: str


@router.post("/subjects/manual", response_model=ManualSubjectOut, status_code=201)
def add_manual_subject(
    body: ManualSubjectIn, conn: Db, config: Conf, table: Table, session: Session
) -> ManualSubjectOut:
    """Mint a course-like identity for a subject that has no Classroom.

    Does the half that needs the database and RETURNS the half that needs the
    file. `timetable.yaml` is never written here for the same reason there is no
    PUT on it: the file holds the weekly pattern and keeps one writer.
    """
    matches = [key for key in table.subjects if key.casefold() == body.subject.casefold()]
    if not matches:
        known = ", ".join(sorted(table.subjects)) or "(none)"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{body.subject!r} is not a subject in {table.path.name}, and names "
                f"are never matched approximately. Known subjects: {known}"
            ),
        )
    subject = matches[0]
    existing = table.subjects[subject]

    if existing is not None and not manual.is_manual(existing):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{subject!r} already maps to Classroom course {existing}. A subject "
                f"has one identity; if that course is wrong, edit {table.path.name}."
            ),
        )

    try:
        course_id = manual.course_id(subject)
    except manual.ManualIdError as err:
        raise _refuse(err) from err

    if existing is not None and existing != course_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{subject!r} already maps to manual course {existing}, which is not "
                f"the id this name mints ({course_id}). Renaming a course id orphans "
                f"its study items."
            ),
        )

    created = store.ensure_manual_course(conn, course_id, subject)
    mapped = existing == course_id
    return ManualSubjectOut(
        subject=subject,
        course_id=course_id,
        created=created,
        already_mapped=mapped,
        paste=f"  {subject}: {course_id}",
        note=(
            f"{table.path.name} already maps it. Nothing to paste."
            if mapped
            else (
                f"Paste the line above into {table.path.name}, under `subjects:`. "
                f"This never edits that file: the timetable is the one source of "
                f"truth for the weekly pattern, and it keeps one writer."
            )
        ),
    )


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


@router.post("/uploads", status_code=201)
def upload(
    conn: Db,
    config: Conf,
    table: Table,
    session: Session,
    file: UploadFile = File(...),
    subject: str = Form(...),
    title: str | None = Form(None),
    posted: str | None = Form(None),
    dry_run: bool = Query(False),
) -> dict:
    """A photographed board, a classmate's notes, an emailed handout.

    The bytes are spooled to a temp file under DATA_DIR -- not the system temp,
    per invariant 5 -- and then handed to the SAME `prepare` and `commit` that
    `agent upload` calls. Not a bytes-validating variant of them: one code path,
    so a refusal I get at a terminal is the refusal I get here, and `?dry_run=true`
    resolves the subject, sniffs the format and hits every refusal without writing,
    exactly as `--dry-run` does.

    Download is the only stage an upload skips. Extract, OCR, packs, quiz and the
    gate are untouched, which is the point of the feature rather than a refinement
    of it.
    """
    spool = config.data_dir / "tmp" / secrets.token_hex(8)
    spool.mkdir(parents=True, exist_ok=True)
    # The client's filename, sanitised. `read_payload` checks the SUFFIX, so the
    # temp file has to carry the real one -- and `safe_filename` is what stops a
    # supplied name from carrying a path separator into this join.
    landing = spool / safe_filename(file.filename or "upload", fallback="upload")

    try:
        written = 0
        with landing.open("wb") as handle:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > upload_mod.MAX_BYTES:
                    # Refused while streaming rather than after buffering it all:
                    # the ceiling exists so a 200 MB scan cannot be held in memory
                    # on a 12 GB box that is also running the sync.
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"that file is over the "
                            f"{upload_mod.MAX_BYTES // 1_048_576} MB ceiling. Split "
                            f"it, or put it in Drive and let the sync fetch it."
                        ),
                    )
                handle.write(chunk)

        try:
            plan = upload_mod.prepare(
                config, conn, table, subject=subject, path=landing,
                title=title, posted=posted,
            )
        except upload_mod.UploadError as err:
            raise _refuse(err) from err

        body = {
            "subject": plan.subject,
            "course_id": plan.course_id,
            "course_name": plan.course_name,
            "title": plan.title,
            "drive_id": plan.file_id,
            "post_id": plan.post_id,
            "posted_at": plan.posted_at,
            "mime_type": plan.mime_type,
            "size_bytes": plan.size_bytes,
            "already_held": plan.already_held,
            "written": False,
        }
        if dry_run:
            body["note"] = "dry run -- nothing written"
            return body

        payload = landing.read_bytes()
        with conn:
            upload_mod.commit(config, conn, plan, payload)
        body["written"] = True
        body["note"] = (
            "extract, OCR, packs and the gate will pick this up on the next "
            "`agent run`. Download is the only stage an upload skips."
        )
        return body
    finally:
        shutil.rmtree(spool, ignore_errors=True)
