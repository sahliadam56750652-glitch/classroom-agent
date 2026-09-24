"""Command line entry point for classroom-agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import auth
from . import backup
from .classroom.client import ClassroomClient
from .classroom.models import ISO_FORMAT, parse_course
from .config import Config, ConfigError, load_config
from .db import store
from .digest import composer
from . import entries
from .files import drive, extract, ocr, packs, upload as upload_mod
from .gate import adjustments as gate_adjust
from .gate import bot as gate_bot
from .gate import messages as gate_messages
from .gate import quiz as gate_quiz
from .gate import scheduler as gate_scheduler
from .gate import sections as gate_sections
from .gate import timetable as timetable_mod
from .llm import provider as llm_provider
from .notify import dispatch
from . import manual
from . import scope as scope_mod
from .notify import telegram as telegram_api
from .sync import deadlines, poller

# Archived courses are excluded unless asked for, and 7 of 25 measured courses
# are archived -- last year's material is exactly what this tool is for.
ALL_COURSE_STATES = ["ACTIVE", "ARCHIVED"]


def _build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="path to config.yaml (default: config.yaml at the repo root)",
    )

    parser = argparse.ArgumentParser(
        prog="agent",
        description="Personal Google Classroom sync, catch-up tracker and revision gate.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "auth",
        parents=[shared],
        help="run the OAuth flow and cache the token",
    )
    sub.add_parser(
        "whoami",
        parents=[shared],
        help="show the authenticated account, granted scopes and data directory",
    )
    sub.add_parser(
        "courses",
        parents=[shared],
        help="fetch and store every course, and show which ones are tracked",
    )

    sync_parser = sub.add_parser(
        "sync",
        parents=[shared],
        help="poll the tracked courses, diff against stored state, record events",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and diff, but write nothing at all",
    )
    sync_parser.add_argument(
        "--seed",
        action="store_true",
        help="first run: store everything and mark every event already notified",
    )
    sync_parser.add_argument(
        "--force",
        action="store_true",
        help="allow --seed even though the database already holds events",
    )

    events_parser = sub.add_parser(
        "events",
        parents=[shared],
        help="list pending events, newest first",
    )
    events_parser.add_argument(
        "--all",
        action="store_true",
        dest="include_notified",
        help="include events that have already been notified",
    )

    deadlines_parser = sub.add_parser(
        "deadlines",
        parents=[shared],
        help="scan stored coursework and record due-date alerts",
    )
    deadlines_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute the alerts but record nothing",
    )

    fetch_parser = sub.add_parser(
        "fetch",
        parents=[shared],
        help="download attachment bytes from Drive into the local library",
    )
    fetch_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report which files would be considered; call Drive not at all",
    )
    fetch_parser.add_argument(
        "--course",
        action="append",
        dest="courses",
        metavar="ID",
        help="limit to one course id; repeatable. Defaults to courses.tracked",
    )
    fetch_parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even when the checksum says the local copy is current",
    )
    fetch_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="stop after N files, for working through a backlog in batches",
    )

    extract_parser = sub.add_parser(
        "extract",
        parents=[shared],
        help="read the fetched files and write their text into the library",
    )
    extract_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="measure only: print the chars-per-page profile, write nothing",
    )
    extract_parser.add_argument(
        "--force",
        action="store_true",
        help="re-read files whose text has already been extracted",
    )

    ocr_parser = sub.add_parser(
        "ocr",
        parents=[shared],
        help="transcribe the pages PyMuPDF could not read, using a vision model",
    )
    ocr_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report which pages would be sent and how many; send nothing",
    )
    ocr_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="send at most N pages, to stay inside a free-tier quota",
    )
    ocr_parser.add_argument(
        "--force",
        action="store_true",
        help="re-transcribe pages that are already cached",
    )
    ocr_parser.add_argument(
        "--verbose",
        action="store_true",
        help="trace the first page: key presence, endpoint, HTTP status and body",
    )
    ocr_parser.add_argument(
        "--status",
        action="store_true",
        help="report the queue order and per-subject progress; transcribe nothing",
    )
    ocr_parser.add_argument(
        "--course",
        action="append",
        dest="courses",
        metavar="ID",
        help="restrict the queue to one course id or timetable subject; repeatable",
    )

    packs_parser = sub.add_parser(
        "packs",
        parents=[shared],
        help="assemble one study document per course from the extracted text",
    )
    packs_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written; write nothing",
    )
    packs_parser.add_argument(
        "--course",
        action="append",
        dest="courses",
        metavar="ID",
        help="build one course; repeatable",
    )
    packs_parser.add_argument(
        "--all",
        action="store_true",
        help="build every tracked course (the default when --course is absent)",
    )
    packs_parser.add_argument(
        "--force",
        action="store_true",
        help="rewrite even when the extracted text has not changed",
    )

    sub.add_parser(
        "missing",
        parents=[shared],
        help="list attachments that no longer exist in Drive",
    )

    study_parser = sub.add_parser(
        "studyitems",
        parents=[shared],
        help="create a revision item for each post whose material was extracted",
    )
    study_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be created; write nothing",
    )
    study_parser.add_argument(
        "--seed",
        action="store_true",
        help="record the existing backlog as skipped rather than pending",
    )
    study_parser.add_argument(
        "--force",
        action="store_true",
        help="allow --seed even though study items already exist",
    )
    study_parser.add_argument(
        "--reopen",
        nargs="+",
        type=int,
        metavar="ID",
        default=None,
        help="put skipped study items back in the queue (the only way out of skipped)",
    )

    adjust_parser = sub.add_parser(
        "adjust",
        parents=[shared],
        help="record that a professor moved, cancelled or added one session",
    )
    adjust_parser.add_argument(
        "--cancel", action="store_true", help="the session did not happen",
    )
    adjust_parser.add_argument(
        "--move", action="store_true", help="the session happened elsewhere or later",
    )
    adjust_parser.add_argument(
        "--extra", action="store_true",
        help="a session the weekly pattern does not contain at all",
    )
    adjust_parser.add_argument(
        "--subject", default=None, metavar="NAME",
        help="the timetable subject (exact, never a near-match)",
    )
    adjust_parser.add_argument(
        "--on", default=None, metavar="YYYY-MM-DD",
        help="the date in the pattern this is about; also filters the listing",
    )
    adjust_parser.add_argument(
        "--at", default=None, metavar="HH:MM",
        help="which session, when the subject meets more than once that day",
    )
    adjust_parser.add_argument(
        "--to", dest="to_date", default=None, metavar="YYYY-MM-DD",
        help="--move: the date it moved to",
    )
    adjust_parser.add_argument(
        "--to-time", dest="to_time", default=None, metavar="HH:MM",
        help="--move: the time it moved to; --extra: when it starts",
    )
    adjust_parser.add_argument(
        "--end", default=None, metavar="HH:MM",
        help="when it ends (default: keep the session's own length)",
    )
    adjust_parser.add_argument(
        "--kind", default=None, choices=sorted(timetable_mod.KINDS),
        help="LEC, TUT, LAB or Project",
    )
    adjust_parser.add_argument(
        "--room", default=None, metavar="TEXT", help="where it happened instead",
    )
    adjust_parser.add_argument(
        "--teacher", default=None, metavar="NAME", help="--extra: who takes it",
    )
    adjust_parser.add_argument(
        "--reason", default=None, metavar="TEXT",
        help="why -- worth having, because how often a session moves is a fact",
    )
    adjust_parser.add_argument(
        "--remove", type=int, default=None, metavar="ID",
        help="drop an adjustment; the pattern reasserts itself",
    )
    adjust_parser.add_argument(
        "--repoint", type=int, default=None, metavar="ID",
        help="point an orphaned adjustment at what its subject is called now",
    )
    adjust_parser.add_argument(
        "--all", action="store_true", dest="include_past",
        help="list adjustments from before today too",
    )
    adjust_parser.add_argument(
        "--dry-run", action="store_true",
        help="resolve and report; write nothing",
    )

    backup_parser = sub.add_parser(
        "backup",
        parents=[shared],
        help="snapshot everything no amount of re-syncing can recover, or restore one",
    )
    backup_parser.add_argument(
        "--out", type=Path, default=None, metavar="DIR",
        help="where to write it (default: DATA_DIR/backups/manual-<UTC>)",
    )
    backup_parser.add_argument(
        "--restore", type=Path, default=None, metavar="DIR",
        help="put a snapshot back. Adds what is missing; never overwrites",
    )
    backup_parser.add_argument(
        "--list", action="store_true", dest="show",
        help="report what a snapshot would hold; write nothing",
    )
    backup_parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be written or restored; change nothing",
    )

    projects_parser = sub.add_parser(
        "projects",
        parents=[shared],
        help="record a project with milestones and a deadline, or show progress",
    )
    projects_parser.add_argument(
        "--add", action="store_true", help="record a new project",
    )
    projects_parser.add_argument(
        "--subject", default=None, metavar="NAME",
        help="the timetable subject it belongs to",
    )
    projects_parser.add_argument(
        "--title", default=None, metavar="TEXT", help="what the project is called",
    )
    projects_parser.add_argument(
        "--deadline", default=None, metavar="WHEN",
        help="YYYY-MM-DD, or 'YYYY-MM-DD HH:MM'. A date alone means end of day",
    )
    projects_parser.add_argument(
        "--deliverable", action="append", dest="deliverables", default=None,
        metavar="TEXT", help="something that has to be handed in; repeatable",
    )
    projects_parser.add_argument(
        "--milestone", action="append", dest="milestones", default=None,
        metavar="TEXT",
        help="a step that either happened or did not; repeatable, in order",
    )
    projects_parser.add_argument(
        "--team", action="append", dest="team", default=None, metavar="NAME",
        help="who is on it; repeatable",
    )
    projects_parser.add_argument(
        "--brief", default=None, metavar="TEXT",
        help="where the brief came from, e.g. 'verbal, 15 Sep lecture'",
    )
    projects_parser.add_argument(
        "--coursework", default=None, metavar="ID",
        help=(
            "the Classroom coursework id this is the same work as, so it has "
            "ONE deadline and not two"
        ),
    )
    projects_parser.add_argument(
        "--show", type=int, default=None, metavar="ID",
        help="show one project and its milestones",
    )
    projects_parser.add_argument(
        "--done", type=int, default=None, metavar="ID",
        help="mark a milestone done (the only way progress moves)",
    )
    projects_parser.add_argument(
        "--close", type=int, default=None, metavar="ID",
        help="close a project: it stops being chased for a deadline",
    )
    projects_parser.add_argument(
        "--all", action="store_true", dest="include_closed",
        help="include closed projects in the listing",
    )
    projects_parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be recorded; write nothing",
    )

    sessions_parser = sub.add_parser(
        "sessions",
        parents=[shared],
        help="log that a session happened and what it covered, or list what was logged",
    )
    sessions_parser.add_argument(
        "--subject", default=None, metavar="NAME",
        help="the timetable subject (required to log; filters the listing)",
    )
    sessions_parser.add_argument(
        "--on", default=None, metavar="YYYY-MM-DD",
        help="the day it was held (default: today, locally)",
    )
    sessions_parser.add_argument(
        "--kind", default="LEC", choices=sorted(timetable_mod.KINDS),
        help="LEC, TUT, LAB or Project (default: LEC)",
    )
    sessions_parser.add_argument(
        "--covered", default=None, metavar="TEXT",
        help="what was actually covered -- this is the whole point of logging it",
    )
    sessions_parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be logged; write nothing",
    )

    tasks_parser = sub.add_parser(
        "tasks",
        parents=[shared],
        help="record a tutorial or exercise sheet as due, or list what is outstanding",
    )
    tasks_parser.add_argument(
        "--subject", default=None, metavar="NAME",
        help="the timetable subject (required to add; filters the listing)",
    )
    tasks_parser.add_argument(
        "--title", default=None, metavar="TEXT",
        help="what it is called, e.g. 'TD 3, exercises 1-6'",
    )
    tasks_parser.add_argument(
        "--due", default=None, metavar="WHEN",
        help=(
            "YYYY-MM-DD, or 'YYYY-MM-DD HH:MM'. A date with no time means end "
            "of day locally, as an absent Classroom dueTime does"
        ),
    )
    tasks_parser.add_argument(
        "--kind", default="tutorial",
        choices=["tutorial", "exercise_sheet", "other"],
        help="what kind of thing it is (default: tutorial)",
    )
    tasks_parser.add_argument(
        "--notes", default=None, metavar="TEXT", help="anything worth remembering",
    )
    tasks_parser.add_argument(
        "--source", default=None, metavar="TEXT",
        help="where it came from, e.g. 'handed out in class'",
    )
    tasks_parser.add_argument(
        "--done", type=int, default=None, metavar="ID",
        help="mark a task done (it then stops being chased by the scanner)",
    )
    tasks_parser.add_argument(
        "--all", action="store_true", dest="include_done",
        help="include completed tasks in the listing",
    )
    tasks_parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be recorded; write nothing",
    )

    upload_parser = sub.add_parser(
        "upload",
        parents=[shared],
        help="add a local file to a subject's library -- a board, notes, a handout",
    )
    upload_parser.add_argument(
        "file",
        type=Path,
        help="the PDF or photograph to add",
    )
    upload_parser.add_argument(
        "--subject",
        required=True,
        metavar="NAME",
        help="the timetable subject it belongs to (exact, never a near-match)",
    )
    upload_parser.add_argument(
        "--title",
        default=None,
        metavar="TEXT",
        help="what to call it in the gate and the pack (default: the filename)",
    )
    upload_parser.add_argument(
        "--posted",
        default=None,
        metavar="YYYY-MM-DD",
        help="when the material is dated, which is what the OCR queue sorts on",
    )
    upload_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve, check the bytes and report; write nothing",
    )

    subjects_parser = sub.add_parser(
        "subjects",
        parents=[shared],
        help="show each subject's identity, or mint one for a subject with no Classroom",
    )
    subjects_parser.add_argument(
        "--add",
        metavar="NAME",
        default=None,
        help=(
            "mint a course-like id for a subject that has no Classroom and never "
            "will, and print the timetable.yaml line to paste"
        ),
    )

    timetable_parser = sub.add_parser(
        "timetable",
        parents=[shared],
        help="show the week the gate will act on, or check the file for errors",
    )
    timetable_parser.add_argument(
        "--check",
        action="store_true",
        help="validate the file and report anything mapped but never gated",
    )
    timetable_parser.add_argument(
        "--on",
        metavar="YYYY-MM-DD",
        default=None,
        help="show one day rather than the whole week (default: the next 7 days)",
    )

    gate_parser = sub.add_parser(
        "gate",
        parents=[shared],
        help="send tomorrow's revision prompt -- the evening gate",
    )
    gate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the message and the plan; send nothing and write nothing",
    )
    gate_parser.add_argument(
        "--on",
        metavar="YYYY-MM-DD",
        default=None,
        help="prepare for this date instead of tomorrow",
    )
    gate_parser.add_argument(
        "--force",
        action="store_true",
        help="send again even though this date already has a prompt",
    )

    quiz_parser = sub.add_parser(
        "quiz",
        parents=[shared],
        help="generate and run the revision quiz for one study item",
    )
    quiz_parser.add_argument(
        "--item",
        type=int,
        required=True,
        metavar="ID",
        help="the study item to quiz on (see `agent studyitems`)",
    )
    quiz_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the questions to stdout; send nothing and start no attempt",
    )

    sub.add_parser(
        "flagged",
        parents=[shared],
        help="list every quiz question I marked as bad, newest first",
    )

    sections_parser = sub.add_parser(
        "sections",
        parents=[shared],
        help="print how one study item would be cut into evening-sized windows",
    )
    sections_parser.add_argument(
        "--item",
        type=int,
        required=True,
        metavar="ID",
        help="the study item to cut up (see `agent studyitems`)",
    )
    sections_parser.add_argument(
        "--pages",
        type=int,
        default=gate_sections.DEFAULT_BUDGET,
        metavar="N",
        help=f"pages one window may hold (default {gate_sections.DEFAULT_BUDGET})",
    )

    bot_parser = sub.add_parser(
        "bot",
        parents=[shared],
        help="listen for button presses -- runs until stopped",
    )
    bot_parser.add_argument(
        "--once",
        action="store_true",
        help="drain the updates waiting now and exit, rather than polling",
    )
    bot_parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECONDS",
        help="how long each long poll waits (default: 30)",
    )

    notify_parser = sub.add_parser(
        "notify",
        parents=[shared],
        help="send the pending events as a briefing, then stamp them notified",
    )
    notify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the briefing to stdout; send nothing and stamp nothing",
    )

    run_parser = sub.add_parser(
        "run",
        parents=[shared],
        help="sync, then scan deadlines, then notify -- what the scheduler calls",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do all three stages read-only: write nothing and send nothing",
    )
    return parser


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("  (none)")
        return
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))
    ]
    print("  " + "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  " + "  ".join("-" * width for width in widths))
    for row in rows:
        print("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_auth(config: Config, args: argparse.Namespace) -> int:
    auth.get_credentials(config)
    print(f"Authenticated as {config.account}.")
    print(f"Token cached at {config.token_path}")
    return 0


def cmd_whoami(config: Config, args: argparse.Namespace) -> int:
    creds = auth.get_credentials(config)
    granted = auth.read_granted_scopes(config)

    print(f"account:  {auth.account_email(creds)}")
    print(f"timezone: {config.timezone}")
    print(f"data_dir: {config.data_dir}")
    print(f"scopes granted ({len(granted)}):")
    for scope in sorted(granted):
        print(f"  {scope}")
    return 0


def cmd_courses(config: Config, args: argparse.Namespace) -> int:
    """Fetch courses.list, store the rows, and show the tracking status.

    Only courses.list is touched. Coursework, materials and attachments come in
    a later phase.
    """
    creds = auth.get_credentials(config)
    client = ClassroomClient(creds)
    conn = store.open_db(config)

    run_id = store.start_sync_run(conn)
    try:
        raw_courses = client.list_courses(ALL_COURSE_STATES)
        for raw in raw_courses:
            store.upsert_course(conn, parse_course(raw))
        conn.commit()
    except Exception as err:
        store.finish_sync_run(conn, run_id, status="error", error=repr(err))
        raise
    store.finish_sync_run(
        conn, run_id, status="ok", items_seen={"courses": len(raw_courses)}
    )

    tracked = set(config.tracked_courses)
    ignored = set(config.ignored_courses)

    rows = []
    for course in store.list_courses(conn):
        if course["id"] in tracked:
            status = "yes"
        elif course["id"] in ignored:
            status = "ignored"
        else:
            status = "-"
        rows.append(
            [
                course["id"],
                course["name"] or "",
                course["section"] or "",
                course["course_state"] or "",
                status,
            ]
        )
    conn.close()

    print(f"{len(rows)} course(s) stored in {config.db_path}")
    print()
    _print_table(["course id", "name", "section", "courseState", "tracked?"], rows)
    print()
    print("  courseState ACTIVE does NOT mean the course is running this term --")
    print("  it means only that no teacher archived it. Every ACTIVE course on")
    print("  this account is from a finished academic year.")
    print()
    print("  The tracked list is curated by hand. Put the course IDs you want")
    print("  synced under courses.tracked in config.yaml -- nothing is tracked")
    print("  automatically, and no course is synced until you list it there.")
    return 0


def _no_tracked_courses() -> None:
    print("No courses are tracked, so there is nothing to sync.")
    print()
    print("Run `agent courses` to see the list, then put the IDs you want")
    print("under courses.tracked in config.yaml. Nothing is tracked for you --")
    print("courseState ACTIVE does not mean a course is running this term.")


def _do_sync(
    config: Config,
    conn,
    *,
    dry_run: bool = False,
    seed: bool = False,
    force: bool = False,
) -> poller.SyncResult | None:
    """Poll and diff. None means there was nothing tracked to poll."""
    if not config.tracked_courses:
        return None
    client = ClassroomClient(auth.get_credentials(config))
    return poller.sync(
        config, conn, dry_run=dry_run, seed=seed, client=client, force=force
    )


def cmd_sync(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        result = _do_sync(
            config, conn, dry_run=args.dry_run, seed=args.seed, force=args.force
        )
    finally:
        conn.close()

    if result is None:
        _no_tracked_courses()
        return 0
    _print_sync(result)
    return 0


def _print_sync(result: poller.SyncResult) -> None:
    label = "dry run -- nothing written" if result.dry_run else "sync complete"
    if result.seeded and not result.dry_run:
        label = "seeded -- every event marked already notified"
    print(f"{label} ({len(result.courses_synced)} course(s))")
    print()

    print("  fetched:")
    for key, count in result.items_seen.items():
        print(f"    {key:22} {count}")

    if result.deleted:
        print()
        print("  soft-deleted (gone from Classroom, kept in the database):")
        for table, count in sorted(result.deleted.items()):
            print(f"    {table:22} {count}")

    print()
    counts = result.event_counts
    if not counts:
        print("  no changes -- nothing to report")
    else:
        print(f"  events ({len(result.events)}):")
        for event_type, count in sorted(counts.items()):
            print(f"    {event_type:26} {count}")
        if result.dry_run:
            print()
            print("  (dry run: these were computed but not recorded)")
        elif result.seeded:
            print()
            print("  (seeded: recorded and stamped as already notified)")


def _do_fetch(
    config: Config,
    conn,
    *,
    dry_run: bool = False,
    courses: list[str] | None = None,
    force: bool = False,
    limit: int | None = None,
) -> drive.FetchResult:
    """Download attachment bytes. Records a sync_runs row unless dry-running."""
    client = None if dry_run else drive.DriveClient(auth.get_credentials(config))

    run_id = None if dry_run else store.start_sync_run(conn)
    try:
        result = drive.fetch(
            config,
            conn,
            client=client,
            course_ids=courses,
            dry_run=dry_run,
            force=force,
            limit=limit,
        )
    except Exception as err:
        conn.rollback()
        if run_id is not None:
            store.finish_sync_run(conn, run_id, status="error", error=repr(err))
        raise
    if run_id is not None:
        conn.commit()
        store.finish_sync_run(conn, run_id, status="ok", items_seen=result.items_seen())
    return result


def cmd_fetch(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        result = _do_fetch(
            config,
            conn,
            dry_run=args.dry_run,
            courses=args.courses,
            force=args.force,
            limit=args.limit,
        )
    finally:
        conn.close()
    _print_fetch(result)
    return 0


def _megabytes(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MB"


def _print_fetch(result: drive.FetchResult) -> None:
    if result.dry_run:
        print(
            f"dry run -- nothing written "
            f"({result.references} reference(s), {result.files} distinct file(s))"
        )
        return

    print(f"fetch complete ({result.files} distinct file(s) from {result.references} reference(s))")
    print()
    print("  downloaded:")
    print(f"    {'fetched':16} {result.fetched}  ({_megabytes(result.bytes_downloaded)})")
    print(f"    {'unchanged':16} {result.skipped}")

    # Dead references are the expected steady state here, not an incident --
    # roughly a sixth of this account's attachments. They are printed every run
    # because a count that only appears when it is zero teaches nothing.
    print()
    print("  not retrievable:")
    for label, count in (
        ("trashed", result.trashed),
        ("missing (404)", result.missing),
        ("unsupported", result.unsupported),
        ("errors", result.errors),
    ):
        print(f"    {label:16} {count}")

    for note in result.notes:
        print(f"    note: {note}")


def _do_extract(
    config: Config, conn, *, dry_run: bool = False, force: bool = False
) -> extract.ExtractResult:
    """Read fetched bytes into text. Needs no credentials -- everything is local."""
    run_id = None if dry_run else store.start_sync_run(conn)
    try:
        result = extract.extract(config, conn, dry_run=dry_run, force=force)
    except Exception as err:
        conn.rollback()
        if run_id is not None:
            store.finish_sync_run(conn, run_id, status="error", error=repr(err))
        raise
    if run_id is not None:
        conn.commit()
        store.finish_sync_run(conn, run_id, status="ok", items_seen=result.items_seen())
    return result


def cmd_extract(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        result = _do_extract(config, conn, dry_run=args.dry_run, force=args.force)
    finally:
        conn.close()
    _print_extract(result)
    return 0


# Buckets for the chars-per-page profile. The Phase 0 samples landed at 0, 11,
# 19, 285 and 598, so the interesting boundary is an order of magnitude wide and
# these buckets are deliberately coarse.
_PROFILE_BUCKETS = (
    ("0 (no text at all)", 0.0, 1.0),
    ("1-24", 1.0, 25.0),
    ("25-99", 25.0, 100.0),
    ("100-499", 100.0, 500.0),
    ("500+", 500.0, float("inf")),
)


def _print_extract(result: extract.ExtractResult) -> None:
    label = "measured -- nothing written" if result.dry_run else "extract complete"
    print(f"{label} ({result.candidates} fetched file(s))")
    print()
    print("  read:")
    print(f"    {'extracted':16} {result.extracted}")
    print(f"    {'already current':16} {result.skipped}")
    print(f"    {'unsupported':16} {result.unsupported}")
    print(f"    {'errors':16} {result.errors}")

    if result.reasons:
        print()
        print("  not readable:")
        for reason, count in sorted(result.reasons.items(), key=lambda item: -item[1]):
            print(f"    {count:3}  {reason}")

    if not result.pdf_profile:
        return

    print()
    print(f"  PDF text profile ({len(result.pdf_profile)} file(s), {result.pdf_pages} page(s)):")
    for label, low, high in _PROFILE_BUCKETS:
        count = sum(1 for _title, rate, _scans in result.pdf_profile if low <= rate < high)
        bar = "#" * min(count, 40)
        print(f"    {label:20} {count:3}  {bar}")

    print()
    # This is the number the OCR decision turns on. Everything else in this
    # command exists to produce it honestly.
    print(f"  pages that need OCR to be readable: {result.scan_pages}")
    if result.scan_pages:
        worst = sorted(result.pdf_profile, key=lambda item: -item[2])[:5]
        print("  worst affected:")
        for title, rate, scans in worst:
            if scans:
                print(f"    {scans:3} scan page(s)  {rate:6.1f} chars/page  {title}")


def _tracing_transport(inner):
    """Wrap the provider's transport to narrate the first HTTP exchange.

    Diagnostic only. It answers "was a request issued at all, and what came
    back", which the summary counts cannot: a page recorded as pending looks
    identical whether it was sent and refused or never sent.
    """
    state = {"logged": False}

    def transport(url, payload, headers):
        first = not state["logged"]
        if first:
            state["logged"] = True
            print(f"  endpoint: {url}")
            print(f"  api key header present: {'x-goog-api-key' in headers}")
            print("  issuing HTTP request ...")
        try:
            response = inner(url, payload, headers)
        except llm_provider._ApiError as err:
            if first:
                print(f"  HTTP status: {err.status}")
                print(f"  body[:500]: {err.body[:500]}")
            raise
        except Exception as err:
            if first:
                print(f"  no HTTP response at all: {err!r}")
            raise
        if first:
            print("  HTTP status: 200")
            print(f"  body[:500]: {json.dumps(response)[:500]}")
        return response

    return transport


def _do_ocr(
    config: Config,
    conn,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    verbose: bool = False,
    in_scope: list[str] | None = None,
    courses: list[str] | None = None,
) -> ocr.OCRResult:
    """Transcribe unread pages. The only stage in this project that costs money."""
    provider = None
    if not dry_run:
        if verbose:
            key = os.environ.get(llm_provider.KEY_ENV) or ""
            print("== ocr trace ==")
            print(f"  {llm_provider.KEY_ENV} found: {bool(key)} (length {len(key)})")
        provider = llm_provider.from_env(
            transport=_tracing_transport(llm_provider._http_post) if verbose else None
        )
        if verbose:
            print(f"  model: {provider.model}")
            print(f"  pacing: {provider.requests_per_minute} req/min")

    run_id = None if dry_run else store.start_sync_run(conn)
    try:
        result = ocr.run(
            config, conn, provider=provider, dry_run=dry_run, limit=limit,
            force=force, verbose=verbose, in_scope=in_scope, courses=courses,
        )
    except Exception as err:
        conn.rollback()
        if run_id is not None:
            store.finish_sync_run(conn, run_id, status="error", error=repr(err))
        raise
    if run_id is not None:
        conn.commit()
        store.finish_sync_run(conn, run_id, status="ok", items_seen=result.items_seen())
    return result


def _in_scope_courses(config: Config) -> tuple[list[str], list[str], str | None]:
    """(in scope, local, note) -- the two course sets a local stage needs.

    In scope is what this semester is about; local is that union tracked. See
    `agent/scope.py` for why they are two lists. A broken or absent timetable
    must not stop OCR or packs, so the note is returned rather than raised and
    printed where the ordering is being explained.
    """
    in_scope, local, note = scope_mod.resolve(config)
    return sorted(in_scope), sorted(local), note


def _resolve_courses(config: Config, wanted: list[str] | None) -> list[str]:
    """--course values as course ids. A value matching nothing is an error.

    A subject NAME is accepted as well as a course id, because a course id is
    twelve digits and the thing I actually want to force to the front is
    "Database". The lookup is exact, through the timetable's `subjects:` map --
    never a near-match. PLAN.md settled that for the gate and it holds here for
    the same reason: a wrong match sends a day's quota to the wrong subject and
    looks exactly like the feature working.
    """
    if not wanted:
        return []
    try:
        subjects = timetable_mod.load(config.timetable_path).subjects
    except timetable_mod.TimetableError:
        subjects = {}
    by_name = {name.casefold(): course for name, course in subjects.items() if course}

    resolved: list[str] = []
    for value in wanted:
        # A manual course id is never in courses.tracked by design, so the
        # subjects map is the only place it can be recognised from.
        if value in config.tracked_courses or value in subjects.values():
            resolved.append(value)
            continue
        course = by_name.get(value.casefold())
        if course is None:
            known = ", ".join(sorted(name for name in subjects if subjects[name])) or "none"
            raise ConfigError(
                f"--course {value!r} is neither a tracked course id nor a subject "
                f"in {config.timetable_path.name}. Subjects with a course: {known}"
            )
        resolved.append(course)
    return list(dict.fromkeys(resolved))


def _print_ocr_order(queue: list, would_send: list, limit: int = 8) -> None:
    """The queue narrowed to files a real run would actually spend requests on.

    `result.queue` is every candidate, finished ones included, because the run
    still has to merge their cached text back. Leading a cost report with a file
    that costs nothing would describe the wrong order.
    """
    sending = {drive_id for drive_id, _page in would_send}
    _print_ocr_queue([item for item in queue if item.drive_id in sending], limit=limit)


def _print_ocr_queue(items: list, limit: int = 8) -> None:
    """The head of the queue, so the ordering can be seen rather than trusted.

    The point of prioritisation is that ~12 pages a day means the order IS the
    outcome. An order nobody can inspect is one that can quietly stop working,
    which is the same class of defect as a summary that cannot tell two states
    apart.
    """
    if not items:
        print("  queue: empty -- nothing needs OCR.")
        return

    head = items[0]
    print(f"  next: {head.course_name} -- {head.title}")
    print(f"        posted {head.posted_at or 'date unknown'} ({head.why})")
    print()
    print(f"  queue order ({len(items)} file(s), first {min(limit, len(items))} shown):")
    for position, item in enumerate(items[:limit], start=1):
        posted = (item.posted_at or "")[:10] or "  unknown "
        print(
            f"    {position:>2}. {posted}  {item.course_name[:22]:<22} "
            f"{item.title[:34]:<34} {item.why}"
        )
    if len(items) > limit:
        print(f"    ... and {len(items) - limit} more")


def _print_ocr_status(rows: list, dead: int) -> None:
    """Per-subject readiness, then the files holding each subject back.

    Phase 3 generates quizzes per subject, so the aggregate figure is the one
    number that cannot answer its question: a library 90% transcribed is no use
    if the missing 10% is all of one course.
    """
    if not rows:
        print("No file in the library has pages that need OCR.")
        return

    courses: dict[str, list] = {}
    for row in rows:
        courses.setdefault(row["course_name"], []).append(row)

    print(f"OCR progress across {len(courses)} subject(s), {len(rows)} file(s).")

    for course_name in sorted(courses, key=str.casefold):
        entries = courses[course_name]
        needed = sum(int(row["needed"]) for row in entries)
        done = sum(int(row["ok"]) for row in entries)
        failed = sum(int(row["failed"]) for row in entries)
        # Anything neither transcribed nor permanently failed is still to do,
        # whether it has a pending row or has never been looked at.
        outstanding = needed - done - failed

        if outstanding == 0 and failed == 0:
            verdict = "READY"
        elif done == 0:
            verdict = "not started"
        else:
            verdict = f"{outstanding} page(s) left"

        print()
        percent = (100 * done / needed) if needed else 100.0
        print(f"  {course_name}")
        print(
            f"    {done}/{needed} pages transcribed ({percent:.0f}%)"
            f"{f', {failed} failed' if failed else ''}  --  {verdict}"
        )

        for row in sorted(entries, key=lambda r: -(int(r["needed"]) - int(r["ok"]))):
            file_needed = int(row["needed"])
            file_done = int(row["ok"])
            file_failed = int(row["failed"])
            if file_done == file_needed and not file_failed:
                continue  # finished files are not what this report is for
            title = row["title"] or row["drive_id"]
            note = f"  ({file_failed} failed)" if file_failed else ""
            print(f"      {file_done:3}/{file_needed:<3} {title[:64]}{note}")

    _print_dead_pointer(dead)


def cmd_ocr(config: Config, args: argparse.Namespace) -> int:
    in_scope, _local, note = _in_scope_courses(config)
    try:
        wanted = _resolve_courses(config, getattr(args, "courses", None))
    except ConfigError as err:
        print(err, file=sys.stderr)
        return 1

    if args.status:
        conn = store.open_db(config)
        try:
            pending = ocr.queue(
                ocr.pending_candidates(conn),
                store.ocr_candidate_posts(conn),
                tracked=config.tracked_courses,
                in_scope=in_scope,
                courses=wanted or None,
            )
            rows = store.ocr_progress(conn)
            if wanted:
                # --course narrows the whole report, not only its first
                # section. A queue showing one subject above a progress table
                # showing eleven reads as the filter having failed.
                rows = [row for row in rows if row["course_id"] in wanted]
            dead = len(store.dead_references(conn))
        finally:
            conn.close()
        if note:
            print(f"  ! {note}")
            print()
        _print_ocr_queue(pending)
        print()
        _print_ocr_status(rows, dead)
        return 0

    conn = store.open_db(config)
    try:
        result = _do_ocr(
            config, conn, dry_run=args.dry_run, limit=args.limit,
            force=args.force, verbose=args.verbose,
            in_scope=in_scope, courses=wanted or None,
        )
        states = store.count_ocr_pages_by_status(conn)
        dead = len(store.dead_references(conn))
        reasons = store.ocr_error_counts(conn)
    finally:
        conn.close()
    if note:
        print(f"  ! {note}")
    _print_ocr(result, states, dead, reasons)
    return 0


def _print_ocr(
    result: ocr.OCRResult,
    states: dict[str, int],
    dead: int,
    reasons: list[tuple[str, int]] | None = None,
) -> None:
    if result.dry_run:
        print(
            f"dry run -- nothing sent "
            f"({result.pages_considered} page(s) across {result.files} file(s))"
        )
        print()
        print(f"    {'already cached':18} {result.cached}")
        print(f"    {'would be sent':18} {len(result.would_send)}")
        if result.would_send:
            print()
            print("  Each one is a model call against the free-tier quota.")
            print("  Use --limit N to work through them over several runs.")
            print()
            # Which pages a --limit run spends its allowance on is decided
            # entirely by this order, so a dry run that hid it would be
            # reporting the cost without the choice.
            _print_ocr_order(result.queue, result.would_send)
        return

    print(f"ocr complete ({result.files} file(s), {result.pages_considered} page(s) considered)")

    # Calls first, pages second. A page reading "pending" is the outcome; how
    # many requests were actually issued is the diagnosis, and reporting only
    # the former makes a dead key, a retired model and a broken TLS chain
    # indistinguishable from a run that deliberately sent nothing.
    print()
    print(f"  model calls attempted: {result.attempted}")
    print(f"    {'succeeded':18} {result.transcribed}")
    print(f"    {'failed':18} {result.call_failures}")

    print()
    print("  pages:")
    print(f"    {'transcribed':18} {result.transcribed}")
    print(f"    {'from cache':18} {result.cached}")
    print(f"    {'never sent':18} {result.never_attempted}")
    print(f"    {'timed out':18} {result.timed_out}")
    print(f"    {'pending':18} {result.pending}")
    print(f"    {'errored':18} {result.failed}")

    if result.timed_out:
        print()
        print(
            f"  {result.timed_out} page(s) timed out after "
            f"{llm_provider.READ_TIMEOUT_SECONDS}s and were retried before giving up."
        )
        print("  They are pending, not lost, and the run carried on past them.")

    if result.attempted == 0 and result.pages_considered:
        print()
        print("  No request was issued. Every page above was skipped before any")
        print("  call -- a cache hit, --limit, or spent quota, per the reasons below.")

    if result.rate_limited:
        print()
        print(f"  Hit the per-minute rate limit {result.rate_limited} time(s) and waited.")
        print("  Those pages are pending, not lost. Lower GEMINI_RPM in .env to")
        print("  pace the run further and stop hitting it.")

    if result.stop_reason == "repeated-rate-limits":
        print()
        print(f"  Stopped: {ocr.MAX_CONSECUTIVE_RATE_LIMITS} rate limits in a row with")
        print("  nothing getting through, so waiting was no longer helping. This is")
        print("  the request allowance, not necessarily the daily one -- try again")
        print("  shortly, and lower GEMINI_RPM in .env if it keeps happening.")
    elif result.quota_exhausted:
        print()
        print("  The DAILY quota is spent -- this is not the per-minute limit.")
        print("  The remaining pages are recorded as pending, not lost:")
        print("  run `agent ocr` again tomorrow to continue.")

    if reasons:
        print()
        print("  why pages are not transcribed:")
        for reason, count in reasons:
            first_line = reason.splitlines()[0] if reason else "(no reason recorded)"
            print(f"    {count:5}  {first_line[:96]}")

    if states:
        print()
        print("  library totals:")
        for state, count in sorted(states.items()):
            print(f"    {state:18} {count}")

    _print_dead_pointer(dead)


def _print_dead_pointer(dead: int) -> None:
    """A standing reminder that some material is simply gone.

    Measured at 20 of 118 tracked attachments. A count that appears once during
    a fetch and never again is effectively silent, and the only remedy -- asking
    the teacher for the file -- needs me to know it happened.
    """
    if dead:
        print()
        print(f"  {dead} attachment(s) no longer exist in Drive. Run `agent missing` to list them.")


def _timetable_or_none(config: Config):
    """The timetable, or None when it cannot be read.

    A broken timetable must not stop the pipeline, and in this stage it fails
    SAFE: `scope.in_scope(None)` is empty, so nothing is created rather than
    everything being created against the wrong set.
    """
    return scope_mod.load(config)[0]


def _print_studyitems_stage(result: tuple[int, int], *, dry_run: bool) -> None:
    """What the stage did, in the two numbers that are different facts.

    "seen" is how many posts have readable material at all; "created" is how
    many of them the gate did not already know about. Printing only the second
    would make a quiet run and an empty library look identical.
    """
    created, seen = result
    verb = "would create" if dry_run else "created"
    print(f"  {verb} {created} study item(s) from {seen} post(s) with material")


def _do_packs(
    config: Config,
    conn,
    *,
    dry_run: bool = False,
    courses: list[str] | None = None,
    force: bool = False,
) -> packs.PacksResult:
    """Assemble the study documents. Local files only -- nothing is uploaded.

    With no --course, the set is `local`: tracked plus in scope. A manual
    subject has a library and a backlog without ever being tracked, so packs
    built from courses.tracked alone would silently omit it.
    """
    if courses is None:
        courses = _in_scope_courses(config)[1]
    run_id = None if dry_run else store.start_sync_run(conn)
    try:
        result = packs.build(
            config, conn, course_ids=courses, dry_run=dry_run, force=force
        )
    except Exception as err:
        conn.rollback()
        if run_id is not None:
            store.finish_sync_run(conn, run_id, status="error", error=repr(err))
        raise
    if run_id is not None:
        conn.commit()
        store.finish_sync_run(conn, run_id, status="ok", items_seen=result.items_seen())
    return result


def cmd_packs(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        result = _do_packs(
            config, conn, dry_run=args.dry_run, courses=args.courses, force=args.force
        )
    finally:
        conn.close()
    _print_packs(result, config)
    return 0


def _print_packs(result: packs.PacksResult, config: Config) -> None:
    verb = "would write" if result.dry_run else "wrote"
    print(f"{verb} {result.written or sum(1 for p in result.packs if p.reason == 'would be written')} "
          f"of {len(result.packs)} pack(s) into {config.packs_dir}")
    print()

    for pack in result.packs:
        if pack.written or pack.reason == "would be written":
            facts = [f"{pack.sources} source(s)", f"{pack.words} words"]
            if pack.ocr_pages:
                facts.append(f"{pack.ocr_pages} transcribed page(s)")
            if pack.unread_pages:
                facts.append(f"{pack.unread_pages} unread page(s)")
            print(f"  {pack.path.name}")
            print(f"      {pack.course_name} -- {', '.join(facts)}")
        elif pack.reason:
            print(f"  {pack.course_name}: {pack.reason}")
        else:
            # The common case on a routine run, and worth saying: an unchanged
            # pack rewritten twice a day would churn a synced folder for nothing.
            print(f"  {pack.course_name}: unchanged")

    unread = sum(pack.unread_pages for pack in result.packs)
    if unread:
        print()
        print(f"  {unread} page(s) across these packs are images nothing has read yet.")
        print("  They appear as explicit placeholders. Run `agent ocr` to fill them in.")


def cmd_missing(config: Config, args: argparse.Namespace) -> int:
    """List every attachment Classroom still advertises but Drive no longer has."""
    conn = store.open_db(config)
    try:
        rows = store.dead_references(conn)
    finally:
        conn.close()

    if not rows:
        print("Every tracked attachment is still present in Drive.")
        return 0

    trashed = sum(1 for row in rows if row["status"] == "trashed")
    gone = sum(1 for row in rows if row["status"] == "missing")
    print(f"{len(rows)} attachment(s) are no longer retrievable: {trashed} trashed, {gone} deleted.")
    print("Classroom keeps advertising them indefinitely. The only fix is asking the teacher.")
    print()

    current = None
    for row in rows:
        course = row["course_name"] or row["course_id"]
        if course != current:
            print(f"  {course}")
            current = course
        label = "trashed" if row["status"] == "trashed" else "deleted"
        # A dead reference usually has no title either: Classroom omits
        # driveFile.title once the target is gone, which is why so many of
        # these can only be named by their post.
        title = row["title"] or "(title withheld by Classroom)"
        print(f"    [{label:7}] {title}")
    return 0


SEED_SKIP_REASON = "backlog before the gate existed"


class SeedWouldBuryBacklog(Exception):
    """--seed was asked for on a database that already holds study items."""


def _do_studyitems(
    config: Config,
    conn,
    *,
    dry_run: bool = False,
    seed: bool = False,
    force: bool = False,
    course_ids: list[str] | None = None,
) -> tuple[int, int]:
    """Create one study item per post with extracted material. (created, seen).

    Mirrors `sync --seed`: the five tracked courses are a finished academic
    year, so creating ~90 pending items would open the Phase 3 gate claiming I
    am 90 lectures behind and make the Phase 4 coverage figure a lie from day
    one. --seed records that history as skipped, with a reason, because a skip
    recorded as anything else is exactly the dishonesty the gate cannot afford.

    `course_ids` is how the `agent run` stage narrows this to **in scope**
    while the command keeps the wider **local** set. The two callers want
    genuinely different things:

      * Typed by hand, this is a deliberate act with `--seed` and `--force`
        available, so it reaches every course whose material is held --
        tracked or in scope -- and I can see what it did.
      * Run unattended twice a day, it must never widen the backlog on its own.
        A course that is tracked but absent from this semester's timetable is
        one I am fetching files from, not one I am being taught, and pending
        items for it would make the gate claim a backlog I do not have.
    """
    if seed and not force and store.count_rows(conn, "study_items") > 0:
        raise SeedWouldBuryBacklog(
            f"--seed would record everything as already skipped, but the "
            f"database already holds {store.count_rows(conn, 'study_items')} "
            f"study item(s). Pass --force if that is genuinely what you want."
        )

    # `local`, not tracked: a manually uploaded board is a post with extracted
    # material that the gate can never serve if no study item is made for it.
    if course_ids is None:
        course_ids = _in_scope_courses(config)[1]
    rows = store.parents_with_extracted_material(conn, course_ids)
    if dry_run:
        existing = {
            (row["entity_type"], row["entity_id"])
            for row in conn.execute("SELECT entity_type, entity_id FROM study_items")
        }
        return sum(
            1 for row in rows if (row["entity_type"], row["entity_id"]) not in existing
        ), len(rows)

    created = 0
    for row in rows:
        created += store.ensure_study_item(
            conn,
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            course_id=row["course_id"],
            state="skipped" if seed else "pending",
            skip_reason=SEED_SKIP_REASON if seed else None,
            skip_source="seed" if seed else None,
        )
    conn.commit()
    return created, len(rows)


def _do_reopen(conn, item_ids: list[int], *, dry_run: bool = False) -> int:
    """Put skipped items back in the queue, reporting each one by name.

    Prints per item rather than a total, because the two ways this does nothing
    -- the id does not exist, and the item was never skipped -- are different
    facts and a bare "0 reopened" would not distinguish them.
    """
    reopened = 0
    for item_id in item_ids:
        row = store.get_study_item(conn, item_id)
        if row is None:
            print(f"  {item_id}: no such study item", file=sys.stderr)
            continue
        if row["state"] != "skipped":
            print(f"  {item_id}: already {row['state']}, not skipped -- left alone")
            continue
        if dry_run:
            print(f"  {item_id}: would reopen ({row['entity_type']} in {row['course_id']})")
            reopened += 1
            continue
        if store.reopen_study_item(conn, item_id):
            reopened += 1
            print(f"  {item_id}: skipped -> pending ({row['skip_reason']})")
    if not dry_run:
        conn.commit()
    return reopened


def cmd_studyitems(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        if args.reopen:
            # A separate verb sharing a subcommand: reopening is the inverse of
            # creating, and running both in one invocation would mean seeding a
            # backlog and un-seeding part of it in the same breath.
            reopened = _do_reopen(conn, args.reopen, dry_run=args.dry_run)
            states = store.count_study_items_by_state(conn)
            print()
            print(f"{'would reopen' if args.dry_run else 'reopened'}: {reopened} "
                  f"of {len(args.reopen)}")
            for state, count in sorted(states.items()):
                print(f"    {state:12} {count}")
            return 0 if reopened == len(args.reopen) else 1

        created, seen = _do_studyitems(
            config, conn, dry_run=args.dry_run, seed=args.seed, force=args.force
        )
        states = store.count_study_items_by_state(conn)
    finally:
        conn.close()

    if args.dry_run:
        print(f"dry run -- nothing written ({created} new of {seen} post(s) with material)")
        return 0

    label = "seeded as skipped" if args.seed else "created"
    print(f"{label}: {created} (of {seen} post(s) with extracted material)")
    if states:
        print()
        for state, count in sorted(states.items()):
            print(f"    {state:12} {count}")
    return 0


def _session_line(session) -> list[str]:
    """One session as it will read in the gate message, plus its subject lines."""
    subjects = " + ".join(session.subjects)
    teachers = " / ".join(part.teacher for part in session.parts if part.teacher)
    rooms = " / ".join(
        dict.fromkeys(part.room for part in session.parts if part.room)
    )
    head = f"  {session.start}-{session.end}  {session.kind:7} {subjects}"
    if teachers:
        head += f" · {teachers}"
    if rooms:
        head += f" · {rooms}"

    lines = [head]
    for part in session.parts:
        if part.manual:
            lines.append(f"           {part.subject}: manual -- gated from what I enter")
        elif not part.identified:
            # Said out loud every time. A subject that is silently never gated
            # is the failure mode that takes months to notice. The two cases
            # are separated because one of them is a thing to do.
            lines.append(
                f"           {part.subject}: awaiting a Classroom course id "
                f"-- never gated"
            )
    return lines


# The five below are argparse-facing adapters, and that is all they are. Every
# rule they used to hold moved into `entries.py` when the HTTP API needed the
# same validation: `cli.py` translates flags into typed arguments and renders
# the result, and nothing that decides anything lives here any more.


def _subject_course(config: Config, name: str) -> tuple[str, str]:
    """(subject, course id) for a name. Loads the file; `entries` does the rest."""
    return entries.subject_course(timetable_mod.load(config.timetable_path), name)


def _due_at(config: Config, value: str) -> str:
    return entries.parse_due(config, value)


def _today(config: Config) -> date:
    return entries.today(config)


def _lines(values: list[str] | None) -> str | None:
    return entries.joined(values)


def _clock_arg(value: str, flag: str) -> str:
    return entries.parse_clock(value, flag)


def _date_arg(value: str, flag: str) -> date:
    return entries.parse_date(value, flag)


def cmd_adjust(config: Config, args: argparse.Namespace) -> int:
    """Record what a professor did to one session on one date.

    timetable.yaml is never written. It holds the weekly PATTERN and keeps one
    writer; this records the dated facts that the pattern cannot express, and
    the gate resolves the two together.
    """
    try:
        table = timetable_mod.load(config.timetable_path)
    except timetable_mod.TimetableError as err:
        print(err, file=sys.stderr)
        return 1

    if args.remove is not None:
        return _remove_adjustment(config, args.remove)
    if args.repoint is not None:
        return _repoint_adjustment(config, table, args)

    # The flag as I type it, and the kind as the table stores it. Spelled out
    # rather than derived: `--move` is stored as 'moved', and deriving one from
    # the other by string surgery is how that became a crash once already.
    kinds = {"--cancel": "cancelled", "--move": "moved", "--extra": "extra"}
    chosen = [name for name, on in
              (("--cancel", args.cancel), ("--move", args.move), ("--extra", args.extra))
              if on]
    if len(chosen) > 1:
        print(f"{' and '.join(chosen)} are three different things; pick one.",
              file=sys.stderr)
        return 1
    if not chosen:
        return _list_adjustments(config, table, args)

    try:
        return _record_adjustment(config, table, args, kind=kinds[chosen[0]])
    except (ConfigError, gate_adjust.AdjustmentError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


def _adjustment_narration(plan: gate_adjust.AdjustmentPlan, args) -> list[str]:
    """What a recorded adjustment does, said in lines.

    Built from the resolved plan rather than by repeating the resolution, which
    is the whole reason `plan` carries which session matched and where it lands
    as facts instead of as sentences.
    """
    lines: list[str] = []

    if plan.kind == "extra":
        lines.append(
            f"  extra:    {plan.subject} on {plan.day:%a %d %b %Y} at {plan.start}"
        )
    else:
        if plan.joint:
            # Said out loud. "I cancelled Database" reading as "and OS with it"
            # is worth a sentence, and it is one session either way.
            other = " + ".join(plan.joint_subjects)
            lines.append(f"  joint:    this session is {other} -- both are affected")

        if plan.kind == "cancelled":
            lines.append(
                f"  cancel:   {plan.subject} {plan.start} {plan.session_kind} "
                f"on {plan.day:%a %d %b %Y}"
            )
        else:
            landing = plan.landing or plan.day
            lines.append(
                f"  move:     {plan.subject} {plan.start} {plan.session_kind} "
                f"on {plan.day:%a %d %b %Y}"
            )
            lines.append(
                f"  to:       {landing:%a %d %b %Y} at "
                f"{plan.fields.get('to_start') or plan.start}"
            )
            if plan.gate_moves:
                lines.append(
                    f"  gate:     the prompt for it moves to "
                    f"{plan.gate_evening:%a %d %b} evening"
                )

    if args.room:
        lines.append(f"  room:     {args.room}")
    if args.reason:
        lines.append(f"  reason:   {args.reason}")
    return lines


def _record_adjustment(config: Config, table, args, *, kind: str) -> int:
    plan = gate_adjust.plan(
        table,
        gate_adjust.AdjustmentSpec(
            kind=kind,
            subject=args.subject,
            on=args.on,
            at=args.at,
            to_date=args.to_date,
            to_time=args.to_time,
            end=args.end,
            to_kind=args.kind,
            room=args.room,
            teacher=args.teacher,
            reason=args.reason,
        ),
    )

    for line in _adjustment_narration(plan, args):
        print(line)

    if args.dry_run:
        print()
        print("  dry run -- nothing written")
        return 0

    conn = store.open_db(config)
    try:
        adjustment_id = gate_adjust.record(conn, plan)
        clash = None
        if adjustment_id is None:
            clash = gate_adjust.find_clash(conn, plan)
        else:
            conn.commit()
    finally:
        conn.close()

    print()
    if adjustment_id is None:
        # Refused, not resolved. Which of them wins is not a question anything
        # here can answer, and guessing would silently discard one of them.
        print(f"  already adjusted: #{clash['id']} {clash['kind']} this session "
              f"on that date.")
        print(f"  Remove it first if this replaces it: agent adjust --remove "
              f"{clash['id']}")
        return 1
    print(f"  recorded as adjustment {adjustment_id}. "
          f"{table.path.name} is unchanged.")
    return 0


def _adjustment_line(row, table) -> list[str]:
    kind = str(row["kind"])
    where = f"{row['applies_on']} {row['session_start']}"
    if kind == "cancelled":
        what = "cancelled"
    elif kind == "extra":
        what = f"extra, {row['to_start']}-{row['to_end'] or '?'}"
    else:
        landing = row["to_date"] or row["applies_on"]
        what = f"-> {landing} {row['to_start'] or row['session_start']}"
    return [str(row["id"]), where, str(row["subject"])[:22], what,
            str(row["reason"] or "")[:28]]


def _list_adjustments(config: Config, table, args) -> int:
    conn = store.open_db(config)
    try:
        if args.on:
            try:
                day = _date_arg(args.on, "--on")
            except ConfigError as err:
                print(f"error: {err}", file=sys.stderr)
                return 1
            rows = store.adjustments_for(conn, day.isoformat())
            resolved = gate_adjust.sessions_on(conn, table, day)
        else:
            today = datetime.now(composer.display_zone(config.timezone)).date()
            since = None if args.include_past else today.isoformat()
            rows = store.list_adjustments(conn, since=since)
            resolved = ()
        stranded = gate_adjust.orphans(conn, table)
    finally:
        conn.close()

    if args.on:
        day = _date_arg(args.on, "--on")
        print(f"{day:%a %d %b %Y} -- as adjusted")
        if not resolved:
            print("  no sessions.")
        for item in resolved:
            mark = f"   ({item.note})" if item.note else ""
            print(f"  {item.session.start}-{item.session.end}  "
                  f"{item.session.kind:7} {' + '.join(item.session.subjects)}{mark}")
        print()

    if rows:
        _print_table(["id", "session", "subject", "what", "reason"],
                     [_adjustment_line(row, table) for row in rows])
    else:
        print("No adjustments recorded." if not args.on else "  nothing adjusted.")
        if not args.on:
            example = next(iter(sorted(table.subjects)), "Database")
            print(f'  agent adjust --cancel --subject "{example}" --on YYYY-MM-DD')
            print(f'  agent adjust --move --subject "{example}" --on YYYY-MM-DD '
                  "--to YYYY-MM-DD --to-time 16:00")

    _print_orphans(stranded, table)
    return 0


def _print_orphans(stranded, table) -> None:
    """Adjustments that name nothing the file still contains.

    Never applied, never silently dropped. The row is still here and the only
    way it moves is a command I type -- re-binding it automatically would be a
    second matching path, and a wrong match adjusts the wrong session and looks
    exactly like it working.

    Printed on every timetable view and not only on demand, because the whole
    symptom of an orphan is a day that quietly does not change.
    """
    if not stranded:
        return
    print()
    print(f"  {len(stranded)} adjustment(s) no longer match "
          f"{table.path.name} and are NOT applied:")
    for orphan in stranded:
        print(f"    #{orphan.adjustment.id}  {orphan.why}")
        if orphan.renamed_to:
            print(f"           that course is now called {orphan.renamed_to!r} -- "
                  f"agent adjust --repoint {orphan.adjustment.id} "
                  f'--subject "{orphan.renamed_to}"')


def _remove_adjustment(config: Config, adjustment_id: int) -> int:
    conn = store.open_db(config)
    try:
        row = gate_adjust.remove(conn, adjustment_id)
        if row is None:
            print(f"no adjustment with id {adjustment_id}.", file=sys.stderr)
            return 1
        conn.commit()
    finally:
        conn.close()
    print(f"  removed: {row['kind']} {row['subject']} on {row['applies_on']}")
    print("  The pattern reasserts itself -- nothing in the file ever moved.")
    return 0


def _repoint_adjustment(config: Config, table, args) -> int:
    if args.subject is None:
        print("--repoint needs --subject: what the subject is called now.",
              file=sys.stderr)
        return 1

    conn = store.open_db(config)
    try:
        moved = gate_adjust.repoint(conn, table, args.repoint, args.subject)
        if moved is None:
            print(f"no adjustment with id {args.repoint}.", file=sys.stderr)
            return 1
        conn.commit()
    except gate_adjust.AdjustmentError as err:
        print(err, file=sys.stderr)
        return 1
    finally:
        conn.close()
    was, now = moved
    print(f"  repointed {args.repoint}: {was!r} -> {now!r}")
    return 0


def cmd_backup(config: Config, args: argparse.Namespace) -> int:
    """Snapshot what cannot be rebuilt, or put one back.

    `deploy/README.md` already has the mechanism -- a weekly rsync pull of the
    whole DATA_DIR. Phase 6 is what makes losing it expensive, and this is the
    part that can be CHECKED rather than believed: a backup that has never been
    restored is a belief, not a backup.
    """
    conn = store.open_db(config)
    try:
        if args.show:
            print("A snapshot taken now would hold:")
            print()
            for line in backup.describe(config, conn):
                print(f"  {line}")
            return 0

        if args.restore is not None:
            return _do_restore(config, conn, args.restore, dry_run=args.dry_run)

        snapshot = backup.write(config, conn, args.out, dry_run=args.dry_run)
    except backup.BackupError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    for table, count in snapshot.rows.items():
        if count:
            print(f"  {table:<24} {count}")
    print(f"  {'uploaded files':<24} {snapshot.files} "
          f"({snapshot.bytes_copied / 1024:.0f} KB)")
    print()
    if snapshot.dry_run:
        print(f"  dry run -- would write {snapshot.total_rows} row(s) to "
              f"{snapshot.path}")
        return 0
    print(f"  wrote {snapshot.total_rows} row(s) to {snapshot.path}")
    print()
    print("  Restore it, into a throwaway DATA_DIR, before trusting it:")
    print(f"      DATA_DIR=./data-check agent backup --restore {snapshot.path}")
    return 0


def _do_restore(config: Config, conn, snapshot_dir, *, dry_run: bool) -> int:
    result = backup.restore(config, conn, snapshot_dir, dry_run=dry_run)
    if not dry_run:
        conn.commit()

    # Per table, so a partial restore is visible rather than assumed. The two
    # numbers are different facts: "added" is what was missing, "kept" is what
    # was already here and was deliberately not overwritten.
    print(f"  {'table':<24} {'added':>7} {'kept':>7}")
    for table in result.added:
        added, skipped = result.added[table], result.skipped[table]
        if added or skipped:
            print(f"  {table:<24} {added:>7} {skipped:>7}")
    print(f"  {'uploaded files':<24} {result.files_added:>7} "
          f"{result.files_present:>7}")
    print()
    if dry_run:
        print(f"  dry run -- would add {result.total_added} row(s), "
              f"leaving {result.total_skipped} already here")
        return 0
    print(f"  added {result.total_added} row(s); left {result.total_skipped} alone.")
    if not result.total_added:
        print("  Nothing was missing. A restore is a no-op when it has already run.")
    return 0


def cmd_projects(config: Config, args: argparse.Namespace) -> int:
    """Projects, their milestones, and the deadline they share with Classroom.

    Progress is milestone-based and never a self-reported percentage. A
    percentage is a feeling typed into a box and I would quietly revise it
    upward; a milestone either happened or it did not, and I cannot round it.
    There is no percentage column to write.
    """
    if args.done is not None:
        return _complete_milestone(config, args.done)
    if args.close is not None:
        return _close_project(config, args.close)
    if args.show is not None:
        return _show_project(config, args.show)
    if args.add:
        return _add_project(config, args)

    conn = store.open_db(config)
    try:
        courses = None
        if args.subject is not None:
            try:
                _, course_id = _subject_course(config, args.subject)
            except (upload_mod.UploadError, timetable_mod.TimetableError) as err:
                print(f"error: {err}", file=sys.stderr)
                return 1
            courses = [course_id]
        rows = store.projects(conn, courses, include_closed=args.include_closed)
    finally:
        conn.close()

    if not rows:
        print("No projects recorded.")
        print('  agent projects --add --subject "Software Design" \\')
        print('                 --title "Compiler front end" --deadline 2026-12-15 \\')
        print('                 --milestone "Lexer" --milestone "Parser"')
        return 0

    zone = composer.display_zone(config.timezone)
    _print_table(
        ["id", "deadline", "subject", "title", "milestones", "linked"],
        [
            [
                str(row["id"]),
                _local_stamp(row["deadline_at"], zone) or "no date",
                str(row["course_name"] or row["course_id"])[:22],
                str(row["title"])[:34],
                # A count, never a percentage. See DESIGN.md section 3.
                f"{row['milestones_done']} of {row['milestones']}"
                if row["milestones"] else "none set",
                str(row["coursework_id"] or ""),
            ]
            for row in rows
        ],
    )
    print()
    print("  agent projects --show <id>     one project and its milestones")
    print("  agent projects --done <id>     mark a MILESTONE done")
    return 0


def _add_project(config: Config, args: argparse.Namespace) -> int:
    if args.subject is None or args.title is None:
        print("--add needs --subject and --title.", file=sys.stderr)
        return 1
    try:
        plan = entries.plan_project(
            config,
            timetable_mod.load(config.timetable_path),
            entries.ProjectSpec(
                subject=args.subject,
                title=args.title,
                deadline=args.deadline,
                deliverables=args.deliverables or [],
                team=args.team or [],
                milestones=args.milestones or [],
                brief=args.brief,
                coursework=args.coursework,
            ),
        )
    except (upload_mod.UploadError, timetable_mod.TimetableError, ConfigError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    zone = composer.display_zone(config.timezone)
    print(f"  subject:  {plan.subject}")
    print(f"  title:    {plan.title}")
    print(f"  deadline: {_local_stamp(plan.deadline_at, zone) or 'no date'}")
    for name in plan.deliverables:
        print(f"  deliver:  {name}")
    for name in plan.milestones:
        print(f"  step:     {name}")
    if plan.team:
        print(f"  team:     {', '.join(plan.team)}")
    if plan.coursework:
        print(f"  linked:   coursework {plan.coursework} -- one deadline, not two")
    if not plan.deadline_at:
        print()
        print("  no --deadline, so the scanner will never alert on this.")

    if args.dry_run:
        print()
        print("  dry run -- nothing written")
        return 0

    conn = store.open_db(config)
    try:
        project_id = entries.create_project(conn, plan)
        conn.commit()
    finally:
        conn.close()

    print()
    if project_id is None:
        print(f"  {plan.subject} already has a project called {plan.title!r}. "
              f"Nothing written.")
        return 0
    print(f"  recorded as project {project_id} with "
          f"{len(plan.milestones)} milestone(s).")
    return 0


def _show_project(config: Config, project_id: int) -> int:
    conn = store.open_db(config)
    try:
        row = store.get_project(conn, project_id)
        steps = store.project_milestones(conn, project_id) if row else []
    finally:
        conn.close()
    if row is None:
        print(f"no project with id {project_id}.", file=sys.stderr)
        return 1

    zone = composer.display_zone(config.timezone)
    print(f"  {row['title']}  ({row['course_name'] or row['course_id']})")
    print(f"  deadline: {_local_stamp(row['deadline_at'], zone) or 'no date'}")
    if row["brief_source"]:
        print(f"  brief:    {row['brief_source']}")
    if row["team"]:
        print(f"  team:     {', '.join(str(row['team']).splitlines())}")
    if row["coursework_id"]:
        print(f"  linked:   coursework {row['coursework_id']}")
    if row["closed_at"]:
        print(f"  closed:   {row['closed_at']}")

    if row["deliverables"]:
        print()
        print("  deliverables")
        for line in str(row["deliverables"]).splitlines():
            print(f"    - {line}")

    print()
    if not steps:
        print("  no milestones set. Progress is counted from them and from")
        print("  nothing else, so a project with none has none to report.")
        return 0
    done = sum(1 for step in steps if step["done_at"])
    print(f"  milestones -- {done} of {len(steps)}")
    for step in steps:
        mark = "x" if step["done_at"] else " "
        when = f"  {str(step['done_at'])[:10]}" if step["done_at"] else ""
        print(f"    [{mark}] {step['id']:>4}  {step['title']}{when}")
    return 0


def _complete_milestone(config: Config, milestone_id: int) -> int:
    conn = store.open_db(config)
    try:
        outcome = entries.complete_milestone(conn, milestone_id)
        if outcome.status == "missing":
            print(f"no milestone with id {milestone_id}. "
                  f"`agent projects --show <id>` lists them.", file=sys.stderr)
            return 1
        if outcome.status == "already":
            print(f"  milestone {milestone_id} was already done "
                  f"({outcome.row['done_at']}).")
            return 0
        conn.commit()
    finally:
        conn.close()
    print(f"  done: {outcome.row['title']}  ({outcome.row['project_title']})")
    return 0


def _close_project(config: Config, project_id: int) -> int:
    conn = store.open_db(config)
    try:
        outcome = entries.close_project(conn, project_id)
        if outcome.status == "missing":
            print(f"no project with id {project_id}.", file=sys.stderr)
            return 1
        if outcome.status == "already":
            print(f"  project {project_id} was already closed "
                  f"({outcome.row['closed_at']}).")
            return 0
        conn.commit()
    finally:
        conn.close()
    print(f"  closed: {outcome.row['title']}. "
          f"It is no longer chased for a deadline.")
    return 0


def cmd_sessions(config: Config, args: argparse.Namespace) -> int:
    """Log that a session happened, or show what has been logged.

    The timetable says a session was SCHEDULED. This says one took place and
    what was in it -- which is the fact no file can hold, because it is not
    known until afterwards.
    """
    if args.subject is not None and (args.covered is not None or args.on is not None):
        return _log_session(config, args)

    conn = store.open_db(config)
    try:
        courses = None
        if args.subject is not None:
            try:
                _, course_id = _subject_course(config, args.subject)
            except (upload_mod.UploadError, timetable_mod.TimetableError) as err:
                print(f"error: {err}", file=sys.stderr)
                return 1
            courses = [course_id]
        rows = store.manual_sessions(conn, courses)
    finally:
        conn.close()

    if not rows:
        print("No sessions logged.")
        print('  agent sessions --subject "Calculus III" --on 2026-09-21 \\')
        print('                 --kind LEC --covered "Cauchy sequences"')
        return 0

    _print_table(
        ["id", "held", "subject", "kind", "covered"],
        [
            [
                str(row["id"]), row["held_on"],
                str(row["course_name"] or row["course_id"]),
                row["kind"], (row["covered"] or "")[:52],
            ]
            for row in rows
        ],
    )
    return 0


def _log_session(config: Config, args: argparse.Namespace) -> int:
    try:
        plan = entries.plan_session(
            config,
            timetable_mod.load(config.timetable_path),
            entries.SessionSpec(
                subject=args.subject,
                kind=args.kind,
                on=args.on,
                covered=args.covered,
            ),
        )
    except (upload_mod.UploadError, timetable_mod.TimetableError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except ConfigError as err:
        print(err, file=sys.stderr)
        return 1

    print(f"  subject: {plan.subject}")
    print(f"  held:    {plan.held_on}  {plan.kind}")
    print(f"  covered: {plan.covered or '(not recorded)'}")

    if args.dry_run:
        print()
        print("  dry run -- nothing written")
        return 0

    conn = store.open_db(config)
    try:
        session_id = entries.log_session(conn, plan)
        conn.commit()
    finally:
        conn.close()
    print()
    print(f"  logged as session {session_id}.")
    return 0


def cmd_tasks(config: Config, args: argparse.Namespace) -> int:
    """Record something due, mark one done, or list what is outstanding.

    A due date on a row is all `sync/deadlines.py` needs, so these join the
    EXISTING T-72/24/3 scanner rather than getting a parallel one -- which is
    how they inherit catch-up safety for nothing.
    """
    if args.done is not None:
        return _complete_task(config, args.done)
    if args.subject is not None and args.title is not None:
        return _add_task(config, args)
    if args.title is not None:
        print("--title needs --subject: a task belongs to a subject.", file=sys.stderr)
        return 1

    conn = store.open_db(config)
    try:
        courses = None
        if args.subject is not None:
            try:
                _, course_id = _subject_course(config, args.subject)
            except (upload_mod.UploadError, timetable_mod.TimetableError) as err:
                print(f"error: {err}", file=sys.stderr)
                return 1
            courses = [course_id]
        rows = store.manual_tasks(conn, courses, include_done=args.include_done)
    finally:
        conn.close()

    if not rows:
        print("Nothing outstanding.")
        print('  agent tasks --subject "Calculus III" --title "TD 3" --due 2026-10-05')
        return 0

    zone = composer.display_zone(config.timezone)
    _print_table(
        ["id", "due", "subject", "kind", "title", "done"],
        [
            [
                str(row["id"]),
                _local_stamp(row["due_at"], zone) or "no date",
                str(row["course_name"] or row["course_id"]),
                str(row["kind"]).replace("_", " "),
                str(row["title"])[:40],
                "yes" if row["done_at"] else "",
            ]
            for row in rows
        ],
    )
    return 0


def _local_stamp(value: str | None, zone) -> str:
    """A stored UTC timestamp as local wall clock. Display only, per the rule."""
    if not value:
        return ""
    try:
        moment = datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return str(value)
    return moment.astimezone(zone).strftime("%a %d %b %H:%M")


def _add_task(config: Config, args: argparse.Namespace) -> int:
    try:
        plan = entries.plan_task(
            config,
            timetable_mod.load(config.timetable_path),
            entries.TaskSpec(
                subject=args.subject,
                title=args.title,
                kind=args.kind,
                due=args.due,
                notes=args.notes,
                source=args.source,
            ),
        )
    except (upload_mod.UploadError, timetable_mod.TimetableError, ConfigError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    zone = composer.display_zone(config.timezone)
    due_at = plan.due_at
    print(f"  subject: {plan.subject}")
    print(f"  title:   {plan.title}")
    print(f"  kind:    {plan.kind.replace('_', ' ')}")
    print(f"  due:     {_local_stamp(due_at, zone) or 'no date'}"
          f"{f'  ({due_at})' if due_at else ''}")

    if plan.unscanned:
        # Said out loud: an undated task is recorded, and it is also invisible
        # to the only thing that would have chased it.
        print()
        print("  no --due, so the deadline scanner will never alert on this.")

    if args.dry_run:
        print()
        print("  dry run -- nothing written")
        return 0

    conn = store.open_db(config)
    try:
        task_id = entries.create_task(conn, plan)
        conn.commit()
    finally:
        conn.close()

    print()
    if task_id is None:
        # Not an error: the UNIQUE key did its job. Two identical rows would
        # mean two alerts for one sheet.
        print("  already recorded -- same subject, title and due date. Nothing written.")
        return 0
    print(f"  recorded as task {task_id}. It joins the T-72/24/3 scanner from now on.")
    return 0


def _complete_task(config: Config, task_id: int) -> int:
    conn = store.open_db(config)
    try:
        outcome = entries.complete_task(conn, task_id)
        if outcome.status == "missing":
            print(f"no task with id {task_id}.", file=sys.stderr)
            return 1
        if outcome.status == "already":
            # A distinct outcome from "marked it done", and worth saying so.
            print(f"  task {task_id} was already done ({outcome.row['done_at']}).")
            return 0
        conn.commit()
    finally:
        conn.close()
    print(f"  done: {outcome.row['title']}")
    return 0


def cmd_upload(config: Config, args: argparse.Namespace) -> int:
    """Put a local file into a subject's library, one stage in."""
    try:
        table = timetable_mod.load(config.timetable_path)
    except timetable_mod.TimetableError as err:
        print(err, file=sys.stderr)
        return 1

    conn = store.open_db(config)
    try:
        try:
            plan = upload_mod.prepare(
                config, conn, table,
                subject=args.subject, path=args.file,
                title=args.title, posted=args.posted,
            )
        except upload_mod.UploadError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1

        print(f"  subject: {plan.subject} ({plan.course_name})")
        print(f"  title:   {plan.title}")
        print(f"  dated:   {plan.posted_at}")
        print(f"  file:    {plan.file_id}  {plan.mime_type}  "
              f"{plan.size_bytes / 1024:.0f} KB")

        if plan.already_held:
            # Keyed by content, so this is not a conflict -- it is the same
            # file. Said plainly rather than written again.
            print()
            print("  already in the library: these exact bytes are already held")
            print(f"  as {plan.file_id}. Nothing written.")
            return 0

        if args.dry_run:
            print()
            print(f"  would write: library/{plan.local_path}")
            print("  dry run -- nothing written")
            return 0

        upload_mod.commit(config, conn, plan, args.file.read_bytes())
        conn.commit()
    finally:
        conn.close()

    print()
    print(f"  wrote library/{plan.local_path} and a 'fetched' extraction row.")
    print("  Download is the only stage an upload skips -- the rest of the")
    print("  pipeline reads it exactly as it reads a lecture from Drive.")
    print()
    print("  Next: `agent run` picks it up at 07:30 and 19:30. To use it tonight:")
    print("      agent extract")
    print(f'      agent ocr --course "{plan.subject}"')
    print("      agent studyitems")
    return 0


