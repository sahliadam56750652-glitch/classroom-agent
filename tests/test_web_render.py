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
import re
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
    """A port that was free a moment ago.

    Inherently a race: the socket is closed before uvicorn binds it, so another
    process can take it in between. That is why the caller RETRIES rather than
    trusting this -- a flaky fixture in a suite run before every commit teaches
    you to re-run a red suite instead of reading it, which is worse than no test.
    """
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

    environment = {**os.environ, "DATA_DIR": str(data)}

    import urllib.request

    # Three attempts, because `free_port` is inherently a race -- the socket is
    # closed before uvicorn binds it -- and losing that race looks exactly like
    # the server failing to start. A full suite run hit this once; a flaky
    # fixture in a suite run before every commit teaches you to re-run a red
    # suite rather than read it.
    process = handle = base = None
    failures = []

    for attempt in range(3):
        port = free_port()
        # A FILE, not a pipe. uvicorn logs a line per request, and an unread
        # subprocess.PIPE holds about 64 KB before the writer BLOCKS -- so the
        # server answered the first several dozen requests and then froze
        # mid-suite, with every later test timing out on a socket read. Nothing
        # was wrong with the server; the harness had stopped listening to it.
        log = root / f"serve-{attempt}.log"
        handle = log.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [
                sys.executable, "-m", "agent.cli", "serve",
                "--config", str(root / "config.yaml"),
                "--host", "127.0.0.1", "--port", str(port),
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=environment,
            cwd=str(REPO_ROOT),
        )

        candidate = f"http://127.0.0.1:{port}"
        healthy = False
        for _ in range(120):
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"{candidate}/api/health", timeout=1
                ) as response:
                    if response.status == 200:
                        healthy = True
                        break
            except Exception:
                time.sleep(0.25)

        if healthy:
            base = candidate
            break

        # Not healthy: record why, clean up, and try another port.
        process.kill()
        process.wait(timeout=10)
        handle.close()
        failures.append(log.read_text(errors="replace")[-1200:])

    if base is None:
        pytest.fail(
            "serve never became healthy in three attempts:\n\n"
            + "\n---\n".join(failures)
        )

    yield base, process

    process.kill()
    process.wait(timeout=10)
    handle.close()


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
    # A REAL 92-page PDF, built with the pymupdf this project already depends on.
    # A stub of "%PDF-1.4 %%EOF" is not a document PDF.js can open, so the reader
    # would fail to parse it and every assertion about the reader would be about
    # the stub rather than about the reader.
    import pymupdf

    document = pymupdf.open()
    for number in range(1, 93):
        page = document.new_page(width=420, height=560)
        page.insert_text((40, 80), f"Page {number}", fontsize=18)
        page.insert_text((40, 120), "Joins and null semantics.", fontsize=11)
    document.save(data / "library" / "files" / "d-render.pdf")
    document.close()
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

    # A second item, fully transcribed and already read, with its questions
    # written -- in a course outside the timetable, so it changes nothing the
    # other screens assert about Database and OS. Study item 2.
    _seed_a_quiz(conn, data)
    conn.commit()
    conn.close()


QUIZ_ITEM = 2


