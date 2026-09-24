"""Serving bytes off the local library, safely and without re-sending them.

Three things happen here, and each one exists because of something measured
rather than assumed.

**The client never sends a path.** It sends a `drive_id`; this resolves
`extractions.local_path` and proves the result is inside `library_dir` before
opening anything. A row whose path escapes is a 500 and a log line, not a 404 --
it means the database is wrong, and that must not look like a file that was never
fetched.

**Range requests work, and they come from Starlette.** Verified against the
installed version rather than trusted: `FileResponse` answers a `Range` header
with 206, a correct `Content-Range` and `Accept-Ranges: bytes`, and 416 for an
unsatisfiable range. So there is no range handler here, and a test asserts the
behaviour so that a version which stops doing it fails loudly rather than
silently sending 92 pages to a phone.

**Conditional GET is ours.** The same check found that Starlette generates an
`ETag` but does NOT honour `If-None-Match` -- it returns 200 with the whole body.
So `not_modified` is written out here. Without it, DESIGN.md's "what has been
delivered is readable offline" means re-pulling a 40 MB deck on every open.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from ..config import Config
from ..db import store
from ..filenames import document_filename, safe_filename

log = logging.getLogger("agent.api.files")


class MissingFile(Exception):
    """The database says there is a file and the disk disagrees."""


def resolve(config: Config, relative: str) -> Path:
    """A stored relative path as an absolute one, proven to be inside the library.

    `local_path` is written by the fetch and upload stages and is always relative
    and forward-slashed -- measured, 0 of 118 rows carry a backslash. This does not
    trust that: it resolves and compares, because the one thing worse than a
    missing document is serving a file from outside the library because a row said
    so.
    """
    root = config.library_dir.resolve()
    candidate = (config.library_dir / relative).resolve()
    if candidate != root and root not in candidate.parents:
        # Loud, and a 500 rather than a 404. A traversal here is a corrupt row or
        # a bug in a writer, and reporting it as "not found" would hide it behind
        # the twenty attachments that genuinely are gone from Drive.
        log.error("api: stored path escapes the library: %r -> %s", relative, candidate)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="a stored file path points outside the library; check the logs.",
        )
    return candidate


def held_file(conn: sqlite3.Connection, config: Config, drive_id: str) -> tuple[Path, sqlite3.Row]:
    """The bytes for one Drive id, or a 404 that says which kind of absent it is.

    Four distinguishable cases, and they are not collapsed into one message:
    nothing is known about this id; the extraction failed or the file is trashed;
    the row holds no local path; the row holds one and the disk does not. The
    recurring lesson is precisely that a summary which cannot tell two states
    apart is a defect in itself.
    """
    row = store.get_extraction(conn, drive_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"nothing is held for {drive_id}. It may never have been fetched.",
        )

    state = str(row["status"] or "")
    if state in ("trashed", "missing"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{drive_id} is {state} in Drive. This is permanent loss, not a "
                f"transient failure -- the only remedy is asking the teacher."
            ),
        )

    relative = row["local_path"]
    if not relative:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{drive_id} is recorded with status {state!r} but no local file. "
                f"Run `agent fetch` to download it."
            ),
        )

    path = resolve(config, str(relative))
    if not path.is_file():
        log.warning("api: %s recorded at %s, which is not on disk", drive_id, relative)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{drive_id} is recorded at {relative} and that file is not on "
                f"this disk. The database and the library disagree."
            ),
        )
    return path, row


def etag_for(path: Path, row: sqlite3.Row | None = None) -> str:
    """A strong validator for one document.

    Drive's md5 where the fetch recorded one, because that identifies the CONTENT
    -- a re-fetch of unchanged bytes then keeps the same tag and costs nothing.
    Size and mtime otherwise, which is what Starlette would have used.
    """
    known = None
    if row is not None and "md5_checksum" in row.keys():
        known = row["md5_checksum"]
    if known:
        return f'"{known}"'
    stat = path.stat()
    return f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'


def not_modified(request: Request, etag: str) -> Response | None:
    """A 304 when the browser already has this exact document, else None.

    Written out because Starlette generates an ETag and then ignores
    `If-None-Match`. Comparing by membership rather than equality: a browser may
    send several tags, and a proxy may weaken one to `W/"..."`.
    """
    header = request.headers.get("if-none-match")
    if not header:
        return None
    offered = {value.strip() for value in header.split(",")}
    offered |= {value.removeprefix("W/") for value in offered}
    if etag in offered or "*" in offered:
        # The headers a 304 must still carry, or a cache treats the entry as
        # stale and asks again next time anyway.
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"},
        )
    return None


def document_response(
    request: Request,
    path: Path,
    *,
    title: str,
    mime_type: str | None,
    row: sqlite3.Row | None = None,
) -> Response:
    """One document, with ranges, a real filename, and a conditional GET.

    `inline`, because the reader displays it rather than saving it. The filename is
    `filenames.document_filename`, the same function Telegram delivery uses -- a
    document has one name everywhere, and changing that rule stays a decision
    about one function.

    `Cache-Control: private`: one user, and no shared cache should ever hold this.
    """
    etag = etag_for(path, row)
    cached = not_modified(request, etag)
    if cached is not None:
        return cached

    name = document_filename(title, path)
    return FileResponse(
        path,
        media_type=mime_type or "application/octet-stream",
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=0, must-revalidate",
            "Content-Disposition": _disposition(name),
        },
    )


def _disposition(name: str) -> str:
    """`inline` with both a plain filename and an RFC 5987 one.

    Two spellings because the plain parameter cannot carry a non-ASCII character
    and half the titles in this library are French. The ASCII fallback is what an
    old client saves it as; `filename*` is what everything current uses.
    """
    safe = safe_filename(name)
    ascii_only = safe.encode("ascii", "replace").decode("ascii").replace("?", "_")
    quoted = ascii_only.replace('"', "")
    encoded = _percent_encode(safe)
    return f'inline; filename="{quoted}"; filename*=UTF-8\'\'{encoded}'


def _percent_encode(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
