"""The client, rendered by a real browser against a real server.

Everything else in this suite runs the API through Starlette's TestClient and the
client not at all. That gap hid five defects in one slice, every one of which
would have reached the phone:

  * Preact `render` APPENDS into a container holding children it did not create,
    so the loading line stayed on screen above the app.
  * `spellcheck="false"` is truthy, so it turned spellcheck ON over a 43-character
    secret.
  * A comment containing a dollar-brace inside an htm template is not a comment --
    it is an empty interpolation, which is a SyntaxError that blanks the page.
  * htm drops the whitespace between adjacent interpolations on separate lines:
    "12 pagesare not transcribed", "Sep 2908:30".
  * And the big one: FastAPI runs a sync `yield` dependency's setup and teardown
    on DIFFERENT threadpool workers, so every request died on "SQLite objects
    created in a thread can only be used in that same thread". TestClient runs a
    request inside one portal thread and never reproduces it.

None of those is subtle once seen, and none was visible without rendering. So
this starts uvicorn, points headless Chrome at it, and reads what came out.

Skipped when Chrome is absent, which keeps the suite runnable anywhere -- but the
skip is reported, because a check that quietly never runs is worse than no check.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.classroom.models import parse_coursework_material, parse_materials, parse_course
from agent.config import REPO_ROOT
from agent.db import store
from agent.gate import sections

TOKEN = "render-test-token-" + "r" * 24

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

PATTERN = """
subjects:
  Database: "842149328479"
  OS: "840878703017"

versions:
  - label: S1 2026-27
    status: confirmed
    effective_from: 2026-01-01
    effective_to: 2027-12-31
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC,
          subject: Database, teacher: Gharbi, room: Lab3 }
      - { day: tue, start: "08:30", end: "10:00", kind: LAB,
          subject: OS, teacher: Mansour, room: Lab4 }
