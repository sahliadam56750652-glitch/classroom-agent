"""The part that listens: long-poll getUpdates, and a router over the buttons.

Restart safety is a property of the shape here rather than something this
module implements. Every button carries only ids, every piece of state is a
row, and the message with its buttons survives on my phone regardless of what
the process does -- so there is no conversation to resume, no in-memory session
to rebuild, and no resume path that could be subtly wrong. Killing the bot
mid-interaction and starting it again loses nothing, because there was never
anything in the process to lose.

The one thing that genuinely has to be persisted is the getUpdates offset.
Telegram holds an unacknowledged update for 24 hours and re-offers it, so a bot
that forgot where it was would either replay every button I pressed yesterday
or miss the one I pressed while it was down. It lives in `bot_state`.

Every handler is idempotent, and that is not defensive programming: Telegram
redelivers, and an old message keeps working buttons forever. A second tap on
"I've read it" must not move the timestamp, and a tap on a run that has already
been skipped must say so rather than skipping it again.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..db import store
from ..llm import provider as llm
from ..notify.telegram import DocumentTooLarge, Telegram, TelegramError
from . import messages, quiz
from .scheduler import Item, Subject, item_by_id, items_for, stored_subjects

OFFSET_KEY = "last_update_id"

SNOOZE = timedelta(hours=2)

# A snooze must not step over the thing it was preparing me for. Past this hour
# on the morning of the gated day, snoozing stops being a delay and becomes a
# silent skip -- and a silent skip is the one outcome this whole phase exists
# to prevent.
SNOOZE_CUTOFF_HOUR = 7

# One post can carry a whole term of handouts. Four is what fits on a phone
# screen before the documents bury the message that explains them; the rest are
# named with links.
MAX_DOCUMENTS = 4


@dataclass
class Handled:
    """What one update did, for the run summary and for tests."""

    kind: str = "ignored"
    detail: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------

def deliver_item(
    conn: sqlite3.Connection,
    config: Config,
    telegram: Telegram,
    *,
    run_id: int,
    subject: Subject,
    item: Item,
    now: str | None = None,
) -> Handled:
    """Send one post's files and its message, then mark it delivered.

    Send first, stamp second, in that order and for the same reason
    notify/dispatch.py does it: a failure has to leave the item exactly as it
    was so the next tap tries again. Delivered-but-never-sent is a lie the
    coverage figure would inherit.

    A file too large to upload is named with its Drive link rather than
    treated as a failure -- the point is that I can get at the lecture, and a
    link achieves that.
    """
    stamp = now or _now_iso()
    rows = store.study_item_files(conn, item.entity_type, item.entity_id)

    sendable = [row for row in rows if row["status"] == "ok" and row["local_path"]]
    notes: list[str] = []

    for row in sendable[:MAX_DOCUMENTS]:
        drive_id = str(row["drive_id"])
        title = str(row["title"] or drive_id)
        path = Path(config.library_dir) / str(row["local_path"])
        caption = messages.document_caption(title, row["pages"])
        filename = messages.document_filename(title, path)
        try:
            response = _send_one(conn, telegram, row, path, caption, filename)
        except DocumentTooLarge:
            notes.append(messages.too_large_line(title, row["url"], int(row["size_bytes"] or 0)))
            continue
        except OSError as err:
            # The row says the bytes are on disk and the disk disagrees. Named
            # rather than dropped: a lecture that silently does not arrive is
            # worse than one that arrives as a link.
            notes.append(f"📎 {messages.link(row['url'], title)} — not on disk ({err.strerror})")
            continue
        _remember(conn, drive_id, response)

    for row in sendable[MAX_DOCUMENTS:]:
        notes.append(f"📎 {messages.link(row['url'], str(row['title'] or row['drive_id']))}")
    for row in rows:
        if row["status"] in ("trashed", "missing"):
            notes.append(
                f"📎 {messages.escape(row['title'] or row['drive_id'])} — "
                f"no longer in Drive"
            )

    remaining = max(len(subject.items) - 1, 0)
    text = messages.item_message(subject, item, remaining=remaining)
    if notes:
        text = text + "\n\n" + "\n".join(notes)

    telegram.send_with_keyboard(text, messages.item_keyboard(run_id, item))

    # Only now. Everything above could have failed and left the item untouched.
    store.advance_study_item(conn, item.item_id, "delivered", now=stamp)
    conn.commit()
    return Handled("delivered", f"{subject.name}: {item.label}")


def _send_one(
    conn, telegram, row, path: Path, caption: str, filename: str
) -> dict[str, Any]:
    """Send by cached file id, falling back to the bytes if Telegram refuses it.

    A stored id can go stale. When it does the answer is to forget it and
    upload again, not to report that the lecture could not be delivered.

    The filename only applies to the upload. A file_id keeps the name it was
    first sent under, which is the price of the cache -- and the reason the
    schema note on telegram_files says to clear the table if the naming rule
    ever changes.
    """
    file_id = row["file_id"]
    if file_id:
        try:
            return telegram.send_document(path, caption=caption, file_id=str(file_id))
        except TelegramError:
            store.forget_telegram_file(conn, str(row["drive_id"]))
    return telegram.send_document(path, caption=caption, filename=filename)


def _remember(conn, drive_id: str, response: dict[str, Any]) -> None:
    document = ((response or {}).get("result") or {}).get("document") or {}
    file_id = document.get("file_id")
    if file_id:
        store.remember_telegram_file(
            conn,
            drive_id=drive_id,
            file_id=str(file_id),
            file_size=document.get("file_size"),
        )


# --------------------------------------------------------------------------
# the quiz
# --------------------------------------------------------------------------

def _subject_of(conn, course_id: str) -> Subject:
    """A Subject wrapper for one item's course, for the messages that need a name."""
    course = store.get_course(conn, course_id)
    return Subject(
        name=str(course["name"]) if course else course_id,
        course_id=course_id,
        course_name=str(course["name"]) if course else course_id,
        sessions=(),
        items=items_for(conn, course_id),
    )


