"""The web client has no compiler, so these are its type checks.

Nothing here runs a browser. Every assertion is about a failure that would
otherwise present as a blank page or a silently wrong screen, which is the class
of defect this project ranks worst -- and the class a no-build client is most
exposed to, because a typo in an import path costs nothing until it is loaded.

Six things are pinned:

  * pinch-zoom is not disabled -- DESIGN.md section 6 names it the single most
    common way a reader becomes unusable on a phone, and it is one attribute away
  * every module a file imports exists on disk
  * every bare specifier resolves through the import map
  * nothing is fetched from a CDN, because offline-first cannot depend on one
  * `contract.js` is current, so a renamed field cannot sit undetected
  * VENDOR.md's table matches what is actually vendored
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from api_fixtures import make_config, seed

from agent.api import contract
from agent.config import REPO_ROOT

WEB = REPO_ROOT / "web"


def web_modules() -> list[Path]:
    """Our own JS, not the vendored bundles."""
    return sorted(
        path
        for path in WEB.rglob("*.js")
        if "vendor" not in path.parts and path.name != "contract.js"
    )


def test_there_is_a_client_to_check():
    """Control: every assertion below is vacuous if this directory is empty."""
    assert (WEB / "index.html").is_file()
    assert len(web_modules()) >= 3, [p.name for p in web_modules()]


# ---------------------------------------------------------------------------
# pinch-zoom
# ---------------------------------------------------------------------------


def test_pinch_zoom_is_not_disabled():
    """DESIGN.md section 6: a 92-page deck has diagrams that only work zoomed.

    Checked as a rule rather than trusted, because `user-scalable=no` is a common
    copy-paste and the symptom -- a reader that cannot be zoomed -- is one I would
    hit at 23:00 rather than while writing it.
    """
    head = (WEB / "index.html").read_text(encoding="utf-8")
    viewport = re.search(r'<meta name="viewport" content="([^"]*)"', head)
    assert viewport, "no viewport meta tag at all"

    content = viewport.group(1)
    assert "user-scalable" not in content, content
    assert "maximum-scale" not in content, content
    assert "width=device-width" in content


def test_no_stylesheet_blocks_touch_zoom():
    """`touch-action: none` on a scroll container disables pinch just as well.

    Comments are stripped first. The reader's stylesheet says in words that it is
    NOT setting this, and a textual scan flagged the explanation -- which would
    have left a real rule and a note about it indistinguishable, and eventually
    got the note deleted to make the test pass.
    """
    for sheet in WEB.rglob("*.css"):
        body = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
        assert "touch-action: none" not in body, sheet.name
        assert "touch-action:none" not in body, sheet.name


# ---------------------------------------------------------------------------
# imports resolve
# ---------------------------------------------------------------------------

IMPORT = re.compile(r'(?:^|\s)(?:import|export)[^;\n]*?from\s+["\']([^"\']+)["\']', re.M)
BARE = re.compile(r"^[a-z@][a-z0-9@/_.-]*$", re.I)


def import_map() -> dict[str, str]:
    head = (WEB / "index.html").read_text(encoding="utf-8")
    block = re.search(r'<script type="importmap">(.*?)</script>', head, re.S)
    assert block, "no import map, so no bare specifier can resolve"
    return json.loads(block.group(1))["imports"]


@pytest.mark.parametrize("module", web_modules(), ids=lambda p: p.name)
def test_every_import_resolves(module):
    """A typo in an import path is a blank page, and nothing else reports it."""
    mapping = import_map()
    for specifier in IMPORT.findall(module.read_text(encoding="utf-8")):
        if specifier.startswith("/"):
            target = WEB / specifier.lstrip("/")
            assert target.is_file(), f"{module.name} imports missing {specifier}"
        elif specifier.startswith("."):
            target = (module.parent / specifier).resolve()
            assert target.is_file(), f"{module.name} imports missing {specifier}"
        else:
            assert specifier in mapping, (
                f"{module.name} imports bare {specifier!r}, which is not in the "
                f"import map -- the browser cannot resolve it and the app will "
                f"fail at parse time"
            )


def test_the_import_map_points_at_files_that_exist():
    for specifier, target in import_map().items():
        assert (WEB / target.lstrip("/")).is_file(), f"{specifier} -> {target}"


def test_the_vendored_hooks_bare_import_is_covered():
    """The specific one that fails at parse time if the map is ever trimmed.

    `preact-hooks.module.js` imports "preact". Without the map entry nothing
    renders and the console says only "Failed to resolve module specifier".
    """
    hooks = (WEB / "vendor" / "preact-hooks.module.js").read_text(encoding="utf-8")
    assert 'from"preact"' in hooks or 'from "preact"' in hooks
    assert "preact" in import_map()


def test_index_references_only_files_that_exist():
    head = (WEB / "index.html").read_text(encoding="utf-8")
    for reference in re.findall(r'(?:href|src)="(/[^"]+)"', head):
        assert (WEB / reference.lstrip("/")).is_file(), reference


# ---------------------------------------------------------------------------
# no CDN
# ---------------------------------------------------------------------------


def test_nothing_is_loaded_from_a_third_party_origin():
    """Offline-first cannot depend on unpkg being reachable.

    Also invariant 5's spirit: the move to the box is a directory copy, and a
    remote import is a dependency that does not travel in it.
    """
    offenders = []
    for path in [*web_modules(), WEB / "index.html", *WEB.rglob("*.css")]:
        body = path.read_text(encoding="utf-8")
        for host in ("unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com",
                     "esm.sh", "//fonts.googleapis.com"):
            if host in body:
                offenders.append(f"{path.name}: {host}")
    assert not offenders, offenders


def test_the_font_is_self_hosted_precached_and_never_waited_for():
    """DESIGN.md section 8: one face, from web/fonts/, offline, swapped in.

    Every @font-face source is a local path that exists and is in the service
    worker's precache -- so the app still renders in its own face with the radio
    off -- and every one swaps, so nothing waits for a font at 23:00 on mobile
    data. The system stack stays behind it as the fallback.
    """
    from agent.api import contract as contract_mod

    css = (WEB / "app.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{(.*?)\}", css, re.S)
    assert faces, "no @font-face at all"
    precached = set(contract_mod.shell_files(WEB))
    for face in faces:
        assert "font-display: swap" in face, face
        for url in re.findall(r'url\("([^"]+)"\)', face):
            assert url.startswith("/fonts/"), url
            assert (WEB / url.lstrip("/")).is_file(), url
            assert url in precached, f"{url} is not precached"
    assert "-apple-system" in css, "the system stack is the fallback"
    assert (WEB / "fonts" / "OFL.txt").is_file(), "the licence travels with the font"


def test_the_body_floor_is_seventeen_pixels():
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert re.search(r"font-size:\s*17px", css), "no 17px floor in app.css"


# ---------------------------------------------------------------------------
# the generated contract
# ---------------------------------------------------------------------------


def test_contract_js_is_current(tmp_path, monkeypatch):
    """A renamed field must not sit undetected behind a stale generated file.

    Regenerated into a temp file and compared, so this fails when the schema has
    moved and `agent webcontract` has not been run.
    """
    from api_fixtures import build

    config = make_config(tmp_path)
    seed(config)
    fresh = contract.build(build(config, monkeypatch))

    on_disk = (WEB / "contract.js").read_text(encoding="utf-8")
    assert contract._without_stamp(on_disk) == contract._without_stamp(fresh), (
        "web/contract.js is out of date -- run `agent webcontract`"
    )


def test_the_contract_names_no_mangled_models():
    """`agent__api__routes__meta__SessionOut` is what a name collision looks like.

    FastAPI mangles two models that share a name, and the result is unreadable in
    the client. Pinned so the next collision is a failing test rather than a
    confusing identifier.
    """
    body = (WEB / "contract.js").read_text(encoding="utf-8")
    mangled = re.findall(r'"[A-Za-z_]+__[A-Za-z_]+"', body)
    assert not mangled, mangled


def test_the_contract_covers_the_routes_the_client_actually_calls():
    """Every path in a fetch call is a key in ROUTES, after templating.

    Otherwise `expect()` silently checks nothing, which would make the contract a
    comfort rather than a check.
    """
    body = (WEB / "contract.js").read_text(encoding="utf-8")
    routes = set(re.findall(r'"((?:GET|POST|PUT|PATCH|DELETE) /api/[^"]*)"', body))
    assert "GET /api/now" in routes
    assert "PUT /api/documents/{drive_id}/position" in routes
    # All four refusals, which only appear once the multi-method route was split.
    for method in ("PUT", "POST", "PATCH", "DELETE"):
        assert f"{method} /api/timetable/file" in routes


# ---------------------------------------------------------------------------
# VENDOR.md tells the truth
# ---------------------------------------------------------------------------


def test_vendor_md_matches_what_is_on_disk():
    """A vendored dependency with no lockfile needs its record to be checkable.

    Byte counts rather than versions, because the version is only in the URL used
    to fetch it -- but a refresh that changes the bytes and not the table is
    exactly the drift this catches.
    """
    table = (WEB / "VENDOR.md").read_text(encoding="utf-8")
    listed = dict(
        (name, int(size.replace(",", "")))
        for name, size in re.findall(
            r"\|\s*`([^`]+)`\s*\|[^|]*\|\s*([\d,]+)\s*\|", table
        )
    )
    assert listed, "no table rows parsed out of VENDOR.md"

    on_disk = {
        path.name: path.stat().st_size for path in (WEB / "vendor").glob("*.js")
    }
    on_disk.update(
        {path.name: path.stat().st_size for path in (WEB / "vendor").glob("*.mjs")}
    )

    assert set(listed) == set(on_disk), (
        f"VENDOR.md lists {sorted(listed)}, disk has {sorted(on_disk)}"
    )
    for name, size in listed.items():
        assert on_disk[name] == size, (
            f"{name} is {on_disk[name]} bytes, VENDOR.md says {size} -- "
            f"update the table"
        )


def test_the_manifest_is_valid_json_and_installable():
    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["icons"], "an installable PWA needs at least one icon"
    for icon in manifest["icons"]:
        assert (WEB / icon["src"].lstrip("/")).is_file(), icon["src"]


def test_no_badge_api_and_no_push_anywhere():
    """DESIGN.md section 7: no badge that only counts up, and no invented push.

    Web push is out of scope entirely -- Telegram keeps deadline alerts by the
    settled decision -- so the absence is checkable rather than a matter of care.
    """
    for path in web_modules():
        body = path.read_text(encoding="utf-8")
        assert "setAppBadge" not in body, path.name
        assert "showNotification" not in body, path.name
        assert "pushManager" not in body, path.name


# ---------------------------------------------------------------------------
# the service worker
# ---------------------------------------------------------------------------


def test_every_precached_path_exists():
    """A typo here is an app that is silently never available offline.

    `sw.js` adds shell files individually and swallows each failure, which is
    deliberate -- `cache.addAll` fails the whole batch on one bad path and would
    leave NOTHING precached. The cost of that choice is that a wrong path is a
    console warning nobody reads, so it is checked here instead.
    """
    body = (WEB / "sw.js").read_text(encoding="utf-8")
    listed = re.search(r"const SHELL_FILES = \[(.*?)\];", body, re.S)
    assert listed, "no SHELL_FILES array in sw.js"

    paths = re.findall(r'"([^"]+)"', listed.group(1))
    assert len(paths) >= 10, paths

    missing = [
        path
        for path in paths
        # "/" is the app, served as index.html rather than a file of its own.
        if path != "/" and not (WEB / path.lstrip("/")).is_file()
    ]
    assert not missing, f"sw.js precaches paths that do not exist: {missing}"


def test_the_pdf_worker_is_not_precached():
    """1.4 MB that only the reader needs.

    Precaching it would make every first load pay for a screen most opens never
    reach, on the mobile connection the whole design is about.
    """
    body = (WEB / "sw.js").read_text(encoding="utf-8")
    listed = re.search(r"const SHELL_FILES = \[(.*?)\];", body, re.S).group(1)
    assert "pdf.worker" not in listed
    assert "pdf.min.mjs" not in listed


def test_the_worker_sits_at_the_root():
    """A service worker only controls its own directory and below.

    At /web/sw.js it would control nothing; it has to be /sw.js to see the app's
    requests at all, and that is a property of where the file IS.
    """
    assert (WEB / "sw.js").is_file()
    assert not list(WEB.glob("*/sw.js")), "a second worker below the root"


def test_documents_are_never_cached_as_a_side_effect():
    """A 40 MB deck must not be kept because I scrolled past it.

    The document handler reads from the cache and falls back to the network; it
    must never write. Only the explicit `keep` message stores one.
    """
    body = (WEB / "sw.js").read_text(encoding="utf-8")
    handler = body[body.index("async function document_("):]
    handler = handler[: handler.index("\n}\n")]
    assert ".put(" not in handler, "the document handler writes to a cache"
    assert "cache.add" not in handler


def test_only_positions_are_queued_offline():
    """Everything else fails visibly.

    A task that looks recorded and is not is the silent-success failure this
    project ranks worst, so the queue is deliberately narrow.
    """
    body = (WEB / "queue.js").read_text(encoding="utf-8")
    assert "position" in body.lower()
    for forbidden in ("/api/tasks", "/api/projects", "/api/uploads", "/api/adjustments"):
        assert forbidden not in body, forbidden


# ---------------------------------------------------------------------------
# installability
# ---------------------------------------------------------------------------


def test_the_manifest_has_what_android_needs_to_install():
    """Bubblewrap wraps THIS at 5d, and it reads the manifest.

    An SVG alone is not enough: Android's installer wants raster icons at 192
    and 512, and without them the install prompt simply never appears -- which
    presents as "it would not install" with nothing said about why.
    """
    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    sizes = {icon.get("sizes") for icon in manifest["icons"]}
    assert "192x192" in sizes
    assert "512x512" in sizes

    types = {icon.get("type") for icon in manifest["icons"]}
    assert "image/png" in types, "an SVG alone will not install on Android"


def test_a_maskable_icon_exists():
    """Without one, a launcher crops the glyph into whatever shape it likes."""
    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    purposes = {icon.get("purpose") for icon in manifest["icons"]}
    assert "maskable" in purposes


def test_an_install_opens_on_the_default_screen():
    """`start_url` is "/" and that is the whole design.

    An installed app that opened on a menu would have lost section 2 entirely:
    the front door is the next single action, and everything else is one tap
    behind it.
    """
    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"


def test_every_icon_the_manifest_names_is_precached():
    """An installed app whose icon needs a round trip shows a blank square."""
    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    shell = (WEB / "sw.js").read_text(encoding="utf-8")
    listed = re.search(r"const SHELL_FILES = \[(.*?)\];", shell, re.S).group(1)
    for icon in manifest["icons"]:
        assert icon["src"] in listed, icon["src"]


def test_the_worker_version_is_the_shell_hash():
    """A changed shell file must change the worker's cache name.

    The shell is cache-first, so an installed PWA runs the cached app.js until
    VERSION moves. A stale VERSION is a deployed fix that the phone never gets,
    presenting as the fix not working -- so it is computed, not remembered.
    """
    from agent.api import contract

    body = (WEB / "sw.js").read_text(encoding="utf-8")
    stamped = re.search(r'^const VERSION = "([^"]*)";', body, re.M).group(1)
    assert stamped == contract.shell_version(WEB), (
        "web/sw.js VERSION is stale for the files it precaches -- run "
        "`agent webcontract`"
    )


def test_precaching_bypasses_the_http_cache():
    """Or a new worker can store the OLD files under the new version's name."""
    body = (WEB / "sw.js").read_text(encoding="utf-8")
    assert 'cache: "reload"' in body


