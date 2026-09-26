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


def test_fonts_are_a_system_stack():
    """DESIGN.md section 6 asks for a system stack at a real reading size.

    A web font is a round-trip before text paints, which is the opposite of what
    a reader opened at 23:00 on mobile data needs.
    """
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert "@font-face" not in css
    assert "-apple-system" in css


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