def _show_question(conn, telegram: Telegram, attempt: quiz.Attempt) -> None:
    """Put the current question on screen, editing in place after the first.

    The message_id is stored the moment Telegram gives it back, before any
    answer can arrive. A quiz that lost track of its own message would start a
    second column of questions on the next tap.
    """
    question = attempt.current
    if question is None:
        return
    text = messages.question_message(attempt, question)
    keyboard = messages.question_keyboard(attempt, question)

    if attempt.message_id:
        telegram.edit_message_text(int(attempt.message_id), text, keyboard)
        return

    response = telegram.send_with_keyboard(text, keyboard)
    message_id = ((response or {}).get("result") or {}).get("message_id")
    if message_id:
        attempt.message_id = int(message_id)
        store.update_quiz_attempt(conn, attempt.attempt_id, state=attempt.to_json())
        conn.commit()


def _finish_quiz(conn, telegram: Telegram, attempt: quiz.Attempt, *, now: str) -> Handled:
    """Grade, promote if it passed, and say what was missed if it did not."""
    result = quiz.settle(conn, attempt, now=now)

    item = store.get_study_item(conn, attempt.item_id)
    course_id = str(item["course_id"]) if item is not None else ""
    remaining = items_for(conn, course_id) if course_id else ()
    course = store.get_course(conn, course_id) if course_id else None

    text = messages.result_message(
        result,
        remaining=len(remaining),
        subject=str(course["name"]) if course else "",
    )
    keyboard = messages.result_keyboard(
        result, next_item=remaining[0] if remaining else None
    )
    if attempt.message_id:
        telegram.edit_message_text(int(attempt.message_id), text, keyboard)
    else:
        telegram.send_with_keyboard(text, keyboard)

    return Handled(
        "verified" if result.passed else "failed",
        f"{result.correct}/{result.counted} on item {attempt.item_id}",
    )


def start_quiz(
    conn: sqlite3.Connection,
    config: Config,
    telegram: Telegram,
    *,
    run_id: int,
    item: Item,
    course_id: str,
    provider: llm.LLMProvider | None = None,
    now: str | None = None,
) -> Handled:
    """Generate or resume a quiz on one item, and show its current question.

    Every failure here leaves the item exactly where it was -- `reviewed` --
    and says which failure it was. Invariant 4 in its most literal form: the
    model writes the questions and nothing else, so a model that cannot be
    reached costs the quiz and not the briefing.
    """
    stamp = now or _now_iso()
    course = store.get_course(conn, course_id)

    try:
        attempt, _ = quiz.begin(
            conn, config, item,
            run_id=run_id,
            course=str(course["name"]) if course else "",
            provider=provider,
            now=stamp,
        )
    except quiz.QuizUnavailable as err:
        # Reviewed, if it was delivered. The lecture arrived and I opened it;
        # the only thing missing is the verification, and saying so plainly is
        # the difference between a degraded feature and a broken one. On an
        # item that was never delivered this call is a no-op, which is right --
        # nothing has happened to it.
        store.advance_study_item(conn, item.item_id, "reviewed", now=stamp)
        conn.commit()
        telegram.send_with_keyboard(
            messages.no_quiz_line(str(err), err.kind),
            messages.item_keyboard(run_id, item),
        )
        return Handled("no-quiz", err.kind)

    if attempt.complete:
        # An attempt that was answered to the end but never settled -- the
        # process died between the last answer and the result. Finish it.
        return _finish_quiz(conn, telegram, attempt, now=stamp)

    _show_question(conn, telegram, attempt)
    return Handled("quiz", f"item {item.item_id}, question {attempt.index + 1}")


