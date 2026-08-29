"""Command line entry point for classroom-agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import auth
from .classroom.client import ClassroomClient
from .classroom.models import parse_course
from .config import Config, ConfigError, load_config
from .db import store
from .digest import composer
from .files import drive, extract, ocr, packs
from .gate import bot as gate_bot
from .gate import messages as gate_messages
from .gate import quiz as gate_quiz
from .gate import scheduler as gate_scheduler
from .gate import sections as gate_sections
from .gate import timetable as timetable_mod
from .llm import provider as llm_provider
from .notify import dispatch
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
    timetabled: list[str] | None = None,
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
            force=force, verbose=verbose, timetabled=timetabled, courses=courses,
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


def _timetabled_courses(config: Config) -> tuple[list[str], str | None]:
    """Course ids named in timetable.yaml, and a note when there are none.

    A broken or absent timetable must not stop OCR. It costs the top tier --
    everything tracked falls back to one bucket ordered by posting date, which
    is still the right answer, just a blunter one -- so the note is returned
    rather than raised, and printed where the ordering is being explained.
    """
    try:
        table = timetable_mod.load(config.timetable_path)
    except timetable_mod.TimetableError as err:
        return [], f"timetable not read, so no subject can be preferred: {err}"
    found = sorted({course for course in table.subjects.values() if course})
    if not found:
        return [], "timetable names no Classroom course, so none can be preferred"
    return found, None


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
    timetabled, note = _timetabled_courses(config)
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
                timetabled=timetabled,
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
            timetabled=timetabled, courses=wanted or None,
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


def _do_packs(
    config: Config,
    conn,
    *,
    dry_run: bool = False,
    courses: list[str] | None = None,
    force: bool = False,
) -> packs.PacksResult:
    """Assemble the study documents. Local files only -- nothing is uploaded."""
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
    config: Config, conn, *, dry_run: bool = False, seed: bool = False, force: bool = False
) -> tuple[int, int]:
    """Create one study item per post with extracted material. (created, seen).

    Mirrors `sync --seed`: the five tracked courses are a finished academic
    year, so creating ~90 pending items would open the Phase 3 gate claiming I
    am 90 lectures behind and make the Phase 4 coverage figure a lie from day
    one. --seed records that history as skipped, with a reason, because a skip
    recorded as anything else is exactly the dishonesty the gate cannot afford.
    """
    if seed and not force and store.count_rows(conn, "study_items") > 0:
        raise SeedWouldBuryBacklog(
            f"--seed would record everything as already skipped, but the "
            f"database already holds {store.count_rows(conn, 'study_items')} "
            f"study item(s). Pass --force if that is genuinely what you want."
        )

    rows = store.parents_with_extracted_material(conn, config.tracked_courses)
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
        if not part.tracked:
            # Said out loud every time. Six of eleven subjects are like this,
            # and a subject that is silently never gated is the failure mode
            # that takes months to notice.
            lines.append(f"           {part.subject}: no Classroom course -- never gated")
    return lines


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

    for day in days:
        version = table.version_for(day)
        label = f"  [{version.label} · {version.status}]" if version else ""
        print()
        print(f"{day:%a %d %b %Y}{label}")

        if version is None:
            print("  no timetable version covers this date -- the gate stays silent.")
            continue
        excused = table.exception_for(day)
        if excused is not None:
            print(f"  no sessions: {excused.reason}")
            continue

        sessions = table.sessions_on(day)
        if not sessions:
            # Sunday, or a weekday this version simply has nothing on.
            print("  no sessions.")
            continue
        for session in sessions:
            for line in _session_line(session):
                print(line)

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
            print(f"  {subject.name:24} {meets:22} not tracked in Classroom")
            continue
        if not subject.has_items:
            note = f"no readable material ({subject.dead_files} dead attachment(s))"
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
            conn, list(config.tracked_courses), table, for_date
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
            list(config.tracked_courses),
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

    sync -> fetch -> extract -> ocr -> deadlines -> notify.

    Classroom state arrives first because everything else reads it; bytes are
    fetched before they can be extracted; text is extracted before a model can
    be asked about the pages it could not read; and the digest goes last so it
    can report what the earlier stages just learned.

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
                _no_tracked_courses()
                return 0
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
        llm_provider.LLMError,
    ) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
