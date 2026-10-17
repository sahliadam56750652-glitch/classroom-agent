"""The gate waits for tonight's material, by fact rather than by timing (2026-10-17).

Last night the PC woke at 22:04 and both missed tasks caught up together: the
gate built its prompt at 21:04:29Z, the sync started at 21:04:31Z, and the
prompt went out without Nanotechnology's new files. Now `agent run` holds
run.lock and records when its material stages are done; `agent gate` waits for
that, or does the material stages itself under the lock -- never notify, which
stays in `agent run` so events still go out exactly once.

The catch-up case is tested for real: two threads, two handles on one lock
file, in either order.
"""

from __future__ import annotations

import argparse
import threading
import time
from datetime import datetime, timezone

import pytest

from agent import backup, cli, locks
from agent.config import Config
from agent.db import store
from agent.files import drive, extract, ocr, packs
from agent.sync import deadlines as deadlines_mod
from agent.sync import poller


@pytest.fixture
def config(tmp_path) -> Config:
    data = tmp_path / "data"
    (data / "library").mkdir(parents=True)
    made = Config(account="me@example.com", timezone="Africa/Tunis", data_dir=data,
                  tracked_courses=["c1"], ignored_courses=[], telegram_chat_id=1)
    store.open_db(made).close()
    return made


@pytest.fixture
def order(monkeypatch):
    """What ran, in order, across threads. Every stage stubbed; sync takes a second."""
    seen: list[str] = []
    guard = threading.Lock()

    def note(name):
        with guard:
            seen.append(name)

    def stage(name, value, delay=0.0):
        def stub(*a, **kw):
            if delay:
                time.sleep(delay)
            note(name)
            return value
        return stub

    monkeypatch.setattr(cli, "_do_sync", stage("run:sync", poller.SyncResult(), delay=1.0))
    monkeypatch.setattr(cli, "_do_fetch", stage("run:fetch", drive.FetchResult()))
    monkeypatch.setattr(cli, "_do_extract", stage("run:extract", extract.ExtractResult()))
    monkeypatch.setattr(cli, "_do_render", stage("run:render", None))
    monkeypatch.setattr(cli, "_do_ocr", stage("run:ocr", ocr.OCRResult()))
    monkeypatch.setattr(cli, "_do_studyitems", stage("run:studyitems", (0, 0)))
    monkeypatch.setattr(cli, "_do_prepare_quizzes", stage("run:quizzes", None))
    monkeypatch.setattr(cli, "_do_packs", stage("run:packs", packs.PacksResult()))
    monkeypatch.setattr(cli, "_do_deadlines", stage("run:deadlines", deadlines_mod.DeadlineScan(events=[])))
    monkeypatch.setattr(backup, "daily", stage("run:backup", None))
    monkeypatch.setattr(cli, "_do_notify", stage("run:notify", 0))

    def gate_pass(config, conn):
        time.sleep(1.0)
        note("gate:material")
        return True

    monkeypatch.setattr(cli, "_gate_material_pass", gate_pass)
    monkeypatch.setattr(cli, "GATE_POLL_SECONDS", 0.1)
    return seen


def run_args():
    return argparse.Namespace(dry_run=False)


