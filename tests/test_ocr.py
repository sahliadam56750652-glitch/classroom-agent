"""Transcribing the pages PyMuPDF could not read.

The vision API is never called: every test passes a FakeProvider that counts
its calls, which is also how the cache tests prove what they claim.

The properties under test are the ones that protect the two scarce things here
-- the free-tier quota, and material that cannot be re-downloaded once a
teacher deletes it. A page is sent at most once ever, and a failure of any kind
leaves the page recoverable rather than lost.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import sqlite3

import pymupdf
import pytest

from agent import cli
from agent.classroom.models import Course, CourseWorkMaterial, Material
from agent.config import Config
from agent.db import store
from agent.files import extract, ocr
from agent.llm.provider import (
    LLMAuthError,
    LLMModelUnavailable,
    LLMQuotaError,
    LLMRateLimited,
    LLMRefused,
    LLMTimeout,
    LLMUnavailable,
)

# --------------------------------------------------------------------------
# fakes and fixtures
# --------------------------------------------------------------------------


class FakeProvider:
    """Counts calls, and can be scripted to fail on a given call number."""

    name = "fake:test-model"

    def __init__(self, text="TRANSCRIBED", errors=None):
        self.text = text
        self.errors = list(errors or [])
        self.calls: list[tuple[bytes, str, str]] = []

    def transcribe_image(self, image, prompt, *, mime_type="image/png"):
        self.calls.append((image, prompt, mime_type))
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        if callable(self.text):
            return self.text(len(self.calls))
        return self.text


@pytest.fixture
def config(tmp_path) -> Config:
    data_dir = tmp_path / "data"
    (data_dir / "library").mkdir(parents=True)
    return Config(
        account="someone@example.com",
        timezone="Africa/Tunis",
        data_dir=data_dir,
        tracked_courses=["c1"],
        ignored_courses=[],
    )


@pytest.fixture
def conn(config):
    connection = store.connect(config.db_path)
    store.upsert_course(
        connection,
        Course(
            id="c1", name="Operating Systems", section=None, room=None, owner_id=None,
            course_state="ACTIVE", enrollment_code=None, alternate_link=None,
            creation_time=None, update_time=None, content_hash="h",
        ),
    )
    yield connection
    connection.close()


def _image_of(caption):
    source = pymupdf.open()
    page = source.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 550, 780), f"{caption} " * 40, fontsize=14)
    pixmap = page.get_pixmap(dpi=72)
    source.close()
    return pixmap


def deck(path, layout, *, captions=None):
    """A PDF built from a layout string: 'n' native page, 's' image page.

    'nsn' is the shape that matters here -- a native slide deck with a diagram
    embedded in the middle, which is what 322 of these 1287 pages actually are.
    """
    captions = captions or {}
    document = pymupdf.open()
    for index, kind in enumerate(layout):
        page = document.new_page()
        if kind == "n":
            page.insert_textbox(
                pymupdf.Rect(40, 40, 550, 780), f"Native page {index}. " * 20, fontsize=11
            )
        else:
            page.insert_image(page.rect, pixmap=_image_of(captions.get(index, f"diagram{index}")))
    document.save(path)
    document.close()
    return path


def a_post(conn, parent_id, course_id="c1", creation_time="2026-01-01T10:00:00Z"):
    """The coursework_material a file hangs off, with a posting date.

    The queue orders on this date, so a test about ordering needs the parent to
    exist -- most tests here do not, and a file whose parent was never created
    is a real state too (a soft-deleted post), so it stays optional.
    """
    store.upsert_coursework_material(
        conn,
        CourseWorkMaterial(
            id=parent_id, course_id=course_id, title=f"post {parent_id}",
            description=None, state="PUBLISHED", topic_id=None,
            alternate_link=None, creation_time=creation_time,
            update_time=creation_time, content_hash="h",
        ),
    )


def a_file(config, conn, drive_id, filename, mime_type, *, builder=None, payload=None,
           scan_pages=0, pages=None, method="pymupdf", text="",
           parent_id="p1", course_id="c1"):
    """A fetched-and-extracted file, as fetch + extract would have left it."""
    destination = config.library_dir / "files" / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    if builder is not None:
        builder(destination)
    else:
        destination.write_bytes(payload or b"")

    text_path = config.library_dir / extract.TEXT_SUBDIR / f"{drive_id}.txt"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")

    store.upsert_material(
        conn,
        Material(
            id=f"coursework_material:{parent_id}:driveFile:{drive_id}",
            parent_type="coursework_material", parent_id=parent_id, course_id=course_id,
            kind="driveFile", ref=drive_id, drive_id=drive_id, title=filename,
            url=None, content_hash="h",
        ),
    )
    store.upsert_extraction(
        conn, drive_id, status="ok", mime_type=mime_type,
        local_path=f"files/{filename}", text_path=f"{extract.TEXT_SUBDIR}/{drive_id}.txt",
        method=method, pages=pages, scan_pages=scan_pages, chars=len(text),
        # As `agent fetch` records it. Without an md5 nothing can be trusted
        # without rendering, which is the pre-Phase-2.4 behaviour.
        md5_checksum=file_md5(destination),
    )
    return destination


def text_of(config, drive_id):
    return (config.library_dir / extract.TEXT_SUBDIR / f"{drive_id}.txt").read_text(encoding="utf-8")


def file_md5(path):
    """What Drive reports for the file, and what `agent fetch` records."""
    return hashlib.md5(path.read_bytes()).hexdigest()


def refetch(conn, drive_id, path):
    """Simulate `agent fetch` seeing a new revision of a file."""
    store.upsert_extraction(conn, drive_id, md5_checksum=file_md5(path))


@pytest.fixture
def renders(monkeypatch):
    """Every page rasterisation, counted.

    Rasterising is 98% of the cost of a run, so "how many pages did this
    render" is the measurement the laziness work exists to change.
    """
    calls: list[int] = []
    original = ocr.render_page

    def counting(page, dpi=ocr.RENDER_DPI):
        calls.append(dpi)
        return original(page, dpi)

    monkeypatch.setattr(ocr, "render_page", counting)
    return calls


# --------------------------------------------------------------------------
# the cache
# --------------------------------------------------------------------------


def test_an_unchanged_page_is_never_sent_twice(config, conn):
    """The quota-protection mechanism. Everything else here depends on it."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ns"), scan_pages=1, pages=2)
    provider = FakeProvider()

    first = ocr.run(config, conn, provider=provider)
    assert (first.transcribed, len(provider.calls)) == (1, 1)

    second = ocr.run(config, conn, provider=provider)

    assert second.transcribed == 0
    assert second.cached == 1
    assert len(provider.calls) == 1  # not one more