def _identity(course_id: str | None, tracked: set[str]) -> str:
    """How a subject is identified, in the words that say what to do about it."""
    if course_id is None:
        return "awaiting a Classroom id"
    if manual.is_manual(course_id):
        return f"{course_id}  (manual)"
    return f"{course_id}  ({'tracked' if course_id in tracked else 'NOT tracked'})"


def cmd_subjects(config: Config, args: argparse.Namespace) -> int:
    """Every subject's identity -- and where one is missing, how to mint it."""
    try:
        table = timetable_mod.load(config.timetable_path)
    except timetable_mod.TimetableError as err:
        print(err, file=sys.stderr)
        return 1

    if args.add is not None:
        return _add_manual_subject(config, table, args.add)

    tracked = set(config.tracked_courses)
    local = scope_mod.local(config, table)
    conn = store.open_db(config)
    try:
        counts = store.study_item_counts(conn)
    finally:
        conn.close()

    rows = []
    for name in sorted(table.subjects, key=str.casefold):
        course_id = table.subjects[name]
        rows.append([
            name,
            _identity(course_id, tracked),
            "yes" if course_id in local else "no",
            str(counts.get(course_id or "", 0)) if course_id in local else "-",
        ])

    print(f"timetable: {table.path}")
    print()
    _print_table(["subject", "identity", "gated?", "items"], rows)
    print()

    awaiting = table.subjects_awaiting_course()
    if awaiting:
        print(f"  {len(awaiting)} subject(s) await a Classroom course id.")
        print("  Join the course, run `agent courses`, and paste the id in --")
        print('  or, if there will never be one: agent subjects --add "<name>"')
    return 0


