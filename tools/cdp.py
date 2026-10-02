"""Just enough of the Chrome DevTools Protocol to take one screenshot at a set time.

    from cdp import screenshot_at
    screenshot_at(chrome, url, (width, height), out_png, at_ms=...)

Why it exists: DESIGN.md section 4 says the 23:00 state must look exactly like
every other state, and the redesign brief asks for a screenshot of it. Headless
Chrome's command line cannot set the clock, so a "23:00" screenshot taken at
16:00 would be a claim rather than a picture.

The clock is FROZEN, the same rule as the auth test: the page's Date starts at
the instant asked for and runs on from there, and the time zone is never
touched. Moving the zone instead was tried first and was wrong -- every due time
on the page then rendered in that zone, so the screenshot showed times I would
never see. Virtual time was tried before that and raced weeks ahead while the
page idled. A script installed into every frame before its own scripts run
replaces Date with one offset to the instant; nothing else changes.

Standard library only -- a tool for this repository should not add a
dependency -- so the WebSocket here is the minimum: one connection, text frames,
client-side masking, fragmented server frames reassembled. Not general purpose.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


class _Socket:
    def __init__(self, url: str):
        parts = urlparse(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parts.path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("DevTools closed the handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.decode(errors="replace"))

    def _exactly(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("DevTools closed the connection")
            data += chunk
        return data

    def send(self, text: str) -> None:
        payload = text.encode()
        size = len(payload)
        header = bytes([0x81])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + size.to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + size.to_bytes(8, "big")
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def receive(self) -> str:
        message = b""
        while True:
            first, second = self._exactly(2)
            size = second & 0x7F
            if size == 126:
                size = int.from_bytes(self._exactly(2), "big")
            elif size == 127:
                size = int.from_bytes(self._exactly(8), "big")
            payload = self._exactly(size)
            opcode = first & 0x0F
            if opcode == 0x8:
                raise ConnectionError("DevTools closed the socket")
            if opcode in (0x1, 0x0):
                message += payload
            if first & 0x80 and opcode in (0x1, 0x0):
                return message.decode()

    def close(self) -> None:
        self.sock.close()


class Page:
    def __init__(self, url: str):
        self.socket = _Socket(url)
        self.next_id = 0

    def call(self, method: str, **params):
        self.next_id += 1
        wanted = self.next_id
        self.socket.send(json.dumps({"id": wanted, "method": method, "params": params}))
        while True:
            reply = json.loads(self.socket.receive())
            if reply.get("id") == wanted:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})


# A Date whose "now" is offset to the asked-for instant and then runs normally.
# Explicit dates (new Date(2026, 9, 5)) are untouched, so formatting a stored
# instant still gives the real local time for it.
_FROZEN = """
(() => {
  const Real = Date;
  const shift = __AT__ - Real.now();
  class Frozen extends Real {
    constructor(...args) {
      if (args.length === 0) super(Real.now() + shift);
      else super(...args);
    }
    static now() {
      return Real.now() + shift;
    }
  }
  globalThis.Date = Frozen;
})();
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def screenshot_at(
    chrome: str,
    url: str,
    window: tuple[int, int],
    out: Path,
    *,
    at_ms: int | None = None,
    settle: float = 7.0,
) -> str | None:
    """Load `url` with the page's clock starting at `at_ms`, and save a PNG.

    Returns what the page itself said the time was, read back from it after the
    load -- so the caller can print evidence rather than trust the setting -- or
    None when no screenshot was taken.
    """
    port = _free_port()
    profile = Path(tempfile.mkdtemp(prefix="cdp-"))
    process = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-sandbox",
         "--hide-scrollbars", f"--user-data-dir={profile}",
         f"--remote-debugging-port={port}", f"--window-size={window[0]},{window[1]}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as found:
                    pages = [t for t in json.load(found) if t.get("type") == "page"]
                if pages:
                    target = pages[0]["webSocketDebuggerUrl"]
                    break
            except OSError:
                pass
            time.sleep(0.1)
        if target is None:
            return None
        page = Page(target)
        if at_ms is not None:
            # Into every frame, before the frame's own scripts: the probe and the
            # app inside its iframe both see the frozen instant.
            page.call(
                "Page.addScriptToEvaluateOnNewDocument",
                source=_FROZEN.replace("__AT__", str(int(at_ms))),
            )
        # The window size on the command line is not the viewport over DevTools;
        # without this the page laid out at 800x600 and the bottom was black.
        page.call(
            "Emulation.setDeviceMetricsOverride",
            width=window[0], height=window[1], deviceScaleFactor=1, mobile=False,
        )
        page.call("Page.enable")
        page.call("Page.navigate", url=url)
        time.sleep(settle)
        # Read back from the APP's frame, not the probe around it: that is the
        # clock the screen was drawn by.
        clock = page.call(
            "Runtime.evaluate",
            expression=(
                "(() => { const f = document.getElementById('f');"
                " const w = f ? f.contentWindow : window;"
                " return new w.Date().toString(); })()"
            ),
            returnByValue=True,
        ).get("result", {}).get("value", "")
        shot = page.call("Page.captureScreenshot", format="png")
        out.write_bytes(base64.b64decode(shot["data"]))
        page.socket.close()
        return str(clock)
    finally:
        process.kill()
        process.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)