def test_a_changed_page_is_sent_again(config, conn):
    """The hash is of the rendered image, so an edited slide is a new page."""
    path = a_file(config, conn, "f1", "f1.pdf", "application/pdf",
                  builder=lambda p: deck(p, "s", captions={0: "before"}),
                  scan_pages=1, pages=1)
    provider = FakeProvider()
    ocr.run(config, conn, provider=provider)

    deck(path, "s", captions={0: "after"})
    refetch(conn, "f1", path)  # a changed file arrives with a changed md5
    result = ocr.run(config, conn, provider=provider)

    assert result.transcribed == 1
    assert len(provider.calls) == 2


def test_the_same_image_in_two_files_is_paid_for_once(config, conn):
    """A diagram reused across two decks is one page of quota."""
    for drive_id in ("f1", "f2"):
        a_file(config, conn, drive_id, f"{drive_id}.pdf", "application/pdf",
               builder=lambda p: deck(p, "s", captions={0: "shared"}), scan_pages=1, pages=1)
    provider = FakeProvider()

    result = ocr.run(config, conn, provider=provider)

    assert len(provider.calls) == 1
    assert result.transcribed == 1
    assert result.cached == 1
    # Both files still get the text.
    assert "TRANSCRIBED" in text_of(config, "f1")
    assert "TRANSCRIBED" in text_of(config, "f2")


# --------------------------------------------------------------------------
# rendering: 98% of the cost, so it happens as rarely as possible
# --------------------------------------------------------------------------


def test_a_second_run_over_an_unchanged_file_renders_nothing(config, conn, renders):
    """The whole point. A --limit 2 run used to cost 315s to make two calls.

    The file's md5 is unchanged, so nothing inside it can have changed, so the
    stored page hashes still describe it and there is nothing to rasterise.
    """
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)

    ocr.run(config, conn, provider=FakeProvider())
    assert len(renders) == 3  # the first run has to, to learn the hashes

    renders.clear()
    result = ocr.run(config, conn, provider=FakeProvider())

    assert renders == []
    assert result.cached == 3


def test_a_changed_checksum_re_renders_the_whole_file(config, conn, renders):
    """A new md5 invalidates every page of that file at once."""
    path = a_file(config, conn, "f1", "f1.pdf", "application/pdf",
                  builder=lambda p: deck(p, "sss", captions={0: "a", 1: "b", 2: "c"}),
                  scan_pages=3, pages=3)
    ocr.run(config, conn, provider=FakeProvider())
    renders.clear()

    deck(path, "sss", captions={0: "x", 1: "y", 2: "z"})
    refetch(conn, "f1", path)
    result = ocr.run(config, conn, provider=FakeProvider())

    assert len(renders) == 3
    assert result.transcribed == 3


def test_limit_does_not_render_pages_it_will_never_send(config, conn, renders):
    """Preparing a page costs more than sending it, so do not prepare it early."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sssss"), scan_pages=5, pages=5)

    result = ocr.run(config, conn, provider=FakeProvider(), limit=2)

    assert len(renders) == 2          # not five
    assert result.attempted == 2
    assert result.never_attempted == 3


def test_pages_deferred_by_limit_stay_pending_and_recoverable(config, conn, renders):
    """Pending semantics are unchanged, even though the page was never rendered."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)

    ocr.run(config, conn, provider=FakeProvider(), limit=1)
    assert store.count_ocr_pages_by_status(conn) == {"ok": 1, "pending": 2}
    deferred = store.get_ocr_page(conn, "f1", 1)
    assert deferred["page_hash"] == ocr.UNRENDERED  # honest: never hashed
    assert deferred["error"] == "run limit reached"

    renders.clear()
    result = ocr.run(config, conn, provider=FakeProvider())

    assert result.transcribed == 2        # picked up exactly where it left off
    assert len(renders) == 2              # and only the two it still needed
    assert store.count_ocr_pages_by_status(conn) == {"ok": 3}


def test_deferring_never_downgrades_an_already_transcribed_page(config, conn, renders):
    """Paid work is not lost because a later page ran into --limit.

    Reproduces a real defect: with no trustworthy md5 the fast path cannot
    fire, so an 'ok' page reached after the limit fell into the deferral
    branch and was rewritten as 'pending', discarding its text. Whether it
    happened at all depended on which file sorted first.
    """
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)
    ocr.run(config, conn, provider=FakeProvider(text="PAID FOR"))
    assert store.count_ocr_pages_by_status(conn) == {"ok": 3}

    # No md5 to trust, so every page has to be resolved the slow way.
    store.upsert_extraction(conn, "f1", md5_checksum=None)
    renders.clear()

    result = ocr.run(config, conn, provider=FakeProvider(), limit=0)

    assert store.count_ocr_pages_by_status(conn) == {"ok": 3}   # nothing downgraded
    assert result.cached == 3
    assert renders == []
    # And the text still reaches the library rather than vanishing from it.
    assert text_of(config, "f1").count("PAID FOR") == 3


def test_a_dry_run_renders_nothing_at_all(config, conn, renders):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)

    result = ocr.run(config, conn, provider=None, dry_run=True)

    assert renders == []
    assert len(result.would_send) == 3