def _add_manual_subject(config: Config, table, name: str) -> int:
    """Mint an identity for a subject with no Classroom, and say where to put it.

    This never writes timetable.yaml. The file is the single source of truth
    for the weekly pattern and Phase 3a settled that it keeps ONE writer -- me.
    So the command does the half that needs the database and prints the half
    that needs the file.
    """
    matches = [key for key in table.subjects if key.casefold() == name.casefold()]
    if not matches:
        known = ", ".join(sorted(table.subjects)) or "(none)"
        print(
            f"{name!r} is not a subject in {config.timetable_path.name}, and "
            f"names are never matched approximately.\n  Known subjects: {known}",
            file=sys.stderr,
        )
        return 1
    subject = matches[0]
    existing = table.subjects[subject]

    if existing is not None and not manual.is_manual(existing):
        print(
            f"{subject!r} already maps to Classroom course {existing}. A subject "
            f"has one identity; if that course is wrong, edit "
            f"{config.timetable_path.name}.",
            file=sys.stderr,
        )
        return 1

    try:
        course_id = manual.course_id(subject)
    except manual.ManualIdError as err:
        print(err, file=sys.stderr)
        return 1

    if existing is not None and existing != course_id:
        print(
            f"{subject!r} already maps to manual course {existing}, which is not "
            f"the id this name mints ({course_id}). Leave the file as it is, or "
            f"edit it by hand -- renaming a course id orphans its study items.",
            file=sys.stderr,
        )
        return 1

    conn = store.open_db(config)
    try:
        created = store.ensure_manual_course(conn, course_id, subject)
        conn.commit()
    finally:
        conn.close()

    print(f"  {'registered' if created else 'already registered'}: "
          f"{course_id}  ({subject})")
    print()
    if existing == course_id:
        print(f"  {config.timetable_path.name} already maps it. Nothing to paste.")
        return 0

    print(f"  Now paste this line into {config.timetable_path.name}, "
          f"under `subjects:`:")
    print()
    print(f"      {subject}: {course_id}")
    print()
    print("  This command does not edit that file. The timetable is the one")
    print("  source of truth for the weekly pattern, and it keeps one writer.")
    print()
    print("  Then: agent timetable --check")
    return 0


