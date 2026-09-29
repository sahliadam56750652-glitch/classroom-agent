"""Screenshot every screen of the web client, at a phone and a desktop width.

    python tools/screenshots.py before
    python tools/screenshots.py after

Writes `web/screenshots/<label>/<screen>-<width>.png`. Same synthetic data every
run, so a before and an after are comparable -- and synthetic rather than read
from `data/`, because these files are committed and my real course titles have no
business in a repository.

The data is shaped like the real install on 2026-09-25: twelve subjects, five with
something unreviewed, four manual subjects with nothing entered, two awaiting a
Classroom course id, one whose every attachment is gone from Drive. Names are the
ones already public in PLAN.md and DESIGN.md.

Two limits, said here rather than discovered:

  * Headless Chrome will not lay a page out narrower than 500px, so the phone
    width is rendered inside a 390px iframe and cropped. That is a real 390px
    viewport, not a scaled 500.
  * The reader's PDF does not paint headlessly -- `--virtual-time-budget` starves
    PDF.js's worker (see web/VENDOR.md). Its screenshot shows the reader's chrome;
    whether the pages paint is checked in a real browser.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from PIL import Image  # noqa: E402  -- python-pptx already depends on Pillow

TOKEN = "screenshot-token-" + "s" * 24
WIDTHS = {"390": (390, 844), "1440": (1440, 900)}

# Fixed dates, so the same screen is captured whatever day this runs on. 2026-10-05
# is a Monday with Database on it; 2026-10-04 is a Sunday with nothing.
BUSY_DAY = "2026-10-05"
CLEAR_DAY = "2026-10-04"

SCREENS = [
    ("signin", "/signin", False),
    ("now", f"/?date={BUSY_DAY}", True),
    ("now-empty", f"/?date={CLEAR_DAY}", True),
    ("subjects", "/subjects", True),
    ("subject", "/subjects/Database", True),
    ("timetable", "/timetable", True),
    ("library", "/library", True),
    ("deadlines", "/deadlines", True),
    ("projects", "/projects", True),
    ("add", "/add", True),
    ("status", "/status", True),
    ("reader", "/read/d-db-ch4", True),
]

CHROME = next(
    (
        path
        for path in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
        )
        if Path(path).is_file()
    ),
    None,
)

TIMETABLE = """
subjects:
  Database: "c-db"
  Operating Systems: "c-os"
  Computer Networks: "c-net"
  Data Structures: "c-dsa"
  Artificial Intelligence: "c-ai"
  Probability & Statistics: "c-prob"
  Calculus III: manual-calculus-iii
  Algebra III: manual-algebra-iii
  Philosophy: manual-philosophy
  Communication: manual-communication
  Software Engineering: null
  Web Development: null