def _handle_quiz(conn, config, telegram, parts, ack, *, provider, stamp) -> Handled:
    """The q: verbs. Every one of them is idempotent by re-reading the row."""
    attempt_id = int(parts[0]) if parts[0].isdigit() else 0
    row = store.get_quiz_attempt(conn, attempt_id)
    if row is None:
        ack("That quiz is no longer on file.")
        return Handled("stale", f"quiz {attempt_id}")
    attempt = quiz.attempt_from_row(row)

    # Two parts: a verb about the attempt as a whole.
    if len(parts) == 2:
        verb = parts[1]
        if verb == "n":
            item = item_by_id(conn, attempt.item_id)
            if item is None:
                ack("That item is no longer on file.")
                return Handled("stale", f"item {attempt.item_id}")
            stored = store.get_study_item(conn, attempt.item_id)
            ack("Fresh questions coming…")
            return start_quiz(
                conn, config, telegram,
                run_id=attempt.run_id, item=item,
                course_id=str(stored["course_id"]) if stored else "",
                provider=provider, now=stamp,
            )
        if verb == "b":
            flagged = quiz.flag_whole_set(conn, attempt, now=stamp)
            ack(
                f"Flagged {flagged} question(s). The next try generates a new set."
                if flagged
                else "Already flagged."
            )
            return Handled("flagged-set", f"{flagged} question(s)")
        ack("I don't recognise that button.")
        return Handled("unknown", verb)

    index = int(parts[1]) if parts[1].isdigit() else -1
    verb = parts[2]

    if verb == "f":
        if not quiz.record_flag(conn, attempt, index, now=stamp):
            ack("Already flagged.")
            return Handled("repeat", f"quiz {attempt_id} q{index}")
        ack("Flagged. It will not count, and the set will be regenerated.")
        if attempt.complete:
            return _finish_quiz(conn, telegram, attempt, now=stamp)
        _show_question(conn, telegram, attempt)
        return Handled("flagged", f"quiz {attempt_id} q{index}")

    if verb.isdigit():
        if row["finished_at"]:
            ack("That quiz is already finished.")
            return Handled("repeat", f"quiz {attempt_id}")
        if not quiz.record_answer(conn, attempt, index, int(verb)):
            ack("Already answered.")
            return Handled("repeat", f"quiz {attempt_id} q{index}")
        ack()
        if attempt.complete:
            return _finish_quiz(conn, telegram, attempt, now=stamp)
        _show_question(conn, telegram, attempt)
        return Handled("answered", f"quiz {attempt_id} q{index}")

    ack("I don't recognise that button.")
    return Handled("unknown", verb)


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

def _plan_of(conn, run_id: int):
    """Rebuild the subject list a stored run was sent with.

    Read back from the row rather than recomputed from today's backlog,
    because callback_data carries an index into the order the buttons were
    built in. Recomputing could hand a tap to a different subject entirely.
    """
    run = store.get_gate_run(conn, run_id)
    if run is None:
        return None, []
    return run, stored_subjects(str(run["plan"]))


def _subject_from_stored(conn, entry: dict[str, Any]) -> tuple[Subject, Item] | None:
    """Turn one stored subject entry back into its current oldest item.

    The backlog is re-read rather than taken from the ids on the run: something
    may have been skipped or reviewed since last night, and serving a lecture I
    have already dealt with would make the gate feel broken.
    """
    course_id = entry.get("course_id")
    if not course_id:
        return None
    items = items_for(conn, str(course_id))
    if not items:
        return None
    subject = Subject(
        name=str(entry.get("name") or course_id),
        course_id=str(course_id),
        course_name=str(entry.get("course_name") or ""),
        sessions=(),
        items=items,
    )
    return subject, items[0]


