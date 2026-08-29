"""Buttons, delivery, and surviving a restart.

The restart property is the one worth stating: nothing here implements a
resume. Every button carries ids only, every piece of state is a row, and the
message survives on the phone regardless of the process -- so the test below
kills the connection mid-interaction, opens a new one, and replays the next tap
without the bot ever knowing anything happened.

The other property is that every handler is idempotent. Telegram redelivers,
and an old message keeps working buttons forever.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agent.classroom.models import Course, Material
from agent.config import Config
from agent.db import store
from agent.gate import bot, messages, scheduler
from agent.gate import timetable as tt
from agent.notify.telegram import Telegram, TelegramError, _ApiError

TUNIS = ZoneInfo("Africa/Tunis")

TIMETABLE = """
subjects:
  DSA: "c-dsa"
  Database: "c-db"
versions:
  - label: S1
    status: confirmed
    effective_from: 2026-09-01
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC, subject: DSA }
      - { day: mon, start: "13:45", end: "15:15", kind: TUT, subject: Database }
"""

MONDAY = date(2026, 9, 14)
EVENING = "2026-09-13T19:00:00Z"          # 20:00 in Tunis
TRACKED = ["c-dsa", "c-db"]


class StubTransport:
    """Records every JSON call and answers with what the test queued."""

    def __init__(self, updates=None):
        self.calls: list[tuple[str, dict]] = []
        self.update_batches = list(updates or [[]])

    def __call__(self, url, payload, timeout=None):
        self.calls.append((url, payload))
        if url.endswith("/getUpdates"):
            batch = self.update_batches.pop(0) if self.update_batches else []
            return {"ok": True, "result": batch}
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def named(self, method):
        return [payload for url, payload in self.calls if url.endswith("/" + method)]

    @property
    def texts(self):
        return [p.get("text", "") for _, p in self.calls]


class StubUploads:
    """Answers a multipart send with a file_id, the way Telegram does."""

    def __init__(self):
        self.calls: list[str] = []
        self.bodies: list[bytes] = []

    def __call__(self, url, body, content_type):
        self.calls.append(content_type)
        self.bodies.append(body)
        return {"ok": True, "result": {"message_id": 99,
                                       "document": {"file_id": "F1", "file_size": 10}}}

    def filenames(self) -> list[str]:
        return [
            body.split(b'filename="')[1].split(b'"')[0].decode("utf-8")
            for body in self.bodies
            if b'filename="' in body
        ]


@pytest.fixture
def config(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "library" / "files").mkdir(parents=True)
    (data_dir / "library" / "files" / "d1.pdf").write_bytes(b"%PDF-1.4 fake")
    return Config(
        account="me@example.com", timezone="Africa/Tunis", data_dir=data_dir,
        tracked_courses=list(TRACKED), ignored_courses=[],
        timetable_path_override=tmp_path / "timetable.yaml",
    )


@pytest.fixture
def table(tmp_path):
    (tmp_path / "timetable.yaml").write_text(TIMETABLE, encoding="utf-8")
    return tt.load(tmp_path / "timetable.yaml")


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    for course_id, name in (("c-dsa", "DSA- pi1A"), ("c-db", "Database GA 2026")):
        store.upsert_course(
            connection,
            Course(id=course_id, name=name, section=None, room=None, owner_id=None,
                   course_state="ACTIVE", enrollment_code=None, alternate_link=None,
                   creation_time=None, update_time=None, content_hash="h"),
        )
    yield connection
    connection.close()


def client(transport=None, uploads=None):
    return Telegram("t", 1, transport=transport or StubTransport(),
                    multipart_transport=uploads or StubUploads(),
                    sleep=lambda _: None)


def post(conn, *, course_id="c-dsa", parent_id="p1", created="2026-09-01T00:00:00Z",
         drive_id="d1", pages=92, scan=14, ocr=14, status="ok", size=1000):
    conn.execute(
        "INSERT INTO coursework_materials (id, course_id, title, alternate_link, "
        "creation_time, content_hash, first_seen_at) "
        "VALUES (?, ?, ?, 'https://classroom/x', ?, 'h', 'now')",
        (parent_id, course_id, f"Lecture {parent_id}", created),
    )
    if drive_id:
        store.upsert_material(
            conn,
            Material(id=f"coursework_material:{parent_id}:driveFile:{drive_id}",
                     parent_type="coursework_material", parent_id=parent_id,
                     course_id=course_id, kind="driveFile", ref=drive_id,
                     drive_id=drive_id, title=f"{drive_id}.pdf",
                     url="https://drive/x", content_hash="h"),
        )
        store.upsert_extraction(
            conn, drive_id, status=status, pages=pages, chars=9000, scan_pages=scan,
            ocr_pages=ocr, local_path=f"files/{drive_id}.pdf",
            text_path=f"text/{drive_id}.txt", size_bytes=size,
        )
    store.ensure_study_item(
        conn, entity_type="coursework_material", entity_id=parent_id,
        course_id=course_id, state="pending",
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM study_items WHERE entity_id = ?", (parent_id,)
    ).fetchone()["id"]


def make_run(conn, config, table, day=MONDAY):
    plan = scheduler.plan_for(conn, TRACKED, table, day)
    run_id = store.create_gate_run(
        conn, for_date=day.isoformat(), plan=plan.to_json(),
        version_label=plan.version_label,
    )
    store.mark_gate_sent(conn, run_id, 500)
    conn.commit()
    return run_id, plan


def press(conn, config, telegram, data, *, now=EVENING, callback_id="cb1"):
    return bot.handle_callback(
        conn, config, telegram,
        {"id": callback_id, "data": data},
        tz=TUNIS, now=now,
    )


# --------------------------------------------------------------- delivery

def test_starting_a_subject_sends_the_document_and_marks_it_delivered(
    conn, config, table
):
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)
    transport, uploads = StubTransport(), StubUploads()

    result = press(conn, config, client(transport, uploads),
                   messages.encode("g", run_id, "s0"))

    assert result.kind == "delivered"
    assert uploads.calls, "the PDF should have been uploaded"
    assert store.get_study_item(conn, item_id)["state"] == "delivered"
    assert store.get_study_item(conn, item_id)["delivered_at"] is not None


def test_the_uploaded_file_id_is_kept_and_reused(conn, config, table):
    post(conn)
    run_id, _ = make_run(conn, config, table)
    transport, uploads = StubTransport(), StubUploads()

    press(conn, config, client(transport, uploads), messages.encode("g", run_id, "s0"))
    assert conn.execute(
        "SELECT file_id FROM telegram_files WHERE drive_id = 'd1'"
    ).fetchone()["file_id"] == "F1"

    # A second delivery costs no upload at all.
    transport2, uploads2 = StubTransport(), StubUploads()
    press(conn, config, client(transport2, uploads2), messages.encode("g", run_id, "s0"))
    assert uploads2.calls == []
    assert any(p.get("document") == "F1" for p in transport2.named("sendDocument"))


def test_a_stale_file_id_falls_back_to_the_bytes(conn, config, table):
    post(conn)
    run_id, _ = make_run(conn, config, table)
    store.remember_telegram_file(conn, drive_id="d1", file_id="STALE")
    conn.commit()

    class Refusing(StubTransport):
        def __call__(self, url, payload, timeout=None):
            if url.endswith("/sendDocument") and payload.get("document") == "STALE":
                raise _ApiError("wrong file identifier", status=400, retry_after=None)
            return super().__call__(url, payload, timeout)

    uploads = StubUploads()
    press(conn, config, client(Refusing(), uploads), messages.encode("g", run_id, "s0"))

    assert uploads.calls, "it should have re-uploaded the bytes"
    assert conn.execute(
        "SELECT file_id FROM telegram_files WHERE drive_id = 'd1'"
    ).fetchone()["file_id"] == "F1"


def test_an_oversized_file_is_linked_rather_than_failing(conn, config, table):
    post(conn, size=80 * 1024 * 1024)
    (config.library_dir / "files" / "d1.pdf").write_bytes(b"x" * (51 * 1024 * 1024))
    run_id, _ = make_run(conn, config, table)
    transport = StubTransport()

    result = press(conn, config, client(transport), messages.encode("g", run_id, "s0"))

    assert result.kind == "delivered"
    assert any("over Telegram's upload" in text for text in transport.texts)


def test_a_missing_file_on_disk_is_named_not_dropped(conn, config, table):
    post(conn)
    (config.library_dir / "files" / "d1.pdf").unlink()
    run_id, _ = make_run(conn, config, table)
    transport = StubTransport()

    result = press(conn, config, client(transport), messages.encode("g", run_id, "s0"))

    assert result.kind == "delivered"
    assert any("not on disk" in text for text in transport.texts)


def test_a_failed_send_leaves_the_item_pending(conn, config, table):
    """Delivered-but-never-sent is a lie the coverage figure would inherit."""
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)

    class Failing(StubTransport):
        def __call__(self, url, payload, timeout=None):
            if url.endswith("/sendMessage"):
                raise TelegramError("nope")
            return super().__call__(url, payload, timeout)

    with pytest.raises(TelegramError):
        press(conn, config, client(Failing()), messages.encode("g", run_id, "s0"))

    assert store.get_study_item(conn, item_id)["state"] == "pending"


def test_delivery_serves_the_oldest_item(conn, config, table):
    post(conn, parent_id="new", created="2026-09-08T00:00:00Z", drive_id="d2")
    old = post(conn, parent_id="old", created="2026-09-01T00:00:00Z", drive_id="d1")
    run_id, _ = make_run(conn, config, table)

    press(conn, config, client(), messages.encode("g", run_id, "s0"))
    assert store.get_study_item(conn, old)["state"] == "delivered"


# --------------------------------------------------------------- transitions

def test_marking_read_moves_delivered_to_reviewed(conn, config, table):
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)
    telegram = client()

    press(conn, config, telegram, messages.encode("g", run_id, "s0"))
    result = press(conn, config, telegram, messages.encode("i", run_id, item_id, "r"))

    assert result.kind == "reviewed"
    row = store.get_study_item(conn, item_id)
    assert row["state"] == "reviewed"
    assert row["reviewed_at"] is not None
    assert row["verified_at"] is None


def test_a_repeated_tap_changes_nothing_and_says_so(conn, config, table):
    """Telegram redelivers, and an old message keeps working buttons forever."""
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)
    telegram = client()
    press(conn, config, telegram, messages.encode("g", run_id, "s0"))

    first = press(conn, config, telegram, messages.encode("i", run_id, item_id, "r"),
                  now="2026-09-13T19:00:00Z")
    stamp = store.get_study_item(conn, item_id)["reviewed_at"]
    second = press(conn, config, telegram, messages.encode("i", run_id, item_id, "r"),
                   now="2026-09-13T21:00:00Z")

    assert (first.kind, second.kind) == ("reviewed", "repeat")
    assert store.get_study_item(conn, item_id)["reviewed_at"] == stamp


def test_re_delivering_does_not_move_delivered_at(conn, config, table):
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)
    telegram = client()

    press(conn, config, telegram, messages.encode("g", run_id, "s0"),
          now="2026-09-13T19:00:00Z")
    first = store.get_study_item(conn, item_id)["delivered_at"]
    press(conn, config, telegram, messages.encode("g", run_id, "s0"),
          now="2026-09-13T22:00:00Z")

    assert store.get_study_item(conn, item_id)["delivered_at"] == first


def test_skipping_an_item_is_logged_and_never_verified(conn, config, table):
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)

    result = press(conn, config, client(), messages.encode("i", run_id, item_id, "k"))

    row = store.get_study_item(conn, item_id)
    assert result.kind == "skipped"
    assert row["state"] == "skipped"
    assert row["skip_source"] == "user"
    assert str(run_id) in row["skip_reason"]
    assert row["verified_at"] is None


def test_skipping_the_whole_prompt_logs_every_item_it_offered(conn, config, table):
    first = post(conn, parent_id="p1", drive_id="d1")
    second = post(conn, parent_id="p2", created="2026-09-02T00:00:00Z", drive_id="d2")
    third = post(conn, course_id="c-db", parent_id="p3", drive_id="d3")
    run_id, _ = make_run(conn, config, table)

    result = press(conn, config, client(), messages.encode("g", run_id, "k"))

    assert result.kind == "skipped"
    for item_id in (first, second, third):
        row = store.get_study_item(conn, item_id)
        assert row["state"] == "skipped"
        assert "evening prompt for 2026-09-14" in row["skip_reason"]
        assert row["skip_source"] == "user"
    assert store.get_gate_run(conn, run_id)["closed_at"] is not None


def test_nothing_the_bot_does_can_reach_verified(conn, config, table):
    """3b has no quiz, so `verified` must be unreachable from every button."""
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)
    telegram = client()

    for data in (
        messages.encode("g", run_id, "s0"),
        messages.encode("i", run_id, item_id, "r"),
        messages.encode("i", run_id, item_id, "k"),
        messages.encode("g", run_id, "k"),
    ):
        press(conn, config, telegram, data)

    assert conn.execute(
        "SELECT count(*) AS n FROM study_items WHERE state = 'verified'"
    ).fetchone()["n"] == 0


def test_the_store_refuses_to_be_walked_to_verified(conn):
    post(conn)
    with pytest.raises(ValueError, match="not a state the gate may move"):
        store.advance_study_item(conn, 1, "verified")


def test_a_skip_without_a_reason_is_refused(conn):
    item_id = post(conn)
    with pytest.raises(ValueError, match="must record why"):
        store.advance_study_item(conn, item_id, "skipped")


# --------------------------------------------------------------- snooze

def test_snoozing_sets_a_time_two_hours_out(conn, config, table):
    post(conn)
    run_id, _ = make_run(conn, config, table)

    result = press(conn, config, client(), messages.encode("g", run_id, "z"))

    assert result.kind == "snoozed"
    assert store.get_gate_run(conn, run_id)["snoozed_until"] == "2026-09-13T21:00:00Z"


def test_snoozing_is_refused_past_the_morning_of_the_gated_day(conn, config, table):
    """A snooze that outlives the lecture is a skip nobody recorded."""
    post(conn)
    run_id, _ = make_run(conn, config, table)

    # 05:30 UTC on the day itself is 06:30 in Tunis; +2h steps over the cutoff.
    result = press(conn, config, client(), messages.encode("g", run_id, "z"),
                   now="2026-09-14T05:30:00Z")

    assert result.kind == "snooze-refused"
    assert store.get_gate_run(conn, run_id)["snoozed_until"] is None


def test_a_due_snooze_is_picked_up(conn, config, table):
    post(conn)
    run_id, _ = make_run(conn, config, table)
    store.snooze_gate_run(conn, run_id, "2026-09-13T21:00:00Z")
    conn.commit()

    assert [r["id"] for r in store.due_gate_runs(conn, "2026-09-13T20:00:00Z")] == []
    assert [r["id"] for r in store.due_gate_runs(conn, "2026-09-13T21:00:00Z")] == [run_id]


def test_a_closed_run_is_never_re_sent(conn, config, table):
    post(conn)
    run_id, _ = make_run(conn, config, table)
    store.snooze_gate_run(conn, run_id, "2026-09-13T21:00:00Z")
    store.close_gate_run(conn, run_id)
    conn.commit()

    assert store.due_gate_runs(conn, "2026-09-14T09:00:00Z") == []


# --------------------------------------------------------------- stale buttons

def test_a_button_for_a_forgotten_run_is_answered_not_crashed(conn, config, table):
    transport = StubTransport()
    result = press(conn, config, client(transport), messages.encode("g", 999, "s0"))

    assert result.kind == "stale"
    assert transport.named("answerCallbackQuery")


def test_a_button_for_a_deleted_item_is_answered_not_crashed(conn, config, table):
    post(conn)
    run_id, _ = make_run(conn, config, table)
    result = press(conn, config, client(), messages.encode("i", run_id, 4242, "r"))
    assert result.kind == "stale"


def test_a_subject_index_past_the_end_is_answered(conn, config, table):
    post(conn)
    run_id, _ = make_run(conn, config, table)
    assert press(conn, config, client(), messages.encode("g", run_id, "s9")).kind == "stale"


def test_starting_a_subject_that_is_now_empty_says_so(conn, config, table):
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)
    store.advance_study_item(conn, item_id, "skipped", skip_reason="by hand")
    conn.commit()

    assert press(conn, config, client(), messages.encode("g", run_id, "s0")).kind == "empty"


def test_an_unrecognised_button_is_acknowledged(conn, config):
    transport = StubTransport()
    result = bot.handle_callback(
        conn, config, client(transport), {"id": "cb", "data": "zzz:1"}, tz=TUNIS
    )
    assert result.kind == "unknown"
    assert transport.named("answerCallbackQuery")


def test_every_press_is_acknowledged(conn, config, table):
    """An unanswered callback spins on the phone until Telegram times it out."""
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)
    transport = StubTransport()
    telegram = client(transport)

    for data in (messages.encode("g", run_id, "s0"),
                 messages.encode("i", run_id, item_id, "r"),
                 messages.encode("g", run_id, "z"),
                 messages.encode("g", run_id, "k")):
        press(conn, config, telegram, data)

    assert len(transport.named("answerCallbackQuery")) == 4


# --------------------------------------------------------------- the loop

def test_the_offset_survives_a_restart(conn, config, table):
    post(conn)
    run_id, _ = make_run(conn, config, table)
    transport = StubTransport(updates=[[
        {"update_id": 77, "callback_query": {"id": "a", "data": messages.encode("g", run_id, "z")}}
    ]])

    bot.poll(conn, config, client(transport), tz=TUNIS, timeout=0, once=True)
    assert store.get_bot_state(conn, bot.OFFSET_KEY) == "77"

    # A fresh loop asks for everything after it, so nothing is replayed.
    transport2 = StubTransport(updates=[[]])
    bot.poll(conn, config, client(transport2), tz=TUNIS, timeout=0, once=True)
    assert transport2.named("getUpdates")[0]["offset"] == 78


def test_a_restart_mid_interaction_resumes_with_no_lost_progress(conn, config, table):
    """The headline property. Nothing implements a resume -- state is in rows,
    the message is on the phone, and the next tap simply works."""
    item_id = post(conn)
    run_id, _ = make_run(conn, config, table)
    db_path = config.db_path

    # Deliver, then lose the process entirely.
    bot.poll(conn, config, client(StubTransport(updates=[[
        {"update_id": 1,
         "callback_query": {"id": "a", "data": messages.encode("g", run_id, "s0")}}
    ]])), tz=TUNIS, timeout=0, once=True)
    assert store.get_study_item(conn, item_id)["state"] == "delivered"
    conn.close()

    # A brand new connection, as a restarted bot would have.
    fresh = store.connect(db_path)
    try:
        transport = StubTransport(updates=[[
            {"update_id": 2,
             "callback_query": {"id": "b", "data": messages.encode("i", run_id, item_id, "r")}}
        ]])
        handled = bot.poll(fresh, config, client(transport), tz=TUNIS, timeout=0, once=True)

        assert [event.kind for event in handled] == ["reviewed"]
        assert store.get_study_item(fresh, item_id)["state"] == "reviewed"
        # And it did not re-offer the update it had already handled.
        assert transport.named("getUpdates")[0]["offset"] == 2
    finally:
        fresh.close()


def test_a_non_callback_update_is_acknowledged_and_ignored(conn, config):
    """Otherwise Telegram re-offers it forever."""
    transport = StubTransport(updates=[[
        {"update_id": 5, "message": {"text": "hello"}}
    ]])
    handled = bot.poll(conn, config, client(transport), tz=TUNIS, timeout=0, once=True)

    assert [event.kind for event in handled] == ["ignored"]
    assert store.get_bot_state(conn, bot.OFFSET_KEY) == "5"


def test_the_loop_only_asks_for_the_updates_it_acts_on(conn, config):
    transport = StubTransport()
    bot.poll(conn, config, client(transport), tz=TUNIS, timeout=0, once=True)
    asked = transport.named("getUpdates")[0]
    assert asked["allowed_updates"] == ["callback_query", "message"]


# --------------------------------------------------------------- filenames

def test_a_delivered_document_is_named_after_the_lecture(conn, config, table):
    """The library is keyed by Drive id. A phone full of
    11kqW48qFWWRMiWNUmOK69ZlkTQeKIgye.pdf is a library I cannot use."""
    post(conn)
    run_id, _ = make_run(conn, config, table)
    uploads = StubUploads()

    press(conn, config, client(StubTransport(), uploads),
          messages.encode("g", run_id, "s0"))

    assert uploads.filenames() == ["d1.pdf"]


def test_the_drive_title_is_what_reaches_the_phone(conn, config, table):
    post(conn)
    conn.execute("UPDATE materials SET title = ? WHERE drive_id = 'd1'",
                 ("Chapter 1 : arbres / listes.pdf",))
    conn.commit()
    run_id, _ = make_run(conn, config, table)
    uploads = StubUploads()

    press(conn, config, client(StubTransport(), uploads),
          messages.encode("g", run_id, "s0"))

    assert uploads.filenames() == ["Chapter 1 arbres listes.pdf"]


def test_a_title_with_no_extension_gains_the_real_one(conn, config, table):
    """A Google-native document has no extension in Drive and is exported to
    PDF locally, so the phone needs telling what to open it with."""
    post(conn)
    conn.execute("UPDATE materials SET title = 'Chapter 1' WHERE drive_id = 'd1'")
    conn.commit()
    run_id, _ = make_run(conn, config, table)
    uploads = StubUploads()

    press(conn, config, client(StubTransport(), uploads),
          messages.encode("g", run_id, "s0"))

    assert uploads.filenames() == ["Chapter 1.pdf"]


def test_a_title_whose_extension_disagrees_with_the_bytes_keeps_both(conn, config, table):
    """Substituting would hide what it was called; appending leaves a file that
    opens and a name that still says where it came from."""
    post(conn)
    conn.execute("UPDATE materials SET title = 'notes.docx' WHERE drive_id = 'd1'")
    conn.commit()
    run_id, _ = make_run(conn, config, table)
    uploads = StubUploads()

    press(conn, config, client(StubTransport(), uploads),
          messages.encode("g", run_id, "s0"))

    assert uploads.filenames() == ["notes.docx.pdf"]


def test_a_missing_title_falls_back_to_the_local_name(conn, config, table):
    post(conn)
    conn.execute("UPDATE materials SET title = NULL WHERE drive_id = 'd1'")
    conn.commit()
    run_id, _ = make_run(conn, config, table)
    uploads = StubUploads()

    press(conn, config, client(StubTransport(), uploads),
          messages.encode("g", run_id, "s0"))

    assert uploads.filenames() == ["d1.pdf"]