def test_a_file_with_no_checksum_still_works_by_rendering(config, conn, renders):
    """Google-native exports carry no md5. Correct but slow beats wrong and fast."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ss"), scan_pages=2, pages=2)
    store.upsert_extraction(conn, "f1", md5_checksum=None)

    ocr.run(config, conn, provider=FakeProvider())
    renders.clear()
    result = ocr.run(config, conn, provider=FakeProvider())

    assert len(renders) == 2      # cannot be trusted without rendering
    assert result.cached == 2     # but the result cache still spares the API call


def test_an_untrusted_hash_never_serves_stale_text(config, conn, renders):
    """If the file changed but the md5 was not updated, the page hash still catches it."""
    path = a_file(config, conn, "f1", "f1.pdf", "application/pdf",
                  builder=lambda p: deck(p, "s", captions={0: "before"}),
                  scan_pages=1, pages=1)
    ocr.run(config, conn, provider=FakeProvider(text="OLD"))
    store.upsert_extraction(conn, "f1", md5_checksum=None)  # no way to trust anything

    deck(path, "s", captions={0: "after"})
    result = ocr.run(config, conn, provider=FakeProvider(text="NEW"))

    assert result.transcribed == 1
    assert "NEW" in text_of(config, "f1")


def test_force_re_sends_a_cached_page(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)
    provider = FakeProvider()
    ocr.run(config, conn, provider=provider)

    ocr.run(config, conn, provider=provider, force=True)

    assert len(provider.calls) == 2


# --------------------------------------------------------------------------
# quota and failure
# --------------------------------------------------------------------------


def test_quota_exhaustion_leaves_pages_pending_and_finishes_the_run(config, conn):
    """Never lose the file, never abort the run."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)
    provider = FakeProvider(errors=[None, LLMQuotaError("quota spent")])

    result = ocr.run(config, conn, provider=provider)

    assert result.transcribed == 1
    assert result.pending == 2
    assert result.quota_exhausted is True
    # Only the failing call was made -- the third page was not attempted.
    assert len(provider.calls) == 2
    states = store.count_ocr_pages_by_status(conn)
    assert states == {"ok": 1, "pending": 2}


def test_a_pending_page_is_retried_on_the_next_run(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ss"), scan_pages=2, pages=2)
    ocr.run(config, conn, provider=FakeProvider(errors=[LLMQuotaError("spent")]))
    assert store.count_ocr_pages_by_status(conn)["pending"] == 2

    result = ocr.run(config, conn, provider=FakeProvider())

    assert result.transcribed == 2
    assert store.count_ocr_pages_by_status(conn) == {"ok": 2}


def test_an_outage_leaves_the_page_pending_and_keeps_going(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ss"), scan_pages=2, pages=2)
    provider = FakeProvider(errors=[LLMUnavailable("503"), None])

    result = ocr.run(config, conn, provider=provider)

    # Unlike quota, an outage does not stop the run -- the next page is tried.
    assert (result.pending, result.transcribed) == (1, 1)


def test_a_refusal_is_an_error_not_a_pending_retry_forever(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)

    result = ocr.run(config, conn, provider=FakeProvider(errors=[LLMRefused("SAFETY")]))

    assert result.failed == 1
    assert store.count_ocr_pages_by_status(conn) == {"error": 1}


def test_an_auth_error_stops_everything_loudly(config, conn):
    """A bad key must not degrade into a library that is quietly never read."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)

    with pytest.raises(LLMAuthError):
        ocr.run(config, conn, provider=FakeProvider(errors=[LLMAuthError("bad key")]))


def test_a_pending_page_never_writes_text_as_if_complete(config, conn):
    """A silent gap would read as a page the lecturer left blank."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ns"), scan_pages=1, pages=2)

    ocr.run(config, conn, provider=FakeProvider(errors=[LLMQuotaError("spent")]))

    written = text_of(config, "f1")
    assert "not transcribed yet" in written
    row = store.get_extraction(conn, "f1")
    # The gap between scan_pages and ocr_pages is what says "incomplete".
    assert row["scan_pages"] == 1
    assert row["ocr_pages"] == 0


def test_a_sent_and_rejected_page_is_counted_apart_from_one_never_sent(config, conn):
    """The defect that made a dead key, a retired model and broken TLS identical.

    Four pages, a limit of two: two are sent and refused, two are never sent.
    All four end up `pending`, so the page counts alone cannot tell them apart
    and the run has to say how many calls it actually issued.
    """
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ssss"), scan_pages=4, pages=4)
    provider = FakeProvider(errors=[LLMUnavailable("connection reset")] * 2)

    result = ocr.run(config, conn, provider=provider, limit=2)

    assert result.attempted == 2
    assert result.call_failures == 2
    assert result.never_attempted == 2
    assert result.pending == 4          # indistinguishable on its own
    assert len(provider.calls) == 2     # and this is what disambiguates it


def test_a_run_that_issues_no_call_at_all_says_so(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ss"), scan_pages=2, pages=2)

    result = ocr.run(config, conn, provider=FakeProvider(), limit=0)

    assert result.attempted == 0
    assert result.never_attempted == 2
    assert result.pages_considered == 2


def test_the_reason_each_page_is_untranscribed_is_readable_back(config, conn):
    """Recorded all along; nothing surfaced it, so diagnosis meant opening the DB."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)

    ocr.run(config, conn, provider=FakeProvider(errors=[LLMUnavailable("TLS failed")]), limit=1)

    reasons = dict(store.ocr_error_counts(conn))
    assert reasons["run limit reached"] == 2
    assert any("TLS failed" in reason for reason in reasons)


def test_a_retired_model_stops_the_run_instead_of_burying_it(config, conn):
    """323 pages marked pending hid one fixable cause. It must surface instead."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)

    with pytest.raises(LLMModelUnavailable):
        ocr.run(config, conn, provider=FakeProvider(errors=[LLMModelUnavailable("retired")]))

    # Nothing was quietly recorded as "we will get to it later".
    assert store.count_ocr_pages_by_status(conn).get("pending") is None


def test_a_per_minute_limit_waits_and_carries_on(config, conn):
    """Unlike a daily cap: giving up here would waste the rest of the day's quota."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)
    slept: list[float] = []
    provider = FakeProvider(errors=[LLMRateLimited("slow down", retry_after=12.0)])

    result = ocr.run(config, conn, provider=provider, sleep=slept.append)

    assert result.rate_limited == 1
    assert result.quota_exhausted is False       # the run was NOT abandoned
    assert result.transcribed == 2               # the other two pages went through
    assert len(provider.calls) == 3
    assert slept == [12.0]                       # it waited the delay it was given


def test_a_rate_limit_without_a_stated_delay_waits_a_minute(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)
    slept: list[float] = []

    ocr.run(config, conn, provider=FakeProvider(errors=[LLMRateLimited("slow down")]),
            sleep=slept.append)

    assert slept == [ocr.RATE_LIMIT_PAUSE]


def test_an_absurd_rate_limit_delay_is_capped(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)
    slept: list[float] = []

    ocr.run(config, conn,
            provider=FakeProvider(errors=[LLMRateLimited("wait", retry_after=9999.0)]),
            sleep=slept.append)

    assert slept == [ocr.MAX_RATE_LIMIT_PAUSE]


def test_a_rate_limited_page_stays_pending_and_recoverable(config, conn):
    """Pending semantics are unchanged: the page is deferred, never lost."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)
    ocr.run(config, conn, provider=FakeProvider(errors=[LLMRateLimited("slow")]),
            sleep=lambda _s: None)
    assert store.count_ocr_pages_by_status(conn) == {"pending": 1}

    assert ocr.run(config, conn, provider=FakeProvider()).transcribed == 1


def test_repeated_rate_limits_stop_the_run_rather_than_sleeping_through_it(config, conn):
    """Waiting is right until it stops working. 300 pages x 16s is not a strategy."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sssss"), scan_pages=5, pages=5)
    slept: list[float] = []
    provider = FakeProvider(errors=[LLMRateLimited("slow", retry_after=16.0)] * 5)

    result = ocr.run(config, conn, provider=provider, sleep=slept.append)

    assert result.stop_reason == "repeated-rate-limits"
    assert len(provider.calls) == ocr.MAX_CONSECUTIVE_RATE_LIMITS
    assert len(slept) == ocr.MAX_CONSECUTIVE_RATE_LIMITS - 1  # no wait after giving up


def test_a_success_resets_the_rate_limit_patience(config, conn):
    """Occasional throttling across a long run must not accumulate into a stop."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sssss"), scan_pages=5, pages=5)
    limited = LLMRateLimited("slow", retry_after=1.0)
    provider = FakeProvider(errors=[limited, None, limited, None, limited])

    result = ocr.run(config, conn, provider=provider, sleep=lambda _s: None)

    assert result.stop_reason is None
    assert result.transcribed == 2
    assert len(provider.calls) == 5