def _seed_a_quiz(conn, data: Path) -> None:
    import json as _json

    from agent.gate import quiz as gate_quiz

    store.upsert_course(conn, parse_course({"id": "c-quiz", "name": "Networks"}))
    attachment = {"driveFile": {"driveFile": {
        "id": "d-quiz", "title": "Lecture 2.pdf", "alternateLink": "x"}}}
    material, _ = parse_coursework_material(
        {"id": "p-quiz", "title": "Lecture 2 — the link layer",
         "creationTime": "2026-09-10T09:00:00Z",
         "updateTime": "2026-09-10T09:00:00Z", "materials": [attachment]},
        "c-quiz",
    )
    store.upsert_coursework_material(conn, material)
    store.upsert_materials(conn, parse_materials("coursework_material", "p-quiz", "c-quiz", [attachment]))
    (data / "library" / "text" / "d-quiz.txt").write_text("Framing and error detection. " * 60, encoding="utf-8")
    shutil.copy(data / "library" / "files" / "d-render.pdf", data / "library" / "files" / "d-quiz.pdf")
    store.upsert_extraction(
        conn, "d-quiz", status="ok", local_path="files/d-quiz.pdf",
        text_path="text/d-quiz.txt", mime_type="application/pdf",
        size_bytes=64, pages=92, chars=1800, scan_pages=0, ocr_pages=0,
        method="pdf", md5_checksum="d-quiz",
    )
    store.ensure_study_item(
        conn, course_id="c-quiz", entity_type="coursework_material",
        entity_id="p-quiz", state="reviewed")
    questions = [
        {"question": f"Question {n + 1} about framing?",
         "options": [f"first {n}", f"second {n}", f"third {n}", f"fourth {n}"],
         "correct": n % 4, "explanation": f"SECRET-EXPLANATION-{n}",
         "source_file": "Lecture 2.pdf", "source_page": n + 2}
        for n in range(6)
    ]
    rows = store.study_item_sources(conn, "coursework_material", "p-quiz")
    store.save_questions(
        conn, item_id=QUIZ_ITEM, source_hash=gate_quiz.fingerprint_of(rows, 6),
        model="stub", questions=_json.dumps(questions),
    )


PROBE = """<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>
<iframe id=f style='width:__WIDTH__px;height:2400px;border:0'></iframe>
<pre id=o></pre><script>
const q = new URLSearchParams(location.search);
const TOKEN = q.get('t');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  if (q.get('s') !== '0') {
    await fetch('/api/session', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: TOKEN})});
  }
  const f = document.getElementById('f');
  await new Promise((r) => { f.onload = r;
    f.src = q.get('p') || (q.get('d') ? '/?date=' + q.get('d') : '/'); });
  await sleep(2500);
  const d = () => f.contentDocument;
  /*STEPS*/
  const doc = f.contentDocument;
  const app = doc.getElementById('app');
  document.getElementById('o').textContent = JSON.stringify({
    text: doc.body.innerText,
    html: app ? app.innerHTML : doc.body.innerHTML,
    path: f.contentWindow.location.pathname,
    client: doc.documentElement.clientWidth,
    scroll: doc.documentElement.scrollWidth,
  });
})();
</script></body></html>"""