def evening(hour=20, minute=0):
    """A moment on 17 Oct, Tunis time (UTC+1), as UTC."""
    return datetime(2026, 10, 17, hour - 1, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------- the catch-up


def test_the_run_first_then_the_gate_waits_for_its_material(config, order):
    """The run takes the lock; the gate, a moment later, waits -- and does not
    sync again, because the run's material is what it was waiting for."""
    result = {}
    runner = threading.Thread(target=lambda: cli.cmd_run(config, run_args()))
    runner.start()
    time.sleep(0.3)
    result["note"] = cli._material_for_gate(config, wait_seconds=30)
    order.append("gate:built")
    runner.join()

    assert result["note"] is None
    assert "gate:material" not in order
    assert order.index("run:studyitems") < order.index("gate:built")
    assert order.count("run:notify") == 1


def test_the_gate_first_then_the_run_waits_for_the_lock(config, order):
    """The gate finds nothing tonight and no run holding the lock, so it brings
    the material in itself; the run, a moment later, waits and then runs whole."""
    result = {}
    gate = threading.Thread(target=lambda: result.update(note=cli._material_for_gate(config, wait_seconds=30)))
    gate.start()
    time.sleep(0.3)
    cli.cmd_run(config, run_args())
    gate.join()

    assert result["note"] is None
    assert order.index("gate:material") < order.index("run:sync")
    # Notify only ever from agent run, once.
    assert order.count("run:notify") == 1


def test_a_run_already_done_tonight_means_no_wait_and_no_lock(config, order):
    conn = store.open_db(config)
    run_id = store.start_pipeline_run(conn, "run", now="2026-10-17T18:31:00Z")
    store.mark_material_done(conn, run_id, classroom_ok=True, now="2026-10-17T18:40:00Z")
    conn.commit()
    conn.close()
    holder = locks.run_lock(config.data_dir)
    assert holder.try_acquire()  # a later stage of that run still holds it
    try:
        assert cli._material_for_gate(config, now=evening(20, 0), wait_seconds=1) is None
    finally:
        holder.release()
    assert order == []


def test_a_run_from_this_morning_does_not_count(config, order):
    conn = store.open_db(config)
    run_id = store.start_pipeline_run(conn, "run", now="2026-10-17T06:30:00Z")
    store.mark_material_done(conn, run_id, classroom_ok=True, now="2026-10-17T06:40:00Z")
    conn.commit()
    conn.close()
    assert cli._material_for_gate(config, now=evening(20, 0), wait_seconds=5) is None
    assert order == ["gate:material"]


def test_after_midnight_last_nights_run_still_counts(config, order):
    conn = store.open_db(config)
    run_id = store.start_pipeline_run(conn, "run", now="2026-10-17T21:04:31Z")
    store.mark_material_done(conn, run_id, classroom_ok=True, now="2026-10-17T21:06:00Z")
    conn.commit()
    conn.close()
    # 00:30 on the 18th in Tunis: the most recent 19:30 is the 17th's.
    late = datetime(2026, 10, 17, 23, 30, tzinfo=timezone.utc)
    assert cli._material_for_gate(config, now=late, wait_seconds=5) is None
    assert order == []


# ---------------------------------------------------------------- the line it carries


def test_giving_up_still_lets_the_prompt_go_with_one_line(config, order):
    conn = store.open_db(config)
    conn.execute("INSERT INTO sync_runs (started_at, finished_at, status) "
                 "VALUES ('2026-10-17T06:30:00Z', '2026-10-17T06:31:00Z', 'ok')")
    conn.commit()
    conn.close()
    holder = locks.run_lock(config.data_dir)
    assert holder.try_acquire()  # a run that never finishes its material
    try:
        started = time.monotonic()
        note = cli._material_for_gate(config, now=evening(20, 0), wait_seconds=1)
    finally:
        holder.release()
    assert time.monotonic() - started < 5
    assert note.startswith("Material was last synced Sat 17 Oct, 07:31")
    assert "did not finish within 0 minutes" in note or "did not finish within" in note


def test_a_run_that_could_not_reach_classroom_says_so(config, order):
    conn = store.open_db(config)
    run_id = store.start_pipeline_run(conn, "run", now="2026-10-17T18:31:00Z")
    store.mark_material_done(conn, run_id, classroom_ok=False, now="2026-10-17T18:32:00Z")
    conn.commit()
    conn.close()
    note = cli._material_for_gate(config, now=evening(20, 0), wait_seconds=1)
    assert note == ("Material was last synced never; tonight the sync could not reach Classroom, "
                    "so anything posted since is not in this prompt.")


def test_a_failed_sync_in_the_run_is_recorded_as_not_reaching_classroom(config, order, monkeypatch):
    def down(*a, **kw):
        raise RuntimeError("Classroom 503")

    monkeypatch.setattr(cli, "_do_sync", down)
    cli.cmd_run(config, run_args())
    conn = store.open_db(config)
    row = conn.execute("SELECT * FROM pipeline_runs").fetchone()
    conn.close()
    assert row["kind"] == "run" and row["classroom_ok"] == 0
    assert row["material_done_at"] and row["finished_at"]


# ---------------------------------------------------------------- the lock


def test_the_lock_is_exclusive_and_released_on_release(tmp_path):
    first, second = locks.FileLock(tmp_path / "x.lock"), locks.FileLock(tmp_path / "x.lock")
    assert first.try_acquire()
    assert not second.try_acquire()
    first.release()
    assert second.try_acquire()
    second.release()


def test_the_lock_is_released_when_its_process_dies(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "run.lock"
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, %r); from agent import locks; "
        "lock = locks.FileLock(%r); assert lock.try_acquire(); print('held', flush=True); time.sleep(60)"
    ) % (str(__import__("agent").__path__[0].rsplit("agent", 1)[0]), str(path))],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert not locks.FileLock(path).try_acquire()
        holder.kill()
        holder.wait(timeout=10)
        # Windows drops a dead process's locks "depending on available system
        # resources" -- usually at once, sometimes a moment later. The gate
        # polls every few seconds, so a moment is fine; never is not.
        mine = locks.FileLock(path)
        assert mine.acquire(10, every=0.1)
        mine.release()
    finally:
        if holder.poll() is None:
            holder.kill()


