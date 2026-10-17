"""PDF zoom, like Chrome's own viewer, on a desktop and a phone (2026-10-17).

Driven by Playwright against the system Chrome and the real server the render
tests start, with the seeded 92-page PDF (420 x 560 pt, portrait). The anchor
maths is tested on its own first, in the browser -- this project has no Node,
and zoom.js is a browser module.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402
from test_web_render import _sign_in, chrome_only, find_chrome, served  # noqa: E402,F401

DESKTOP = {"viewport": {"width": 1920, "height": 1080}}
PHONE = {"viewport": {"width": 412, "height": 915}, "is_mobile": True, "has_touch": True,
         "device_scale_factor": 2.625}


@pytest.fixture(scope="module")
def browser():
    if find_chrome() is None:
        pytest.skip("no Chrome")
    with sync_playwright() as p:
        launched = p.chromium.launch(executable_path=find_chrome())
        yield launched
        launched.close()


def open_reader(browser, base, device, *, zoom=None, path="/read/d-render"):
    context = browser.new_context(**device)
    name, value = _sign_in(base).split("=", 1)
    context.add_cookies([{"name": name, "value": value, "url": base}])
    if zoom is not None:
        context.add_init_script(
            f"try {{ localStorage.setItem('lectern.zoom.d-render', {json.dumps(json.dumps(zoom))}); }} catch (e) {{}}"
        )
    page = context.new_page()
    page.goto(base + path)
    page.wait_for_selector(".page canvas", timeout=30000)
    page.wait_for_timeout(500)
    return context, page


SPOT = """([x, y]) => {
  const node = document.elementFromPoint(x, y);
  const sheet = node && node.closest('.page');
  if (!sheet) return null;
  const r = sheet.getBoundingClientRect();
  return { page: Number(sheet.dataset.page), fx: (x - r.left) / r.width, fy: (y - r.top) / r.height };
}"""

PERCENT = "() => { const m = document.querySelector('.zoom-menu'); return m.options[m.selectedIndex].textContent.trim(); }"


# ---------------------------------------------------------------- the maths, alone


@chrome_only
def test_the_anchor_maths(served, browser):
    base, _ = served
    context, page = open_reader(browser, base, DESKTOP)
    try:
        found = page.evaluate("""async () => {
          const z = await import('/reader/zoom.js');
          const sizes = Array.from({ length: 5 }, () => ({ width: 420, height: 560 }));
          const frame = { width: 1000, height: 800, padTop: 68, padSide: 8, padBottom: 64, gap: 12 };
          const small = z.layout(sizes, 1, frame);
          const big = z.layout(sizes, 2.5, frame);
          // A spot under a point stays under it through a zoom.
          const scroll = { left: 0, top: 900 };
          const point = { x: 600, y: 300 };
          const before = z.locate(small, scroll.left + point.x, scroll.top + point.y);
          const after = z.anchorScroll(small, big, scroll, point);
          const again = z.locate(big, after.left + point.x, after.top + point.y);
          // A pinch whose midpoint moves keeps the spot under the NEW midpoint.
          const moved = { x: 640, y: 260 };
          const pinched = z.anchorScroll(small, big, scroll, point, moved);
          const underMoved = z.locate(big, pinched.left + moved.x, pinched.top + moved.y);
          return {
            before, again, underMoved,
            pinch: [z.pinchPercent(1, 100, 250), z.pinchPercent(1, 100, 10), z.pinchPercent(3, 100, 400)],
            steps: [z.stepUp(1), z.stepDown(1), z.stepUp(4), z.stepDown(0.25), z.stepUp(1.37)],
            visible: z.visibleRange(small, 0, 800, 1),
            ratio: [z.renderRatio(400, 600, 2.625), z.renderRatio(5000, 6600, 2.625), z.renderRatio(9000, 100, 1)],
            centred: small.boxes[0].x === (1000 - 420) / 2,
            pans: big.width > 1000,
            auto1920: z.percentOf(z.scaleFor({ mode: 'auto' }, { width: 595, height: 842 },
              { width: 1920, height: 1080, padTop: 68, padSide: 8, padBottom: 64 })),
            auto412: z.percentOf(z.scaleFor({ mode: 'auto' }, { width: 595, height: 842 },
              { width: 412, height: 915, padTop: 68, padSide: 8, padBottom: 64 })),
          };
        }""")
    finally:
        context.close()
    assert found["before"]["page"] == found["again"]["page"]
    assert abs(found["before"]["fx"] - found["again"]["fx"]) < 1e-6
    assert abs(found["before"]["fy"] - found["again"]["fy"]) < 1e-6
    assert found["underMoved"]["page"] == found["before"]["page"]
    assert abs(found["underMoved"]["fy"] - found["before"]["fy"]) < 1e-6
    assert found["pinch"] == [2.5, 0.25, 4]
    assert found["steps"] == [1.1, 0.9, 4, 0.25, 1.5]
    assert found["visible"][0] == 1 and found["visible"][1] >= 2
    two_point_six, capped, side = found["ratio"]
    assert two_point_six == 2.625
    assert 5000 * 6600 * capped * capped <= 16_777_216 + 1 and capped < 2.625
    assert 9000 * side <= 8192 + 1e-6
    assert found["centred"] and found["pans"]
    # Auto caps a portrait page on a wide monitor, and fits the width on a phone.
    assert abs(found["auto1920"] - 1.25) < 1e-9
    assert abs(found["auto412"] - (412 - 16) / 595 / (96 / 72)) < 1e-9


# ---------------------------------------------------------------- in the reader


@chrome_only
@pytest.mark.parametrize("device", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_fit_page_shows_the_whole_page(served, browser, device):
    base, _ = served
    context, page = open_reader(browser, base, device)
    try:
        page.select_option(".zoom-menu", "page")
        page.wait_for_timeout(600)
        box = page.evaluate("""() => {
          const pages = document.querySelector('.pages').getBoundingClientRect();
          const bar = document.querySelector('.reader-bar').getBoundingClientRect();
          const sheet = [...document.querySelectorAll('.page')].find((p) => {
            const r = p.getBoundingClientRect(); return r.bottom > bar.bottom && r.top < pages.bottom;
          });
          const r = sheet.getBoundingClientRect();
          return { top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                   barBottom: bar.bottom, viewBottom: pages.bottom, viewRight: pages.right };
        }""")
        label = page.evaluate(PERCENT)
    finally:
        context.close()
    assert box["top"] >= box["barBottom"] - 1, box
    assert box["bottom"] <= box["viewBottom"] + 1, box
    assert box["left"] >= 0 and box["right"] <= box["viewRight"] + 1, box
    assert label.startswith("Fit page")


@chrome_only
def test_ctrl_wheel_zooms_anchored_at_the_cursor(served, browser):
    base, _ = served
    context, page = open_reader(browser, base, DESKTOP)
    try:
        # Zoomed past the window's width, so the page pans sideways and the spot
        # can be held across as well as down. (Below that the page stays
        # centred, as in Chrome's viewer: only the spot down the page can hold.)
        point = (1000, 600)
        before = page.evaluate(SPOT, list(point))
        start = page.evaluate(PERCENT)
        page.mouse.move(*point)
        page.keyboard.down("Control")
        page.mouse.wheel(0, -700)
        page.keyboard.up("Control")
        page.wait_for_timeout(800)
        after = page.evaluate(SPOT, list(point))
        reached = page.evaluate(PERCENT)
        app_zoom = page.evaluate("() => window.visualViewport ? window.visualViewport.scale : 1")
    finally:
        context.close()
    assert before and after
    assert reached == "400%" and reached != start
    assert after["page"] == before["page"]
    assert abs(after["fx"] - before["fx"]) < 0.02 and abs(after["fy"] - before["fy"]) < 0.02
    # The PDF zoomed, not the app.
    assert app_zoom == 1


@chrome_only
def test_ctrl_plus_and_ctrl_zero(served, browser):
    base, _ = served
    context, page = open_reader(browser, base, DESKTOP)
    try:
        page.mouse.move(960, 500)
        page.keyboard.press("Control+=")
        page.wait_for_timeout(500)
        up = page.evaluate(PERCENT)
        page.keyboard.press("Control+0")
        page.wait_for_timeout(500)
        back = page.evaluate(PERCENT)
    finally:
        context.close()
    assert up == "150%"  # Auto is 125% on 1920 wide; the next step up
    assert back.startswith("Auto")


@chrome_only
def test_a_read_position_restores_to_the_same_spot_at_another_zoom(served, browser):
    """Saved on the desktop at 100%, opened on the phone at 200%: the same page,
    the same fraction down it, at the line under the header."""
    base, _ = served
    context, page = open_reader(browser, base, DESKTOP, zoom={"mode": "custom", "percent": 1})
    try:
        page.evaluate("""() => {
          const pages = document.querySelector('.pages');
          const bar = document.querySelector('.reader-bar').getBoundingClientRect();
          const sheet = document.querySelector('.page[data-page="7"]');
          const r = sheet.getBoundingClientRect();
          pages.scrollTop += (r.top + 0.4 * r.height) - bar.bottom;
        }""")
        page.wait_for_timeout(2500)  # past the settle, and the outbox sends it
    finally:
        context.close()

    context, page = open_reader(browser, base, PHONE, zoom={"mode": "custom", "percent": 2})
    try:
        page.wait_for_timeout(800)
        spot = page.evaluate("""() => {
          const pages = document.querySelector('.pages').getBoundingClientRect();
          const bar = document.querySelector('.reader-bar').getBoundingClientRect();
          const y = bar.bottom + 1;
          const x = pages.left + pages.width / 2;
          const sheet = [...document.querySelectorAll('.page')].find((p) => {
            const r = p.getBoundingClientRect(); return r.top <= y && r.bottom >= y;
          });
          const r = sheet.getBoundingClientRect();
          return { page: Number(sheet.dataset.page), fy: (y - r.top) / r.height };
        }""")
    finally:
        context.close()
    assert spot["page"] == 7, spot
    assert abs(spot["fy"] - 0.4) < 0.03, spot


@chrome_only
def test_an_oversized_zoom_never_yields_a_blank_canvas(served, browser):
    """400% on a 2.625x phone wants a 3700 x 4900 css page: 125 million device
    pixels. Rendered at a lower resolution, never blank."""
    base, _ = served
    context, page = open_reader(browser, base, PHONE, zoom={"mode": "custom", "percent": 4})
    try:
        page.wait_for_timeout(1500)
        found = page.evaluate("""() => {
          const canvas = document.querySelector('.page canvas');
          const probe = document.createElement('canvas');
          probe.width = 64; probe.height = 64;
          const ctx = probe.getContext('2d');
          ctx.drawImage(canvas, 0, 0, 64, 64);
          const data = ctx.getImageData(0, 0, 64, 64).data;
          let opaque = 0, dark = 0;
          for (let i = 0; i < data.length; i += 4) {
            if (data[i + 3] > 0) opaque++;
            if (data[i] < 200 && data[i + 3] > 0) dark++;
          }
          return { w: canvas.width, h: canvas.height, ratio: Number(canvas.dataset.ratio), opaque, dark };
        }""")
    finally:
        context.close()
    assert found["w"] * found["h"] <= 16_777_216, found
    assert found["ratio"] < 2.625, found
    assert found["opaque"] == 64 * 64, found  # painted, not transparent
    assert found["dark"] > 0, found  # and the page's text is there


@chrome_only
def test_a_double_tap_zooms_in_and_back_to_auto(served, browser):
    base, _ = served
    context, page = open_reader(browser, base, PHONE)
    try:
        start = page.evaluate(PERCENT)
        page.touchscreen.tap(206, 500)
        page.touchscreen.tap(206, 500)
        page.wait_for_timeout(800)
        zoomed = page.evaluate(PERCENT)
        page.touchscreen.tap(206, 500)
        page.touchscreen.tap(206, 500)
        page.wait_for_timeout(800)
        back = page.evaluate(PERCENT)
        app_zoom = page.evaluate("() => window.visualViewport ? window.visualViewport.scale : 1")
    finally:
        context.close()
    assert start.startswith("Auto")
    assert not zoomed.startswith("Auto")
    assert back.startswith("Auto")
    assert app_zoom == 1