def _snooze_until(run, now: datetime, tz) -> tuple[str | None, str]:
    """When to come back, or why not to.

    Refusing to snooze past the morning of the gated day is the honest part.
    Two hours at a time from an evening prompt reaches 08:00 in five taps, and
    a snooze that outlives the lecture is a skip nobody recorded.
    """
    target = now + SNOOZE
    for_date = datetime.strptime(str(run["for_date"]), "%Y-%m-%d").date()
    cutoff = datetime.combine(
        for_date, datetime.min.time().replace(hour=SNOOZE_CUTOFF_HOUR), tzinfo=tz
    ).astimezone(timezone.utc)
    if target >= cutoff:
        return None, (
            "That would push this past the morning of the day it is for. "
            "Start it or skip it — a snooze that outlives the lecture is a "
            "skip nobody recorded."
        )
    return target.strftime("%Y-%m-%dT%H:%M:%SZ"), ""


def handle_callback(
    conn: sqlite3.Connection,
    config: Config,
    telegram: Telegram,
    query: dict[str, Any],
    *,
    tz,
    now: str | None = None,
    provider: llm.LLMProvider | None = None,
) -> Handled:
    """One button press. Always acknowledged, whatever else happens."""
    callback_id = str(query.get("id") or "")
    data = str(query.get("callback_data") or query.get("data") or "")
    kind, parts = messages.decode(data)
    stamp = now or _now_iso()

    def ack(text: str = "", *, alert: bool = False) -> None:
        if callback_id:
            telegram.answer_callback_query(callback_id, text, alert=alert)

    if kind == "g" and len(parts) >= 2:
        return _handle_gate(conn, config, telegram, parts, ack, tz=tz, stamp=stamp)
    if kind == "i" and len(parts) >= 3:
        return _handle_item(
            conn, config, telegram, parts, ack,
            tz=tz, stamp=stamp, provider=provider,
        )
    if kind == "q" and len(parts) >= 2:
        return _handle_quiz(
            conn, config, telegram, parts, ack, provider=provider, stamp=stamp
        )

    ack("I don't recognise that button.")
    return Handled("unknown", data)


def _handle_gate(conn, config, telegram, parts, ack, *, tz, stamp) -> Handled:
    run_id = int(parts[0]) if parts[0].isdigit() else 0
    verb = parts[1]
    run, subjects = _plan_of(conn, run_id)
    if run is None:
        ack("That prompt is no longer on file.")
        return Handled("stale", f"gate {run_id}")

    if verb.startswith("s"):
        index = int(verb[1:] or -1) if verb[1:].isdigit() else -1
        if not 0 <= index < len(subjects):
            ack("That subject is no longer in this prompt.")
            return Handled("stale", f"gate {run_id} subject {index}")
        resolved = _subject_from_stored(conn, subjects[index])
        if resolved is None:
            ack("Nothing left unreviewed there.")
            return Handled("empty", subjects[index].get("name", ""))
        subject, item = resolved
        ack("Sending it now…")
        return deliver_item(
            conn, config, telegram,
            run_id=run_id, subject=subject, item=item, now=stamp,
        )

    if verb == "z":
        until, refusal = _snooze_until(run, _parse_iso(stamp), tz)
        if until is None:
            ack(refusal, alert=True)
            return Handled("snooze-refused", refusal)
        store.snooze_gate_run(conn, run_id, until)
        conn.commit()
        ack("Back in two hours.")
        return Handled("snoozed", until)

    if verb == "k":
        skipped = _skip_all(conn, subjects, str(run["for_date"]), stamp)
        store.close_gate_run(conn, run_id, now=stamp)
        conn.commit()
        if run["message_id"]:
            telegram.edit_message_text(
                int(run["message_id"]),
                messages.closing_note(
                    datetime.strptime(str(run["for_date"]), "%Y-%m-%d").date(),
                    skipped,
                    "Skipped",
                ),
            )
        ack(f"Logged as skipped: {skipped} item(s).")
        return Handled("skipped", f"{skipped} item(s)")

    ack("I don't recognise that button.")
    return Handled("unknown", verb)


def _skip_all(conn, subjects, for_date: str, stamp: str) -> int:
    """Skip every item this prompt offered, each one logged with its reason.

    Recorded as `skipped`, never as `verified`, and always with a reason naming
    where the skip came from. A gate with no escape gets abandoned in a week; a
    gate that quietly forgives makes the coverage figure a lie.
    """
    reason = f"skipped from the evening prompt for {for_date}"
    count = 0
    for entry in subjects:
        for item_id in entry.get("item_ids") or []:
            if store.advance_study_item(
                conn, int(item_id), "skipped", skip_reason=reason, now=stamp
            ):
                count += 1
    return count


