"""Telegram transport: escaping, splitting, and retry.

No test in this file may reach the network. Every client is built with a stub
transport, and test_no_test_can_reach_the_network pins that the default
transport is never used by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.notify.telegram import (
    MAX_FILENAME,
    MESSAGE_LIMIT,
    Telegram,
    TelegramError,
    _ApiError,
    _multipart,
    escape,
    link,
    safe_filename,
    split_message,
)


class StubTransport:
    """Records every POST and replies with whatever the test queued."""

    def __init__(self, replies=None):
        self.calls: list[tuple[str, dict]] = []
        self.replies = list(replies or [])

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if not self.replies:
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def texts(self) -> list[str]:
        return [payload["text"] for _, payload in self.calls]


def make(transport=None, sleeps=None):
    slept = sleeps if sleeps is not None else []
    return Telegram(
        "123:ABC",
        4242,
        transport=transport or StubTransport(),
        sleep=slept.append,
    )


# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------

def test_escapes_the_three_html_characters():
    assert escape("Algo & Data <Structures>") == "Algo &amp; Data &lt;Structures&gt;"


def test_course_title_with_angle_brackets_ampersand_and_quotes():
    """The exact shape that breaks a briefing: a real course title."""
    title = 'TD 3: <Sets> & "Relations"'
    escaped = escape(title)

    # The three characters that would be parsed as markup are neutralised.
    assert "<Sets>" not in escaped
    assert escaped == 'TD 3: &lt;Sets&gt; &amp; "Relations"'
    # Quotes are left alone on purpose: they carry no meaning outside an
    # attribute, and &quot; in a course title is worse than a quote.
    assert '"Relations"' in escaped


def test_none_escapes_to_empty_string():
    assert escape(None) == ""


def test_link_escapes_label_and_href_separately():
    result = link('https://x.test/?a=1&b=2', 'Sets & <Relations>')

    # The href is attribute-escaped, so the ampersand cannot end the attribute.
    assert 'href="https://x.test/?a=1&amp;b=2"' in result
    assert ">Sets &amp; &lt;Relations&gt;<" in result


def test_link_without_url_degrades_to_escaped_text():
    """A soft-deleted item has no usable link; text beats an anchor to nowhere."""
    assert link(None, "Lecture <7>") == "Lecture &lt;7&gt;"


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------

def test_short_text_is_one_message():
    assert split_message("hello") == ["hello"]


def test_empty_text_is_no_messages():
    assert split_message("   ") == []


def test_splits_a_10k_digest_on_course_boundaries():
    """The required case: a long digest breaks between courses, never inside one."""
    courses = []
    for index in range(10):
        lines = [f"<b>Course {index}</b>"]
        lines += [f"item {index}-{item} " + "x" * 80 for item in range(12)]
        courses.append("\n".join(lines))
    digest = "\n\n".join(courses)
    assert len(digest) > 10_000

    parts = split_message(digest)

    assert len(parts) > 1
    assert all(len(part) <= MESSAGE_LIMIT for part in parts)
    # Nothing was dropped or duplicated.
    assert "\n\n".join(parts) == digest
    # Every part begins at a course header, which is only true if each break
    # landed on a course boundary.
    for part in parts:
        assert part.startswith("<b>Course ")
    # And no course block was torn in half.
    for index in range(10):
        header = f"<b>Course {index}</b>"
        holder = [part for part in parts if header in part]
        assert len(holder) == 1
        for item in range(12):
            assert f"item {index}-{item} " in holder[0]


def test_a_course_too_big_for_one_message_splits_per_item():
    """Second-level boundary: inside one course, break between items."""
    lines = ["<b>Databases</b>"] + [f"item {i} " + "y" * 100 for i in range(60)]
    block = "\n".join(lines)
    assert len(block) > MESSAGE_LIMIT

    parts = split_message(block)

    assert len(parts) > 1
    assert all(len(part) <= MESSAGE_LIMIT for part in parts)
    # Rejoining on the item separator reproduces the block exactly, so every
    # break fell on a newline between items rather than mid-line.
    assert "\n".join(parts) == block
    for part in parts:
        for line in part.split("\n"):
            assert line == "<b>Databases</b>" or line.startswith("item ")


def test_a_single_oversized_line_is_hard_cut_as_a_last_resort():
    """Nothing structural is left to split on, and losing text would be worse."""
    parts = split_message("z" * (MESSAGE_LIMIT + 50))

    assert len(parts) == 2
    assert all(len(part) <= MESSAGE_LIMIT for part in parts)
    assert "".join(parts) == "z" * (MESSAGE_LIMIT + 50)


def test_split_never_returns_an_oversized_part():
    mixed = "\n\n".join(["<b>A</b>\n" + "a" * 5000, "<b>B</b>\nshort", "c" * 9000])
    parts = split_message(mixed)
    assert parts and all(0 < len(part) <= MESSAGE_LIMIT for part in parts)


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------

def test_send_message_posts_html_to_the_configured_chat():
    transport = StubTransport()
    make(transport).send_message("<b>Databases</b>")

    (url, payload), = transport.calls
    assert url.endswith("/bot123:ABC/sendMessage")
    assert payload["chat_id"] == 4242
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "<b>Databases</b>"


def test_never_markdown():
    """MarkdownV2 would need 18 characters escaped; one miss is a silent 400."""
    transport = StubTransport()
    make(transport).send_message("hi")
    assert transport.calls[0][1]["parse_mode"] == "HTML"


def test_link_previews_are_disabled():
    """Six linked assignments would otherwise unfurl into six cards."""
    transport = StubTransport()
    make(transport).send_message("hi")
    assert transport.calls[0][1]["link_preview_options"] == {"is_disabled": True}


def test_send_message_refuses_text_over_the_limit():
    transport = StubTransport()
    with pytest.raises(TelegramError, match="over the 4096"):
        make(transport).send_message("x" * (MESSAGE_LIMIT + 1))
    assert transport.calls == []


def test_send_long_sends_each_part_in_order():
    transport = StubTransport()
    digest = "\n\n".join(f"<b>C{i}</b>\n" + "x" * 3000 for i in range(3))

    make(transport).send_long(digest)

    assert len(transport.calls) == 3
    assert [text.split("\n")[0] for text in transport.texts] == [
        "<b>C0</b>",
        "<b>C1</b>",
        "<b>C2</b>",
    ]


def test_ok_false_on_a_200_is_still_a_failure():
    transport = StubTransport([{"ok": False, "description": "chat not found"}])
    with pytest.raises(TelegramError, match="chat not found"):
        make(transport).send_message("hi")


# --------------------------------------------------------------------------
# retry
# --------------------------------------------------------------------------

def test_429_is_retried_honouring_retry_after():
    transport = StubTransport(
        [_ApiError("rate limited", status=429, retry_after=7.0), {"ok": True}]
    )
    slept: list[float] = []
    Telegram("t", 1, transport=transport, sleep=slept.append).send_message("hi")

    assert len(transport.calls) == 2
    assert slept == [7.0]


def test_retry_after_is_capped():
    """A 429 can name any delay; a cron run must not block for an hour."""
    transport = StubTransport(
        [_ApiError("slow down", status=429, retry_after=9999.0), {"ok": True}]
    )
    slept: list[float] = []
    Telegram("t", 1, transport=transport, sleep=slept.append).send_message("hi")

    assert slept == [60.0]


def test_5xx_backs_off_exponentially():
    transport = StubTransport(
        [
            _ApiError("bad gateway", status=502, retry_after=None),
            _ApiError("bad gateway", status=502, retry_after=None),
            {"ok": True},
        ]
    )
    slept: list[float] = []
    Telegram("t", 1, transport=transport, sleep=slept.append).send_message("hi")

    assert len(transport.calls) == 3
    assert slept == [1.0, 2.0]


def test_400_is_not_retried():
    """A malformed request will not improve by being sent again."""
    transport = StubTransport([_ApiError("bad request", status=400, retry_after=None)])
    slept: list[float] = []

    with pytest.raises(TelegramError, match="bad request"):
        Telegram("t", 1, transport=transport, sleep=slept.append).send_message("hi")

    assert len(transport.calls) == 1
    assert slept == []


def test_401_is_not_retried():
    transport = StubTransport([_ApiError("unauthorized", status=401, retry_after=None)])
    with pytest.raises(TelegramError):
        make(transport).send_message("hi")
    assert len(transport.calls) == 1


def test_gives_up_after_the_attempt_limit():
    transport = StubTransport(
        [_ApiError("boom", status=503, retry_after=None) for _ in range(10)]
    )
    slept: list[float] = []

    with pytest.raises(TelegramError, match="boom"):
        Telegram("t", 1, transport=transport, sleep=slept.append).send_message("hi")

    # Five attempts means four waits between them.
    assert len(transport.calls) == 5
    assert slept == [1.0, 2.0, 4.0, 8.0]


def test_no_test_can_reach_the_network():
    """The default transport is real HTTP; every test must replace it."""
    from agent.notify import telegram as module

    client = make()
    assert client._transport is not module._http_post


# --------------------------------------------------------------------------
# filenames
# --------------------------------------------------------------------------

def uploader(sent):
    def upload(url, body, content_type):
        sent.append(body)
        return {"ok": True, "result": {"message_id": 1, "document": {"file_id": "F1"}}}
    return upload


def test_a_title_that_is_already_a_filename_survives_intact():
    assert safe_filename("Chapter 1.pdf") == "Chapter 1.pdf"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("TD n3 : arbres / listes.pdf", "TD n3 arbres listes.pdf"),
        ("a<b>c|d?e*f.pdf", "a b c d e f.pdf"),
        ("  spaced   out  .pdf", "spaced out.pdf"),
        ("..hidden.pdf", "hidden.pdf"),
        ("trailing dots...", "trailing dots"),
    ],
)
def test_illegal_characters_are_removed(raw, expected):
    assert safe_filename(raw) == expected


def test_a_newline_in_a_title_cannot_smuggle_a_header(tmp_path):
    """The name goes straight into Content-Disposition. A CR or LF would end
    that line early and let whatever follows be read as another header."""
    source = tmp_path / "d1.pdf"
    source.write_bytes(b"%PDF-1.4")

    body, _ = _multipart({}, source, 'x"\r\nX-Evil: 1.pdf')

    # The value between the quotes is the whole test: no quote to close the
    # attribute early, and no line break to start a header of its own.
    quoted = body.split(b'filename="')[1].split(b'"')[0]
    assert b"\r" not in quoted and b"\n" not in quoted
    assert quoted == b"x X-Evil 1.pdf"
    assert body.count(b"Content-Disposition") == 1


def test_a_windows_device_name_is_not_produced():
    assert safe_filename("CON.pdf") == "_CON.pdf"
    assert safe_filename("lpt3.txt") == "_lpt3.txt"


def test_an_empty_or_unusable_title_falls_back():
    assert safe_filename("") == "attachment"
    assert safe_filename("///") == "attachment"
    assert safe_filename("  ", fallback="d1.pdf") == "d1.pdf"


def test_a_very_long_title_is_capped_but_keeps_its_extension():
    name = safe_filename("x" * 400 + ".pdf")
    assert len(name) <= MAX_FILENAME
    assert name.endswith(".pdf")


def test_sanitising_twice_changes_nothing():
    once = safe_filename('a<b>"c.pdf')
    assert safe_filename(once) == once


def test_the_upload_is_named_by_the_caller_not_by_the_local_path(tmp_path):
    """The library is keyed by Drive id, so without this every lecture arrives
    as 11kqW48qFWWRMiWNUmOK69ZlkTQeKIgye.pdf and the phone becomes unusable."""
    source = tmp_path / "11kqW48qFWWRMiWNUmOK69ZlkTQeKIgye.pdf"
    source.write_bytes(b"%PDF-1.4")
    sent = []

    client = Telegram("123:ABC", 4242, transport=StubTransport(),
                      multipart_transport=uploader(sent), sleep=lambda _: None)
    client.send_document(source, caption="x", filename="Chapter 1.pdf")

    assert b'filename="Chapter 1.pdf"' in sent[0]
    assert b"11kqW48" not in sent[0]


def test_without_a_filename_the_local_name_is_still_used(tmp_path):
    source = tmp_path / "d1.pdf"
    source.write_bytes(b"%PDF-1.4")
    sent = []

    client = Telegram("123:ABC", 4242, transport=StubTransport(),
                      multipart_transport=uploader(sent), sleep=lambda _: None)
    client.send_document(source)

    assert b'filename="d1.pdf"' in sent[0]


def test_the_mime_type_comes_from_the_bytes_not_from_the_sent_name(tmp_path):
    """A Google Doc exported to PDF is sent as a PDF whatever it was called."""
    source = tmp_path / "d1.pdf"
    source.write_bytes(b"%PDF-1.4")
    sent = []

    client = Telegram("123:ABC", 4242, transport=StubTransport(),
                      multipart_transport=uploader(sent), sleep=lambda _: None)
    client.send_document(source, filename="Chapter 1.docx.pdf")

    assert b"Content-Type: application/pdf" in sent[0]


def test_an_oversized_file_names_the_title_in_its_error(tmp_path):
    source = tmp_path / "d1.pdf"
    source.write_bytes(b"x" * (51 * 1024 * 1024))
    client = Telegram("123:ABC", 4242, transport=StubTransport(), sleep=lambda _: None)

    with pytest.raises(TelegramError) as err:
        client.send_document(source, filename="Chapter 1.pdf")
    assert "Chapter 1.pdf" in str(err.value)
