"""Storing a dated change to the weekly pattern.

`timetable.yaml` stays the source of truth for the PATTERN. Nothing here is a
session in its own right: every row names a session in the file and says what
happened to it on one date. The file is read and never written.

These rows are entered by hand and no amount of re-syncing recovers them, so
two properties matter as much as the shape: a sync can never reach them, and
`agent backup` always carries them.
"""

from __future__ import annotations

import pytest

from agent import backup
from agent.classroom.models import parse_course
from agent.db import store

MANUAL = "manual-calculus-iii"


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "academic.db")
    store.upsert_course(connection, parse_course({"id": "c1", "name": "UNIX"}))
    store.ensure_manual_course(connection, MANUAL, "Calculus III")
    connection.commit()
    yield connection
    connection.close()


def a_cancellation(conn, *, day="2026-09-21", subject="Calculus III", start="08:30"):
    return store.add_adjustment(
        conn, applies_on=day, kind="cancelled", subject=subject,
        session_start=start, reason="professor away",
    )


def a_move(conn, *, day="2026-09-21", to_date="2026-09-23", subject="Calculus III",
           start="08:30", to_start="13:00", to_end="14:30"):
    return store.add_adjustment(
        conn, applies_on=day, kind="moved", subject=subject, session_start=start,
        to_date=to_date, to_start=to_start, to_end=to_end,
    )


# --------------------------------------------------------------------------
# the shape
# --------------------------------------------------------------------------

def test_an_adjustment_names_a_session_and_a_date(conn):
    adjustment_id = a_cancellation(conn)
    conn.commit()

    row = store.get_adjustment(conn, adjustment_id)
    assert row["applies_on"] == "2026-09-21"
    assert row["kind"] == "cancelled"
    assert row["subject"] == "Calculus III"
    assert row["session_start"] == "08:30"
    assert row["reason"] == "professor away"


def test_the_three_kinds_are_the_only_ones(conn):
    for kind, start in (("moved", "08:30"), ("cancelled", "10:15"), ("extra", "13:00")):
        assert store.add_adjustment(
            conn, applies_on="2026-09-21", kind=kind, subject="Calculus III",
            session_start=start,
        ) is not None


def test_a_bad_kind_raises_rather_than_reading_as_a_clash(conn):
    """Both would otherwise come back as None. "One already exists" and "that
    is not a kind" are different facts, and a summary that cannot tell two
    states apart is a defect in itself."""
    with pytest.raises(ValueError, match="not an adjustment kind"):
        store.add_adjustment(
            conn, applies_on="2026-09-21", kind="postponed",
            subject="Calculus III", session_start="17:00",
        )


def test_course_id_is_a_note_and_not_a_link(conn):
    """No foreign key on purpose: the subject may be mapped to null, or to a
    Classroom course the sync has not fetched yet."""
    adjustment_id = store.add_adjustment(
        conn, applies_on="2026-09-21", kind="cancelled", subject="Philosophy",
        session_start="10:15", course_id="a-course-that-does-not-exist",
    )
    conn.commit()
    assert store.get_adjustment(conn, adjustment_id)["course_id"] == (
        "a-course-that-does-not-exist"
    )


# --------------------------------------------------------------------------
# refuse, do not guess
# --------------------------------------------------------------------------

def test_two_adjustments_on_one_session_on_one_date_are_refused(conn):
    """Which of them wins is not a question anything here can answer."""
    assert a_cancellation(conn) is not None
    conn.commit()

    assert a_move(conn) is None
    assert store.find_adjustment(
        conn, applies_on="2026-09-21", subject="Calculus III", session_start="08:30"
    )["kind"] == "cancelled", "the first one must still stand"


def test_the_same_subject_at_a_different_time_is_a_different_session(conn):
    """A subject meeting twice on one day is two sessions, and each can be
    adjusted on its own."""
    assert a_cancellation(conn, start="08:30") is not None
    assert a_cancellation(conn, start="14:45") is not None
    conn.commit()
    assert len(store.adjustments_for(conn, "2026-09-21")) == 2


def test_the_same_session_on_a_different_date_is_a_different_adjustment(conn):
    assert a_cancellation(conn, day="2026-09-21") is not None
    assert a_cancellation(conn, day="2026-09-28") is not None
    conn.commit()
    assert len(store.list_adjustments(conn)) == 2


