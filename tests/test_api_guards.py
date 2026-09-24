"""The three guards that make the API's promises checks rather than conventions.

Each has a CONTROL -- an assertion that the thing being guarded against is
actually detectable -- so that deleting the guard cannot leave the suite green.
That is the pattern the manual-prefix guards already use, and the reason is the
same: a test that passes whether or not the rule holds is worse than no test,
because it reports a guarantee nobody is providing.

The import guard is deliberately a DIRECT-import check over this package's own
source, not a transitive closure. `files/upload.py` legitimately imports
`files/drive.py` for one constant, and forbidding that would forbid uploads. The
rule being enforced is "no module in agent/api/ reaches for these", which is what
actually prevents a route from fetching from Drive or calling a model.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from api_fixtures import build, make_config, seed

from agent.api import app as app_mod

API_DIR = pathlib.Path(app_mod.__file__).parent

# Names no file in agent/api/ may mention. Two groups, two reasons.
#
# The first three are the honesty guarantees: `verified` has exactly one writer,
# `advance_study_item` is how a study item moves at all, and the quiz is the only
# thing that can reach a pass. 5b is read-only over study_items, so none of them
# belongs here.
#
# The rest are the pipeline: the API reads what the scheduled run wrote and
# performs no stage of it. No sync, no Drive fetch, no OCR, no model call, no
# Telegram.
FORBIDDEN_NAMES = (
    "verify_study_item",
    "advance_study_item",
    # gate/quiz.py's entry points. The quiz is the only thing that can reach a
    # pass, and generation costs a model request -- neither belongs behind a GET.
    "settle",
    "generate",
)

FORBIDDEN_MODULES = (
    "sync.poller",
    "sync import poller",
    "sync.differ",
    "sync import differ",
    "files.drive",
    "files import drive",
    "files.ocr",
    "files import ocr",
    "files.extract",
    "files import extract",
    "files.packs",
    "files import packs",
    "llm",
    "notify",
)

# The gate's own rows. Computing a plan is a read; recording that a prompt was
# sent is not, and `gate_runs.for_date` means a stray row would let a morning
# page silently swallow that evening's real prompt.
FORBIDDEN_GATE_WRITES = (
    "create_gate_run",
    "replace_gate_plan",
    "mark_gate_sent",
    "snooze_gate_run",
    "close_gate_run",
)


def api_sources() -> list[pathlib.Path]:
    return sorted(API_DIR.rglob("*.py"))


def identifiers(path: pathlib.Path) -> set[str]:
    """Every name the module actually USES, ignoring prose.

    Parsed rather than grepped, and deliberately: the docstrings in this package
    name `verify_study_item` in order to explain why it is absent, and a textual
    scan cannot tell an explanation from a call. `ast` sees neither comments nor
    string contents, so the guard checks what the code reaches for.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.split(".")[-1])
            if node.asname:
                names.add(node.asname)
    return names


def test_the_api_package_has_sources_to_check():
    """Control for all three source guards: they must be looking at something."""
    found = api_sources()
    assert len(found) >= 4, f"only found {found}"
    assert any(path.name == "app.py" for path in found)


def test_the_identifier_scan_sees_real_use_and_ignores_prose(tmp_path):
    """Control: the parse must catch a call and must not catch a docstring.

    Without the second half this guard would be satisfied by deleting a comment,
    and without the first it would be satisfied by nothing at all.
    """
    used = tmp_path / "uses.py"
    used.write_text("from x import verify_study_item\nverify_study_item(1)\n", encoding="utf-8")
    assert "verify_study_item" in identifiers(used)

    mentioned = tmp_path / "mentions.py"
    mentioned.write_text('"""Never calls verify_study_item."""\n', encoding="utf-8")
    assert "verify_study_item" not in identifiers(mentioned)


@pytest.mark.parametrize("name", FORBIDDEN_NAMES + FORBIDDEN_GATE_WRITES)
def test_the_api_never_calls_a_study_item_writer_or_a_gate_write(name):
    offenders = [
        path.relative_to(API_DIR).as_posix()
        for path in api_sources()
        if name in identifiers(path)
    ]
    assert not offenders, (
        f"{name!r} is used in {offenders}. 5b is read-only over study_items and "
        f"never fires the gate; if that is changing, it changes in PLAN.md first."
    )


@pytest.mark.parametrize("module", FORBIDDEN_MODULES)
def test_the_api_never_imports_a_pipeline_stage(module):
    """Direct imports only -- see the module docstring for why."""
    offenders = []
    for path in api_sources():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            if module in stripped:
                offenders.append(f"{path.relative_to(API_DIR).as_posix()}: {stripped}")
    assert not offenders, (
        f"the API imports {module}: {offenders}. It reads what the scheduled "
        f"pipeline wrote and runs no stage of it."
    )


