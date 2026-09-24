"""The library and the reader — DESIGN.md section 6.

"Most of my time here is dense PDFs, on a phone, at night. The reader is the
product; everything else is navigation."

So this module is mostly about not getting in the way: bytes with ranges, text
when the bytes will not do, and one cursor that is honest about having lost its
place.

The one write in the whole of 5b that is not a hand-entered record lives here:
`PUT /api/documents/{drive_id}/position`. It stores the CONTENT HASH of the last
page seen, never an index alone, which is what lets a position survive the
document being re-fetched -- and lets the app say the document changed instead of
silently starting again at page one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from ... import scope as scope_mod
from ...db import store
from ...filenames import document_filename
from ...gate import sections
from .. import files, schemas
from ..deps import Conf, Db, MaybeTable, Session

router = APIRouter()


class PositionIn(BaseModel):
    """Where I stopped. The page index is the shortcut; the hash is the identity.

    `page_hash` is optional because a genuinely blank page cannot be hashed --
    `sections.anchor` returns None below its character floor, and one page in the
    1094 measured was blank. A position with no hash still works; it just cannot
    survive a re-fetch, and `GET` says so rather than pretending.
    """

    page_index: int = Field(ge=0)
    page_hash: str | None = None


@router.get("/library", response_model=list[schemas.LibraryPostOut])
def library(
    conn: Db,
    config: Conf,
    table: MaybeTable,
    session: Session,
    course: str | None = Query(None),
    q: str | None = Query(None, min_length=1),
) -> list[schemas.LibraryPostOut]:
    """Every post whose text this install actually holds.

    The unit is the post, not the attachment: a lecture with slides and a handout
    is one thing to revise. `store.parents_with_extracted_material` already makes
    that choice, and it joins through `extractions` so a post whose only attachment
    is trashed yields nothing rather than an empty shell.

    Scoped to `scope.local` -- tracked union in scope, which is what every stage
    reading material off this disk takes. A course that is neither is not in this
    semester and not fetched either.
    """
    local = scope_mod.local(config, table)
    wanted = [course] if course else sorted(local)
    if course and course not in local:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{course} is neither tracked nor in this semester's timetable.",
        )

    posts = store.parents_with_extracted_material(conn, wanted)
    names = {str(row["id"]): str(row["name"]) for row in store.list_courses(conn)}

    found: list[schemas.LibraryPostOut] = []
    for row in posts:
        entity_type = str(row["entity_type"])
        entity_id = str(row["entity_id"])
        detail = _post_detail(conn, entity_type, entity_id)
        if detail is None:
            continue
        title = str(detail["title"] or entity_id)
        if q and q.casefold() not in title.casefold():
            continue
        item = conn.execute(
            "SELECT id, state FROM study_items WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        ).fetchone()
        totals = _text_totals(conn, entity_type, entity_id)
        found.append(
            schemas.LibraryPostOut(
                entity_type=entity_type,
                entity_id=entity_id,
                course_id=str(row["course_id"]),
                course_name=names.get(str(row["course_id"]), ""),
                title=title,
                creation_time=detail["creation_time"],
                files=int(row["files"]),
                pages=totals["pages"],
                unread=totals["unread"],
                study_item_id=int(item["id"]) if item else None,
                study_item_state=str(item["state"]) if item else None,
            )
        )
    # Newest first: the same instinct as the OCR queue's posting-date order, and
    # what I want when I open the library during a term.
    found.sort(key=lambda post: (post.creation_time or "", post.title), reverse=True)
    return found


def _post_detail(
    conn: sqlite3.Connection, entity_type: str, entity_id: str
) -> sqlite3.Row | None:
    tables = {
        "coursework": "coursework",
        "coursework_material": "coursework_materials",
        "announcement": "announcements",
    }
    name = tables.get(entity_type)
    if name is None:
        return None
    column = "title" if name != "announcements" else "text AS title"
    return conn.execute(
        f"SELECT {column}, creation_time FROM {name} WHERE id = ?", (entity_id,)
    ).fetchone()


def _text_totals(
    conn: sqlite3.Connection, entity_type: str, entity_id: str
) -> dict[str, int]:
    """Pages held and pages nothing has read, for one post.

    `unread` is the same arithmetic `gate_backlog` applies: pages carrying an
    image and almost no text, minus the ones a model has transcribed.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(e.pages), 0) AS pages, "
        "       COALESCE(SUM(MAX(e.scan_pages - e.ocr_pages, 0)), 0) AS unread "
        "  FROM (SELECT DISTINCT drive_id FROM materials "
        "         WHERE parent_type = ? AND parent_id = ? AND deleted_at IS NULL) m "
        "  JOIN extractions e ON e.drive_id = m.drive_id AND e.status = 'ok'",
        (entity_type, entity_id),
    ).fetchone()
    return {"pages": int(row["pages"] or 0), "unread": int(row["unread"] or 0)}