def test_removing_one_lets_the_pattern_reassert_itself(conn):
    adjustment_id = a_cancellation(conn)
    conn.commit()
    assert store.delete_adjustment(conn, adjustment_id) is True
    assert store.delete_adjustment(conn, adjustment_id) is False
    assert store.adjustments_for(conn, "2026-09-21") == []


# --------------------------------------------------------------------------
# finding what changes a day
# --------------------------------------------------------------------------

def test_a_day_sees_both_what_left_it_and_what_arrived(conn):
    """Two different questions, one answer. A session moved in from another
    week is exactly the case a single-column query would miss."""
    a_move(conn, day="2026-09-21", to_date="2026-09-23")
    conn.commit()

    assert [row["kind"] for row in store.adjustments_for(conn, "2026-09-21")] == ["moved"]
    assert [row["kind"] for row in store.adjustments_for(conn, "2026-09-23")] == ["moved"]
    assert store.adjustments_for(conn, "2026-09-22") == []


def test_a_cancellation_is_only_seen_on_its_own_date(conn):
    a_cancellation(conn, day="2026-09-21")
    conn.commit()
    assert store.adjustments_for(conn, "2026-09-22") == []


def test_listing_since_a_date_keeps_a_move_that_lands_in_the_future(conn):
    """Either end of a move can be the future one."""
    a_move(conn, day="2026-09-01", to_date="2026-12-01")
    conn.commit()
    assert len(store.list_adjustments(conn, since="2026-10-01")) == 1


def test_repointing_follows_a_rename(conn):
    adjustment_id = a_cancellation(conn, subject="Calculus III")
    conn.commit()

    assert store.repoint_adjustment(
        conn, adjustment_id, subject="Calculus 3", course_id=MANUAL
    ) is True
    row = store.get_adjustment(conn, adjustment_id)
    assert (row["subject"], row["course_id"]) == ("Calculus 3", MANUAL)


# --------------------------------------------------------------------------
# manual rows: no sync reaches them, every backup carries them
# --------------------------------------------------------------------------

def test_no_sync_reconciliation_can_reach_the_table(conn):
    """Not a promise to keep in mind -- `soft_delete_missing` refuses the table
    by name, because it is not in RESOURCE_TABLES."""
    assert "timetable_adjustments" not in store.RESOURCE_TABLES
    with pytest.raises(ValueError, match="not a reconcilable resource table"):
        store.soft_delete_missing(conn, "timetable_adjustments", "c1", set())


def test_adjustments_survive_a_full_sync(conn):
    from agent.sync.poller import sync

    a_cancellation(conn)
    a_move(conn, start="14:45")
    conn.commit()

    class FakeConfig:
        tracked_courses = ["c1"]
        ignored_courses: list[str] = []

    class FakeClient:
        def list_coursework(self, course_id):
            return []

        list_coursework_materials = list_announcements = list_submissions = (
            list_coursework
        )

    sync(FakeConfig(), conn, client=FakeClient())
    sync(FakeConfig(), conn, client=FakeClient())
    assert len(store.list_adjustments(conn)) == 2


def test_a_backup_carries_every_adjustment(conn, tmp_path):
    """They join events.notified_at and study_items on the list of what no
    amount of re-running recovers."""
    from agent.config import Config

    config = Config(
        account="someone@example.com", timezone="Africa/Tunis",
        data_dir=tmp_path / "data", tracked_courses=["c1"], ignored_courses=[],
    )
    (config.data_dir / "library").mkdir(parents=True, exist_ok=True)
    a_cancellation(conn)
    a_move(conn, start="14:45")
    conn.commit()

    snapshot = backup.write(config, conn, tmp_path / "snap")
    assert snapshot.rows["timetable_adjustments"] == 2

    fresh = store.connect(tmp_path / "restored.db")
    try:
        backup.restore(config, fresh, snapshot.path)
        fresh.commit()
        restored = store.list_adjustments(fresh)
        assert len(restored) == 2
        assert {row["kind"] for row in restored} == {"cancelled", "moved"}
    finally:
        fresh.close()