# ---------------------------------------------------------------------------
# the visual system: contrast, recomputed from the stylesheet itself
# ---------------------------------------------------------------------------
#
# DESIGN.md section 8 lists a ratio beside every colour token. Those numbers are
# only true while the hex values are the ones they were measured on, so this
# reads the tokens out of app.css and measures again -- a token edited for taste
# cannot quietly fall below WCAG AA.


THEMES = ("light", "dark")


def _tokens(theme: str) -> dict[str, str]:
    """Every colour token in :root, resolved for one theme.

    Tokens are `light-dark(#light, #dark)` pairs (DESIGN.md section 8), so the
    light value is the first and the dark the second. A plain hex counts for
    both.
    """
    css = (WEB / "app.css").read_text(encoding="utf-8")
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1)
    found = {}
    for name, value in re.findall(r"--([a-z0-9-]+):\s*([^;]+);", root):
        pair = re.fullmatch(r"light-dark\((#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)", value.strip())
        if pair:
            found[name] = pair.group(1 if theme == "light" else 2)
        elif re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
            found[name] = value.strip()
    return found


def _luminance(hex_colour):
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


SURFACES = ("ground", "card", "raised")


def test_both_themes_define_the_same_tokens():
    """A token with a light value and no dark one is a theme half designed."""
    light, dark = _tokens("light"), _tokens("dark")
    assert light.keys() == dark.keys()
    assert {"ground", "card", "ink", "accent", "passed", "s0-fg", "s11-fill"} <= light.keys()
    # And they differ: the dark theme is not the light one with a new name.
    assert light["ground"] != dark["ground"] and light["ink"] != dark["ink"]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("ink", ["ink", "ink-2", "ink-3", "accent", "passed"])