def cmd_timetable(config: Config, args: argparse.Namespace) -> int:
    try:
        table = timetable_mod.load(config.timetable_path)
    except timetable_mod.TimetableError as err:
        print(err, file=sys.stderr)
        return 1

    print(f"timetable: {table.path}")
    notes = timetable_mod.warnings(table, config.tracked_courses)

    if args.check:
        print(f"  {len(table.subjects)} subject(s), {len(table.versions)} version(s), "
              f"{len(table.exceptions)} exception(s)")
        for version in table.versions:
            ends = version.effective_to or "open-ended"
            print(f"  {version.label!r} [{version.status}] "
                  f"{version.effective_from} -> {ends}, "
                  f"{len(version.sessions)} session(s)")
        print()
        if notes:
            # Warnings, not errors: this is the normal state of my timetable.
            for note in notes:
                print(f"  note: {note}")
        else:
            print("  nothing to report.")
        print()
        print("  file is valid.")
        return 0

    if args.on:
        try:
            days = [date.fromisoformat(args.on)]
        except ValueError:
            print(f"--on must be a date like 2026-09-15, got {args.on!r}", file=sys.stderr)
            return 1
    else:
        today = datetime.now(composer.display_zone(config.timezone)).date()
        days = [today + timedelta(days=offset) for offset in range(7)]

    conn = store.open_db(config)
    try:
        # The pattern AS ADJUSTED, which is the whole point of printing a day:
        # I am looking at it to find out what is actually happening, and a
        # cancelled lecture still listed here is the failure this prevents.
        by_day = {day: gate_adjust.sessions_on(conn, table, day) for day in days}
        gone_from = {day: gate_adjust.departures(conn, table, day) for day in days}
        stranded = gate_adjust.orphans(conn, table)
    finally:
        conn.close()

    for day in days:
        version = table.version_for(day)
        label = f"  [{version.label} · {version.status}]" if version else ""
        print()
        print(f"{day:%a %d %b %Y}{label}")

        resolved = by_day[day]
        if not resolved and not gone_from[day]:
            if version is None:
                print("  no timetable version covers this date -- the gate stays silent.")
                continue
            excused = table.exception_for(day)
            if excused is not None:
                print(f"  no sessions: {excused.reason}")
                continue
            # Sunday, or a weekday this version simply has nothing on. A
            # day emptied BY adjustments falls through instead, so that what
            # was taken off it is still printed.
            print("  no sessions.")
            continue

        if not resolved:
            print("  nothing left on this day:")

        for item in resolved:
            session = item.session
            for line in _session_line(session):
                print(line)
            if item.note:
                # Why this day differs from the printed timetable. Without it
                # the only way to find out is to remember.
                print(f"           ({item.note})")

        if gone_from[day] and resolved:
            # Under their own heading rather than interleaved: the list above
            # reads down the day as it will actually happen, and a struck-out
            # line in the middle of it reads as something to turn up for.
            print("  not happening:")
        for item in gone_from[day]:
            # Shown in order to be crossed out. A cancelled lecture that simply
            # vanishes leaves the day unexplained, and an unexplained day is
            # indistinguishable from a bug in the resolver.
            session = item.session
            print(f"    {session.start}-{session.end}  {session.kind:7} "
                  f"{' + '.join(session.subjects)}"
                  f"  -- {gate_adjust.departure_note(item)}")

    _print_orphans(stranded, table)

    if notes:
        print()
        for note in notes:
            print(f"  note: {note}")
    return 0


