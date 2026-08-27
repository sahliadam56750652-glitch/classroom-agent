"""Compose pending events into the HTML of one briefing.

Templated, deterministic, and free of any LLM. Phase 3 may rewrite the wording,
but the structure and the delivery have to work without a model: an LLM outage
must degrade the briefing to a plain template, never suppress it, so the plain
template is what exists first and stays the fallback.

Ordering is by urgency, not chronology. A grade posted this morning matters
less than an assignment due in three hours, and a digest sorted by timestamp
buries the second under the first.

Silence is a valid output. compose() returns None for an empty event list and
nothing is sent -- a bot that says "nothing new today" twice a day trains me to
swipe it away without looking, and then the one message that mattered goes with
it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..classroom.models import ISO_FORMAT
from ..notify.telegram import escape, link

# Urgency, most urgent first. Every event type the differ and the deadline
# scanner can produce appears here; anything unlisted sorts last rather than
# vanishing, because an event type nobody thought about is still news.
#
# due_date_changed sits with the deadlines rather than with the edits it is
# technically one of: a deadline that moved is deadline news, and burying it
# under "someone edited a description" is how a rescheduled hand-in gets missed.
URGENCY: tuple[str, ...] = (
    "deadline_t3",
    "deadline_t24",
    "deadline_t72",
    "due_date_changed",
    "grade_posted",
    "grade_changed",
    "submission_state_changed",
    "new_coursework",
    "new_material",
    "new_announcement",
    "coursework_updated",
    "material_updated",
    "announcement_updated",
)

_RANK = {event_type: index for index, event_type in enumerate(URGENCY)}
_UNRANKED = len(URGENCY)

HOUR_LABELS = {72: "72 h", 24: "24 h", 3: "3 h"}


@dataclass(frozen=True)
class Block:
    """One course's section of the briefing, and the events it accounts for.

    The event ids travel with the text so delivery can stamp exactly what it
    managed to send, rather than stamping the whole digest and losing whatever
    a failure halfway through never delivered.
    """

    course_name: str
    html: str
    event_ids: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------
# reading rows
# --------------------------------------------------------------------------

def _payload(row: Any) -> dict[str, Any]:
    """The event payload as a dict, whether it arrived as JSON text or a dict.

    The store hands back the raw JSON column; tests build rows directly. Both
    are accepted so the composer can be tested without a database.
    """
    raw = row["payload"]
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column that may not exist on a hand-built test row."""
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _local(value: str | None, tz: ZoneInfo | timezone) -> str:
    """Render a stored UTC timestamp in the display timezone.

    Stored timestamps are UTC; this is the only place they become local. A
    value that will not parse is shown verbatim rather than dropped -- a
    strange-looking date is far better than an item that silently disappears
    from the briefing.
    """
    if not value:
        return ""
    try:
        moment = datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(value)
    return moment.astimezone(tz).strftime("%a %d %b %H:%M")


def display_zone(name: str) -> ZoneInfo | timezone:
    """The configured zone, or UTC if the platform cannot resolve it.

    Windows ships no IANA database. The tzdata package is a dependency for
    exactly that reason, but if it is somehow missing, the briefing goes out
    with UTC times rather than not going out at all.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


# --------------------------------------------------------------------------
# one line per event
# --------------------------------------------------------------------------

def _title(row: Any, payload: dict[str, Any]) -> str:
    """The best human label available for the thing this event is about."""
    title = payload.get("title")
    if title:
        return str(title)

    # Announcements have no title, only a body. First line, trimmed.
    text = payload.get("text")
    if text:
        first = str(text).strip().splitlines()[0].strip()
        if len(first) > 80:
            first = first[:77].rstrip() + "..."
        if first:
            return first

    return f"(untitled {str(_get(row, 'entity_type', 'item')).replace('_', ' ')})"


def _url(row: Any, payload: dict[str, Any], links: dict[tuple[str, str], str]) -> str | None:
    """alternateLink for this event's entity, from the payload or the lookup."""
    from_payload = payload.get("alternate_link")
    if from_payload:
        return str(from_payload)
    key = (str(_get(row, "entity_type", "")), str(_get(row, "entity_id", "")))
    return links.get(key)


