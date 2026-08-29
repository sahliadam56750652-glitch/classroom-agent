"""Send messages through the Telegram Bot API over plain HTTP.

Every call is a POST with a JSON body, so this uses urllib from the standard
library rather than adding a dependency.

Phase 3b added receiving. `get_updates` long-polls, and the gate routes what
comes back. python-telegram-bot was reconsidered at that point and rejected
again: what the gate needs is one long-poll call and a router over a handful of
one-letter verbs, which is a page of synchronous code against the client that
already exists here, and an async framework wrapped around a synchronous
sqlite3 store would mean two paradigms for one user.

HTML, never MarkdownV2. MarkdownV2 requires escaping `_ * [ ] ( ) ~ ` > # + -
= | { } . !` and course titles, filenames and lecture text here contain all of
those. One missed escape is a 400 from the API and a briefing that silently
never arrives. HTML needs three characters escaped (`&`, `<`, `>`), which
html.escape() does exactly, and Telegram ignores unknown tags rather than
rejecting the message.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..config import telegram_settings

API_BASE = "https://api.telegram.org"

# Telegram truncates nothing and explains nothing: a message one character over
# the limit comes back as a 400, so splitting is the caller's job, not the
# API's.
MESSAGE_LIMIT = 4096

# 429 carries its own retry_after and is not really an error; 5xx is Telegram
# having a bad minute. Everything else (400 malformed HTML, 401 bad token, 403
# blocked by the user) is a fact about the request that will not improve by
# being sent again.
# A bot may upload 50 MB. A lecture deck can exceed that, and the API answers
# with a 413 rather than anything about size, so the check happens here.
UPLOAD_LIMIT = 50 * 1024 * 1024

# Telegram truncates a caption at 1024 characters without saying so.
CAPTION_LIMIT = 1024

# How long an ordinary call may take. getUpdates asks for more, because it
# deliberately holds the connection open.
REQUEST_TIMEOUT_SECONDS = 30

# How long getUpdates holds the connection open waiting for something to
# happen. Long polling rather than a webhook: a webhook needs a public HTTPS
# endpoint, and this runs on a laptop today and a free-tier box tomorrow.
POLL_TIMEOUT_SECONDS = 30

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0

# A 429 can name any delay it likes. Honour it, but refuse to block a cron run
# for an hour on one message.
MAX_RETRY_AFTER_SECONDS = 60.0


class TelegramError(Exception):
    """A send failed. The caller must not stamp notified_at."""


class DocumentTooLarge(TelegramError):
    """The file is over the bot upload limit.

    Its own type because the answer is different from every other send failure:
    retrying will never help, and the caller should offer the Drive link
    instead of reporting that delivery failed.
    """


def escape(value: Any) -> str:
    """Escape one interpolated value for parse_mode=HTML.

    Every value that reaches a message goes through here. quote=False because
    quotes carry no meaning in Telegram's HTML subset outside of an attribute,
    and escaping them would put `&quot;` in front of me in a course title.
    """
    return html.escape("" if value is None else str(value), quote=False)


def link(url: str | None, label: str) -> str:
    """An <a> tag, or the bare escaped label when there is no URL.

    A missing alternateLink is normal -- a soft-deleted item keeps its row but
    a link to a deleted post is worse than no link -- so this degrades to text
    rather than emitting an anchor pointing nowhere.
    """
    if not url:
        return escape(label)
    # href goes in an attribute, so quotes genuinely have to be escaped here.
    return f'<a href="{html.escape(str(url), quote=True)}">{escape(label)}</a>'


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------

def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split text into messages under the limit, on logical boundaries only.

    Three levels, each tried before the next: blank-line boundaries (which is
    one course), then single newlines (which is one item), and only if a single
    line is somehow longer than a whole message, a hard cut. Never mid-sentence
    while any structural boundary is still available.
    """
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")

    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    blocks = [block.strip("\n") for block in text.split("\n\n")]
    return _pack([block for block in blocks if block], "\n\n", limit)


def _pack(pieces: Sequence[str], separator: str, limit: int) -> list[str]:
    """Greedily fill messages with whole pieces, splitting a piece only if it
    cannot fit in a message on its own."""
    messages: list[str] = []
    current = ""

    for piece in pieces:
        candidate = f"{current}{separator}{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            messages.append(current)
            current = ""

        if len(piece) <= limit:
            current = piece
            continue

        # This single piece is too big for one message. Drop a level: split it
        # per item, and only hard-cut a line that is itself oversized.
        chunks = _split_piece(piece, limit)
        messages.extend(chunks[:-1])
        current = chunks[-1]

    if current:
        messages.append(current)
    return messages