def test_stopping_on_rate_limits_is_not_reported_as_a_daily_cap(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)

    result = ocr.run(config, conn, sleep=lambda _s: None,
                     provider=FakeProvider(errors=[LLMRateLimited("slow")] * 3))

    assert result.quota_exhausted is True          # no more calls this run
    assert result.stop_reason == "repeated-rate-limits"   # but not "come back tomorrow"


def test_a_timeout_leaves_the_page_pending_and_the_run_continues(config, conn):
    """One slow response must not end a batch with 19 pages of quota left.

    The read timeout escaped every handler and killed the run with a traceback,
    losing the whole batch. Now it is a classified failure like any other.
    """
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)
    provider = FakeProvider(errors=[LLMTimeout("did not respond within 60s")])

    result = ocr.run(config, conn, provider=provider)

    assert result.timed_out == 1
    assert result.transcribed == 2          # the run carried on past it
    assert len(provider.calls) == 3
    assert store.count_ocr_pages_by_status(conn) == {"ok": 2, "pending": 1}


def test_timeouts_are_counted_apart_from_other_pending_pages(config, conn):
    """"3 pages timed out" must be visible, not hidden inside a pending count."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sssss"), scan_pages=5, pages=5)
    provider = FakeProvider(
        errors=[LLMTimeout("t"), LLMUnavailable("503"), LLMTimeout("t"), None, LLMTimeout("t")]
    )

    result = ocr.run(config, conn, provider=provider)

    assert result.timed_out == 3
    assert result.pending == 4              # timeouts plus the 503
    assert result.transcribed == 1
    assert result.items_seen()["timed_out"] == 3


def test_a_timed_out_page_says_so_in_its_recorded_reason(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)

    ocr.run(config, conn, provider=FakeProvider(errors=[LLMTimeout("did not respond")]))

    reasons = dict(store.ocr_error_counts(conn))
    assert any(reason.startswith("timed out:") for reason in reasons)


def test_a_timed_out_page_is_retried_on_the_next_run(config, conn):
    """Pending means deferred, never lost -- the same as every other failure."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)
    ocr.run(config, conn, provider=FakeProvider(errors=[LLMTimeout("t")]))

    result = ocr.run(config, conn, provider=FakeProvider(text="GOT IT"))

    assert result.transcribed == 1
    assert "GOT IT" in text_of(config, "f1")


def test_a_timeout_does_not_count_as_an_exhausted_quota(config, conn):
    """A slow response says nothing about the allowance, so the run keeps its budget."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ss"), scan_pages=2, pages=2)

    result = ocr.run(config, conn, provider=FakeProvider(errors=[LLMTimeout("t")]))

    assert result.quota_exhausted is False
    assert result.stop_reason is None


def test_a_daily_cap_still_stops_the_run(config, conn):
    """The behaviour that already worked, pinned so rate limiting cannot erode it."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "sss"), scan_pages=3, pages=3)
    provider = FakeProvider(errors=[LLMQuotaError("daily quota spent")])

    result = ocr.run(config, conn, provider=provider, sleep=lambda _s: None)

    assert result.quota_exhausted is True
    assert result.stop_reason == "daily-quota"
    assert result.rate_limited == 0
    assert len(provider.calls) == 1


def test_limit_bounds_the_pages_sent(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ssss"), scan_pages=4, pages=4)
    provider = FakeProvider()

    result = ocr.run(config, conn, provider=provider, limit=2)

    assert len(provider.calls) == 2
    assert result.transcribed == 2
    assert result.pending == 2
    assert result.quota_exhausted is False  # a budget, not an outage


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------


def test_page_order_is_preserved_when_merging(config, conn):
    """Transcriptions land in their own page slots, never appended at the end."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "nsn"), scan_pages=1, pages=3)
    provider = FakeProvider(text="MIDDLE DIAGRAM")

    ocr.run(config, conn, provider=provider)

    pages = text_of(config, "f1").split(extract.PAGE_BREAK)
    assert len(pages) == 3
    assert "Native page 0" in pages[0]
    assert "MIDDLE DIAGRAM" in pages[1]
    assert "Native page 2" in pages[2]


def test_several_transcriptions_keep_their_own_slots(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "snsns"), scan_pages=3, pages=5)
    provider = FakeProvider(text=lambda n: f"OCR-{n}")

    ocr.run(config, conn, provider=provider)

    pages = text_of(config, "f1").split(extract.PAGE_BREAK)
    assert [i for i, page in enumerate(pages) if page.startswith("OCR-")] == [0, 2, 4]
    assert "Native page 1" in pages[1]


def test_a_sparse_page_keeps_its_text_alongside_the_transcription(config, conn):
    """A slide title above a diagram is what ties the transcription to a lecture.

    Note the caption is deliberately short. A page carrying more than
    MIN_CHARS_PER_PAGE of real text classifies as native and is never sent --
    see the threshold's limits in extract.classify_page.
    """
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 550, 200), "Figure 3: memory hierarchy",
                        fontsize=11)
    page.insert_image(pymupdf.Rect(40, 300, 550, 700), pixmap=_image_of("cache"))
    path = config.library_dir / "files" / "f1.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()

    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda _p: None, scan_pages=1, pages=1)

    ocr.run(config, conn, provider=FakeProvider(text="DIAGRAM TEXT"))

    written = text_of(config, "f1")
    assert "memory hierarchy" in written
    assert "DIAGRAM TEXT" in written


def test_the_extraction_records_that_a_model_produced_some_of_the_text(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ns"), scan_pages=1, pages=2)

    ocr.run(config, conn, provider=FakeProvider())

    row = store.get_extraction(conn, "f1")
    assert row["method"] == "pymupdf+ocr"
    assert row["ocr_pages"] == 1
    page = store.get_ocr_page(conn, "f1", 1)
    assert page["model"] == "fake:test-model"


def test_the_method_suffix_is_added_once_however_often_ocr_runs(config, conn):
    """It used to grow to 'pymupdf+ocr+ocr+ocr' -- the provenance column, corrupted."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ns"), scan_pages=1, pages=2)

    for _ in range(3):
        ocr.run(config, conn, provider=FakeProvider(), force=True)

    assert store.get_extraction(conn, "f1")["method"] == "pymupdf+ocr"