@router.get("/documents/{drive_id}", response_model=schemas.DocumentOut)
def document(drive_id: str, conn: Db, session: Session) -> schemas.DocumentOut:
    """What is known about one attachment, readable or not."""
    row = store.get_extraction(conn, drive_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"nothing is held for {drive_id}.",
        )
    material = conn.execute(
        "SELECT title, url FROM materials WHERE drive_id = ? LIMIT 1", (drive_id,)
    ).fetchone()
    title = str((material["title"] if material else None) or drive_id)
    local = row["local_path"]
    return schemas.DocumentOut(
        drive_id=drive_id,
        title=title,
        url=material["url"] if material else None,
        status=row["status"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        pages=row["pages"],
        filename=(
            document_filename(title, str(local)) if local else None
        ),
        readable=bool(local) and str(row["status"] or "") == "ok",
    )


@router.get("/documents/{drive_id}/file")
def document_file(
    drive_id: str, request: Request, conn: Db, config: Conf, session: Session
) -> Response:
    """The bytes, with ranges and a conditional GET.

    A 92-page deck must not arrive whole because the reader asked for page 34, and
    it must not arrive twice because I closed the tab. See `api/files.py`.
    """
    path, row = files.held_file(conn, config, drive_id)
    material = conn.execute(
        "SELECT title FROM materials WHERE drive_id = ? LIMIT 1", (drive_id,)
    ).fetchone()
    title = str((material["title"] if material else None) or drive_id)
    return files.document_response(
        request, path, title=title, mime_type=row["mime_type"], row=row
    )


@router.get("/documents/{drive_id}/text")
def document_text(
    drive_id: str,
    conn: Db,
    config: Conf,
    session: Session,
    page: int | None = Query(None, ge=1),
) -> Response:
    """The extracted text, or one page of it.

    Here because DESIGN.md forbids a state the app counts but will not show me.
    This is the material a quiz is grounded in: if I cannot read what the model
    read, I cannot judge a question that cites it, and an unauditable coverage
    figure is the one failure this project says it cannot recover from.

    `page` is 1-based, matching what the reader displays, while `ocr_pages` and
    `Window` are 0-based. Converted once, here, rather than leaving two
    conventions loose in the client.
    """
    row = store.get_extraction(conn, drive_id)
    if row is None or not row["text_path"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no extracted text is held for {drive_id}.",
        )
    path = files.resolve(config, str(row["text_path"]))
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{drive_id} has text recorded at {row['text_path']} and that file "
                f"is not on this disk."
            ),
        ) from None

    if page is None:
        return Response(content=body, media_type="text/plain; charset=utf-8")

    pages = body.split(sections.PAGE_BREAK)
    if page > len(pages):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{drive_id} has {len(pages)} page(s); page {page} is past the end.",
        )
    return Response(
        content=pages[page - 1], media_type="text/plain; charset=utf-8"
    )