@pytest.mark.parametrize("surface", SURFACES)
def test_text_tokens_clear_aa_on_every_surface(theme, ink, surface):
    tokens = _tokens(theme)
    ratio = _contrast(tokens[ink], tokens[surface])
    assert ratio >= 4.5, f"{theme}: --{ink} on --{surface} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("surface", ("ground", "card"))
def test_control_edges_clear_three_to_one(theme, surface):
    """WCAG 1.4.11: a control's boundary has to be findable."""
    tokens = _tokens(theme)
    ratio = _contrast(tokens["edge"], tokens[surface])
    assert ratio >= 3, f"{theme}: --edge on --{surface} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", THEMES)
def test_the_primary_button_label_is_readable(theme):
    tokens = _tokens(theme)
    assert _contrast(tokens["accent-ink"], tokens["accent"]) >= 4.5
    assert _contrast(tokens["accent-ink"], tokens["accent-hover"]) >= 4.5


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("index", range(12))
def test_every_subject_colour_is_readable_in_both_themes(theme, index):
    """Each hue's three values, used the way section 8 says, all clear AA.

    fg is text on its own tint and on a plain card; fill carries ink (a chip, a
    timetable block); tint carries ink (a tinted card's body text).
    """
    tokens = _tokens(theme)
    fg, tint, fill = (tokens[f"s{index}-{part}"] for part in ("fg", "tint", "fill"))
    for name, a, b in (
        ("fg on tint", fg, tint),
        ("fg on card", fg, tokens["card"]),
        ("ink on fill", tokens["ink"], fill),
        ("ink on tint", tokens["ink"], tint),
        ("ink-2 on tint", tokens["ink-2"], tint),
    ):
        ratio = _contrast(a, b)
        assert ratio >= 4.5, f"{theme} s{index}: {name} is {ratio:.2f}:1"