@pytest.mark.parametrize(
    "before,after",
    [(None, "pymupdf+ocr"), ("pymupdf", "pymupdf+ocr"), ("image", "image+ocr"),
     ("pymupdf+ocr", "pymupdf+ocr")],
)
def test_the_ocr_suffix_is_idempotent(before, after):
    assert ocr._with_ocr_suffix(before) == after


# --------------------------------------------------------------------------
# image attachments
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg"])
def test_an_image_attachment_is_extracted_as_one_unread_page(tmp_path, mime_type):
    """Not 'unsupported' -- that would drop a photographed board permanently."""
    path = tmp_path / "board.png"
    path.write_bytes(b"fake image bytes")

    result = extract.extract_file(path, mime_type)

    assert result is not None
    assert (result.pages, result.scan_pages, result.method) == (1, 1, "image")


def test_an_image_attachment_routes_to_ocr(config, conn):
    a_file(config, conn, "img", "board.png", "image/png",
           payload=b"\x89PNG fake bytes", scan_pages=1, pages=1, method="image")
    provider = FakeProvider(text="x = 5 on the whiteboard")

    result = ocr.run(config, conn, provider=provider)

    assert result.transcribed == 1
    # The original bytes go up, not a re-render of them.
    assert provider.calls[0][0] == b"\x89PNG fake bytes"
    assert provider.calls[0][2] == "image/png"
    assert "whiteboard" in text_of(config, "img")


# --------------------------------------------------------------------------
# selection and dry run
# --------------------------------------------------------------------------


def test_a_fully_native_pdf_is_never_considered(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "nnn"), scan_pages=0, pages=3)
    provider = FakeProvider()

    result = ocr.run(config, conn, provider=provider)

    assert result.files == 0
    assert provider.calls == []


def test_dry_run_sends_nothing_but_reports_the_cost(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "nss"), scan_pages=2, pages=3)
    provider = FakeProvider()

    result = ocr.run(config, conn, provider=provider, dry_run=True)

    assert provider.calls == []
    assert len(result.would_send) == 2
    assert store.count_rows(conn, "ocr_pages") == 0


def test_dry_run_counts_cache_hits_separately(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "ss"), scan_pages=2, pages=2)
    ocr.run(config, conn, provider=FakeProvider(), limit=1)

    result = ocr.run(config, conn, provider=None, dry_run=True)

    assert result.cached == 1
    assert len(result.would_send) == 1


def test_a_real_run_without_a_provider_is_refused(config, conn):
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)

    with pytest.raises(ocr.OCRError):
        ocr.run(config, conn, provider=None)


def test_a_missing_local_file_is_skipped_not_fatal(config, conn):
    path = a_file(config, conn, "f1", "f1.pdf", "application/pdf",
                  builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)
    path.unlink()

    assert ocr.run(config, conn, provider=FakeProvider()).files == 0


