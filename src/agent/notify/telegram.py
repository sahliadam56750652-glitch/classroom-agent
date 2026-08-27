"""Send messages through the Telegram Bot API over plain HTTP.

Sending is a POST with a JSON body, so this uses urllib from the standard
library rather than adding a dependency. python-telegram-bot is deliberately
absent: it is an async framework built for *receiving* -- polling for updates,
routing callback queries, running conversation handlers -- and none of that
exists yet. Phase 1.5 adds the bot that listens, and that is when an async
framework starts paying for itself. Until then it would be a large dependency
carried for a one-line HTTP call.

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
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
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
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0

# A 429 can name any delay it likes. Honour it, but refuse to block a cron run
# for an hour on one message.
MAX_RETRY_AFTER_SECONDS = 60.0


class TelegramError(Exception):
    """A send failed. The caller must not stamp notified_at."""


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


def _http_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON and decode the reply. Raises TelegramError on any failure."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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
        sleep: Callable[[float], None] = time.sleep,
        api_base: str = API_BASE,
    ):
        if not token:
            raise ValueError("a bot token is required")
        self.token = token
        self.chat_id = chat_id
        self._transport = transport or _http_post
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

    def send_long(self, text: str) -> list[dict[str, Any]]:
        """Split on logical boundaries and send each part in order.

        Stops at the first failure and raises. Parts already sent stay sent --
        which is why the caller stamps notified_at per message rather than once
        at the end.
        """
        return [self.send_message(part) for part in split_message(text)]

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with explicit backoff on 429 and 5xx, and never on anything else."""
        url = self._url(method)
        last: TelegramError | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._transport(url, payload)
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


def from_config(config, *, transport: Transport | None = None) -> Telegram:
    """Build a client, or raise a ConfigError naming the missing half."""
    token, chat_id = telegram_settings(config)
    return Telegram(token, chat_id, transport=transport)