def _tomorrow(config: Config) -> date:
    """The next calendar day, locally.

    Local, not UTC: "tomorrow" is a wall-clock fact, and at 20:00 Africa/Tunis
    the two already disagree about which day it is for part of the year.
    """
    return datetime.now(composer.display_zone(config.timezone)).date() + timedelta(days=1)


def _send_gate(
    conn,
    plan: gate_scheduler.GatePlan,
    telegram,
    *,
    force: bool = False,
) -> tuple[int | None, str]:
    """Record the run, send the prompt, then stamp it. (run id, what happened).

    The order is the point, and it is the same one notify/dispatch.py uses: the
    row exists before the send so a crash leaves something to retry, and
    sent_at is written only once the message has actually landed.
    """
    for_date = plan.for_date.isoformat()
    existing = store.get_gate_run_for(conn, for_date)

    if existing is not None and existing["sent_at"] and not force:
        # UNIQUE(for_date) doing its job: this is what stops a second run the
        # same evening from sending the same prompt twice.
        return int(existing["id"]), f"already sent for {for_date}"

    if existing is None:
        run_id = store.create_gate_run(
            conn,
            for_date=for_date,
            plan=plan.to_json(),
            version_label=plan.version_label,
        )
    else:
        run_id = int(existing["id"])
        store.replace_gate_plan(
            conn, run_id, plan=plan.to_json(), version_label=plan.version_label
        )
    conn.commit()

    text = gate_messages.compose(plan)
    keyboard = gate_messages.keyboard(plan, run_id)
    response = telegram.send_with_keyboard(text, keyboard)

    message_id = ((response or {}).get("result") or {}).get("message_id")
    store.mark_gate_sent(conn, run_id, message_id)
    conn.commit()
    return run_id, "sent"