def test_the_empty_sentinel_becomes_empty_text(config, conn):
    """The model saying 'nothing here' must not land in the library verbatim."""
    a_file(config, conn, "f1", "f1.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1)

    ocr.run(config, conn, provider=FakeProvider(text=ocr.EMPTY_SENTINEL))

    assert ocr.EMPTY_SENTINEL not in text_of(config, "f1")


# --------------------------------------------------------------------------
# dead references
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# per-subject progress
# --------------------------------------------------------------------------


def _course(conn, course_id, name):
    store.upsert_course(
        conn,
        Course(
            id=course_id, name=name, section=None, room=None, owner_id=None,
            course_state="ACTIVE", enrollment_code=None, alternate_link=None,
            creation_time=None, update_time=None, content_hash="h",
        ),
    )


def _material(conn, drive_id, course_id, title):
    store.upsert_material(
        conn,
        Material(
            id=f"coursework_material:p-{course_id}:driveFile:{drive_id}",
            parent_type="coursework_material", parent_id=f"p-{course_id}",
            course_id=course_id, kind="driveFile", ref=drive_id, drive_id=drive_id,
            title=title, url=None, content_hash="h",
        ),
    )


def test_progress_is_reported_per_subject_not_in_aggregate(config, conn):
    """A library 90% done is no use if the missing 10% is all of one subject."""
    _course(conn, "c2", "Databases")
    _material(conn, "os1", "c1", "Chapter 6.pdf")
    _material(conn, "db1", "c2", "Relational model.pdf")
    store.upsert_extraction(conn, "os1", status="ok", mime_type="application/pdf",
                            local_path="files/os1.pdf", scan_pages=2, pages=10)
    store.upsert_extraction(conn, "db1", status="ok", mime_type="application/pdf",
                            local_path="files/db1.pdf", scan_pages=3, pages=12)
    # Operating Systems is finished; Databases has one page of three.
    for index in range(2):
        store.upsert_ocr_page(conn, drive_id="os1", page_index=index,
                              page_hash=f"h{index}", status="ok", text="t")
    store.upsert_ocr_page(conn, drive_id="db1", page_index=0, page_hash="d0",
                          status="ok", text="t")

    rows = {row["course_name"]: row for row in store.ocr_progress(conn)}

    assert rows["Operating Systems"]["ok"] == 2
    assert rows["Operating Systems"]["needed"] == 2      # ready for quiz generation
    assert rows["Databases"]["ok"] == 1
    assert rows["Databases"]["needed"] == 3              # still half-read


def test_pages_nothing_has_looked_at_still_count_as_outstanding(config, conn):
    """A file with no ocr_pages rows is at 0/N, not absent from the report."""
    _material(conn, "f1", "c1", "Untouched.pdf")
    store.upsert_extraction(conn, "f1", status="ok", mime_type="application/pdf",
                            local_path="files/f1.pdf", scan_pages=5, pages=20)

    (row,) = store.ocr_progress(conn)

    assert (row["needed"], row["ok"], row["pending"], row["failed"]) == (5, 0, 0, 0)


def test_failed_pages_are_reported_apart_from_pending(config, conn):
    _material(conn, "f1", "c1", "Refused.pdf")
    store.upsert_extraction(conn, "f1", status="ok", mime_type="application/pdf",
                            local_path="files/f1.pdf", scan_pages=3, pages=3)
    store.upsert_ocr_page(conn, drive_id="f1", page_index=0, page_hash="a",
                          status="error", error="SAFETY")
    store.upsert_ocr_page(conn, drive_id="f1", page_index=1, page_hash="b",
                          status="pending", error="run limit reached")

    (row,) = store.ocr_progress(conn)

    assert (row["failed"], row["pending"], row["ok"]) == (1, 1, 0)


def test_a_file_with_no_scan_pages_is_not_in_the_report(config, conn):
    """Fully native documents need no OCR, so they are not "outstanding"."""
    _material(conn, "f1", "c1", "All text.pdf")
    store.upsert_extraction(conn, "f1", status="ok", mime_type="application/pdf",
                            local_path="files/f1.pdf", scan_pages=0, pages=8)

    assert store.ocr_progress(conn) == []


def test_a_file_shared_by_two_courses_reports_under_both(config, conn):
    """A subject is not ready because a different subject holds the same file."""
    _course(conn, "c2", "Databases")
    _material(conn, "shared", "c1", "Shared.pdf")
    _material(conn, "shared", "c2", "Shared.pdf")
    store.upsert_extraction(conn, "shared", status="ok", mime_type="application/pdf",
                            local_path="files/shared.pdf", scan_pages=2, pages=4)

    rows = store.ocr_progress(conn)

    assert {row["course_name"] for row in rows} == {"Operating Systems", "Databases"}
    assert all(int(row["needed"]) == 2 for row in rows)


def test_the_status_command_prints_per_subject_and_exits_zero(config, conn, monkeypatch, capsys):
    _material(conn, "f1", "c1", "Chapter 6.pdf")
    store.upsert_extraction(conn, "f1", status="ok", mime_type="application/pdf",
                            local_path="files/f1.pdf", scan_pages=2, pages=9)
    store.upsert_ocr_page(conn, drive_id="f1", page_index=0, page_hash="a",
                          status="ok", text="t")

    class KeepOpen:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            pass

    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    args = argparse.Namespace(status=True, dry_run=False, limit=None,
                              force=False, verbose=False)

    assert cli.cmd_ocr(config, args) == 0

    out = capsys.readouterr().out
    assert "Operating Systems" in out
    assert "1/2" in out
    assert "Chapter 6.pdf" in out


def test_dead_references_are_listable(config, conn):
    a_file(config, conn, "live", "live.pdf", "application/pdf",
           builder=lambda p: deck(p, "n"), pages=1)
    for drive_id, status in (("binned", "trashed"), ("gone", "missing")):
        store.upsert_material(
            conn,
            Material(
                id=f"coursework_material:p2:driveFile:{drive_id}", parent_type="coursework_material",
                parent_id="p2", course_id="c1", kind="driveFile", ref=drive_id,
                drive_id=drive_id, title=None, url=None, content_hash="h",
            ),
        )
        store.upsert_extraction(conn, drive_id, status=status)

    rows = store.dead_references(conn)

    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"trashed", "missing"}
    assert rows[0]["course_name"] == "Operating Systems"


# --------------------------------------------------------------------------
# queue order -- which pages the day's ~12 requests are spent on
# --------------------------------------------------------------------------

def _posts(*rows):
    """store.ocr_candidate_posts output, without needing a database.

    Each row is (drive_id, course_id, posted_at), which is the whole of what
    the ordering rests on.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    built = []
    for drive_id, course_id, posted_at in rows:
        built.append(
            connection.execute(
                "SELECT ? AS drive_id, ? AS course_id, ? AS posted_at, "
                "       ? AS title, ? AS course_name",
                (drive_id, course_id, posted_at, f"{drive_id}.pdf", course_id),
            ).fetchone()
        )
    connection.close()
    return built


def _files(*drive_ids):
    """Candidate extraction rows, in the drive_id order _candidates returns."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    built = [
        connection.execute("SELECT ? AS drive_id, 1 AS scan_pages", (drive_id,)).fetchone()
        for drive_id in sorted(drive_ids)
    ]
    connection.close()
    return built


def _order(ranked):
    return [item.drive_id for item in ranked]


def test_newer_material_precedes_older():
    """The gate asks about what was posted recently, so that is what gets read."""
    ranked = ocr.queue(
        _files("old", "new", "middle"),
        _posts(
            ("old", "c1", "2025-09-01T10:00:00Z"),
            ("new", "c1", "2026-09-14T10:00:00Z"),
            ("middle", "c1", "2026-01-20T10:00:00Z"),
        ),
        tracked=["c1"],
    )
    assert _order(ranked) == ["new", "middle", "old"]


def test_a_tracked_course_precedes_an_untracked_one():
    """Even when the untracked material is newer. An untracked course is not
    synced and never gated, so a request spent on it buys nothing."""
    ranked = ocr.queue(
        _files("tracked", "untracked"),
        _posts(
            ("tracked", "c1", "2025-01-01T10:00:00Z"),
            ("untracked", "cX", "2026-09-14T10:00:00Z"),
        ),
        tracked=["c1"],
    )
    assert _order(ranked) == ["tracked", "untracked"]
    assert ranked[0].why == "tracked"
    assert ranked[1].why == "not tracked"