@router.get("/documents/{drive_id}/position", response_model=schemas.PositionOut)
def read_position(
    drive_id: str, conn: Db, config: Conf, session: Session
) -> schemas.PositionOut:
    """Where I stopped, or why that is no longer knowable.

    The anchor is checked against the document as it is NOW. When the stored hash
    is no longer among its pages, the answer is `page: null, changed: true` and a
    sentence -- said out loud, because a silent fall back to page one is a
    misreport, and this project ranks a misreport as its own defect.
    """
    stored = store.get_read_position(conn, drive_id)
    if stored is None:
        return schemas.PositionOut(drive_id=drive_id)

    saved_hash = stored["page_hash"]
    index = int(stored["page_index"])
    updated = str(stored["updated_at"])

    if not saved_hash:
        # Nothing to check it against: the page was too thin to hash. Honoured as
        # an index, and said, rather than presented as something it is not.
        return schemas.PositionOut(
            drive_id=drive_id,
            page=index + 1,
            updated_at=updated,
            note=(
                "that page carried too little text to identify by content, so this "
                "position is an index and will not survive the document changing."
            ),
        )

    found = _index_of_anchor(conn, config, drive_id, saved_hash)
    if found is None:
        return schemas.PositionOut(
            drive_id=drive_id,
            page=None,
            page_hash=saved_hash,
            updated_at=updated,
            changed=True,
            note=(
                "this document changed since it was last read -- the page that was "
                "open is no longer in it. Starting from the beginning rather than "
                "from a page that would now be different material."
            ),
        )

    return schemas.PositionOut(
        drive_id=drive_id,
        page=found + 1,
        page_hash=saved_hash,
        updated_at=updated,
        # Worth saying when the page MOVED: the position is right and the number
        # is not the one I left.
        note=(
            "" if found == index else
            f"the document changed and this page moved from {index + 1} to {found + 1}."
        ),
    )


@router.put("/documents/{drive_id}/position", response_model=schemas.PositionOut)
def set_read_position(
    drive_id: str, body: PositionIn, conn: Db, session: Session
) -> schemas.PositionOut:
    """Remember where I stopped. One row per document -- a cursor, not a history."""
    if store.get_extraction(conn, drive_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"nothing is held for {drive_id}, so there is no place in it.",
        )
    store.set_read_position(
        conn, drive_id, page_index=body.page_index, page_hash=body.page_hash
    )
    stored = store.get_read_position(conn, drive_id)
    assert stored is not None  # just written
    return schemas.PositionOut(
        drive_id=drive_id,
        page=body.page_index + 1,
        page_hash=body.page_hash,
        updated_at=str(stored["updated_at"]),
    )


def _index_of_anchor(
    conn: sqlite3.Connection, config, drive_id: str, wanted: str
) -> int | None:
    """Which page of this document hashes to `wanted` today. None when none does.

    Recomputed from the text on disk rather than stored, because the whole point
    is to compare the saved anchor against the document as it is now. `ocr_pages`
    supplies a transcription where a page's own text is too thin to identify it,
    which is the same fold-in `sections.anchor` does for the gate.
    """
    row = store.get_extraction(conn, drive_id)
    if row is None or not row["text_path"]:
        return None
    try:
        body = files.resolve(config, str(row["text_path"])).read_text(encoding="utf-8")
    except OSError:
        return None

    transcriptions = store.ocr_pages_for(conn, drive_id)
    for index, page_text in enumerate(body.split(sections.PAGE_BREAK)):
        transcribed = transcriptions.get(index)
        found = sections.anchor(
            page_text, str(transcribed["text"]) if transcribed else None
        )
        if found == wanted:
            return index
    return None


@router.get("/packs/{course_id}")
def pack(
    course_id: str, request: Request, conn: Db, config: Conf, session: Session
) -> Response:
    """The built study pack for one course.

    This is how a pack reaches a notebook now that the server is the host. The
    Drive-synced-folder route died at 5a -- there is no sync client on the box --
    and the alternative was adding the `drive.file` scope, which would have been a
    deliberate SECOND exception to invariant 6: a write scope requested because the
    project means to write. Serving the file costs nothing beyond the reader's own
    plumbing and leaves invariant 6 with exactly one exception. See PLAN.md.
    """
    row = store.get_pack(conn, course_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no pack has been built for {course_id}. Run `agent packs`.",
        )

    # The stored `path` is absolute -- `packs.build` writes it that way, because
    # packs_dir is the one path deliberately allowed to point outside data_dir.
    # Only its FILENAME is used here, re-joined to the configured directory, so a
    # database restored onto a different box finds its packs instead of pointing
    # at a directory that machine never had. The name is a slug of the course
    # name and is stable.
    path = (config.packs_dir / Path(str(row["path"])).name).resolve()
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"the pack for {course_id} is recorded at {row['path']} and that "
                f"file is not on this disk. Run `agent packs` to rebuild it."
            ),
        )

    course = store.get_course(conn, course_id)
    title = str((course["name"] if course else None) or course_id)
    return files.document_response(
        request, path, title=f"{title} study pack", mime_type="text/markdown"
    )