@pytest.mark.parametrize("index", range(12))
def test_no_subject_colour_is_red_or_orange(index):
    """A subject that read as the alarm would read as a subject in trouble."""
    import colorsys

    for theme in THEMES:
        colour = _tokens(theme)[f"s{index}-fg"]
        r, g, b = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
        hue = colorsys.rgb_to_hls(r, g, b)[0] * 360
        assert not (hue < 38 or hue > 345), f"{theme} s{index} {colour} is hue {hue:.0f}"


def test_the_client_reads_the_hour_in_exactly_one_place():
    """The 23:00 state must look like every other state.

    DESIGN.md section 4, as amended: the greeting on Today is the only thing
    allowed to know the hour, and it changes a word. Any other module reading
    the clock's hour could start styling itself by it -- a night tint, a "late"
    warning -- which would be the app deciding I am behind because of the time.
    Dates are read (tomorrow is a wall-clock fact); the hour is read once.
    """
    readers = []
    for module in WEB.rglob("*.js"):
        if "vendor" in module.parts:
            continue
        body = re.sub(r"//[^\n]*|/\*.*?\*/", "", module.read_text(encoding="utf-8"), flags=re.S)
        if "getHours" in body or "getUTCHours" in body:
            readers.append(module.name)
    assert readers == ["greeting.js"], readers