def _print_plan(plan: gate_scheduler.GatePlan) -> None:
    print(f"  for: {plan.for_date:%a %d %b %Y}")
    if plan.version_label:
        status = " (provisional)" if plan.provisional else ""
        print(f"  timetable: {plan.version_label}{status}")

    if plan.silent_because:
        print(f"  nothing to send -- {plan.silent_because}")
        return

    for subject in plan.subjects:
        meets = ", ".join(f"{s.start} {s.kind}" for s in subject.sessions)
        if not subject.gated:
            why = (
                "awaiting a Classroom course id"
                if subject.awaiting_course
                else "mapped to a course with no material here"
            )
            print(f"  {subject.name:24} {meets:22} {why}")
            continue
        if not subject.has_items:
            note = (
                "nothing entered yet"
                if subject.manual
                else f"no readable material ({subject.dead_files} dead attachment(s))"
            )
        elif not subject.items:
            note = "up to date"
        else:
            note = f"{len(subject.items)} unreviewed, {subject.ready_count} ready"
            if subject.unread_pages:
                note += f", {subject.unread_pages} page(s) untranscribed"
        print(f"  {subject.name:24} {meets:22} {note}")

    if not plan.worth_sending:
        print()
        print("  nothing to send -- every subject tomorrow is up to date.")


