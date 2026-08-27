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
from .sync import poller

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


def cmd_sync(config: Config, args: argparse.Namespace) -> int:
    conn = store.open_db(config)
    try:
        if not config.tracked_courses:
            print("No courses are tracked, so there is nothing to sync.")
            print()
            print("Run `agent courses` to see the list, then put the IDs you want")
            print("under courses.tracked in config.yaml. Nothing is tracked for you --")
            print("courseState ACTIVE does not mean a course is running this term.")
            return 0

        client = ClassroomClient(auth.get_credentials(config))
        result = poller.sync(
            config,
            conn,
            dry_run=args.dry_run,
            seed=args.seed,
            client=client,
            force=args.force,
        )
    finally:
        conn.close()

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


COMMANDS = {
    "auth": cmd_auth,
    "whoami": cmd_whoami,
    "courses": cmd_courses,
    "sync": cmd_sync,
    "events": cmd_events,
}


def main(argv: list[str] | None = None) -> int:
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
