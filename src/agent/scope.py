"""Which courses this semester is about, as distinct from which ones are polled.

Through Phase 5 these were one list, and only because every course came from
Classroom. They stop being one list the moment a subject does not.

  * **tracked** is the poller's allowlist -- `courses.tracked` in config.yaml,
    curated by hand, and the answer to "which Classroom courses do I fetch".
    Nothing else belongs in it; a manual course id in that list makes the
    poller 404 and one sync stamp `deleted_at` across everything typed by hand.
    See `manual.py` for the guard that refuses one.

  * **in scope** is the queue's question: which material belongs to the week I
    am actually having. It is every course id the timetable's `subjects:` map
    names, which picks up manual subjects for free, because a manual subject is
    in that map by construction.

  * **local** is the union, and it is what every stage that reads material off
    this disk takes: study items, packs, the gate. "Material I hold", wherever
    it came from.

`in_scope` is a function of the FILE and never of the clock -- the timetable
contributes the courses it NAMES, not the ones meeting this week, for the same
reason `files/ocr.py:queue` gives: a set that reshuffles every Monday is
invariant 1's mistake wearing a different hat.
"""

from __future__ import annotations

from .config import Config
from .gate import timetable as timetable_mod
from .gate.timetable import Timetable


def in_scope(table: Timetable | None) -> frozenset[str]:
    """Course ids this semester is about: every id the subjects map names.

    A subject mapped to null contributes nothing -- it has no identity yet, so
    there is nothing for a queue or a gate to hold against it.
    """
    if table is None:
        return frozenset()
    return frozenset(course for course in table.subjects.values() if course)


def local(config: Config, table: Timetable | None) -> frozenset[str]:
    """tracked | in scope -- every course whose stored material we read.

    The union rather than either half: a tracked course absent from this
    semester's timetable still has a library and a backlog worth serving, and
    an in-scope course that is manual has one without ever being tracked.
    """
    return frozenset(config.tracked_courses) | in_scope(table)


def load(config: Config) -> tuple[Timetable | None, str | None]:
    """The timetable, or None and a note saying why it could not be read.

    Returned rather than raised. A broken or absent timetable must not stop OCR
    or packs: it costs the top tier -- everything tracked falls back to one
    bucket ordered by posting date, which is still the right answer, just a
    blunter one -- so the note is printed where the ordering is explained.
    """
    try:
        return timetable_mod.load(config.timetable_path), None
    except timetable_mod.TimetableError as err:
        return None, f"timetable not read, so no subject can be preferred: {err}"


def resolve(config: Config) -> tuple[frozenset[str], frozenset[str], str | None]:
    """(in scope, local, note) in one call, for a command that needs both."""
    table, note = load(config)
    scoped = in_scope(table)
    if table is not None and not scoped:
        note = "timetable names no course id, so no subject can be preferred"
    return scoped, local(config, table), note