versions:
  - label: S1 2026-27
    status: confirmed
    effective_from: 2026-09-01
    effective_to: 2027-01-23
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC, subject: Database, teacher: Gharbi, room: A12 }
      - { day: mon, start: "10:15", end: "11:45", kind: TUT, subject: Calculus III, teacher: Yassine, room: B3 }
      - { day: mon, start: "13:00", end: "14:30", kind: LEC, subject: Computer Networks, teacher: Mohsen, room: A14 }
      - { day: mon, start: "14:45", end: "16:15", kind: LEC, subject: Philosophy, teacher: Amri, room: C1 }
      - { day: tue, start: "08:30", end: "10:00", kind: LEC, subject: Operating Systems, teacher: Mansour, room: A12 }
      - { day: tue, start: "10:15", end: "11:45", kind: LEC, subject: Algebra III, teacher: Ben Ali, room: B1 }
      - day: tue
        start: "13:00"
        end: "15:00"
        kind: LAB
        subject: Database
        teacher: Gharbi
        room: Lab3
        also:
          - { subject: Operating Systems, teacher: Mansour, room: Lab4 }
      - { day: wed, start: "08:30", end: "10:00", kind: LEC, subject: Data Structures, teacher: Trabelsi, room: A10 }
      - { day: wed, start: "10:15", end: "11:45", kind: LEC, subject: Artificial Intelligence, teacher: Karray, room: A14 }
      - { day: wed, start: "13:00", end: "14:30", kind: TUT, subject: Probability & Statistics, teacher: Jlassi, room: B2 }
      - { day: thu, start: "08:30", end: "10:00", kind: LEC, subject: Software Engineering, teacher: Hamdi, room: A12 }
      - { day: thu, start: "10:15", end: "11:45", kind: TUT, subject: Computer Networks, teacher: Mohsen, room: Lab2 }
      - { day: thu, start: "13:00", end: "14:30", kind: LEC, subject: Communication, teacher: Riahi, room: C2 }
      - { day: fri, start: "08:30", end: "10:00", kind: LAB, subject: Data Structures, teacher: Trabelsi, room: Lab1 }
      - { day: fri, start: "10:15", end: "11:45", kind: LEC, subject: Web Development, teacher: Saidi, room: A10 }
      - { day: fri, start: "13:00", end: "14:30", kind: TUT, subject: Artificial Intelligence, teacher: Karray, room: B3 }
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seed(root: Path) -> Path:
    """A DATA_DIR in the shape of the real one, and the config beside it."""
    from agent.classroom.models import (
        parse_course,
        parse_coursework,
        parse_coursework_material,
        parse_materials,
    )
    from agent.db import store
    from agent.gate import sections

    import pymupdf

    data = root / "data"
    (data / "library" / "files").mkdir(parents=True)
    (data / "library" / "text").mkdir(parents=True)
    (data / "logs").mkdir(parents=True)
    (root / "timetable.yaml").write_text(TIMETABLE, encoding="utf-8")
    (root / "config.yaml").write_text(
        "account: someone@example.com\n"
        "timezone: Africa/Tunis\n"
        "courses:\n"
        '  tracked: ["c-db", "c-os", "c-net", "c-dsa", "c-ai", "c-prob"]\n'
        "  ignored: []\n"
        f"timetable_path: {(root / 'timetable.yaml').as_posix()}\n"
        "telegram:\n  chat_id: 1\n  bot_username: study_gate_bot\n"
        "api:\n  secure_cookie: false\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(f"WEB_API_TOKEN={TOKEN}\n", encoding="utf-8")

    conn = store.connect(data / "academic.db")
    for course_id, name in (
        ("c-db", "Database GA 2026"),
        ("c-os", "Operating Systems 2026"),
        ("c-net", "Computer Networks"),
        ("c-dsa", "Data Structures & Algorithms"),
        ("c-ai", "Artificial Intelligence"),
        ("c-prob", "Probability & Statistics"),
    ):
        store.upsert_course(conn, parse_course({"id": course_id, "name": name}))
    for course_id, name in (
        ("manual-calculus-iii", "Calculus III"),
        ("manual-algebra-iii", "Algebra III"),
        ("manual-philosophy", "Philosophy"),
        ("manual-communication", "Communication"),
    ):
        store.ensure_manual_course(conn, course_id, name)

    def post(course, post_id, drive_id, title, posted, pages, scans=0, words=None):
        attachment = {"driveFile": {"driveFile": {
            "id": drive_id, "title": title, "alternateLink": "https://drive/x"}}}
        material, _ = parse_coursework_material(
            {"id": post_id, "title": title, "creationTime": posted,
             "updateTime": posted, "materials": [attachment]},
            course,
        )
        store.upsert_coursework_material(conn, material)
        store.upsert_materials(conn, parse_materials(
            "coursework_material", post_id, course, [attachment]))
        body = words or (
            "Consider a relation R with attributes A, B and C. The operation keeps "
            "every tuple whose B satisfies the predicate and discards the rest."
        )
        text = [f"{title}\n\nSlide {n + 1}. {body}" for n in range(pages)]
        (data / "library" / "text" / f"{drive_id}.txt").write_text(
            sections.PAGE_BREAK.join(text), encoding="utf-8")
        document = pymupdf.open()
        for n in range(pages):
            page = document.new_page(width=595, height=842)
            page.insert_text((60, 90), title, fontsize=18)
            page.insert_text((60, 130), f"Slide {n + 1} of {pages}", fontsize=12)
        document.save(data / "library" / "files" / f"{drive_id}.pdf")
        document.close()
        store.upsert_extraction(
            conn, drive_id, status="ok", local_path=f"files/{drive_id}.pdf",
            text_path=f"text/{drive_id}.txt", mime_type="application/pdf",
            size_bytes=4096, pages=pages, chars=pages * 180,
            scan_pages=scans, ocr_pages=0, method="pdf", md5_checksum=drive_id,
        )
        for index in range(pages - scans, pages):
            store.upsert_ocr_page(conn, drive_id=drive_id, page_index=index,
                                  page_hash=f"{drive_id}-{index}", status="pending")
        store.ensure_study_item(conn, course_id=course,
                                entity_type="coursework_material", entity_id=post_id)

    post("c-db", "p-db-ch4", "d-db-ch4", "Chapter 4 — SQL joins and null semantics",
         "2026-09-08T09:00:00Z", 92, scans=12)
    post("c-db", "p-db-td2", "d-db-td2", "TD2 — normal forms", "2026-09-15T09:00:00Z", 6)
    post("c-db", "p-db-ch5", "d-db-ch5", "Chapter 5 — transactions",
         "2026-09-22T09:00:00Z", 38)
    post("c-os", "p-os-l3", "d-os-l3", "Lecture 3 — scheduling", "2026-09-09T09:00:00Z", 44, scans=3)
    post("c-os", "p-os-lab1", "d-os-lab1", "Lab 1 — processes and fork()",
         "2026-09-16T09:00:00Z", 8)
    post("c-net", "p-net-l2", "d-net-l2", "Lecture 2 — the link layer",
         "2026-09-10T09:00:00Z", 31)
    post("c-dsa", "p-dsa-l4", "d-dsa-l4", "Lecture 4 — balanced trees",
         "2026-09-11T09:00:00Z", 27, scans=27)
    post("c-ai", "p-ai-l2", "d-ai-l2", "Lecture 2 — search strategies",
         "2026-09-12T09:00:00Z", 55, scans=6)
    post("c-ai", "p-ai-td1", "d-ai-td1", "TD1 — heuristics", "2026-09-19T09:00:00Z", 4)

    # Probability: every attachment gone from Drive, so no readable material.
    for index in range(3):
        attachment = {"driveFile": {"driveFile": {
            "id": f"d-prob-{index}", "title": f"Chapitre {index + 1}",
            "alternateLink": "https://drive/x"}}}
        material, _ = parse_coursework_material(
            {"id": f"p-prob-{index}", "title": f"Chapitre {index + 1}",
             "creationTime": "2026-09-05T09:00:00Z",
             "updateTime": "2026-09-05T09:00:00Z", "materials": [attachment]},
            "c-prob",
        )
        store.upsert_coursework_material(conn, material)
        store.upsert_materials(conn, parse_materials(
            "coursework_material", f"p-prob-{index}", "c-prob", [attachment]))
        store.upsert_extraction(conn, f"d-prob-{index}", status="missing")

    # Deadlines: one already gone (the only red), some ahead, one undated task.
    for work_id, title, due in (
        ("w-net-tp1", "TP1 — packet capture", "2026-09-24"),
        ("w-db-tp3", "TP3 — joins in practice", "2026-10-08"),
        ("w-os-tp2", "TP2 — a tiny shell", "2026-10-14"),
    ):
        y, m, d = (int(x) for x in due.split("-"))
        work, _ = parse_coursework(
            {"id": work_id, "title": title,
             "dueDate": {"year": y, "month": m, "day": d},
             "dueTime": {"hours": 22, "minutes": 59}},
            {"w-net-tp1": "c-net", "w-db-tp3": "c-db", "w-os-tp2": "c-os"}[work_id],
        )
        store.upsert_coursework(conn, work)
    store.add_manual_task(conn, course_id="manual-calculus-iii",
                          title="Série 2 — double integrals", kind="exercise_sheet",
                          due_at="2026-10-09T22:59:00Z")
    store.add_manual_task(conn, course_id="manual-algebra-iii",
                          title="Read chapter 3 before the tutorial", kind="reading")

    project_id = store.add_project(
        conn, course_id="c-dsa", title="Route planner",
        deadline_at="2026-11-20T22:59:00Z",
        deliverables="A report\nThe source code", team="Adam\nYoussef",
    )
    for index, name in enumerate(("Pick the graph library", "Load the map",
                                  "Dijkstra, then A*", "Write the report")):
        milestone = store.add_milestone(conn, project_id, name)
        if index < 2:
            store.complete_milestone(conn, milestone)

    # One moved session and one cancelled, so the timetable shows both.
    store.add_adjustment(conn, applies_on="2026-10-05", kind="moved", subject="Computer Networks",
                         course_id="c-net", session_start="13:00", to_date="2026-10-05",
                         to_start="15:00", to_end="16:30", reason="room clash")
    store.add_adjustment(conn, applies_on="2026-10-07", kind="cancelled",
                         subject="Artificial Intelligence", course_id="c-ai",
                         session_start="10:15", reason="conference")
    conn.commit()
    conn.close()
    return data


PROBE = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>html,body{margin:0;background:#000}</style></head><body>
<iframe id=f style='width:__W__px;height:__H__px;border:0;display:block'></iframe>
<script>
const q = new URLSearchParams(location.search);
(async () => {
  if (q.get('s') !== '0') {
    await fetch('/api/session', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: q.get('t')})});
  }
  document.getElementById('f').src = q.get('p');
})();
</script></body></html>"""


def capture(base: str, out: Path, name: str, path: str, signed_in: bool) -> None:
    for label, (width, height) in WIDTHS.items():
        probe = REPO / "web" / "__shot.html"
        probe.write_text(
            PROBE.replace("__W__", str(width)).replace("__H__", str(height)),
            encoding="utf-8",
        )
        profile = Path(tempfile.mkdtemp(prefix="shot-"))
        raw = out / f".{name}-{label}.raw.png"
        window = (max(width, 520), height)
        url = f"{base}/__shot.html?t={TOKEN}&p={urllib.request.quote(path, safe='/?=&')}"
        if not signed_in:
            url += "&s=0"
        try:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--no-sandbox", "--hide-scrollbars", f"--user-data-dir={profile}",
                 f"--window-size={window[0]},{window[1]}",
                 "--virtual-time-budget=9000",
                 f"--screenshot={raw}", url],
                capture_output=True, timeout=120,
            )
        finally:
            probe.unlink(missing_ok=True)
            shutil.rmtree(profile, ignore_errors=True)
        if not raw.is_file():
            print(f"  ! {name} at {label}px: no screenshot")
            continue
        with Image.open(raw) as image:
            image.crop((0, 0, width, height)).save(out / f"{name}-{label}.png")
        raw.unlink()
        print(f"  {name}-{label}.png")


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "after"
    if CHROME is None:
        print("no Chrome to render with", file=sys.stderr)
        return 1
    out = REPO / "web" / "screenshots" / label
    out.mkdir(parents=True, exist_ok=True)

    root = Path(tempfile.mkdtemp(prefix="shots-"))
    data = seed(root)
    port = free_port()
    log = (root / "serve.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "agent.cli", "serve", "--config", str(root / "config.yaml"),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO),
        env={**os.environ, "DATA_DIR": str(data)},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=1):
                    break
            except Exception:
                time.sleep(0.25)
        else:
            print((root / "serve.log").read_text(errors="replace")[-2000:])
            return 1
        print(f"writing {out}")
        for name, path, signed_in in SCREENS:
            capture(base, out, name, path, signed_in)
    finally:
        server.kill()
        server.wait(timeout=10)
        log.close()
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