def test_a_timetabled_course_precedes_a_merely_tracked_one():
    """A subject that meets is worth more than one that is only synced."""
    ranked = ocr.queue(
        _files("meets", "synced"),
        _posts(
            ("meets", "c1", "2025-01-01T10:00:00Z"),
            ("synced", "c2", "2026-09-14T10:00:00Z"),
        ),
        tracked=["c1", "c2"],
        timetabled=["c1"],
    )
    assert _order(ranked) == ["meets", "synced"]
    assert ranked[0].why == "tracked, in timetable"


def test_the_new_semester_beats_the_archive_wholesale():
    """The case this exists for: 279 archived pages must not starve two weeks
    of new material at ~12 requests a day."""
    ranked = ocr.queue(
        _files("arch1", "arch2", "arch3", "term1", "term2"),
        _posts(
            ("arch1", "old", "2025-10-01T10:00:00Z"),
            ("arch2", "old", "2025-11-01T10:00:00Z"),
            ("arch3", "old", "2025-12-01T10:00:00Z"),
            ("term1", "new", "2026-09-15T10:00:00Z"),
            ("term2", "new", "2026-09-16T10:00:00Z"),
        ),
        tracked=["new"],
        timetabled=["new"],
    )
    assert _order(ranked)[:2] == ["term2", "term1"]
    assert set(_order(ranked)[2:]) == {"arch1", "arch2", "arch3"}


def test_course_restricts_the_queue():
    ranked = ocr.queue(
        _files("a", "b", "c"),
        _posts(
            ("a", "c1", "2026-09-01T10:00:00Z"),
            ("b", "c2", "2026-09-02T10:00:00Z"),
            ("c", "c1", "2026-09-03T10:00:00Z"),
        ),
        tracked=["c1", "c2"],
        courses=["c1"],
    )
    assert _order(ranked) == ["c", "a"]


def test_course_filtering_drops_a_file_with_no_post_at_all():
    """It cannot be shown to belong to the wanted course, so it is not in it."""
    ranked = ocr.queue(_files("orphan"), _posts(), tracked=["c1"], courses=["c1"])
    assert ranked == []


def test_the_order_is_stable_across_runs():
    """A queue drained six pages at a time has to resume where it left off.
    Nothing in the sort key changes as pages are transcribed."""
    files = _files("a", "b", "c", "d")
    posts = _posts(
        ("a", "c1", "2026-09-01T10:00:00Z"),
        ("b", "c1", "2026-09-01T10:00:00Z"),   # a deliberate tie
        ("c", "c2", "2026-09-05T10:00:00Z"),
        ("d", "cX", "2026-09-09T10:00:00Z"),
    )
    first = _order(ocr.queue(files, posts, tracked=["c1", "c2"], timetabled=["c1"]))
    for _ in range(5):
        assert _order(ocr.queue(files, posts, tracked=["c1", "c2"], timetabled=["c1"])) == first
    # And the tie between a and b is broken by drive_id, not by luck.
    assert first == ["a", "b", "c", "d"]


def test_a_tie_on_date_falls_back_to_drive_id():
    ranked = ocr.queue(
        _files("zz", "aa"),
        _posts(("zz", "c1", "2026-09-01T10:00:00Z"), ("aa", "c1", "2026-09-01T10:00:00Z")),
        tracked=["c1"],
    )
    assert _order(ranked) == ["aa", "zz"]


def test_a_file_with_no_post_sorts_last_but_is_never_dropped():
    ranked = ocr.queue(
        _files("orphan", "posted"),
        _posts(("posted", "cX", "2020-01-01T10:00:00Z")),
        tracked=[],
    )
    assert _order(ranked) == ["posted", "orphan"]
    assert ranked[1].posted_at == ""


def test_a_file_in_two_courses_takes_the_better_one():
    """A deck shared between a tracked course and an archived one is tracked
    material. Ranking it on its least relevant attachment would bury it."""
    ranked = ocr.queue(
        _files("shared", "plain"),
        _posts(
            ("shared", "cX", "2020-01-01T10:00:00Z"),
            ("shared", "c1", "2026-09-01T10:00:00Z"),
            ("plain", "c1", "2026-08-01T10:00:00Z"),
        ),
        tracked=["c1"],
    )
    assert _order(ranked) == ["shared", "plain"]
    assert ranked[0].course_id == "c1"


def test_an_undated_post_does_not_outrank_a_dated_one_in_the_same_tier():
    ranked = ocr.queue(
        _files("dated", "undated"),
        _posts(("dated", "c1", "2020-01-01T10:00:00Z"), ("undated", "c1", "")),
        tracked=["c1"],
    )
    assert _order(ranked) == ["dated", "undated"]


def test_an_empty_queue_is_not_an_error():
    assert ocr.queue([], []) == []


# --------------------------------------------------------------------------
# the order, end to end through a real run
# --------------------------------------------------------------------------

@pytest.fixture
def two_courses(config, conn):
    """An archived course and a new-term one, each with a scan page.

    The shape the prioritisation exists for: last year's backlog sitting in
    front of material the gate is about to be asked about.
    """
    store.upsert_course(
        conn,
        Course(
            id="c2", name="Archived Networks", section=None, room=None, owner_id=None,
            course_state="ARCHIVED", enrollment_code=None, alternate_link=None,
            creation_time=None, update_time=None, content_hash="h",
        ),
    )
    a_post(conn, "old", course_id="c2", creation_time="2025-03-01T10:00:00Z")
    a_post(conn, "new", course_id="c1", creation_time="2026-09-15T10:00:00Z")
    a_file(config, conn, "aaa_old", "old.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1,
           parent_id="old", course_id="c2")
    a_file(config, conn, "zzz_new", "new.pdf", "application/pdf",
           builder=lambda p: deck(p, "s"), scan_pages=1, pages=1,
           parent_id="new", course_id="c1")
    return conn


def test_run_transcribes_the_new_term_first(config, conn, two_courses):
    """--limit 1 must spend its one request on this term, even though the
    archived file sorts first by drive_id and would have won before."""
    provider = FakeProvider()
    result = ocr.run(config, conn, provider=provider, limit=1)

    assert [item.drive_id for item in result.queue][0] == "zzz_new"
    assert "TRANSCRIBED" in text_of(config, "zzz_new")
    assert "not transcribed yet" in text_of(config, "aaa_old")
    assert provider.calls and len(provider.calls) == 1