def _handle_item(conn, config, telegram, parts, ack, *, tz, stamp, provider=None) -> Handled:
    run_id = int(parts[0]) if parts[0].isdigit() else 0
    item_id = int(parts[1]) if parts[1].isdigit() else 0
    verb = parts[2]

    row = store.get_study_item(conn, item_id)
    if row is None:
        ack("That item is no longer on file.")
        return Handled("stale", f"item {item_id}")

    if verb == "q":
        item = item_by_id(conn, item_id)
        if item is None:
            ack("That item is no longer on file.")
            return Handled("stale", f"item {item_id}")
        ack("Setting the questions…")
        return start_quiz(
            conn, config, telegram,
            run_id=run_id, item=item, course_id=str(row["course_id"]),
            provider=provider, now=stamp,
        )

    if verb == "d":
        # The "next item" button from a passed quiz. Deliver by id rather than
        # by subject index: the index belongs to last night's keyboard, and by
        # now the backlog has moved by exactly the item that was just verified.
        item = item_by_id(conn, item_id)
        if item is None:
            ack("That item is no longer on file.")
            return Handled("stale", f"item {item_id}")
        ack("Sending it now…")
        return deliver_item(
            conn, config, telegram,
            run_id=run_id,
            subject=_subject_of(conn, str(row["course_id"])),
            item=item,
            now=stamp,
        )

    if verb == "r":
        moved = store.advance_study_item(conn, item_id, "reviewed", now=stamp)
        conn.commit()
        ack("Marked as read." if moved else f"Already {row['state']}.")
        return Handled("reviewed" if moved else "repeat", str(item_id))

    if verb == "k":
        reason = f"skipped at delivery, gate run {run_id}"
        moved = store.advance_study_item(
            conn, item_id, "skipped", skip_reason=reason, now=stamp
        )
        conn.commit()
        ack("Logged as skipped." if moved else f"Already {row['state']}.")
        return Handled("skipped" if moved else "repeat", str(item_id))

    if verb == "z":
        run = store.get_gate_run(conn, run_id)
        if run is None:
            ack("That prompt is no longer on file.")
            return Handled("stale", f"gate {run_id}")
        until, refusal = _snooze_until(run, _parse_iso(stamp), tz)
        if until is None:
            ack(refusal, alert=True)
            return Handled("snooze-refused", refusal)
        store.snooze_gate_run(conn, run_id, until)
        conn.commit()
        ack("Back in two hours.")
        return Handled("snoozed", until)

    ack("I don't recognise that button.")
    return Handled("unknown", verb)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def resend_due(
    conn: sqlite3.Connection,
    telegram: Telegram,
    resend: Callable[[sqlite3.Row], None],
    *,
    now: str | None = None,
) -> int:
    """Re-send any prompt whose snooze has run out."""
    stamp = now or _now_iso()
    due = store.due_gate_runs(conn, stamp)
    for run in due:
        resend(run)
    return len(due)


def poll(
    conn: sqlite3.Connection,
    config: Config,
    telegram: Telegram,
    *,
    tz,
    timeout: int = 30,
    once: bool = False,
    on_event: Callable[[Handled], None] | None = None,
    resend: Callable[[sqlite3.Row], None] | None = None,
    provider: llm.LLMProvider | None = None,
) -> list[Handled]:
    """Long-poll and route, until stopped.

    The offset is advanced and committed per update, not per batch. An update
    handled and then lost to a crash before its offset was stored would be
    replayed on the next start -- which the idempotent handlers survive, but
    only because they are idempotent, and there is no reason to lean on that
    when a commit per button costs nothing at this volume.
    """
    handled: list[Handled] = []
    stored = store.get_bot_state(conn, OFFSET_KEY)
    offset = int(stored) + 1 if stored else 0

    while True:
        if resend is not None:
            resend_due(conn, telegram, resend)

        updates = telegram.get_updates(offset, timeout=timeout)
        for update in updates:
            offset = int(update["update_id"]) + 1
            query = update.get("callback_query")
            if query is not None:
                # The Bot API nests the payload under "data"; handle_callback
                # accepts either spelling so a test can build the obvious shape.
                result = handle_callback(
                    conn, config, telegram, query, tz=tz, provider=provider
                )
            else:
                # Anything else -- a plain message, an edit -- is not part of
                # the gate. Acknowledged by advancing the offset and ignored,
                # rather than left to be re-offered forever.
                result = Handled("ignored", str(update.get("update_id")))
            handled.append(result)
            if on_event is not None:
                on_event(result)
            store.set_bot_state(conn, OFFSET_KEY, str(update["update_id"]))
            conn.commit()

        if once:
            return handled
