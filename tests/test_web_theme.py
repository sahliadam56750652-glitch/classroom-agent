"""Each theme paints what it says, even with Chrome's "dark theme for sites" on.

Found on the S25 (2026-10-16): Light chosen while Android was in dark mode
showed a dark page -- lighter than our own Dark. That is Chrome's auto-dark
(WebContentsForceDark) darkening a page that declared it was light-only, not
our Dark tokens. Auto-dark recolours at PAINT time and leaves computed styles
alone, so this checks both: the body's computed background, which proves the
right tokens are in use, and the pixels actually painted, which proves nothing
darkened them. And a PDF page in the reader is never darkened -- DESIGN.md
section 6.

Driven by Playwright against the system Chrome (the render tests' own).

**Which browser for which system scheme, and why.** "System dark" runs with
force-dark ON (WebContentsForceDark and forceDarkModeEnabled): that is the S25
-- Android dark, so Chrome dark, so its dark theme for sites active. "System
light" runs with it OFF, because the other combination does not exist on a
phone: Chrome's dark theme for sites is on only while Chrome itself is dark, and
then every page is told prefers-color-scheme: dark. Measured 2026-10-16, the
flags plus an emulated "light" make even an unthemed light page compute dark
tokens -- an artefact of forcing two contradicting switches, not the app.

Measured before the fix, in the real combination (system dark, force-dark on):
Light computed our DARK ground, rgb(23, 21, 18), because the page said only
"light" (and a leftover meta said "dark"), so Chrome resolved light-dark() to
its dark half; Bright computed its own ground and was PAINTED rgb(31, 34, 36),
recoloured at paint time; and the theme-color followed the phone. After: every
theme declares exactly what it is -- "only light", "only dark", or "light dark"
for System -- and paints its own ground.
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("playwright")

from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from test_web_render import _sign_in, chrome_only, find_chrome, served  # noqa: E402,F401

GROUND = {"light": "#f3efe7", "dark": "#171512", "bright": "#f4f7fb"}
FORCE_DARK = ["--enable-features=WebContentsForceDark", "--blink-settings=forceDarkModeEnabled=true"]


def _rgb(hex_colour):
    return tuple(int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))


def _parse_css_rgb(text):
    inside = text[text.index("(") + 1 : text.index(")")]
    return tuple(int(float(part)) for part in inside.replace(",", " ").split()[:3])


def _close(a, b, tolerance=6):
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


@pytest.fixture(scope="module")
def browsers():
    """{"light": plain Chrome, "dark": Chrome with its dark theme for sites on}."""
    if find_chrome() is None:
        pytest.skip("no Chrome")
    with sync_playwright() as p:
        plain = p.chromium.launch(executable_path=find_chrome())
        forced = p.chromium.launch(executable_path=find_chrome(), args=FORCE_DARK)
        yield {"light": plain, "dark": forced}
        plain.close()
        forced.close()


def _page(browser, base, cookie, theme, scheme):
    context = browser.new_context(color_scheme=scheme, viewport={"width": 390, "height": 844})
    name, value = cookie.split("=", 1)
    context.add_cookies([{"name": name, "value": value, "url": base}])
    stored = "null" if theme == "system" else f'"{theme}"'
    context.add_init_script(
        f"try {{ const t = {stored}; if (t) localStorage.setItem('lectern.theme', t); "
        f"else localStorage.removeItem('lectern.theme'); }} catch (e) {{}}"
    )
    return context, context.new_page()


def _painted(page, x, y):
    shot = page.screenshot(clip={"x": x, "y": y, "width": 2, "height": 2})
    return Image.open(io.BytesIO(shot)).convert("RGB").getpixel((0, 0))


@chrome_only
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("theme", ["light", "bright", "dark", "system"])
def test_the_page_is_the_theme_it_says_even_under_force_dark(served, browsers, theme, scheme):
    base, _ = served
    expected = GROUND[scheme if theme == "system" else theme]
    context, page = _page(browsers[scheme], base, _sign_in(base), theme, scheme)
    try:
        page.goto(f"{base}/status")
        page.wait_for_selector("text=Status", timeout=15000)
        page.wait_for_timeout(400)
        computed = _parse_css_rgb(page.evaluate("getComputedStyle(document.body).backgroundColor"))
        # A strip of bare ground: the 16px gutter beside the content.
        painted = _painted(page, 4, 600)
        declared = page.evaluate(
            "[document.querySelector('meta[name=color-scheme]')?.content,"
            " getComputedStyle(document.documentElement).colorScheme]"
        )
        theme_colours = page.evaluate(
            "[...document.querySelectorAll('meta[name=theme-color]')].map(m => [m.content, m.media])"
        )
    finally:
        context.close()
    assert _close(computed, _rgb(expected)), (theme, scheme, "computed", computed, expected)
    assert _close(painted, _rgb(expected)), (theme, scheme, "painted", painted, expected, declared)
    # One theme-color, no media query, set to the chosen theme's ground.
    assert theme_colours == [[expected, ""]] or theme_colours == [[expected, None]], theme_colours


@chrome_only
@pytest.mark.parametrize("theme", ["light", "bright", "dark", "system"])
def test_a_pdf_page_is_never_darkened(served, browsers, theme):
    """Force-dark on, the phone in dark mode: the page stays the page."""
    base, _ = served
    context, page = _page(browsers["dark"], base, _sign_in(base), theme, "dark")
    try:
        page.goto(f"{base}/read/d-render")
        canvas = page.wait_for_selector(".page canvas:not(.blank)", timeout=20000)
        page.wait_for_timeout(800)
        box = canvas.bounding_box()
        # Near the bottom-right of page 1: blank white paper, below the text.
        pixel = _painted(page, box["x"] + box["width"] - 12, box["y"] + box["height"] - 12)
    finally:
        context.close()
    assert min(pixel) >= 240, (theme, "the PDF page was darkened", pixel)
