"""Command line entry point for classroom-agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import auth
from .classroom.client import ClassroomClient
from .classroom.models import parse_course
from .config import Config, ConfigError, load_config
from .db import store
from .digest import composer
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
        rows, timezone_name=config.timezone, links=links
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


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """sync -> deadline scan -> notify. What the Phase 3 scheduler calls.

    The stages run in this order because each feeds the next: the sync stores
    the coursework the deadline scan reads, and both write the events the
    notify step sends. They stay separately runnable -- a stage that can only
    be exercised as part of a pipeline cannot be debugged.
    """
    conn = store.open_db(config)
    try:
        print("== sync ==")
        sync_result = _do_sync(config, conn, dry_run=args.dry_run)
        if sync_result is None:
            _no_tracked_courses()
            return 0
        _print_sync(sync_result)

        print()
        print("== deadlines ==")
        _print_deadlines(
            _do_deadlines(config, conn, dry_run=args.dry_run), dry_run=args.dry_run
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
    ) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