def render(
    base: str,
    tmp_path: Path,
    date: str | None = None,
    path: str | None = None,
    *,
    sign_in: bool = True,
    steps: str = "",
    block_cookies: bool = False,
    width: int = 390,
) -> dict:
    """Load the app in a real browser and return its text, console and size.

    `steps` is JavaScript run inside the probe after the app has loaded, with
    `d()` returning the app's document and `sleep(ms)` available -- which is how
    a test signs in through the real form, or revokes a session mid-use and then
    taps a nav link, instead of asserting on a state it had to fake.

    `block_cookies` sets the profile to refuse every cookie. That is the real
    shape of "sign-in succeeded and the next call still 401s": the POST returns
    200 and the browser throws the Set-Cookie away, which is what a Secure cookie
    over plain http to a non-localhost host does.
    """
    probe = REPO_ROOT / "web" / "__render_probe.html"
    probe.write_text(
        PROBE.replace("__WIDTH__", str(width)).replace("/*STEPS*/", steps),
        encoding="utf-8",
    )
    profile = tmp_path / "chrome"
    if block_cookies:
        (profile / "Default").mkdir(parents=True, exist_ok=True)
        (profile / "Default" / "Preferences").write_text(
            json.dumps(
                {"profile": {"default_content_setting_values": {"cookies": 2}}}
            ),
            encoding="utf-8",
        )
    try:
        url = f"{base}/__render_probe.html?t={TOKEN}"
        if date:
            url += f"&d={date}"
        if path:
            url += f"&p={path}"
        if not sign_in:
            url += "&s=0"
        result = subprocess.run(
            [
                find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-sandbox", f"--user-data-dir={profile}",
                f"--window-size={max(width + 40, 520)},960",
                "--virtual-time-budget=20000", "--enable-logging=stderr",
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

    # The deficit line, as a screen reader and DESIGN.md say it. The subject is
    # a chip now, so the line is read from its text rather than from layout.
    line = re.search(r'class="t-state deficit">(.*?)</p>', busy["html"], re.S).group(1)
    words = " ".join(re.sub(r"<[^>]+>", " ", line).split())
    assert words == "Database · 1 unreviewed", words
    assert "Chapter 4 — SQL joins" in text
    assert "92 pages" in text
    assert "an evening is about pages 1-20" in text
    assert "This counts as read, not verified." in text

    # Opening the material first, then the two answers section 4 asks for, at
    # equal weight -- in the app since 5d, through the same functions the bot
    # uses. Skip says on its face that it is logged.
    assert "Open pages 1-20" in text
    assert "I've read it" in text
    assert "Skip — logged" in text
    assert "Read and Skip are in Telegram" not in text


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

    # "Nothing to review", not "Nothing waiting": Today also lists what is due.
    assert "Nothing to review." in text
    assert "Nothing waiting" not in text
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


def _sign_in(base: str) -> str:
    import urllib.request

    request = urllib.request.Request(
        f"{base}/api/session",
        data=json.dumps({"token": TOKEN}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.headers.get("set-cookie", "").split(";")[0]


# The reader's PDF RENDERING is deliberately not driven headlessly, and the
# reason is a property of the harness rather than of the reader.
#
# `--virtual-time-budget` fast-forwards TIMERS, which is what makes the screen
# tests above take four seconds instead of thirty. It does not fast-forward the
# network, and it starves a Web Worker: PDF.js loaded, `getDocument` created its
# task, and `task.promise` then never settled and never requested a single byte,
# inside a budget that virtual time had already exhausted. Nothing was wrong.
#
# Measured in a REAL Chrome against the same server, the same code reports
# `OPENED-pages-92-width-595` and the whole loop appears in the access log:
#
#     GET  /read/d-chap4                     200   deep link -> the app
#     GET  /api/documents/d-chap4            200   metadata and windows
#     GET  /api/documents/d-chap4/position   200   restore
#     GET  /api/documents/d-chap4/file       304   conditional GET hit
#     PUT  /api/documents/d-chap4/position   200   the settle timer saved it
#
# So the split is deliberate: everything AROUND the reader is tested here at the
# HTTP level, where it is fast and certain -- the ranges, the position round
# trip, the deep link, the conditional GET. Whether 92 pages actually paint is
# checked by looking, which is what DESIGN.md asks for anyway.
#
# What this does NOT cover, said plainly rather than left implied: that pages
# paint, that the chrome hides on scroll, and that pinch-zoom works on a real
# phone. Those are eye checks.

@chrome_only
def test_the_reader_records_where_i_stopped(served):
    """And the SERVER derives the anchor, because a browser cannot.

    The reader sends only `page_index`; `sections.anchor` hashes PyMuPDF's
    extraction text and PDF.js produces materially different text for the same
    page, so a client-computed hash would never match.
    """
    import urllib.request

    base, _ = served
    cookie = _sign_in(base)

    request = urllib.request.Request(
        f"{base}/api/documents/d-render/position",
        data=json.dumps({"page_index": 40}).encode(),
        headers={"Content-Type": "application/json", "Cookie": cookie},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        written = json.load(response)
    assert written["page"] == 41
    assert written["page_hash"], "the server did not derive an anchor"

    read = urllib.request.Request(
        f"{base}/api/documents/d-render/position", headers={"Cookie": cookie}
    )
    with urllib.request.urlopen(read, timeout=30) as response:
        assert json.load(response)["page"] == 41


@chrome_only
def test_the_reader_can_open_page_one_without_pulling_the_file(served):
    """The property the whole reader rests on, checked against a real server.

    PDF.js asks for the tail (the cross-reference table), then the pages it needs.
    What matters is that each of those is a 206 of a few KB rather than a 200 of
    the whole document -- if it ever silently becomes a 200, the only symptom is
    that a 92-page deck "feels slow" on mobile data.
    """
    import urllib.request

    base, _ = served
    cookie = _sign_in(base)
    url = f"{base}/api/documents/d-render/file"

    whole = urllib.request.Request(url, headers={"Cookie": cookie})
    with urllib.request.urlopen(whole, timeout=30) as response:
        size = int(response.headers["content-length"])
        assert response.headers.get("accept-ranges") == "bytes"
        # Drained deliberately. Abandoning a streaming response leaves the
        # server writing into a socket nobody is reading, and the next request
        # queues behind it -- which is how this test wedged the two after it.
        response.read()
    assert size > 20_000, "the seeded PDF is too small to prove anything"

    # The tail, which is the first thing PDF.js asks for.
    tail = urllib.request.Request(
        url, headers={"Cookie": cookie, "Range": f"bytes={size - 2048}-"}
    )
    with urllib.request.urlopen(tail, timeout=30) as response:
        assert response.status == 206
        body = response.read()
    assert len(body) == 2048, len(body)

    # And an arbitrary chunk from the middle.
    middle = urllib.request.Request(
        url, headers={"Cookie": cookie, "Range": "bytes=4096-8191"}
    )
    with urllib.request.urlopen(middle, timeout=30) as response:
        assert response.status == 206
        assert response.headers["content-range"] == f"bytes 4096-8191/{size}"
        assert len(response.read()) == 4096


@chrome_only
def test_a_deep_link_into_the_reader_serves_the_app(served):
    """`/read/abc` is not a file. It must return index.html, not a 404.

    Two bugs in that mount both looked correct and never fired: StaticFiles
    RAISES its 404 rather than returning one, and it raises starlette's
    exception, not FastAPI's subclass.
    """
    import urllib.request

    base, _ = served
    with urllib.request.urlopen(f"{base}/read/anything", timeout=30) as response:
        assert response.status == 200
        assert b"<title>Lectern</title>" in response.read()


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


@pytest.fixture(scope="module")
def subjects(served, tmp_path_factory):
    base, _ = served
    return render(base, tmp_path_factory.mktemp("subjects"), path="/subjects")


@chrome_only
def test_the_subjects_screen_shows_four_distinguishable_states(subjects):
    """DESIGN.md section 3: never collapsed into a number.

    The seeded timetable has Database (behind) and OS (a tracked course with no
    material here), so two of the states are on screen at once and the third --
    a subject awaiting an id -- is what `no Classroom course yet` would say.
    """
    text = subjects["text"]
    assert "Database" in text
    assert "OS" in text
    # Behind is COUNTED, never described as a share.
    assert "1 unreviewed" in text


@chrome_only
def test_no_percentage_on_the_subjects_screen(subjects):
    """The server cannot send one. This checks the client did not compute one."""
    assert "%" not in subjects["text"], subjects["text"]


@chrome_only
def test_no_progress_bar_for_a_subject(subjects):
    """Section 3 allows a bar for ONE ITEM and nothing larger.

    A bar for a subject is a progress indicator for something that never
    completes, which is a mood ring.
    """
    assert "<progress" not in subjects["html"]
    assert "ruler" not in subjects["html"]


@chrome_only
def test_the_whole_picture_is_one_tap_away(busy):
    """Section 2: reachable, and never the front door."""
    # The five places of DESIGN.md section 8 -- and never a count on any of
    # them, which is the badge section 7 forbids.
    for place in ("Today", "Study", "Work", "Timetable", "More"):
        assert place in busy["text"], place
    assert "Counted" not in busy["text"]
    # The navigation comes AFTER the content in the document: the next action
    # is what a screen reader, and a slow first paint, reach first.
    assert busy["html"].index("<nav") > busy["html"].index("deficit")
    # No number on a tab or a sidebar link, ever.
    for nav in re.findall(r"<nav\b.*?</nav>", busy["html"], flags=re.S):
        words = re.sub(r"<[^>]+>", " ", nav)
        assert not re.search(r"\d", words), words


@pytest.fixture(scope="module")
def timetable(served, tmp_path_factory):
    """The timetable with the first session's menu opened, as a thumb would."""
    base, _ = served
    steps = """
      // A phone shows one day, opening on today; Monday is the day that has
      // a session in this fixture, so choose it first.
      const monday = d().querySelector('.tt-day-chip');
      if (monday) monday.click();
      await sleep(400);
      const first = d().querySelector('.session-menu-button:not([disabled])');
      if (first) first.click();
      await sleep(600);
    """
    return render(base, tmp_path_factory.mktemp("tt"), path="/timetable", steps=steps)


@chrome_only
def test_the_timetable_offers_to_record_what_happened(timetable):
    """The Phase 5 requirement: a moved or cancelled session, in a few taps.

    On a phone at 22:00 hand-editing timetable.yaml does not happen, so the gate
    prepares me for a lecture that is not taking place.
    """
    # One menu per session, opened from the session: Cancel and Move are in it,
    # and there is no row of buttons repeated on every line.
    assert "Cancel this session" in timetable["text"]
    assert "Move it to another time" in timetable["text"]
    assert timetable["html"].count('role="menu"') == 1


@chrome_only
def test_the_timetable_screen_never_offers_to_edit_the_file(timetable):
    """The file holds the pattern and keeps ONE writer.

    A control that wrote it would undo the Phase 3a decision by giving one
    source of truth two writers, so there must be no such control -- and the
    screen says so rather than leaving its absence to be noticed.
    """
    html_out = timetable["html"]
    assert "timetable.yaml is never written" in timetable["text"]
    for forbidden in ("/api/timetable/file", "Edit pattern", "Save timetable"):
        assert forbidden not in html_out, forbidden


@chrome_only
def test_no_dialog_is_used_to_record_a_move(timetable):
    """`prompt()` blocks the page and is suppressed outright in some installed
    PWAs -- a Move button that silently does nothing is worse than none."""
    import re as _re

    for path in ("web/screens/timetable.js",):
        body = (REPO_ROOT / path).read_text(encoding="utf-8")
        # Comments stripped first. This file explains in words that it does NOT
        # use prompt(), and a textual scan flagged the explanation -- which
        # would leave a real call and a note about it indistinguishable, and
        # eventually get the note deleted to make the test pass.
        body = _re.sub(r"/\*.*?\*/", "", body, flags=_re.S)
        body = _re.sub(r"^\s*//.*$", "", body, flags=_re.M)
        for blocking in ("prompt(", "confirm(", "alert("):
            assert blocking not in body, blocking


@chrome_only
def test_the_deadlines_screen_invents_no_urgency(served, tmp_path_factory):
    """`due_at` is the only fact about time. No countdown, no escalation."""
    base, _ = served
    found = render(base, tmp_path_factory.mktemp("dl"), path="/deadlines")
    text = found["text"]
    for forbidden in ("hours left", "days left", "overdue in", "urgent", "!"):
        assert forbidden not in text, forbidden


@pytest.fixture(scope="module")
def add_screen(served, tmp_path_factory):
    base, _ = served
    return render(base, tmp_path_factory.mktemp("add"), path="/add")


@chrome_only
def test_the_add_screen_offers_the_three_hand_entries(add_screen):
    """Phase 6's point: a third of my week has no Classroom at all."""
    text = add_screen["text"]
    assert "Upload a board or a handout" in text
    assert "Record a task" in text
    assert "Log a session" in text
    assert "Start a project" in text


@chrome_only
def test_the_camera_is_one_tap_away_with_the_gallery_beside_it(add_screen):
    """For the subjects with no Classroom, a photographed board is how material
    arrives. The first door opens the back camera directly; the second takes a
    photo from the gallery or a file, because capture= alone would refuse one."""
    found = re.findall(r"<input[^>]*type=\"file\"[^>]*>", add_screen["html"])
    camera = [tag for tag in found if 'capture="environment"' in tag]
    gallery = [tag for tag in found if "capture" not in tag]
    assert len(camera) == 1 and 'accept="image/*"' in camera[0], found
    assert len(gallery) == 1 and "image/*" in gallery[0] and ".pdf" in gallery[0], found
    assert "Photograph a board" in add_screen["text"]
    assert "Choose a photo or file" in add_screen["text"]


@chrome_only
def test_the_upload_form_says_what_happens_next(add_screen):
    """An uploaded file enters the pipeline unchanged. That is the feature."""
    # Said in the screen's voice now; the rule underneath is unchanged and
    # still on the API's own reply (`note`) and in agent/api/routes/entries.py.
    assert "read, transcribed and quizzed like anything from Classroom" in add_screen["text"]


@chrome_only
def test_no_percentage_control_anywhere_in_the_client():
    """Milestone-based progress, enforced where it can be undone.

    A percentage is a feeling typed into a box. The server sends none, and a
    slider or a percent field here would reintroduce exactly what the milestone
    design exists to prevent.
    """
    import re as _re

    for path in sorted((REPO_ROOT / "web").rglob("*.js")):
        if "vendor" in path.parts:
            continue
        body = path.read_text(encoding="utf-8")
        body = _re.sub(r"/\*.*?\*/", "", body, flags=_re.S)
        body = _re.sub(r"^\s*//.*$", "", body, flags=_re.M)
        assert 'type="range"' not in body, path.name
        assert "percent" not in body.lower(), path.name


@chrome_only
def test_nothing_in_the_client_computes_a_share_of_a_deficit():
    """The other half of the enforcement.

    The server omits the field; that only means anything while the client
    declines to reconstruct it, and `unreviewed / (unreviewed + verified)` is one
    line away at any time.
    """
    import re as _re

    for path in sorted((REPO_ROOT / "web").rglob("*.js")):
        if "vendor" in path.parts:
            continue
        body = _re.sub(
            r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=_re.M
        )
        # A division whose right-hand side mentions a count we publish.
        suspicious = _re.findall(
            r"/\s*\(?\s*\w*(?:unreviewed|verified|total|milestones_total)", body
        )
        assert not suspicious, f"{path.name}: {suspicious}"


# ---------------------------------------------------------------------------
# signed out -- the three ways it happens
# ---------------------------------------------------------------------------
#
# A 401 anywhere means signed out, and the only right response is the sign-in
# screen: no nav, no "Sign out" for a session that does not exist, and never the
# raw "GET /api/x failed with 401" that an unhandled error falls through to.

RAW_STATUS = re.compile(r"(GET|POST|PUT|DELETE) /api/\S* failed with \d{3}")


def _looks_signed_out(found: dict) -> None:
    assert found["path"] == "/signin", found["path"]
    assert "Sign out" not in found["text"], found["text"][:300]
    assert "<nav" not in found["html"], "a nav rendered for a session that does not exist"
    assert not RAW_STATUS.search(found["text"]), found["text"][:300]


@chrome_only
def test_opening_with_no_session_lands_on_signin(served, tmp_path_factory):
    """Asked of the server before the shell renders, and answered by it."""
    base, _ = served
    found = render(base, tmp_path_factory.mktemp("s1"), path="/", sign_in=False)
    _looks_signed_out(found)
    assert "WEB_API_TOKEN" in found["text"]


@chrome_only
def test_a_401_mid_use_routes_to_signin(served, tmp_path_factory):
    """The shell is up, the session goes away, and the next tap finds out.

    This is the exact shape of the reported bug: a screen's own error handler
    received the 401 and printed it, under a nav and a Sign out that no longer
    meant anything.
    """
    base, _ = served
    found = render(
        base,
        tmp_path_factory.mktemp("s2"),
        path="/",
        steps="""
          await fetch('/api/session', {method: 'DELETE', credentials: 'same-origin'});
          const link = d().querySelector('a[href="/study"]');
          if (link) link.click();
          await sleep(2500);
        """,
    )
    _looks_signed_out(found)


@chrome_only
def test_a_cookie_the_browser_refused_is_named(served, tmp_path_factory):
    """Sign-in returns 200 and the next call is still a 401.

    Nothing else can explain that pair, so the screen says it: the browser did
    not keep the session cookie, and api.secure_cookie is the usual reason -- a
    Secure cookie over plain http is accepted only for localhost.
    """
    base, _ = served
    found = render(
        base,
        tmp_path_factory.mktemp("s3"),
        path="/signin",
        sign_in=False,
        block_cookies=True,
        steps="""
          const input = d().querySelector('input[type=password]');
          input.value = TOKEN;
          input.dispatchEvent(new Event('input', {bubbles: true}));
          await sleep(300);
          d().querySelector('form').requestSubmit();
          await sleep(3000);
        """,
    )
    assert found["path"] == "/signin", found["path"]
    assert "api.secure_cookie" in found["text"], found["text"][:500]
    assert not RAW_STATUS.search(found["text"]), found["text"][:300]


def test_no_screen_builds_raw_status_text():
    """The source of the reported string, removed at the source.

    `ApiError`'s default message was `${route} failed with ${status}`, and every
    screen printed `err.message` when there was no detail. Checked statically as
    well as rendered, because a screen nobody renders in a test can still reach
    it.
    """
    for module in sorted((REPO_ROOT / "web").rglob("*.js")):
        if "vendor" in module.parts:
            continue
        body = module.read_text(encoding="utf-8")
        assert "failed with ${" not in body, module.name
        assert "err.message" not in body, (
            f"{module.name} prints err.message; use describe(err) so a user sees "
            f"a sentence rather than a status line"
        )


@chrome_only
def test_an_open_quiz_shows_the_question_and_never_the_answer(served, tmp_path_factory):
    """Phase 5d: the browser sits the quiz, and the server grades it.

    The explanation exists on the server and must not be anywhere in the page
    until the attempt is finished.
    """
    base, _ = served
    found = render(base, tmp_path_factory.mktemp("quiz"), path=f"/quiz/{QUIZ_ITEM}")
    assert "Question 1 of 6" in found["text"], found["text"][:400]
    assert "first 0" in found["text"]
    assert "This question is wrong" in found["text"]
    assert "SECRET-EXPLANATION" not in found["html"]
    assert found["scroll"] <= found["client"]


@chrome_only
def test_a_finished_quiz_shows_counts_and_links_to_the_page(served, tmp_path_factory):
    """Answer everything wrong; the result is counts and each miss links its page."""
    base, _ = served
    steps = """
    for (let n = 0; n < 6; n++) {
      const buttons = [...d().querySelectorAll('.quiz-option')];
      if (!buttons.length) break;
      // Always the last option: wrong for questions whose answer is not 3.
      buttons[buttons.length - 1].click();
      await sleep(700);
    }
    await sleep(800);
    """
    found = render(base, tmp_path_factory.mktemp("quizdone"), path=f"/quiz/{QUIZ_ITEM}", steps=steps)
    text = found["text"]
    assert " of 6" in text, text[:600]
    assert "%" not in text
    assert "What you missed" in text
    assert "Open page" in text
    assert "/read/d-quiz?page=" in found["html"]


@chrome_only
def test_study_holds_subjects_quizzes_and_library(served, tmp_path_factory):
    """DESIGN.md section 8: Study's three parts, a row of links under its title."""
    base, _ = served
    found = render(base, tmp_path_factory.mktemp("study"), path="/study")
    html_out = found["html"]
    for part in ('href="/study"', 'href="/quizzes"', 'href="/library"'):
        assert part in html_out, part
    assert "Database" in found["text"]
    assert "%" not in found["text"]
    assert found["scroll"] <= found["client"]


@chrome_only
def test_the_greeting_stays_evening_until_five(served, tmp_path_factory):
    """01:30 is still the evening to someone studying -- never "Good morning".

    Run in a real browser against greeting.js itself, at fixed instants, so the
    hours are the module's and not a copy of them.
    """
    base, _ = served
    steps = """
      const { greeting } = await import('/greeting.js');
      const at = (h, m) => greeting(new Date(2026, 9, 2, h, m));
      d().body.innerText = JSON.stringify({
        '00:00': at(0, 0), '01:30': at(1, 30), '04:59': at(4, 59),
        '05:00': at(5, 0), '11:59': at(11, 59), '12:00': at(12, 0),
        '17:59': at(17, 59), '18:00': at(18, 0), '23:00': at(23, 0),
      });
    """
    found = render(base, tmp_path_factory.mktemp("greet"), path="/", steps=steps)
    said = json.loads(found["text"])
    for clock in ("00:00", "01:30", "04:59", "18:00", "23:00"):
        assert said[clock] == "Good evening", (clock, said[clock])
    for clock in ("05:00", "11:59"):
        assert said[clock] == "Good morning", (clock, said[clock])
    for clock in ("12:00", "17:59"):
        assert said[clock] == "Good afternoon", (clock, said[clock])