def test_the_greeting_only_ever_says_three_things():
    """Never "late", never "still up": the time of day, and nothing else."""
    body = (WEB / "greeting.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.S)
    said = set(re.findall(r'return "([^"]+)"', code))
    assert said == {"Good morning", "Good afternoon", "Good evening"}, said


def test_every_module_stylesheet_and_font_is_precached():
    """Offline means the whole shell, not most of it.

    A module missing from SHELL_FILES works online and fails only with the
    radio off -- found once already, for greeting.js, by reading the list.
    """
    from agent.api import contract as contract_mod

    precached = set(contract_mod.shell_files(WEB))
    shipped = [
        path
        for pattern in ("*.js", "screens/*.js", "reader/*.js", "styles/*.css", "fonts/*.woff2", "*.css")
        for path in WEB.glob(pattern)
        if path.name not in ("sw.js",) and not path.name.startswith("__")
    ]
    assert shipped, "found nothing to check"
    missing = sorted(
        "/" + path.relative_to(WEB).as_posix()
        for path in shipped
        if "/" + path.relative_to(WEB).as_posix() not in precached
    )
    assert not missing, f"not precached: {missing}"


def test_the_app_is_called_lectern_wherever_a_person_sees_a_name():
    """DESIGN.md section 8: Lectern on the screen; the repo keeps its own name."""
    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"] == "Lectern" and manifest["short_name"] == "Lectern"
    head = (WEB / "index.html").read_text(encoding="utf-8")
    assert "<title>Lectern</title>" in head
    brand = (WEB / "ui.js").read_text(encoding="utf-8")
    assert '<span class="brand-name">Lectern</span>' in brand
    # No screen still says the old names. Comments may tell the history.
    for module in list(WEB.glob("*.js")) + list(WEB.glob("screens/*.js")):
        code = re.sub(r"//[^\n]*|/\*.*?\*/", "", module.read_text(encoding="utf-8"), flags=re.S)
        for old in ("classroom-agent", ">Margin<", '"Margin'):
            assert old not in code, f"{module.name} still says {old}"


def test_no_stylesheet_ever_inverts_the_page():
    """DESIGN.md section 6: the PDF renders exactly as the professor made it.

    In either theme. An inverted slide deck destroys diagrams, code screenshots
    and photographed boards -- the content the vision OCR exists to preserve.
    Comments are stripped, because the reader's stylesheet says this in words.
    """
    for sheet in WEB.rglob("*.css"):
        body = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
        for forbidden in ("invert(", "mix-blend-mode: difference", "mix-blend-mode:difference"):
            assert forbidden not in body, f"{sheet.name}: {forbidden}"