def cmd_gate(config: Config, args: argparse.Namespace) -> int:
    try:
        table = timetable_mod.load(config.timetable_path)
    except timetable_mod.TimetableError as err:
        print(err, file=sys.stderr)
        return 1

    if args.on:
        try:
            for_date = date.fromisoformat(args.on)
        except ValueError:
            print(f"--on must be a date like 2026-09-15, got {args.on!r}", file=sys.stderr)
            return 1
    else:
        for_date = _tomorrow(config)

    conn = store.open_db(config)
    try:
        plan = gate_scheduler.plan_for(
            conn, scope_mod.local(config, table), table, for_date
        )
        _print_plan(plan)

        if not plan.worth_sending:
            # Silence is the correct output, not a failure. A bot that says
            # "nothing tonight" every evening trains me to swipe it away, and
            # then the one that mattered goes with it.
            return 0

        print()
        if args.dry_run:
            print("  --- the message as it would arrive ---")
            print(gate_messages.compose(plan))
            print()
            for row in gate_messages.keyboard(plan, 0)["inline_keyboard"]:
                print("  " + "  ".join(f"[{b['text']}]" for b in row))
            print()
            print("  dry run -- nothing sent, nothing written")
            return 0

        telegram = telegram_api.from_config(config)
        run_id, what = _send_gate(conn, plan, telegram, force=args.force)
        print(f"  {what} (gate run {run_id}, {plan.total_items} item(s))")
        return 0
    except (ConfigError, telegram_api.TelegramError) as err:
        print(err, file=sys.stderr)
        return 1
    finally:
        conn.close()


def _print_questions(generated: gate_quiz.Generated, config: Config) -> None:
    """The questions as they would be asked, with the answer marked.

    The whole point of --dry-run is judging quality by eye before the phone
    ever sees them, so the correct option and its source are shown -- which is
    exactly what the sent version must never do.
    """
    print(f"  model: {generated.model}"
          f"{'  (from cache -- no request made)' if generated.cached else ''}")
    print(f"  source hash: {generated.source_hash[:16]}…")
    if generated.note:
        print(f"  the model noted: {generated.note}")
    if generated.truncated:
        print(f"  material was trimmed at {gate_quiz.MAX_SOURCE_CHARS} characters")
    total = len(generated.questions)
    # The smallest score that clears the threshold, worked out the same way
    # Attempt.passed does it rather than by rounding -- 0.75 of six is five, not
    # four and a half, and printing the wrong figure here would be worse than
    # printing none.
    need = next((k for k in range(total + 1) if k / total >= config.quiz_pass_threshold), total)
    print(f"  {total} question(s); {need} of {total} to pass "
          f"at {config.quiz_pass_threshold:g}")
    print()

    for number, question in enumerate(generated.questions, start=1):
        print(f"  {number}. {question.question}")
        for index, option in enumerate(question.options):
            mark = "*" if index == question.correct else " "
            print(f"     {mark} {gate_messages.CHOICES[index]}. {option}")
        if question.explanation:
            print(f"       why: {question.explanation}")
        if question.where:
            print(f"       from: {question.where}")
        print()