def test_a_second_bot_leaves_quietly(config, monkeypatch, capsys):
    held = locks.bot_lock(config.data_dir)
    assert held.try_acquire()
    try:
        monkeypatch.setattr(cli, "_listen", lambda config, args: pytest.fail("listened twice"))
        assert cli.cmd_bot(config, argparse.Namespace(once=False, timeout=30)) == 0
    finally:
        held.release()
    assert "already listening" in capsys.readouterr().out


# ---------------------------------------------------------------- cmd_gate, whole


def test_the_gate_sends_its_notes_after_waiting(config, monkeypatch):
    """cmd_gate end to end, stages stubbed: the material line and a bot-down line
    reach the message that is sent, right after the header."""
    from test_gate_messages import build

    from agent.gate import scheduler

    monkeypatch.setattr(cli.timetable_mod, "load", lambda path: object())
    monkeypatch.setattr(cli.scope_mod, "local", lambda config, table: set())
    monkeypatch.setattr(scheduler, "plan_for", lambda *a, **kw: build())
    monkeypatch.setattr(cli.gate_scheduler, "plan_for", lambda *a, **kw: build())
    monkeypatch.setattr(cli, "_material_for_gate",
                        lambda config: "Material was last synced Sat 17 Oct, 07:31; tonight the sync could not reach Classroom, so anything posted since is not in this prompt.")
    sent = []

    class Telegram:
        def send_with_keyboard(self, text, keyboard):
            sent.append(text)
            return {"ok": True, "result": {"message_id": 9}}

    monkeypatch.setattr(cli.telegram_api, "from_config", lambda config: Telegram())
    args = argparse.Namespace(on=None, dry_run=False, force=False)
    assert cli.cmd_gate(config, args) == 0
    (text,) = sent
    notes = text.split("\n\n")[1]
    assert "The bot is not running" in notes
    assert "Material was last synced Sat 17 Oct, 07:31" in notes


def test_a_dry_run_gate_never_waits_or_syncs(config, monkeypatch, capsys):
    from test_gate_messages import build

    monkeypatch.setattr(cli.timetable_mod, "load", lambda path: object())
    monkeypatch.setattr(cli.scope_mod, "local", lambda config, table: set())
    monkeypatch.setattr(cli.gate_scheduler, "plan_for", lambda *a, **kw: build())
    monkeypatch.setattr(cli, "_material_for_gate", lambda config: pytest.fail("waited on a dry run"))
    assert cli.cmd_gate(config, argparse.Namespace(on=None, dry_run=True, force=False)) == 0
    assert "dry run -- nothing sent" in capsys.readouterr().out