"""

# 2026-09-28 is a Monday (Database, with a backlog); 2026-09-29 a Tuesday (OS,
# with nothing). Fixed dates so the two states are reachable whatever today is.
BUSY_DAY = "2026-09-28"
CLEAR_DAY = "2026-09-29"


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("chrome") or shutil.which("google-chrome")


chrome_only = pytest.mark.skipif(
    find_chrome() is None, reason="no Chrome on this machine to render with"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """A real uvicorn, against a real temp DATA_DIR, for the module."""
    if find_chrome() is None:
        pytest.skip("no Chrome")

    root = tmp_path_factory.mktemp("render")
    data = root / "data"
    (data / "library" / "files").mkdir(parents=True)
    (data / "library" / "text").mkdir(parents=True)
    (data / "logs").mkdir(parents=True)
    (root / "timetable.yaml").write_text(PATTERN, encoding="utf-8")
    (root / "config.yaml").write_text(
        "account: someone@example.com\n"
        "timezone: Africa/Tunis\n"
        "courses:\n"
        '  tracked: ["842149328479", "840878703017"]\n'
        "  ingored: []\n".replace("ingored", "ignored")
        + "telegram:\n  chat_id: 1\n  bot_username: my_study_bot\n"
        "api:\n  secure_cookie: false\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(f"WEB_API_TOKEN={TOKEN}\n", encoding="utf-8")

    _seed(data)

    port = free_port()
    environment = {**os.environ, "DATA_DIR": str(data)}
    process = subprocess.Popen(
        [
            sys.executable, "-m", "agent.cli", "serve",
            "--config", str(root / "config.yaml"),
            "--host", "127.0.0.1", "--port", str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        cwd=str(REPO_ROOT),
        encoding="utf-8",
        errors="replace",
    )

    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if process.poll() is not None:
            pytest.fail(f"serve exited: {process.stdout.read()[-2000:]}")
        try:
            import urllib.request

            with urllib.request.urlopen(f"{base}/api/health", timeout=1) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.25)
    else:
        process.kill()
        pytest.fail("serve never became healthy")

    yield base, process

    process.kill()
    process.wait(timeout=10)


def _seed(data: Path) -> None:
    conn = store.connect(data / "academic.db")
    store.upsert_course(conn, parse_course({"id": "842149328479", "name": "Database GA"}))
    store.upsert_course(conn, parse_course({"id": "840878703017", "name": "OS"}))

    pages = [
        f"Joins and null semantics. Slide {n + 1}. Consider a relation R with "
        "attributes A, B and C; the operation preserves every tuple whose B "
        "value satisfies the predicate and discards the rest."
        for n in range(92)
    ]
    attachment = {"driveFile": {"driveFile": {
        "id": "d-render", "title": "Chapter 4 — SQL joins", "alternateLink": "x"}}}
    material, _ = parse_coursework_material(
        {"id": "p-render", "title": "Chapter 4 — SQL joins",
         "creationTime": "2026-09-04T09:00:00Z",
         "updateTime": "2026-09-04T09:00:00Z", "materials": [attachment]},
        "842149328479",
    )
    store.upsert_coursework_material(conn, material)
    store.upsert_materials(conn, parse_materials(
        "coursework_material", "p-render", "842149328479", [attachment]))
    (data / "library" / "text" / "d-render.txt").write_text(
        sections.PAGE_BREAK.join(pages), encoding="utf-8")
    (data / "library" / "files" / "d-render.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    store.upsert_extraction(
        conn, "d-render", status="ok", local_path="files/d-render.pdf",
        text_path="text/d-render.txt", mime_type="application/pdf",
        size_bytes=64, pages=92, chars=9000, scan_pages=12, ocr_pages=0,
        method="pdf", md5_checksum="d-render",
    )
    for index in range(80, 92):
        store.upsert_ocr_page(
            conn, drive_id="d-render", page_index=index,
            page_hash=f"h{index}", status="pending")
    store.ensure_study_item(
        conn, course_id="842149328479",
        entity_type="coursework_material", entity_id="p-render")
    conn.commit()
    conn.close()


def render(base: str, tmp_path: Path, date: str | None = None) -> dict:
    """Load the app in a real browser and return its text, console and size."""
    probe = REPO_ROOT / "web" / "__render_probe.html"
    probe.write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        "<iframe id=f style='width:390px;height:900px;border:0'></iframe>"
        "<pre id=o></pre><script>\n"
        "const q=new URLSearchParams(location.search);\n"
        "(async()=>{await fetch('/api/session',{method:'POST',"
        "credentials:'same-origin',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({token:q.get('t')})});\n"
        "const f=document.getElementById('f');\n"
        "f.src=q.get('d')?'/?date='+q.get('d'):'/';\n"
        "f.onload=()=>setTimeout(()=>{const d=f.contentDocument;\n"
        "document.getElementById('o').textContent=JSON.stringify({"
        "text:d.body.innerText,html:d.getElementById('app').innerHTML,"
        "client:d.documentElement.clientWidth,"
        "scroll:d.documentElement.scrollWidth});},2500);})();\n"
        "</script></body></html>",
        encoding="utf-8",
    )
    try:
        url = f"{base}/__render_probe.html?t={TOKEN}"
        if date:
            url += f"&d={date}"
        profile = tmp_path / f"chrome-{date or 'now'}"
        result = subprocess.run(
            [
                find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-sandbox", f"--user-data-dir={profile}",
                "--virtual-time-budget=12000", "--enable-logging=stderr",
                "--log-level=0", "--dump-dom", url,
            ],
            capture_output=True,
            timeout=120,
            # UTF-8 explicitly. `text=True` decodes with the locale encoding,
            # which on Windows is cp1252 -- so Chrome's UTF-8 came back as
            # "Database Â· 1 unreviewed" and every assertion about a middle dot
            # or an em dash failed against a perfectly correct page.
            encoding="utf-8",
            errors="replace",
        )
    finally:
        probe.unlink(missing_ok=True)

    # Read out of the <pre>, not from a delimiter: the probe's own script source
    # is in the dumped DOM too, so any literal marker appears twice and the
    # second one lands past the payload.
    import html as html_mod
    import re

    body = result.stdout
    found = re.search(r'<pre id="o">(.*?)</pre>', body, re.S)
    assert found and found.group(1).strip(), f"probe produced nothing:\n{body[:1500]}"
    payload = json.loads(html_mod.unescape(found.group(1)))
    payload["console"] = [
        line for line in result.stderr.splitlines() if "CONSOLE" in line
    ]
    return payload


@pytest.fixture(scope="module")
def busy(served, tmp_path_factory):
    """The 23:00 state, rendered once for the whole file.

    Module-scoped deliberately. Chrome costs about ten seconds a launch, and a
    fixture per assertion turned a 90-second suite into minutes -- which is the
    kind of cost that gets a valuable test deleted rather than fixed.
    """
    base, _ = served
    return render(base, tmp_path_factory.mktemp("busy"), BUSY_DAY)


@pytest.fixture(scope="module")
def clear(served, tmp_path_factory):
    """The empty state, rendered once for the whole file."""
    base, _ = served
    return render(base, tmp_path_factory.mktemp("clear"), CLEAR_DAY)


# ---------------------------------------------------------------------------


@chrome_only
def test_the_app_renders_with_no_console_errors(busy):
    """A SyntaxError anywhere in the module graph is a blank page and nothing else."""
    errors = [
        line for line in busy["console"]
        if "error" in line.lower() or "Uncaught" in line
    ]
    assert not errors, errors


@chrome_only
def test_the_loading_line_is_replaced_not_joined(busy):
    """Preact appends into a container it did not create. It must be cleared."""
    assert "booting" not in busy["html"]


@chrome_only
def test_the_waiting_state_says_what_design_md_asks_for(busy):
    """DESIGN.md section 2: subject, title, the window, how much is readable."""
    text = busy["text"]

    assert "Database · 1 unreviewed" in text
    assert "Chapter 4 — SQL joins" in text
    assert "92 pages" in text
    assert "an evening is about pages 1-20" in text
    assert "This counts as read, not verified." in text
    assert "Open in Telegram" in text


@chrome_only
def test_no_words_are_run_together(busy, clear):
    """htm drops whitespace between adjacent interpolations.

    It produced "12 pagesare not transcribed" and "Sep 2908:30", both of which
    render, pass every other test, and look broken to a person.
    """
    for found in (busy, clear):
        text = found["text"]
        assert "pagesare" not in text
        assert "pagesis" not in text
        # A digit immediately followed by a capital, or two sentences with no
        # gap, are the shapes this failure takes.
        import re

        assert not re.search(r"\d{2}:\d{2}[A-Za-z]", text), text
        assert not re.search(r"[a-z]\d{2}:\d{2}", text), text


@chrome_only
def test_the_empty_state_says_almost_nothing(clear):
    """DESIGN.md section 4: one line, then get out of the way."""
    text = clear["text"]

    assert "Nothing waiting." in text
    assert "Next session: OS lab" in text
    # No praise, no backfill, no suggestion of something else to do.
    for forbidden in ("Great", "Well done", "streak", "Nice", "!"):
        assert forbidden not in text, forbidden


@chrome_only
def test_nothing_overflows_at_phone_width(busy, clear):
    """390px is a real phone. A horizontal scrollbar on a reader is a defect."""
    for found in (busy, clear):
        assert found["client"] == 390, found["client"]
        assert found["scroll"] <= found["client"], found["scroll"]


@chrome_only
def test_no_percentage_reaches_the_screen(busy, clear):
    """The server cannot send one; this checks the client did not invent one."""
    for found in (busy, clear):
        assert "%" not in found["text"], found["text"]


@chrome_only
def test_real_requests_survive_the_threadpool(served, tmp_path):
    """The bug TestClient structurally cannot catch.

    FastAPI runs a sync `yield` dependency's setup and teardown on different
    workers, so the request connection must tolerate a thread hand-off. If it
    does not, every request 500s -- which is exactly what a browser saw while
    1,415 tests were green.
    """
    import urllib.error
    import urllib.request

    base, process = served
    # Signing in is a WRITE, which is where the teardown commit happens.
    request = urllib.request.Request(
        f"{base}/api/session",
        data=json.dumps({"token": TOKEN}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        cookie = response.headers.get("set-cookie", "").split(";")[0]

    # Ten sequential reads, patiently. Each one is a fresh dependency setup and
    # teardown, which is where the hand-off happens -- volume is not the point,
    # and a tight timeout here only makes the test flaky on a loaded machine.
    for _ in range(10):
        read = urllib.request.Request(
            f"{base}/api/now?date={BUSY_DAY}", headers={"Cookie": cookie}
        )
        with urllib.request.urlopen(read, timeout=30) as response:
            assert response.status == 200