def test_run_respects_the_course_filter(config, conn, two_courses):
    provider = FakeProvider()
    result = ocr.run(config, conn, provider=provider, courses=["c2"])

    assert [item.drive_id for item in result.queue] == ["aaa_old"]
    assert "TRANSCRIBED" in text_of(config, "aaa_old")
    # The unfiltered file is untouched -- not merged, not marked pending.
    assert "TRANSCRIBED" not in text_of(config, "zzz_new")


def test_the_timetable_lifts_a_tracked_course_above_another(config, conn, two_courses):
    """Both tracked; only one meets. The one that meets goes first, whatever
    the posting dates say."""
    config.tracked_courses.append("c2")
    ordered = ocr.queue(
        ocr._candidates(conn), store.ocr_candidate_posts(conn),
        tracked=config.tracked_courses, timetabled=["c2"],
    )
    assert [item.drive_id for item in ordered] == ["aaa_old", "zzz_new"]
    assert ordered[0].why == "tracked, in timetable"


def test_a_partly_drained_queue_resumes_where_it_left_off(config, conn, two_courses):
    """Two --limit 1 runs must cover both files, not the same one twice."""
    first = ocr.run(config, conn, provider=FakeProvider(), limit=1)
    order_before = [item.drive_id for item in first.queue]
    second = ocr.run(config, conn, provider=FakeProvider(), limit=1)

    assert [item.drive_id for item in second.queue] == order_before
    assert "TRANSCRIBED" in text_of(config, "zzz_new")
    assert "TRANSCRIBED" in text_of(config, "aaa_old")


def test_pending_candidates_drops_a_finished_file(config, conn, two_courses):
    """What `--status` leads with must be what a run would work on next."""
    assert len(ocr.pending_candidates(conn)) == 2
    ocr.run(config, conn, provider=FakeProvider(), limit=1)
    left = [row["drive_id"] for row in ocr.pending_candidates(conn)]
    assert left == ["aaa_old"]


def test_the_queue_carries_the_course_name_for_reporting(config, conn, two_courses):
    ordered = ocr.queue(
        ocr.pending_candidates(conn), store.ocr_candidate_posts(conn),
        tracked=config.tracked_courses,
    )
    assert ordered[0].course_name == "Operating Systems"
    assert ordered[0].posted_at == "2026-09-15T10:00:00Z"


def test_a_soft_deleted_post_stops_dating_its_file(config, conn, two_courses):
    """The post is gone, so its date is not evidence about anything. The file
    still gets transcribed -- it just stops claiming to be this term's."""
    conn.execute(
        "UPDATE coursework_materials SET deleted_at = ? WHERE id = 'new'",
        ("2026-09-20T10:00:00Z",),
    )
    conn.commit()
    ordered = ocr.queue(
        ocr._candidates(conn), store.ocr_candidate_posts(conn),
        tracked=config.tracked_courses,
    )
    assert [item.drive_id for item in ordered] == ["aaa_old", "zzz_new"]
    assert ordered[1].posted_at == ""


# --------------------------------------------------------------------------
# the command surface: seeing the order, and forcing it
# --------------------------------------------------------------------------

TIMETABLE = """
subjects:
  Operating Systems: "c1"
  Networks: null
versions:
  - label: S1
    status: confirmed
    effective_from: 2026-09-01
    sessions:
      - { day: mon, start: "08:30", end: "10:00", kind: LEC,
          subject: Operating Systems }
"""


class KeepOpen:
    """A connection the command may 'close' without ending the test."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _with_timetable(config, text=None):
    """`config` pointed at a real timetable file. Config is frozen, so this
    replaces it rather than mutating it."""
    path = config.data_dir / "timetable.yaml"
    path.write_text(text if text is not None else TIMETABLE, encoding="utf-8")
    return dataclasses.replace(config, timetable_path_override=path)


def _ocr_args(**overrides):
    values = dict(status=False, dry_run=False, limit=None, force=False,
                  verbose=False, courses=None)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_status_shows_which_course_is_next(config, conn, two_courses, monkeypatch, capsys):
    config = _with_timetable(config)
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))

    assert cli.cmd_ocr(config, _ocr_args(status=True)) == 0

    out = capsys.readouterr().out
    assert "next: Operating Systems" in out
    assert "queue order" in out
    # The prioritisation stated, not merely applied.
    assert "tracked, in timetable" in out
    assert out.index("new.pdf") < out.index("old.pdf")


def test_status_says_so_when_the_timetable_cannot_be_read(
    config, conn, two_courses, monkeypatch, capsys
):
    """A missing timetable costs the top tier and must not be silent -- the
    ordering would still work and would quietly be the blunter one."""
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    assert cli.cmd_ocr(config, _ocr_args(status=True)) == 0
    assert "timetable not read" in capsys.readouterr().out


def test_course_accepts_a_subject_name(config, conn, two_courses, monkeypatch, capsys):
    """A course id is twelve digits; the thing I want to force is a subject."""
    config = _with_timetable(config)
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))

    assert cli.cmd_ocr(config, _ocr_args(status=True, courses=["Operating Systems"])) == 0
    out = capsys.readouterr().out
    assert "new.pdf" in out
    assert "old.pdf" not in out


def test_course_refuses_a_name_it_cannot_resolve(config, conn, two_courses, capsys):
    """Never a near-match. A wrong match spends the day's quota on the wrong
    subject and looks exactly like the feature working."""
    config = _with_timetable(config)

    assert cli.cmd_ocr(config, _ocr_args(status=True, courses=["Operatng Systems"])) == 1
    assert "neither a tracked course id nor a subject" in capsys.readouterr().err


def test_a_subject_with_no_course_cannot_be_forced(config, conn, two_courses, capsys):
    """Six of eleven subjects map to null. Naming one is a mistake, not a
    request to transcribe nothing."""
    config = _with_timetable(config)

    assert cli.cmd_ocr(config, _ocr_args(status=True, courses=["Networks"])) == 1
    assert "neither a tracked course id nor a subject" in capsys.readouterr().err


def test_a_dry_run_shows_the_order_it_would_spend_in(
    config, conn, two_courses, monkeypatch, capsys
):
    monkeypatch.setattr(store, "open_db", lambda _config: KeepOpen(conn))
    assert cli.cmd_ocr(config, _ocr_args(dry_run=True)) == 0

    out = capsys.readouterr().out
    assert "queue order" in out
    assert out.index("new.pdf") < out.index("old.pdf")