def cmd_quiz(config: Config, args: argparse.Namespace) -> int:
    """One item's quiz, on stdout or on the phone.

    Exits non-zero whenever no quiz was produced, whatever the reason, and
    prints which reason it was. One rule is easier to trust than a taxonomy of
    exit codes, and the message already distinguishes "come back tomorrow" from
    "go and fix the key".
    """
    conn = store.open_db(config)
    try:
        item = gate_scheduler.item_by_id(conn, args.item)
        if item is None:
            print(f"No study item with id {args.item}.", file=sys.stderr)
            return 1

        row = store.get_study_item(conn, args.item)
        course = store.get_course(conn, str(row["course_id"]))
        name = str(course["name"]) if course else str(row["course_id"])
        print(f"  item {args.item}: {item.label}")
        print(f"  course: {name}")
        print(f"  state: {item.state}, {item.files} file(s), {item.pages} page(s)")
        if item.state not in gate_quiz.QUIZZABLE_STATES:
            print()
            print(f"  no quiz -- this item is {item.state}.")
            print("  The material has not been delivered, so a pass would not be")
            print("  evidence of anything. `agent gate` sends it; --reopen brings")
            print("  a skipped one back.")
            return 1
        if not item.ready:
            # Said before anything is spent, because this is the refusal the
            # whole phase turns on: a quiz over untranscribed pages would be a
            # quiz about the parts that happen to be legible.
            print()
            print(f"  no quiz -- {item.blocked_reason}.")
            print("  Deliver it and mark it read; verifying needs the OCR finished.")
            print("  `agent ocr` transcribes the backlog a few pages at a time.")
            return 1
        print()

        if args.dry_run:
            generated = gate_quiz.generate(conn, config, item, course=name)
            _print_questions(generated, config)
            print("  dry run -- no attempt started, nothing sent, nothing marked.")
            if not generated.cached:
                print("  the generated set was cached, so running the real quiz")
                print("  costs no further request.")
            return 0

        telegram = telegram_api.from_config(config)
        result = gate_bot.start_quiz(
            conn, config, telegram,
            run_id=0, item=item, course_id=str(row["course_id"]),
        )
        print(f"  {result.kind}: {result.detail}")
        return 0 if result.kind != "no-quiz" else 1
    except gate_quiz.QuizUnavailable as err:
        print(f"no quiz -- {err}", file=sys.stderr)
        return 1
    except (ConfigError, telegram_api.TelegramError) as err:
        print(err, file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_sections(config: Config, args: argparse.Namespace) -> int:
    """How one item would be cut into evening-sized windows. Decides nothing.

    A measurement command, the same kind as `agent extract --dry-run` printing
    the chars-per-page profile that settled the OCR question. It answers one
    thing that cannot be answered from a schema: do the boundaries land
    anywhere a person would have put them? Nothing here writes a row, calls a
    model, or touches Drive.
    """
    conn = store.open_db(config)
    try:
        item = gate_scheduler.item_by_id(conn, args.item)
        if item is None:
            print(f"No study item with id {args.item}.", file=sys.stderr)
            return 1
        if args.pages < 1:
            print("--pages must be at least 1.", file=sys.stderr)
            return 1

        print(f"  item {args.item}: {item.label}")
        print(f"  state: {item.state}, {item.files} file(s), {item.pages} page(s)")
        print(f"  budget: {args.pages} page(s) per window")
        print()

        rows = store.study_item_sources(conn, item.entity_type, item.entity_id)
        if not rows:
            print("  no extracted text on this post -- nothing to cut up.")
            return 1

        total_windows = 0
        unreadable = 0
        for row in rows:
            document = gate_sections.read_document(
                row,
                config.library_dir,
                store.ocr_pages_for(conn, str(row["drive_id"])),
                budget=args.pages,
            )
            if document is None:
                unreadable += 1
                title = row["file_title"] or row["drive_id"]
                print(f"  {title}")
                print(f"    text recorded in the database but not on disk: {row['text_path']}")
                print()
                continue

            total_windows += len(document.windows)
            source = (
                "slide titles from the PDF bookmarks"
                if document.titled
                else "no bookmark table -- cuts fall on the budget alone"
            )
            print(f"  {document.title} -- {document.pages} page(s), {source}")
            print(f"    {document.anchored} of {document.pages} page(s) anchorable by content")
            if document.untracked_scans:
                print(
                    f"    ! OCR has never run over this file, so its "
                    f"{document.untracked_scans} scan page(s) are unlocated -- the "
                    f"per-window figures below are incomplete"
                )
            for window in document.windows:
                marks = []
                if window.snapped:
                    marks.append("snapped")
                if window.unread:
                    marks.append(f"{window.unread} unread")
                if window.topics > 1:
                    marks.append(f"{window.topics} topics")
                suffix = f"   [{', '.join(marks)}]" if marks else ""
                name = window.title or "(untitled)"
                if window.continues:
                    # A topic longer than the budget spans two windows. Said, or
                    # the same title twice in a row reads as a duplicate row.
                    name = f"{name} (continued)"
                head = f"    {window.index + 1:>2}. {window.label:<16} {window.pages:>3}p"
                print(f"{head}  {name}{suffix}")
            print()

        print(f"  {total_windows} window(s) across {len(rows) - unreadable} file(s).")
        if total_windows <= 1:
            print("  This item is already a session's worth; windowing changes nothing.")
        return 0
    except ConfigError as err:
        print(err, file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_flagged(config: Config, args: argparse.Namespace) -> int:
    """Every question I ever flagged. Without this the flag button is write-only."""
    conn = store.open_db(config)
    try:
        rows = store.list_flags(conn)
        if not rows:
            print("  (none flagged)")
            return 0

        for row in rows:
            question = json.loads(str(row["question"]))
            where = row["source_file"] or "source not named"
            if row["source_page"]:
                where += f", page {row['source_page']}"
            print(f"  {row['flagged_at']}  item {row['study_item_id']}  "
                  f"{row['course_name'] or row['course_id']}")
            print(f"    {question.get('question', '')}")
            for index, option in enumerate(question.get("options") or []):
                mark = "*" if index == question.get("correct") else " "
                print(f"     {mark} {gate_messages.CHOICES[index]}. {option}")
            print(f"    from {where} · written by {row['model'] or 'unknown'}")
            print()
        print(f"  {len(rows)} flagged question(s).")
        return 0
    finally:
        conn.close()


def cmd_bot(config: Config, args: argparse.Namespace) -> int:
    """Listen for button presses. This is the long-running half of the gate."""
    zone = composer.display_zone(config.timezone)
    conn = store.open_db(config)
    try:
        telegram = telegram_api.from_config(config)
    except ConfigError as err:
        conn.close()
        print(err, file=sys.stderr)
        return 1

    def resend(run) -> None:
        """A snooze ran out. Send the prompt again from the stored plan."""
        table = timetable_mod.load(config.timetable_path)
        plan = gate_scheduler.plan_for(
            conn,
            scope_mod.local(config, table),
            table,
            date.fromisoformat(str(run["for_date"])),
        )
        if not plan.worth_sending:
            store.close_gate_run(conn, int(run["id"]))
            conn.commit()
            return
        _send_gate(conn, plan, telegram, force=True)

    def report(event: gate_bot.Handled) -> None:
        print(f"  {event.kind}: {event.detail}")

    if not args.once:
        print(f"listening as {config.account} -- ctrl-c to stop")
    try:
        handled = gate_bot.poll(
            conn,
            config,
            telegram,
            tz=zone,
            timeout=args.timeout,
            once=args.once,
            on_event=report,
            resend=resend,
        )
    except KeyboardInterrupt:
        print()
        print("stopped.")
        return 0
    finally:
        conn.close()

    print(f"handled {len(handled)} update(s)")
    return 0


def cmd_events(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        rows = store.list_events(conn, include_notified=args.include_notified)
    finally:
        conn.close()

    if not rows:
        scope = "events" if args.include_notified else "pending events"
        print(f"No {scope}.")
        return 0

    table = []
    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        title = payload.get("title") or payload.get("text") or ""
        table.append(
            [
                row["type"],
                row["course_name"] or row["course_id"] or "",
                title.replace("\n", " ")[:48],
                row["created_at"],
                "" if row["notified_at"] is None else "notified",
            ]
        )

    headers = ["type", "course", "title", "created_at", ""]
    _print_table(headers, table)
    print()
    pending = sum(1 for row in rows if row["notified_at"] is None)
    print(f"  {len(rows)} shown, {pending} pending")
    return 0


def _do_deadlines(config: Config, conn, *, dry_run: bool = False) -> deadlines.DeadlineScan:
    """Recompute deadline alerts and record the ones not already recorded."""
    result = deadlines.scan(conn, list(config.tracked_courses))
    if not dry_run:
        for event in result.events:
            if store.insert_event(conn, event):
                result.events_written += 1
        # Thresholds a more urgent alert already speaks for. Written with
        # notified_at set, so they are never sent and never fire again.
        stamp = store._utc_now_iso()
        for event in result.suppressed:
            if store.insert_event(conn, event, notified_at=stamp):
                result.suppressed_written += 1
        conn.commit()
    return result


def _print_deadlines(result: deadlines.DeadlineScan, *, dry_run: bool) -> None:
    counts = result.event_counts
    if not counts:
        print(f"no deadline alerts due ({result.considered} assignment(s) checked)")
    else:
        verb = "would record" if dry_run else "recorded"
        print(f"{verb} {len(result.events)} deadline alert(s):")
        for event_type in ("deadline_t3", "deadline_t24", "deadline_t72"):
            if counts.get(event_type):
                print(f"    {event_type:14} {counts[event_type]}")
        if not dry_run and result.events_written != len(result.events):
            already = len(result.events) - result.events_written
            print(f"    ({already} already recorded by an earlier run)")

    if result.suppressed:
        verb = "would record" if dry_run else "recorded"
        print(
            f"  {verb} {len(result.suppressed)} less urgent threshold(s) as "
            f"already notified -- a nearer alert covers the same assignment"
        )

    # Reported, never warned about: more than half of all coursework has no due
    # date at all, so this is the normal shape of the data.
    print(
        f"  {result.without_due_date} of {result.considered} assignment(s) have "
        f"no due date and were skipped"
    )


def cmd_deadlines(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        result = _do_deadlines(config, conn, dry_run=args.dry_run)
    finally:
        conn.close()
    _print_deadlines(result, dry_run=args.dry_run)
    return 0


def _do_notify(config: Config, conn, *, dry_run: bool = False) -> int:
    """Compose the pending events and send them. Returns a process exit code."""
    rows = store.list_events(conn, include_notified=False)
    links = store.entity_links(conn, rows)
    blocks = composer.compose_blocks(
        rows,
        timezone_name=config.timezone,
        links=links,
        # What the attachments actually amount to, and which subjects hold
        # material nothing has managed to read. Both are Phase 2 facts the
        # digest could not previously state.
        summaries=store.material_summary(conn, rows),
        unread_by_course=store.unreadable_pages_by_course(conn),
    )

    if not blocks:
        # Deliberately only on stdout. Nothing goes to Telegram: a bot that
        # says "nothing new today" trains me to swipe notifications away
        # unread, and then the one that mattered goes with them.
        print("nothing pending -- no briefing sent")
        return 0

    pending = sum(len(block.event_ids) for block in blocks)

    if dry_run:
        # The exact text that would be sent, joined the way delivery joins it.
        print("\n\n".join(block.html for block in blocks))
        print()
        print(
            f"  dry run -- {pending} event(s) across {len(blocks)} course(s); "
            f"nothing sent, nothing stamped"
        )
        return 0

    # Only now is a bot token required. A dry run has to work on a machine that
    # has never configured one.
    telegram = telegram_api.from_config(config)
    result = dispatch.deliver(conn, blocks, telegram)

    if result.failed:
        print(
            f"send failed after {result.messages_sent} message(s): {result.error}",
            file=sys.stderr,
        )
        print(
            f"  {result.events_notified} event(s) stamped as notified; the rest "
            f"stay pending and the next run will retry them",
            file=sys.stderr,
        )
        return 1

    print(
        f"sent {result.messages_sent} message(s), "
        f"{result.events_notified} event(s) across {len(blocks)} course(s)"
    )
    return 0


def cmd_notify(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        return _do_notify(config, conn, dry_run=args.dry_run)
    finally:
        conn.close()


def _stage(name: str, work, *, report=None) -> bool:
    """Run one pipeline stage. A failure is reported, never propagated.

    This is the rule that makes `agent run` safe to schedule: the briefing is
    the point of the whole system, and a Drive outage, a spent LLM quota or a
    malformed PDF must not be able to stop it from being sent. Every stage
    before notify is therefore best-effort, and a stage that fails says so
    loudly rather than silently -- a failure that produces no visible output is
    the worst failure mode this project has.
    """
    print()
    print(f"== {name} ==")
    try:
        result = work()
    except Exception as err:
        # Deliberately broad. A stage can fail in ways this code has not
        # imagined -- tonight it was a socket timeout -- and none of them are
        # worth losing a deadline alert over.
        print(f"  {name} failed: {err}", file=sys.stderr)
        print(f"  continuing -- later stages do not depend on {name} succeeding.")
        return False
    if report is not None and result is not None:
        report(result)
    return True


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """The whole pipeline, in the order each stage feeds the next.

    sync -> fetch -> extract -> ocr -> studyitems -> packs -> deadlines -> notify.

    Classroom state arrives first because everything else reads it; bytes are
    fetched before they can be extracted; text is extracted before a model can
    be asked about the pages it could not read; study items are made once the
    material they point at is readable; and the digest goes last so it can
    report what the earlier stages just learned.

    Only sync failing ends the run, and only because the stages after it would
    have nothing to work on. Everything else is best-effort -- see _stage.
    """
    conn = store.open_db(config)
    try:
        print("== sync ==")
        try:
            sync_result = _do_sync(config, conn, dry_run=args.dry_run)
        except Exception as err:
            # Even here the briefing still goes out: a sync that failed leaves
            # yesterday's unnotified events pending, and a deadline that is
            # still approaching is still worth sending.
            print(f"  sync failed: {err}", file=sys.stderr)
            print("  continuing to deadlines and notify on stored state.")
            sync_result = None
        else:
            if sync_result is None:
                # Nothing tracked is no longer nothing to do. Since Phase 6 a
                # subject can have a library, a backlog and a deadline without
                # Classroom knowing it exists -- and at the start of a term,
                # before any course is joined, that is the NORMAL state rather
                # than an edge case. Returning here would mean an uploaded
                # board is never extracted, never gated, and no briefing is
                # sent about an exercise sheet due tomorrow.
                _no_tracked_courses()
                print("  Continuing: manual material and deadlines do not need one.")
            else:
                _print_sync(sync_result)

        _stage(
            "fetch",
            lambda: _do_fetch(config, conn, dry_run=args.dry_run),
            report=_print_fetch,
        )
        _stage(
            "extract",
            lambda: _do_extract(config, conn, dry_run=args.dry_run),
            report=_print_extract,
        )

        # Bounded by default: the free tier allows roughly 20 pages a day and
        # the scheduler fires twice, so an unbounded run would spend the whole
        # allowance in one go and leave nothing for a manual catch-up.
        if config.ocr_run_limit:
            _stage(
                "ocr",
                lambda: _do_ocr(
                    config, conn, dry_run=args.dry_run, limit=config.ocr_run_limit
                ),
                report=lambda result: _print_ocr(
                    result,
                    store.count_ocr_pages_by_status(conn),
                    len(store.dead_references(conn)),
                    store.ocr_error_counts(conn),
                ),
            )
        else:
            print()
            print("== ocr ==")
            print("  skipped: ocr.run_limit is 0 in config.yaml")

        # Strictly in scope, and narrower than the command on purpose: see
        # _do_studyitems. An archived or ignored course has material in the
        # library from earlier phases, and this stage must never turn it into a
        # backlog the gate then claims I am behind on.
        _stage(
            "studyitems",
            lambda: _do_studyitems(
                config, conn, dry_run=args.dry_run,
                course_ids=sorted(scope_mod.in_scope(_timetable_or_none(config))),
            ),
            report=lambda result: _print_studyitems_stage(result, dry_run=args.dry_run),
        )
        _stage(
            "packs",
            lambda: _do_packs(config, conn, dry_run=args.dry_run),
            report=lambda result: _print_packs(result, config),
        )
        _stage(
            "deadlines",
            lambda: _do_deadlines(config, conn, dry_run=args.dry_run),
            report=lambda result: _print_deadlines(result, dry_run=args.dry_run),
        )

        print()
        print("== notify ==")
        return _do_notify(config, conn, dry_run=args.dry_run)
    finally:
        conn.close()


COMMANDS = {
    "auth": cmd_auth,
    "whoami": cmd_whoami,
    "courses": cmd_courses,
    "sync": cmd_sync,
    "fetch": cmd_fetch,
    "extract": cmd_extract,
    "ocr": cmd_ocr,
    "packs": cmd_packs,
    "missing": cmd_missing,
    "studyitems": cmd_studyitems,
    "upload": cmd_upload,
    "adjust": cmd_adjust,
    "backup": cmd_backup,
    "projects": cmd_projects,
    "sessions": cmd_sessions,
    "tasks": cmd_tasks,
    "subjects": cmd_subjects,
    "timetable": cmd_timetable,
    "gate": cmd_gate,
    "quiz": cmd_quiz,
    "sections": cmd_sections,
    "flagged": cmd_flagged,
    "bot": cmd_bot,
    "events": cmd_events,
    "deadlines": cmd_deadlines,
    "notify": cmd_notify,
    "run": cmd_run,
}


def _use_utf8_output() -> None:
    """Print UTF-8 regardless of the console's code page.

    The Windows console defaults to cp1252, which cannot encode the emoji the
    digest uses, so `agent notify --dry-run` died with a UnicodeEncodeError
    before printing anything. errors="replace" is the belt-and-braces half: an
    unprintable character must degrade to a '?' rather than take the whole run
    down with it. This is display only -- what goes to Telegram is UTF-8 JSON
    over HTTP and never passes through here.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # Redirected to something that is not a reconfigurable text stream.
            pass


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    args = _build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        return COMMANDS[args.command](config, args)
    except (
        ConfigError,
        auth.AuthError,
        store.StoreError,
        poller.SeedRefused,
        poller.UnknownCourse,
        drive.DriveError,
        extract.ExtractError,
        SeedWouldBuryBacklog,
        ocr.OCRError,
        packs.PackError,
        upload_mod.UploadError,
        backup.BackupError,
        llm_provider.LLMError,
        gate_adjust.AdjustmentError,
    ) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
