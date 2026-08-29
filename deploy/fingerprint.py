#!/usr/bin/env python3
"""Print a comparable fingerprint of a DATA_DIR. Standard library only.

Run it on the laptop before the move and on the server after, then diff the two
outputs. Identical output means the database and the library arrived whole.

Deliberately imports nothing from `agent`. The question "did the data arrive"
has to be answerable separately from "does the code run", or a broken install
and a truncated copy look the same -- which is the failure this project's
recurring lesson is about. It also means this runs before `pip install -e .`.

    python deploy/fingerprint.py ./data > laptop.txt
    python deploy/fingerprint.py ~/classroom-agent/data > server.txt
    diff laptop.txt server.txt

Row counts come from sqlite_master rather than a hand-written list, so a table
added in a later phase is covered without anyone remembering to add it here.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

# The rows that cannot be rebuilt from the Classroom API, and the one that
# cannot be rebuilt at all. Everything else in the database is a mirror.
IRREPLACEABLE = (
    ("events.notified_at", "SELECT count(*) FROM events WHERE notified_at IS NOT NULL"),
    ("study_items", "SELECT count(*) FROM study_items"),
    ("ocr_pages ok", "SELECT count(*) FROM ocr_pages WHERE status = 'ok'"),
    ("bot_state", "SELECT count(*) FROM bot_state"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_lines(db_path: Path) -> list[str]:
    if not db_path.is_file():
        return [f"database        MISSING at {db_path}"]

    # Read-only, so running this against the live server database is safe even
    # while the bot is polling.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        lines = [
            f"schema_version  {conn.execute('SELECT version FROM schema_version WHERE id = 1').fetchone()[0]}",
            f"journal_mode    {conn.execute('PRAGMA journal_mode').fetchone()[0]}",
            f"integrity       {conn.execute('PRAGMA quick_check').fetchone()[0]}",
            "",
            "-- row counts",
        ]
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            lines.append(f"{table:<24} {count}")

        lines += ["", "-- cannot be rebuilt from the API"]
        for label, sql in IRREPLACEABLE:
            try:
                lines.append(f"{label:<24} {conn.execute(sql).fetchone()[0]}")
            except sqlite3.Error as err:
                lines.append(f"{label:<24} unavailable: {err}")
        return lines
    finally:
        conn.close()


def library_lines(library: Path, manifest: Path | None) -> list[str]:
    if not library.is_dir():
        return ["", f"library         MISSING at {library}"]

    files = sorted(p for p in library.rglob("*") if p.is_file())
    rolling = hashlib.sha256()
    total = 0
    entries = []
    for path in files:
        relative = path.relative_to(library).as_posix()
        digest = sha256_file(path)
        total += path.stat().st_size
        # Path and content both, so a renamed file is a difference too.
        rolling.update(f"{relative} {digest}\n".encode())
        entries.append(f"{digest}  {relative}")

    if manifest is not None:
        manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")

    return [
        "",
        "-- library",
        f"{'files':<24} {len(files)}",
        f"{'bytes':<24} {total}",
        f"{'sha256 of the whole':<24} {rolling.hexdigest()}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dir", type=Path, help="the DATA_DIR to fingerprint")
    parser.add_argument(
        "--manifest",
        type=Path,
        metavar="PATH",
        help="also write a per-file sha256 list, for locating a single bad file",
    )
    args = parser.parse_args(argv)

    data_dir = args.data_dir.expanduser().resolve()
    if not data_dir.is_dir():
        print(f"no such directory: {data_dir}", file=sys.stderr)
        return 1

    # The directory name itself is left out on purpose: it differs between the
    # two machines and would make every comparison fail.
    lines = database_lines(data_dir / "academic.db")
    lines += library_lines(data_dir / "library", args.manifest)

    lines += ["", "-- credentials present (contents never printed)"]
    for name in ("token.json", "credentials.json"):
        lines.append(f"{name:<24} {'yes' if (data_dir / name).is_file() else 'NO'}")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