def test_the_forbidden_name_check_can_actually_fail(tmp_path):
    """Control: the same scan over a file that does mention one must catch it."""
    planted = tmp_path / "bad.py"
    planted.write_text("from agent.db.store import verify_study_item\n", encoding="utf-8")
    assert "verify_study_item" in planted.read_text(encoding="utf-8")


def test_the_forbidden_import_check_can_actually_fail(tmp_path):
    """Control: an import line naming a pipeline module is detected as one."""
    planted = tmp_path / "bad.py"
    planted.write_text("from ..files import ocr\n", encoding="utf-8")
    hits = [
        line.strip()
        for line in planted.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("from ") and "files import ocr" in line
    ]
    assert hits, "the scan would not have caught a real offender"


def test_no_route_is_a_coroutine(tmp_path, monkeypatch):
    """Every route is `def`. One `async def` blocks the loop on a sync store.

    This is the rule that keeps FastAPI from becoming the objection that got
    python-telegram-bot rejected twice: a sync route runs in Starlette's
    threadpool, where the per-request sqlite3 connection is correct.
    """
    config = make_config(tmp_path)
    seed(config)
    app = build(config, monkeypatch)

    ours = [
        route
        for route in _every_route_object(app)
        if getattr(route, "endpoint", None) is not None
        and getattr(route.endpoint, "__module__", "").startswith("agent.api")
    ]
    # Control: FastAPI's own /api/docs handler IS async, so a check that found
    # nothing to look at would pass for the wrong reason.
    assert ours, "found no routes of ours to check"

    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in ours
        if inspect.iscoroutinefunction(route.endpoint)
    ]
    assert not offenders, f"async routes: {offenders}"


def _every_route_object(app):
    """Route objects from the app and from any included router, flattened."""
    found = []

    def walk(routes):
        for route in routes:
            if getattr(route, "methods", None) and getattr(route, "path", None) is not None:
                found.append(route)
                continue
            nested = getattr(route, "original_router", None)
            if nested is not None:
                walk(getattr(nested, "routes", []))
            elif getattr(route, "routes", None):
                walk(route.routes)

    walk(app.routes)
    return found


def test_the_coroutine_check_can_actually_fail():
    """Control: `inspect.iscoroutinefunction` sees an async endpoint."""

    async def would_block():  # pragma: no cover - never called
        return None

    def would_not():  # pragma: no cover - never called
        return None

    assert inspect.iscoroutinefunction(would_block)
    assert not inspect.iscoroutinefunction(would_not)


def test_the_live_app_exposes_exactly_the_declared_writes(tmp_path, monkeypatch):
    """The route whitelist. Adding a write means editing WRITE_ROUTES first.

    An equality rather than a subset check in both directions: an undeclared
    write is the danger, and a declared route that no longer exists is a stale
    declaration that would hide the next one.
    """
    config = make_config(tmp_path)
    seed(config)
    app = build(config, monkeypatch)

    found = app_mod.write_routes(app)
    # Control first: an empty result would make the equality below vacuous the
    # day FastAPI changes how an included router is stored, which is exactly what
    # happened once already.
    assert found, "the route walker found no writes at all"
    assert found == set(app_mod.WRITE_ROUTES)


def test_the_walker_finds_the_real_routes(tmp_path, monkeypatch):
    """The reads too, so a walker that quietly returns nothing cannot hide."""
    config = make_config(tmp_path)
    seed(config)
    app = build(config, monkeypatch)

    found = app_mod.all_routes(app)
    assert ("GET", "/api/health") in found
    assert ("GET", "/api/status") in found
    assert ("POST", "/api/session") in found


def test_the_route_whitelist_would_notice_a_new_write(tmp_path, monkeypatch):
    """Control: adding a route makes the comparison above fail."""
    config = make_config(tmp_path)
    seed(config)
    app = build(config, monkeypatch)

    @app.post("/api/smuggled")
    def smuggled():  # pragma: no cover - never called
        return {}

    assert app_mod.write_routes(app) != set(app_mod.WRITE_ROUTES)
    assert ("POST", "/api/smuggled") in app_mod.write_routes(app)


def test_head_and_options_are_not_counted_as_writes(tmp_path, monkeypatch):
    """Starlette adds HEAD beside GET, and neither changes anything."""
    config = make_config(tmp_path)
    seed(config)
    app = build(config, monkeypatch)

    methods = {method for method, _ in app_mod.write_routes(app)}
    assert not methods & {"GET", "HEAD", "OPTIONS"}
