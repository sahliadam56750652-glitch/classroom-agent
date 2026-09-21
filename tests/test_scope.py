"""The tracked / in-scope split.

Through Phase 5 these were one list, and only because every course came from
Classroom. `agent/scope.py` separates them so that material for a subject this
semester is about outranks last year's archive even when that subject is not --
and can never be -- in `courses.tracked`.

The property to protect: the split changes nothing for a library that is all
Classroom. Every course the current timetable names is also tracked, so
`local == tracked` and the queue head is unmoved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import scope
from agent.classroom.models import Material, parse_course
from agent.config import Config
from agent.db import store
from agent.files import ocr
from agent.gate import timetable as tt
from agent.sync.poller import sync

REPO_ROOT = Path(__file__).resolve().parents[1]

MANUAL = "manual-calculus-iii"


def a_timetable(tmp_path, subjects: dict[str, str | None], name="timetable.yaml"):
    lines = ["subjects:"]
    for subject, course in subjects.items():
        lines.append(f"  {subject}: {course if course else 'null'}")
    first = next(iter(subjects))
    lines += [
        "versions:",
        "  - label: S1",
        "    status: confirmed",
        "    effective_from: 2026-09-15",
        "    sessions:",
        '      - { day: mon, start: "08:30", end: "10:00", kind: LEC, '
        f"subject: {first} }}",
    ]
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tt.load(path)


def a_config(tmp_path, tracked, timetable_path=None):
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=tmp_path / "data",
        tracked_courses=list(tracked),
        ignored_courses=[],
        timetable_path_override=timetable_path,
    )


# --------------------------------------------------------------------------
# the two sets
# --------------------------------------------------------------------------

def test_in_scope_is_every_course_the_subjects_map_names(tmp_path):
    table = a_timetable(tmp_path, {"UNIX": "809686876412", "Calculus III": MANUAL})
    assert scope.in_scope(table) == frozenset({"809686876412", MANUAL})


def test_a_null_subject_contributes_nothing(tmp_path):
    """It has no identity yet, so there is nothing to hold against it."""
    table = a_timetable(tmp_path, {"UNIX": "809686876412", "Philosophy": None})
    assert scope.in_scope(table) == frozenset({"809686876412"})


def test_in_scope_of_no_timetable_is_empty():
    assert scope.in_scope(None) == frozenset()


def test_local_is_the_union(tmp_path):
    table = a_timetable(tmp_path, {"UNIX": "809686876412", "Calculus III": MANUAL})
    config = a_config(tmp_path, ["809686876412", "842149328479"])
    assert scope.local(config, table) == frozenset(
        {"809686876412", "842149328479", MANUAL}
    )


def test_local_survives_an_unreadable_timetable(tmp_path):
    """A broken file costs the top tier, never the whole stage."""
    broken = tmp_path / "timetable.yaml"
    broken.write_text("versions: [", encoding="utf-8")
    config = a_config(tmp_path, ["c1"], timetable_path=broken)
    table, note = scope.load(config)
    assert table is None
    assert "timetable not read" in note
    assert scope.local(config, table) == frozenset({"c1"})


def test_the_shipped_example_names_only_courses_it_maps():
    """The example file is what a new install starts from, so it must resolve."""
    table = tt.load(REPO_ROOT / "timetable.example.yaml")
    assert scope.in_scope(table) == frozenset(
        course for course in table.subjects.values() if course
    )


# --------------------------------------------------------------------------
# what the split is FOR
# --------------------------------------------------------------------------

def _files(*drive_ids):
    return [{"drive_id": drive_id} for drive_id in drive_ids]


def _posts(*rows):
    return [
        {
            "drive_id": drive_id,
            "course_id": course_id,
            "posted_at": posted_at,
            "title": drive_id,
            "course_name": course_id,
        }
        for drive_id, course_id, posted_at in rows
    ]


def test_manual_material_outranks_the_archive_although_it_is_not_tracked():
    """The stated goal of the split.

    A photographed board for a subject that meets tomorrow, against 279 pages
    of last year. Before the split the manual course fell to tier 2 -- behind
    the archive -- because `_tier` read `courses.tracked`, and a manual course
    can never be in it.
    """
    ranked = ocr.queue(
        _files("archive1", "archive2", "board"),
        _posts(
            ("archive1", "old", "2026-05-01T10:00:00Z"),
            ("archive2", "old", "2026-06-01T10:00:00Z"),
            ("board", MANUAL, "2026-09-21T10:00:00Z"),
        ),
        tracked=["old"],
        in_scope=[MANUAL],
    )
    assert [item.drive_id for item in ranked][0] == "board"
    assert ranked[0].why == "in scope this semester"
    assert ranked[1].why == "tracked"


def test_a_tracked_course_off_the_timetable_still_beats_an_untracked_one():
    """Tier 1 survives the split: synced-but-not-this-semester is still worth
    more than material for a course I chose not to track."""
    ranked = ocr.queue(
        _files("synced", "stranger"),
        _posts(
            ("synced", "c2", "2025-01-01T10:00:00Z"),
            ("stranger", "cX", "2026-09-20T10:00:00Z"),
        ),
        tracked=["c1", "c2"],
        in_scope=["c1"],
    )
    assert [item.drive_id for item in ranked] == ["synced", "stranger"]
    assert [item.why for item in ranked] == ["tracked", "not in scope"]


def test_in_scope_beats_tracked_even_when_the_course_is_both():
    ranked = ocr.queue(
        _files("both"),
        _posts(("both", "c1", "2026-09-01T10:00:00Z")),
        tracked=["c1"],
        in_scope=["c1"],
    )
    assert ranked[0].why == "in scope this semester"


def test_the_order_is_still_blind_to_the_clock_and_to_progress():
    """Both properties predate the split and neither may be lost to it."""
    files = _files("a", "b")
    posts = _posts(
        ("a", "c1", "2026-09-01T10:00:00Z"), ("b", MANUAL, "2026-09-02T10:00:00Z")
    )
    first = [
        item.drive_id
        for item in ocr.queue(files, posts, tracked=["c1"], in_scope=[MANUAL])
    ]
    for _ in range(3):
        again = ocr.queue(files, posts, tracked=["c1"], in_scope=[MANUAL])
        assert [item.drive_id for item in again] == first


# --------------------------------------------------------------------------
# and what it must not disturb
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, courses):
        self.courses = courses
        self.asked_for = []

    def _for(self, course_id, key):
        self.asked_for.append(course_id)
        return list(self.courses.get(course_id, {}).get(key, []))

    def list_coursework(self, course_id):
        return self._for(course_id, "coursework")

    def list_coursework_materials(self, course_id):
        return self._for(course_id, "coursework_materials")

    def list_announcements(self, course_id):
        return self._for(course_id, "announcements")

    def list_submissions(self, course_id):
        return self._for(course_id, "submissions")


class FakeConfig:
    tracked_courses = ["c1"]
    ignored_courses: list[str] = []


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    store.upsert_course(connection, parse_course({"id": "c1", "name": "UNIX"}))
    store.upsert_course(connection, parse_course({"id": MANUAL, "name": "Calculus III"}))
    connection.commit()
    yield connection
    connection.close()


def test_an_in_scope_course_that_is_not_tracked_is_never_polled(conn):
    """The protection PLAN.md leans on, stated as a test rather than remembered.

    The poller walks `courses.tracked` and nothing else, so a course that is in
    scope without being tracked is never fetched and never compared against a
    live state it has no counterpart in.
    """
    # A post and an attachment for the in-scope-but-untracked course, as a
    # manual upload will leave them.
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, content_hash, "
        "first_seen_at, creation_time) VALUES (?, ?, ?, 'h', ?, ?)",
        (
            "manual-post-abc",
            MANUAL,
            "Board, 21 Sep",
            "2026-09-21T10:00:00Z",
            "2026-09-21T10:00:00Z",
        ),
    )
    store.upsert_material(
        conn,
        Material(
            id="coursework_material:manual-post-abc:driveFile:manual-file-xyz",
            parent_type="coursework_material",
            parent_id="manual-post-abc",
            course_id=MANUAL,
            kind="driveFile",
            ref="manual-file-xyz",
            drive_id="manual-file-xyz",
            title="board.jpg",
            url=None,
            content_hash="h",
        ),
    )
    conn.commit()

    client = FakeClient({"c1": {"coursework": [{"id": "w1", "title": "TD 1"}]}})
    result = sync(FakeConfig(), conn, client=client)

    assert MANUAL not in client.asked_for
    assert result.deleted == {}
    for table, key in (
        ("coursework_materials", "manual-post-abc"),
        ("materials", "coursework_material:manual-post-abc:driveFile:manual-file-xyz"),
    ):
        row = conn.execute(
            f"SELECT deleted_at FROM {table} WHERE id = ?", (key,)
        ).fetchone()
        assert row["deleted_at"] is None, f"{table} row was soft-deleted by a sync"


def test_a_second_sync_still_emits_nothing_with_manual_rows_present(conn):
    """The project's central property, unmoved by the split."""
    data = {"c1": {"coursework": [{"id": "w1", "title": "TD 1"}]}}
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, content_hash, "
        "first_seen_at) VALUES ('manual-post-abc', ?, 'Board', 'h', ?)",
        (MANUAL, "2026-09-21T10:00:00Z"),
    )
    conn.commit()

    sync(FakeConfig(), conn, client=FakeClient(data))
    second = sync(FakeConfig(), conn, client=FakeClient(data))
    assert second.events == []
