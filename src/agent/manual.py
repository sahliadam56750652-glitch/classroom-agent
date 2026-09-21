"""The `manual-` id namespace: everything this project knows that Google does not.

A subject with no Classroom still meets twice a week, still sets exercise
sheets, and still leaves a board worth photographing. Phase 6 gives that
material a place to live, and the cheapest correct place turned out to be the
tables it already has: `courses.id` and `extractions.drive_id` are both
`TEXT PRIMARY KEY`, so a locally minted id is structurally a course and
structurally a file, and nothing downstream inspects the shape of either.

What makes that safe rather than merely convenient is that every minted id
carries one reserved prefix, so "is this row mine or Google's?" is a question
the database can answer:

    manual-<slug>          a course     -- manual-calculus-iii
    manual-post-<hash>     a post       -- one upload, one session's worth
    manual-file-<hash>     a file       -- the bytes, keyed by their own sha256

Three properties the prefix has to have, and does:

  * **It cannot collide with Classroom.** Course, coursework and announcement
    ids from the API are decimal digit strings. `manual-` cannot be one.
  * **It is filename-safe.** `library/files/<drive_id>.<ext>` puts a file id
    straight into a path, and ':' -- the obvious namespace separator -- is
    illegal on Windows. '-' is legal everywhere.
  * **It survives id composition.** `models.material_id` builds an attachment
    id as `<parent_type>:<parent_id>:<kind>:<ref>`, so a manual attachment's
    own id does not start with the prefix -- but its `parent_id` does, which is
    why `store.soft_delete_missing` tests `parent_id` for `materials` and `id`
    for everything else.

**The hard constraint.** A manual course id must NEVER enter `courses.tracked`.
`store.soft_delete_missing` runs per tracked course id and stamps `deleted_at`
on everything the live state stopped returning -- so a manual course in that
list means the poller 404s on it and one sync deletes everything typed in by
hand. That is enforced in three places, not left to memory:

  1. `config._course_ids` refuses a manual id in `courses.tracked` by name.
  2. `store.soft_delete_missing` never stamps a row carrying the prefix, even
     when it is reached for a genuinely tracked course -- which is the case
     that matters, because an upload may well be FOR a tracked subject.
  3. `store.drive_references` omits manual drive ids, so `agent fetch` never
     asks Drive for a file that was never in it.

Minting is deterministic: the same subject name always mints the same course
id, and the same bytes always mint the same file id. That gives an upload
idempotency for free -- re-uploading the same photograph is the same row and
costs nothing -- which is invariant 2's rule ("diff on content, not on when")
applied to a photograph.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

PREFIX = "manual-"
POST_PREFIX = f"{PREFIX}post-"
FILE_PREFIX = f"{PREFIX}file-"

# Long enough that a collision is not a thing to think about, short enough that
# the id still fits on a terminal line next to a filename.
_FILE_DIGEST = 16
_POST_DIGEST = 12

_UNSAFE = re.compile(r"[^a-z0-9]+")


class ManualIdError(Exception):
    """A name that cannot become an id, said out loud rather than mangled."""


def is_manual(identifier: str | None) -> bool:
    """Is this id one of ours rather than one of Google's?

    None is False, not an error: plenty of columns are nullable and every
    caller would otherwise need the same guard.
    """
    return bool(identifier) and str(identifier).startswith(PREFIX)


def slug(name: str) -> str:
    """A subject name as the stable half of its course id.

    Accents are folded rather than dropped, because the real timetable is
    half French and 'Décision' and 'Decision' must not mint two courses.
    """
    folded = unicodedata.normalize("NFKD", str(name))
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    cleaned = _UNSAFE.sub("-", ascii_only.lower()).strip("-")
    if not cleaned:
        raise ManualIdError(
            f"{name!r} has no letters or digits in it, so it cannot become a "
            f"course id. Rename the subject in timetable.yaml."
        )
    return cleaned


def course_id(subject_name: str) -> str:
    """The id a manual subject is addressed by, everywhere a course id is.

    Deterministic, so re-running `agent subjects --add` on a name that already
    has a course is a no-op rather than a second course for one subject.
    """
    return f"{PREFIX}{slug(subject_name)}"


def file_id(payload: bytes) -> str:
    """A file's id, which is a hash of the file.

    Keyed by content and not by name or by clock, so the same photograph
    uploaded twice is one row -- the same rule as the quiz question cache, and
    for the same reason.
    """
    return f"{FILE_PREFIX}{hashlib.sha256(payload).hexdigest()[:_FILE_DIGEST]}"


def post_id(course: str, title: str, posted_at: str) -> str:
    """A post's id, from what makes it that post rather than another.

    The course it belongs to, what it is called, and when it was posted. Two
    boards photographed on one day for one subject need different titles, and
    `agent upload` defaults the title to the filename, which supplies that.
    """
    key = f"{course}\x00{title}\x00{posted_at}".encode()
    return f"{POST_PREFIX}{hashlib.sha256(key).hexdigest()[:_POST_DIGEST]}"