def _grade(value: Any) -> str:
    """Format a grade without a trailing .0 on whole marks."""
    if value is None:
        return "?"
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def render_line(row: Any, *, tz: ZoneInfo | timezone, links: dict[tuple[str, str], str]) -> str:
    """One event as one line of HTML. Every interpolated value is escaped."""
    payload = _payload(row)
    event_type = str(_get(row, "type", ""))
    label = link(_url(row, payload, links), _title(row, payload))

    if event_type.startswith("deadline_t"):
        hours = payload.get("hours_before")
        window = HOUR_LABELS.get(hours, f"{escape(hours)} h") if hours else "soon"
        due = _local(payload.get("due_at"), tz)
        return f"⏰ due in under {window} — {label} · {escape(due)}"

    if event_type == "due_date_changed":
        before = _local(payload.get("before"), tz) or "no date"
        after = _local(payload.get("after"), tz) or "no date"
        return f"📅 due date moved — {label} · {escape(before)} → {escape(after)}"

    if event_type == "grade_posted":
        return f"📊 graded {escape(_grade(payload.get('after')))} — {label}"

    if event_type == "grade_changed":
        before = _grade(payload.get("before"))
        after = _grade(payload.get("after"))
        return f"📊 regraded {escape(before)} → {escape(after)} — {label}"

    if event_type == "submission_state_changed":
        before = str(payload.get("before") or "?")
        after = str(payload.get("after") or "?")
        return f"✅ submission {escape(before)} → {escape(after)} — {label}"

    if event_type in {"new_coursework", "new_material", "new_announcement"}:
        heading = {
            "new_coursework": "🆕 new assignment",
            "new_material": "🆕 new material",
            "new_announcement": "🆕 announcement",
        }[event_type]
        line = f"{heading} — {label}"
        count = payload.get("attachment_count") or 0
        if count:
            line += f" · {escape(count)} file{'s' if count != 1 else ''}"
        if event_type == "new_coursework" and payload.get("due_at"):
            line += f" · due {escape(_local(payload.get('due_at'), tz))}"
        return line

    if event_type in {"coursework_updated", "material_updated", "announcement_updated"}:
        return _render_update(event_type, payload, label)

    # An event type nobody has written a template for. Say something rather
    # than dropping it -- a silently missing event is the failure mode this
    # whole project exists to avoid.
    return f"• {escape(event_type.replace('_', ' '))} — {label}"


def _render_update(event_type: str, payload: dict[str, Any], label: str) -> str:
    """An edit to something already seen.

    The branch that matters: files arriving on an existing post is a main way
    content actually reaches me -- announcements alone carry 211 of 375
    measured attachments -- so it reads as real news. A reworded sentence is a
    one-liner and deliberately says nothing more.
    """
    if payload.get("attachments_added"):
        added = payload.get("added_count") or 0
        plural = "s" if added != 1 else ""
        return f"📎 {escape(added)} new file{plural} added to {label}"

    removed = payload.get("removed_count") or 0
    if removed and not payload.get("text_changed"):
        plural = "s" if removed != 1 else ""
        return f"✂️ {escape(removed)} file{plural} removed from {label}"

    what = {
        "coursework_updated": "assignment",
        "material_updated": "material",
        "announcement_updated": "announcement",
    }[event_type]
    return f"✏️ {escape(what)} edited — {label}"


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------

def _sort_key(row: Any) -> tuple[int, str, int]:
    """Urgency first, then oldest first within a rank, then id for stability."""
    event_type = str(_get(row, "type", ""))
    return (
        _RANK.get(event_type, _UNRANKED),
        str(_get(row, "created_at", "")),
        int(_get(row, "id", 0) or 0),
    )


def compose_blocks(
    events,
    *,
    timezone_name: str = "UTC",
    links: dict[tuple[str, str], str] | None = None,
) -> list[Block]:
    """Group events into one block per course, courses ordered by urgency.

    A course is as urgent as the most urgent thing in it, so the subject with
    an assignment due in three hours leads the briefing.
    """
    rows = list(events)
    if not rows:
        return []

    tz = display_zone(timezone_name)
    links = links or {}

    grouped: dict[str, list[Any]] = {}
    for row in rows:
        name = str(
            _get(row, "course_name") or _get(row, "course_id") or "Unknown course"
        )
        grouped.setdefault(name, []).append(row)

    blocks: list[Block] = []
    for name, course_rows in grouped.items():
        course_rows.sort(key=_sort_key)
        lines = [f"<b>{escape(name)}</b>"]
        lines.extend(render_line(row, tz=tz, links=links) for row in course_rows)
        blocks.append(
            Block(
                course_name=name,
                html="\n".join(lines),
                event_ids=[int(_get(row, "id", 0) or 0) for row in course_rows],
            )
        )

    # Each group is already sorted, so its first event is its most urgent.
    blocks.sort(key=lambda block: _sort_key(grouped[block.course_name][0]))
    return blocks


def compose(
    events,
    *,
    timezone_name: str = "UTC",
    links: dict[tuple[str, str], str] | None = None,
) -> str | None:
    """The whole briefing as one HTML string, or None when there is nothing.

    None is the correct output for a quiet day. Callers must not turn it into
    a cheerful "nothing new" message.
    """
    blocks = compose_blocks(events, timezone_name=timezone_name, links=links)
    if not blocks:
        return None
    # Blank line between courses: the boundary split_message() prefers to break
    # on, so a long digest splits between subjects rather than inside one.
    return "\n\n".join(block.html for block in blocks)
