"""Everything entered by hand, over HTTP.

Every route here calls `entries.py` and nothing else. The validation, the
refusals and the orchestration are the same functions `agent projects` and
`agent tasks` call -- which
is the point of slice 0 having lifted them out of `cli.py`. Two implementations of
"which subject is this" is how the CLI and the API come to disagree, and a
disagreement between them looks exactly like either one working.

Nothing here commits. The `get_db` dependency does, once, on a clean return.

Uploads, session logging and minting a manual subject were removed on
2026-10-13, when manual logging was cancelled. Tasks and projects stay, filed
under the `manual-` ids the four no-Classroom subjects already have.
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from ... import entries as entries_mod
from ...config import ConfigError
from ...db import store
from .. import convert, files, schemas
from ..idempotency import idempotent
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


class LinkIn(BaseModel):
    url: str
    title: str | None = None


class PinIn(BaseModel):
    drive_id: str


def _project_out(conn, row) -> schemas.ProjectOut:
    """A project with its milestones and its links & files, as every route returns it."""
    project_id = int(row["id"])
    attached = []
    for value in store.project_attachments(conn, project_id):
        held = store.held_document(conn, str(value["drive_id"])) if value["kind"] == "document" else None
        attached.append(convert.project_attachment(value, held))
    return convert.project(row, store.project_milestones(conn, project_id), attached)


def _project_or_404(conn, project_id: int):
    row = store.get_project(conn, project_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no project with id {project_id}."
        )
    return row


@router.get("/projects", response_model=list[schemas.ProjectOut])
def projects(
    conn: Db, session: Session, include_closed: bool = Query(False)
) -> list[schemas.ProjectOut]:
    rows = store.projects(conn, include_closed=include_closed)
    return [
        _project_out(conn, row)
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
    return _project_out(conn, row)


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
    except (ConfigError, entries_mod.SubjectError) as err:
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
    return _project_out(conn, row)


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
    return _project_out(conn, row)


@router.post("/milestones/{milestone_id}/complete", response_model=schemas.MilestoneOut)
@idempotent()
def complete_milestone(
    milestone_id: int, request: Request, conn: Db, session: Session
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


# ---------------------------------------------------------------------------
# links & files (2026-10-16)
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/links", response_model=schemas.ProjectOut, status_code=201)
@idempotent(status=201)
def add_project_link(
    project_id: int, body: LinkIn, request: Request, conn: Db, session: Session
) -> schemas.ProjectOut:
    """A link -- http or https only, never fetched. Queued by the phone's outbox."""
    try:
        added = entries_mod.add_project_link(conn, project_id, body.url, body.title)
    except entries_mod.AttachmentError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)) from err
    if added is None:
        _project_or_404(conn, project_id)
    return _project_out(conn, _project_or_404(conn, project_id))


@router.post("/projects/{project_id}/documents", response_model=schemas.ProjectOut, status_code=201)
@idempotent(status=201)
def pin_project_document(
    project_id: int, body: PinIn, request: Request, conn: Db, session: Session
) -> schemas.ProjectOut:
    """Pin one of the project's course's library documents -- a brief from Classroom."""
    try:
        added = entries_mod.pin_project_document(conn, project_id, body.drive_id)
    except entries_mod.AttachmentError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)) from err
    if added is None:
        _project_or_404(conn, project_id)
    return _project_out(conn, _project_or_404(conn, project_id))


# An explicit operation_id names the form body "Body_upload_project_file";
# FastAPI's default would put the path's double underscores into it.
@router.post(
    "/projects/{project_id}/files",
    response_model=schemas.ProjectOut,
    status_code=201,
    operation_id="upload_project_file",
)
def upload_project_file(
    project_id: int, conn: Db, config: Conf, session: Session, file: UploadFile = File(...)
) -> schemas.ProjectOut:
    """An upload, 50 MB at most. Never queued: it needs the PC, and the app says so."""
    _project_or_404(conn, project_id)
    try:
        entries_mod.add_project_file(conn, config, project_id, file.filename or "", file.file)
    except entries_mod.AttachmentError as err:
        too_big = "limit" in str(err)
        raise HTTPException(
            status_code=(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if too_big else status.HTTP_400_BAD_REQUEST
            ),
            detail=str(err),
        ) from err
    return _project_out(conn, _project_or_404(conn, project_id))


@router.delete("/projects/{project_id}/attachments/{attachment_id}", response_model=schemas.ProjectOut)
@idempotent()
def remove_project_attachment(
    project_id: int, attachment_id: int, request: Request, conn: Db, session: Session
) -> schemas.ProjectOut:
    """Take a link or file off. The bytes stay on disk and in the backup."""
    outcome = entries_mod.remove_project_attachment(conn, project_id, attachment_id)
    if outcome.status == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"project {project_id} has no attachment {attachment_id}.",
        )
    return _project_out(conn, _project_or_404(conn, project_id))


@router.get("/projects/{project_id}/files/{attachment_id}")
def project_file(
    project_id: int, attachment_id: int, request: Request, conn: Db, config: Conf, session: Session
) -> Response:
    """An uploaded file, with ranges and an ETag like a library document.

    PDFs and images inline -- the viewer shows them -- and everything else as a
    download; always with nosniff, so the type is the one sniffed at upload.
    """
    row = store.get_project_attachment(conn, attachment_id)
    if row is None or int(row["project_id"]) != project_id or row["kind"] != "file":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"project {project_id} has no file {attachment_id}.",
        )
    path = entries_mod.project_file_path(config, row)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="that file is recorded but not on disk. agent backup --restore puts it back.",
        )
    mime = row["mime_type"] or "application/octet-stream"
    return files.document_response(
        request, path, title=str(row["title"]), mime_type=mime, row=row,
        download=mime not in entries_mod.INLINE_MIMES,
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
    return _project_out(conn, row)


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
    except (ConfigError, entries_mod.SubjectError) as err:
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
@idempotent()
def complete_task(task_id: int, request: Request, conn: Db, session: Session) -> schemas.TaskOut:
    outcome = entries_mod.complete_task(conn, task_id)
    if outcome.status == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no task with id {task_id}."
        )
    row = store.get_manual_task(conn, task_id)
    assert row is not None
    return convert.task(row)