def _split_piece(piece: str, limit: int) -> list[str]:
    """One oversized block, split per line and then, as a last resort, hard."""
    lines: list[str] = []
    for line in piece.split("\n"):
        if len(line) <= limit:
            lines.append(line)
            continue
        # A single line longer than a whole message. Nothing structural is left
        # to split on, and dropping text would be worse than a blunt cut.
        lines.extend(line[i : i + limit] for i in range(0, len(line), limit))
    return _pack(lines, "\n", limit)


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

# (url, payload) -> decoded JSON response. Swapped out wholesale in tests so
# that no test can reach the network even by accident.
Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


def _http_post(
    url: str, payload: dict[str, Any], timeout: float = REQUEST_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """POST JSON and decode the reply. Raises TelegramError on any failure."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        # The error body carries the description and, on a 429, retry_after.
        # It is the useful part of the failure, so it must not be discarded.
        raw = err.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {}
        raise _http_error(err.code, decoded, raw) from err
    except urllib.error.URLError as err:
        raise TelegramError(f"could not reach the Telegram API: {err.reason}") from err
    except json.JSONDecodeError as err:
        raise TelegramError(f"Telegram returned a body that is not JSON: {err}") from err


MultipartTransport = Callable[[str, bytes, str], dict[str, Any]]

# Illegal on Windows, and `/` would make the receiving end read a path where a
# name was meant. Control characters are here for a second reason: a CR or LF
# in a filename would end the Content-Disposition line early and let the rest
# of the name be read as another header.
_ILLEGAL_IN_FILENAMES = set('<>:"/\\|?*') | {chr(code) for code in range(32)}

# CON.pdf is not a file you can save on Windows. Vanishingly unlikely from a
# Drive title, and three lines to be sure of.
_RESERVED_STEMS = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{digit}" for prefix in ("com", "lpt") for digit in range(1, 10)
}

# Well inside every filesystem's limit, and short enough to read on a phone.
MAX_FILENAME = 120


def safe_filename(name: str, *, fallback: str = "attachment") -> str:
    """A Drive title turned into something a phone can actually save.

    Applied by _multipart to whatever it is handed, so a caller cannot break
    the multipart headers with a quote or a newline however careless it is.
    Idempotent, so composing a name and then sanitising it again is free.
    """
    cleaned = "".join(
        " " if char in _ILLEGAL_IN_FILENAMES else char for char in str(name or "")
    )
    # Leading dots hide the file; trailing dots and spaces are silently dropped
    # by Windows, which turns "Chapter 1." into a name that does not round-trip.
    cleaned = " ".join(cleaned.split()).strip(". ")
    if not cleaned:
        return fallback

    stem, dot, suffix = cleaned.rpartition(".")
    if not dot:
        stem, suffix = cleaned, ""
    if stem.lower() in _RESERVED_STEMS:
        stem = f"_{stem}"

    room = MAX_FILENAME - len(suffix) - (1 if suffix else 0)
    if room < 1:
        # A pathological "name" that is all extension. Keep the front of it.
        return cleaned[:MAX_FILENAME]
    stem = stem[:room].rstrip(". ") or fallback
    return f"{stem}.{suffix}" if suffix else stem


def _multipart(
    fields: dict[str, Any], path: Path, filename: str | None = None
) -> tuple[bytes, str]:
    """Build one multipart/form-data body carrying a file and some fields.

    Written out rather than pulled from a library because it is the only
    multipart request in the project and the alternative is a dependency for
    thirty lines. Non-string field values are JSON-encoded, which is what the
    Bot API expects for reply_markup on a multipart call -- passing a dict
    unencoded here is a 400 that complains about the wrong thing.
    """
    boundary = f"----classroom-agent-{uuid.uuid4().hex}"
    marker = f"--{boundary}".encode()
    parts: list[bytes] = []

    for name, value in fields.items():
        encoded = value if isinstance(value, str) else json.dumps(value)
        parts.append(
            marker
            + b"\r\n"
            + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + str(encoded).encode("utf-8")
            + b"\r\n"
        )

    # The mime type comes from the bytes on disk, the name from the caller: the
    # local file is `files/<drive id>.pdf` and the name I want to see on the
    # phone is "Chapter 1.pdf".
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    sent_as = safe_filename(filename or os.path.basename(path.name))
    parts.append(
        marker
        + b"\r\n"
        + f'Content-Disposition: form-data; name="document"; '
          f'filename="{sent_as}"\r\n'.encode()
        + f"Content-Type: {mime}\r\n\r\n".encode()
        + path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _http_post_multipart(url: str, body: bytes, content_type: str) -> dict[str, Any]:
    """POST an already-built multipart body. Same failure taxonomy as above."""
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    try:
        # Uploads are slower than a text message by orders of magnitude.
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS * 4) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {}
        raise _http_error(err.code, decoded, raw) from err
    except urllib.error.URLError as err:
        raise TelegramError(f"could not reach the Telegram API: {err.reason}") from err
    except json.JSONDecodeError as err:
        raise TelegramError(f"Telegram returned a body that is not JSON: {err}") from err


class _ApiError(TelegramError):
    """An HTTP-level failure, carrying what the retry loop needs to decide."""

    def __init__(self, message: str, *, status: int, retry_after: float | None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _http_error(status: int, decoded: dict[str, Any], raw: str) -> _ApiError:
    description = decoded.get("description") or raw.strip() or "no description"
    parameters = decoded.get("parameters") or {}
    retry_after = parameters.get("retry_after")
    return _ApiError(
        f"Telegram API returned {status}: {description}",
        status=status,
        retry_after=float(retry_after) if retry_after is not None else None,
    )


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------

class Telegram:
    """A bot token and a chat id, and the two verbs this phase needs.

    Synchronous on purpose. `agent notify` runs from cron, sends a handful of
    messages and exits; there is nothing to await it.
    """

    def __init__(
        self,
        token: str,
        chat_id: int,
        *,
        transport: Transport | None = None,
        multipart_transport: MultipartTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        api_base: str = API_BASE,
    ):
        if not token:
            raise ValueError("a bot token is required")
        self.token = token
        self.chat_id = chat_id
        self._transport = transport or _http_post
        # Separate from the JSON transport so a test can stub uploads without
        # also having to understand multipart bodies.
        self._multipart_transport = multipart_transport or _http_post_multipart
        self._sleep = sleep
        self._api_base = api_base

    def _url(self, method: str) -> str:
        return f"{self._api_base}/bot{self.token}/{method}"

    def send_message(self, text: str) -> dict[str, Any]:
        """Send one message as HTML, retrying 429 and 5xx.

        Callers escape their own interpolated values -- see escape() and
        link(). This method cannot do it for them: by the time the text arrives
        the tags and the content are one string.
        """
        if len(text) > MESSAGE_LIMIT:
            # Telegram would answer 400 with a description that does not
            # mention length. Fail here, where the cause is obvious.
            raise TelegramError(
                f"message is {len(text)} characters, over the {MESSAGE_LIMIT} "
                f"limit. Route it through send_long()."
            )

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            # Link previews turn a digest of six linked assignments into six
            # unfurled cards and bury the text.
            "link_preview_options": {"is_disabled": True},
        }
        return self._request("sendMessage", payload)

    def send_with_keyboard(
        self, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one message carrying inline buttons, and return the result.

        Separate from send_message because the caller needs the message_id back
        to edit it later, and because a keyboard cannot be split across
        messages -- an over-long text here is a composition bug, not something
        to paper over by sending two.
        """
        if len(text) > MESSAGE_LIMIT:
            raise TelegramError(
                f"message is {len(text)} characters, over the {MESSAGE_LIMIT} "
                f"limit, and a keyboard cannot be split across messages. "
                f"Shorten it before sending."
            )
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._request("sendMessage", payload)

    def edit_message_text(
        self,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Rewrite a message in place.

        Editing rather than sending keeps the interaction one coherent thing on
        screen instead of a growing column of near-identical messages.

        Telegram answers an edit that would change nothing with 400 "message is
        not modified". That is a success as far as the caller is concerned --
        the message already says what we wanted -- so it is swallowed here
        rather than making every caller know about it.
        """
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        # Always sent, so that clearing a keyboard is possible: omitting it
        # leaves the old buttons live on a message that has moved on.
        payload["reply_markup"] = reply_markup or {"inline_keyboard": []}
        try:
            return self._request("editMessageText", payload)
        except _ApiError as err:
            if err.status == 400 and "not modified" in str(err).lower():
                return {"ok": True, "result": {"message_id": message_id}}
            raise

    def answer_callback_query(
        self, callback_id: str, text: str = "", *, alert: bool = False
    ) -> dict[str, Any]:
        """Acknowledge a button press. Called for every one of them, always.

        Skipping it leaves the button spinning on the phone until Telegram
        times it out, which reads as a broken bot even when the work succeeded.
        Failures here are swallowed for the same reason: losing the
        acknowledgement of an action that already happened must not make the
        action look like it failed.
        """
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            # Telegram caps this at 200 characters.
            payload["text"] = text[:200]
        if alert:
            payload["show_alert"] = True
        try:
            return self._request("answerCallbackQuery", payload)
        except TelegramError:
            return {"ok": False}

    def get_updates(
        self, offset: int, *, timeout: int = POLL_TIMEOUT_SECONDS
    ) -> list[dict[str, Any]]:
        """Long-poll for updates from `offset` onward.

        `offset` is also the acknowledgement: asking for N tells Telegram
        everything below N was handled, and it stops re-offering them. Updates
        it still holds are kept for 24 hours, which is what lets a button
        pressed while the bot was down still arrive when it comes back -- as
        long as the offset itself survived the restart, which is why it lives
        in bot_state and not in a variable.

        Only the update kinds the gate acts on are requested. Anything else
        would be fetched, acknowledged and dropped.
        """
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["callback_query", "message"],
        }
        # The socket has to outlast the long poll itself, or every quiet
        # 30-second window would surface as a timeout and a reconnect.
        response = self._request(
            "getUpdates", payload, read_timeout=timeout + REQUEST_TIMEOUT_SECONDS
        )
        result = response.get("result")
        return result if isinstance(result, list) else []

    def send_document(
        self,
        path: Path,
        *,
        caption: str = "",
        file_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Send a file, by cached id when we have one and by bytes otherwise.

        A file_id costs no upload and sidesteps the size ceiling entirely on
        every send after the first, so it is always preferred. The caller
        stores what comes back.

        `filename` is what the file is called on the phone. It matters more
        than it looks: the local library is keyed by Drive id, so without it
        every lecture arrives as 11kqW48qFWWRMiWNUmOK69ZlkTQeKIgye.pdf and the
        phone becomes a folder of files that cannot be told apart. Note that a
        file_id carries the name it was uploaded with, so renaming means
        forgetting the id and sending the bytes again.
        """
        payload: dict[str, Any] = {"chat_id": self.chat_id}
        if caption:
            payload["caption"] = caption[:CAPTION_LIMIT]
            payload["parse_mode"] = "HTML"
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        if file_id:
            payload["document"] = file_id
            return self._request("sendDocument", payload)

        size = path.stat().st_size
        if size > UPLOAD_LIMIT:
            raise DocumentTooLarge(
                f"{filename or path.name} is {size / (1024 * 1024):.0f} MB, over "
                f"Telegram's {UPLOAD_LIMIT // (1024 * 1024)} MB upload limit"
            )
        return self._upload(path, payload, filename)

    def _upload(
        self, path: Path, fields: dict[str, Any], filename: str | None = None
    ) -> dict[str, Any]:
        """One multipart POST. Retried on the same statuses as everything else."""
        url = self._url("sendDocument")
        last: TelegramError | None = None

        for attempt in range(MAX_ATTEMPTS):
            body, content_type = _multipart(fields, path, filename)
            try:
                response = self._multipart_transport(url, body, content_type)
            except _ApiError as err:
                if err.status not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS - 1:
                    raise
                last = err
                self._sleep(self._delay(err, attempt))
                continue
            if not response.get("ok", False):
                raise TelegramError(
                    f"Telegram rejected the upload: "
                    f"{response.get('description') or response}"
                )
            return response

        raise TelegramError(f"giving up after {MAX_ATTEMPTS} attempts: {last}")

    def send_long(self, text: str) -> list[dict[str, Any]]:
        """Split on logical boundaries and send each part in order.

        Stops at the first failure and raises. Parts already sent stay sent --
        which is why the caller stamps notified_at per message rather than once
        at the end.
        """
        return [self.send_message(part) for part in split_message(text)]

    def _request(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST with explicit backoff on 429 and 5xx, and never on anything else.

        `read_timeout` is only passed on when a caller asks for one, so a
        two-argument transport -- which is every stub in the test suite --
        keeps working unchanged.
        """
        url = self._url(method)
        last: TelegramError | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = (
                    self._transport(url, payload)
                    if read_timeout is None
                    else self._transport(url, payload, read_timeout)
                )
            except _ApiError as err:
                if err.status not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS - 1:
                    raise
                last = err
                self._sleep(self._delay(err, attempt))
                continue

            # HTTP 200 with ok=false happens, and it is still a failure.
            if not response.get("ok", False):
                raise TelegramError(
                    f"Telegram rejected the request: "
                    f"{response.get('description') or response}"
                )
            return response

        # Only reachable if MAX_ATTEMPTS is misconfigured to zero.
        raise TelegramError(f"giving up after {MAX_ATTEMPTS} attempts: {last}")

    def _delay(self, err: _ApiError, attempt: int) -> float:
        """How long to wait. A 429 says so itself; a 5xx gets exponential backoff."""
        if err.retry_after is not None:
            return min(max(err.retry_after, 0.0), MAX_RETRY_AFTER_SECONDS)
        return BACKOFF_BASE_SECONDS * (2**attempt)


def from_config(
    config,
    *,
    transport: Transport | None = None,
    multipart_transport: MultipartTransport | None = None,
) -> Telegram:
    """Build a client, or raise a ConfigError naming the missing half."""
    token, chat_id = telegram_settings(config)
    return Telegram(
        token, chat_id, transport=transport, multipart_transport=multipart_transport
    )
