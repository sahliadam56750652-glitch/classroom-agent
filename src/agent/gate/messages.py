"""The gate's messages and keyboards, and the callback vocabulary behind them.

Two Telegram limits are enforced here rather than discovered in production,
because neither of them raises anything useful: a message one character over
4096 comes back as a 400 that does not mention length, and callback_data over
64 bytes silently fails on the button that needed it most.

The callback vocabulary is therefore ids and one-letter verbs, never content.
No course id, no subject name, no title ever goes into callback_data -- a
subject is addressed by its INDEX in the stored plan, which is why the plan is
stored. `encode` asserts the limit so that a violation is a failing test rather
than a dead button on my phone.

Three kinds, and every one of them is ids the whole way down:

    g:<run>:s<i>          the evening prompt: start subject i
    g:<run>:z | k         snooze it, or skip the lot
    i:<run>:<item>:r      one item: I have read it
    i:<run>:<item>:q      one item: quiz me on it
    i:<run>:<item>:d      one item: deliver this one (the "next" button)
    i:<run>:<item>:z | k  snooze, or skip this one
    q:<attempt>:<n>:0..3  answer question n with that option
    q:<attempt>:<n>:f     flag question n as a bad question
    q:<attempt>:n         a fresh attempt on the same item
    q:<attempt>:b         the whole question set was wrong

An option is addressed by its index for the same reason a subject is: the index
is one byte and the text is not, and the index is what grading compares against.

Everything interpolated goes through `escape`. Course titles, filenames and
lecture text in this account contain `&`, `<` and `>`, and one unescaped
character means the whole prompt silently fails to arrive.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..notify.telegram import MESSAGE_LIMIT, escape, link
from .quiz import Attempt, Question, Result
from .scheduler import MAX_SUBJECT_BUTTONS, GatePlan, Item, Subject

# Telegram's own ceiling. Asserted rather than trusted: it is a byte count, and
# the ids here are ASCII, but a future verb carrying anything else would be
# caught the moment a test builds a keyboard.
CALLBACK_LIMIT = 64

# Leaves room for the "and N more" line the trimmer may add.
TEXT_BUDGET = MESSAGE_LIMIT - 200

SNOOZE_LABEL = "😴 Snooze 2h"
SKIP_LABEL = "⏭ Skip — log it"
QUIZ_LABEL = "🧠 Quiz me"
FLAG_LABEL = "🚩 Bad question"
RETRY_LABEL = "🔄 Try again"

# A, B, C, D. The buttons carry the index; these are only for reading.
CHOICES = "ABCD"


class CallbackTooLong(ValueError):
    """A callback payload would be silently dropped by Telegram."""


def encode(*parts: Any) -> str:
    """Join callback parts, refusing anything Telegram would drop.

    Everything here is an id or a one-letter verb, so this never fires in
    practice -- which is exactly why it is an assertion and not a truncation.
    Truncating would produce a button that resolves to the wrong row.
    """
    data = ":".join(str(part) for part in parts)
    size = len(data.encode("utf-8"))
    if size > CALLBACK_LIMIT:
        raise CallbackTooLong(
            f"callback_data is {size} bytes, over Telegram's {CALLBACK_LIMIT}: "
            f"{data!r}. Store a row and send its id."
        )
    return data


def decode(data: str) -> tuple[str, list[str]]:
    """('g', ['41', 's0']) -- the kind, and whatever followed it."""
    parts = (data or "").split(":")
    return (parts[0] if parts else ""), parts[1:]


def button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def url_button(text: str, url: str) -> dict[str, str]:
    return {"text": text, "url": url}


# --------------------------------------------------------------------------
# the evening prompt
# --------------------------------------------------------------------------

def _session_line(session) -> str:
    people = " / ".join(part.teacher for part in session.parts if part.teacher)
    rooms = " / ".join(
        dict.fromkeys(part.room for part in session.parts if part.room)
    )
    subjects = " + ".join(session.subjects)
    line = (
        f"<b>{escape(session.start)}–{escape(session.end)}</b> "
        f"{escape(session.kind)} · {escape(subjects)}"
    )
    trailing = " · ".join(part for part in (people, rooms) if part)
    return f"{line}\n    <i>{escape(trailing)}</i>" if trailing else line


def _subject_line(subject: Subject) -> str:
    """One subject's standing, in the terms that make it honest.

    Four distinguishable states, and the difference between the last two is the
    whole point of this phase: a subject with nothing readable must never render
    the same way as a subject I am on top of.
    """
    name = f"<b>{escape(subject.name)}</b>"

    if not subject.gated:
        return f"{name} — no Classroom course, never gated"

    if not subject.has_items:
        missing = (
            f" — {subject.dead_files} attachment(s) are gone from Drive"
            if subject.dead_files
            else ""
        )
        return f"{name} — no readable material{missing}"

    if not subject.items:
        return f"{name} — up to date"

    counts = f"{len(subject.items)} unreviewed"
    if subject.blocked_count:
        counts += f", {subject.ready_count} ready"
    lines = [f"{name} — {counts}"]

    item = subject.next_item
    if item is not None:
        detail = f"    next: {escape(item.label)}"
        if item.pages:
            detail += f" ({item.pages} page(s))"
        lines.append(detail)
        if not item.ready:
            # Said out loud, every time. Quizzing on a lecture the agent has
            # never actually read is the failure the gate exists to prevent,
            # and staying quiet about it would be the same failure one step
            # earlier.
            lines.append(f"    ⚠ {escape(item.blocked_reason)}")
    return "\n".join(lines)


def compose(plan: GatePlan) -> str:
    """The whole evening prompt as one message.

    One message, not one per session. My week is ~20 sessions across ~11
    subjects; three prompts a day is how a gate gets muted.
    """
    stamp = plan.for_date.strftime("%a %d %b")
    header = [f"🎓 <b>Tomorrow — {escape(stamp)}</b>"]
    if plan.version_label:
        status = " · provisional" if plan.provisional else ""
        header.append(f"<i>{escape(plan.version_label)}{escape(status)}</i>")

    schedule = [_session_line(session) for session in plan.sessions]
    standing = [_subject_line(subject) for subject in plan.subjects]

    waiting = plan.total_items
    footer = [f"<b>{waiting} item(s) waiting.</b>"] if waiting else []

    blocks = ["\n".join(header), "\n".join(schedule), "\n\n".join(standing), *footer]
    return _fit([block for block in blocks if block])


def _fit(blocks: list[str]) -> str:
    """Join blocks, dropping detail from the end until it fits one message.

    An inline keyboard attaches to exactly one message, so the prompt cannot be
    split the way the digest is. Detail is dropped from the tail and the loss
    is stated -- a prompt truncated in silence would be a prompt that hides a
    subject.
    """
    text = "\n\n".join(blocks)
    if len(text) <= TEXT_BUDGET:
        return text

    kept = list(blocks)
    dropped = 0
    while kept and len("\n\n".join(kept)) > TEXT_BUDGET:
        kept.pop()
        dropped += 1
    kept.append(f"<i>({dropped} section(s) omitted — this day is too full to show)</i>")
    return "\n\n".join(kept)


def keyboard(plan: GatePlan, run_id: int) -> dict[str, Any]:
    """One button per subject, then snooze and skip.

    Subjects are addressed by index into `plan.actionable`, which is the order
    stored on the gate run. Never by course id: that would put twelve digits of
    content into callback_data for no gain, and content in callback_data is the
    habit that eventually overflows it.
    """
    rows: list[list[dict[str, str]]] = []
    for index, subject in enumerate(plan.actionable[:MAX_SUBJECT_BUTTONS]):
        waiting = len(subject.items)
        rows.append(
            [button(f"▶ {subject.name} ({waiting})", encode("g", run_id, f"s{index}"))]
        )
    rows.append(
        [
            button(SNOOZE_LABEL, encode("g", run_id, "z")),
            button(SKIP_LABEL, encode("g", run_id, "k")),
        ]
    )
    return {"inline_keyboard": rows}


# --------------------------------------------------------------------------
# one item, delivered
# --------------------------------------------------------------------------

def item_message(subject: Subject, item: Item, *, remaining: int) -> str:
    lines = [
        f"📘 <b>{escape(subject.name)}</b> — {escape(item.label)}",
    ]

    facts = []
    if item.files:
        facts.append(f"{item.files} file(s)")
    if item.pages:
        facts.append(f"{item.pages} page(s)")
    if facts:
        lines.append(f"<i>{escape(' · '.join(facts))}</i>")

    lines.append("")
    if item.ready:
        lines.append("All of this lecture is readable, so it can be quizzed.")
    else:
        lines.append(f"⚠ {escape(item.blocked_reason)}.")
        lines.append(
            "I can't quiz you on material I haven't read, so this counts as "
            "read, not verified."
        )

    if remaining:
        lines.append("")
        lines.append(f"<i>{remaining} more waiting in {escape(subject.name)}.</i>")
    return "\n".join(lines)


def item_keyboard(run_id: int, item: Item) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if item.alternate_link:
        rows.append([url_button("Open in Classroom", item.alternate_link)])
    if item.ready:
        # Only on an item whose pages have all been read. Offering it on one
        # with untranscribed pages would promise a quiz that generation is
        # going to refuse, and a button that apologises is worse than no button.
        rows.append([button(QUIZ_LABEL, encode("i", run_id, item.item_id, "q"))])
    rows.append([button("✓ I've read it", encode("i", run_id, item.item_id, "r"))])
    rows.append(
        [
            button(SNOOZE_LABEL, encode("i", run_id, item.item_id, "z")),
            button(SKIP_LABEL, encode("i", run_id, item.item_id, "k")),
        ]
    )
    return {"inline_keyboard": rows}


def document_caption(title: str, pages: int | None) -> str:
    """Under Telegram's 1024-character caption limit by construction."""
    suffix = f" · {pages} page(s)" if pages else ""
    caption = f"{escape(title)}{escape(suffix)}"
    return caption[:1000]


def too_large_line(title: str, url: str | None, size_bytes: int) -> str:
    megabytes = size_bytes / (1024 * 1024)
    return (
        f"📎 {link(url, title)} — {megabytes:.0f} MB, over Telegram's upload "
        f"limit, so here is the link instead."
    )


def closing_note(plan_date: date, count: int, what: str) -> str:
    return (
        f"<i>{escape(what)} for {escape(plan_date.strftime('%a %d %b'))} — "
        f"{count} item(s) logged.</i>"
    )


# --------------------------------------------------------------------------
# the quiz
# --------------------------------------------------------------------------

def question_message(attempt: Attempt, question: Question) -> str:
    """One question, as one message that will be edited into the next.

    The options are in the text rather than on the buttons. Option text runs to
    a sentence -- "the height of the tree, because each level halves the
    remaining range" -- and a button is a phone-width strip that would truncate
    it in the middle. Four buttons reading A B C D fit on one row, cannot be
    misread, and put the whole option where it is legible.
    """
    number = attempt.index + 1
    head = f"🧠 <b>{escape(attempt.label or 'Quiz')}</b> — question {number} of {attempt.total}"
    lines = [head, "", escape(question.question), ""]
    for index, option in enumerate(question.options):
        lines.append(f"<b>{CHOICES[index]}.</b> {escape(option)}")
    if question.where:
        lines.append("")
        lines.append(f"<i>from {escape(question.where)}</i>")
    return _clip("\n".join(lines))


def _clip(text: str) -> str:
    """One block that has to survive, shortened rather than dropped.

    _fit drops whole sections from the tail, which is right for the evening
    prompt and wrong here: the last thing to drop would be the question itself,
    and a message reading "1 section omitted" with four answer buttons under it
    is worse than a long question.
    """
    if len(text) <= TEXT_BUDGET:
        return text
    return text[: TEXT_BUDGET - 40].rstrip() + "\n\n<i>(shortened to fit)</i>"


def question_keyboard(attempt: Attempt, question: Question) -> dict[str, Any]:
    """A B C D, then the flag.

    The flag is on EVERY question, not on the ones that look wrong to whoever
    wrote this. Bad generated questions are the main risk to trusting the gate
    and a flag is the only way to find them, so the cost of flagging has to be
    one tap from wherever I am when I notice.
    """
    answers = [
        button(CHOICES[index], encode("q", attempt.attempt_id, attempt.index, index))
        for index in range(len(question.options))
    ]
    flag = button(FLAG_LABEL, encode("q", attempt.attempt_id, attempt.index, "f"))
    return {"inline_keyboard": [answers, [flag]]}


def _review_line(index: int, question: Question, chosen: int | None) -> str:
    """What was missed on one question, in enough detail to learn from."""
    lines = [f"<b>{index + 1}.</b> {escape(question.question)}"]
    if chosen is not None and 0 <= chosen < len(question.options):
        lines.append(
            f"   you said <b>{CHOICES[chosen]}</b> — {escape(question.options[chosen])}"
        )
    lines.append(
        f"   answer <b>{CHOICES[question.correct]}</b> — "
        f"{escape(question.options[question.correct])}"
    )
    if question.explanation:
        lines.append(f"   <i>{escape(question.explanation)}</i>")
    if question.where:
        lines.append(f"   <i>{escape(question.where)}</i>")
    return "\n".join(lines)


def result_message(result: Result, *, remaining: int = 0, subject: str = "") -> str:
    """The verdict, and everything that was got wrong.

    A pass says so briefly. A failure spends its length on what was missed,
    with the source named, because the point of failing is knowing what to go
    back and read.
    """
    attempt = result.attempt
    label = escape(attempt.label or "this lecture")
    mark = f"{result.correct} of {result.counted}"
    ratio = f"pass mark {round(attempt.pass_ratio * 100)}%"

    if result.passed:
        blocks = [f"✅ <b>Passed</b> — {label}\n{mark} · {ratio}"]
        blocks.append(
            "Marked as <b>verified</b>."
            if result.verified
            else "Already verified — nothing to change."
        )
        if remaining:
            blocks.append(
                f"<i>{remaining} more waiting in {escape(subject)}.</i>"
                if subject
                else f"<i>{remaining} more waiting.</i>"
            )
        return _fit(blocks)

    flagged = sum(1 for flag in attempt.flags if flag)
    header = [f"❌ <b>Not passed</b> — {label}", f"{mark} · {ratio}"]
    if flagged:
        header.append(
            f"<i>{flagged} flagged question(s) left out of the count.</i>"
        )
    if not attempt.counted:
        header.append(
            "<i>Every question was flagged, so there was nothing to mark. The "
            "set has been retired and the next try generates a fresh one.</i>"
        )

    # In the header block, not at the end: _fit trims from the tail, and
    # the one line that must never be trimmed is the one saying what this
    # did to the item.
    header.append("<i>The item stays read, not verified.</i>")

    blocks = ["\n".join(header)]
    missed = [
        _review_line(index, attempt.questions[index], attempt.answers[index])
        for index in attempt.wrong
    ]
    if missed:
        blocks.append("<b>What was missed</b>")
        blocks.extend(missed)
    if result.repeated:
        blocks.append(
            "<i>That is three failures on this lecture. It is worth considering "
            "that the questions are wrong rather than that you are — flag the "
            "set and it will be regenerated from scratch.</i>"
        )
    return _fit(blocks)


def result_keyboard(
    result: Result, *, next_item: Item | None = None
) -> dict[str, Any]:
    """What to do next: carry on after a pass, decide something after a failure."""
    attempt = result.attempt
    rows: list[list[dict[str, str]]] = []

    if result.passed:
        if next_item is not None:
            rows.append(
                [
                    button(
                        f"▶ Next: {_shorten(next_item.label)}",
                        encode("i", attempt.run_id, next_item.item_id, "d"),
                    )
                ]
            )
        return {"inline_keyboard": rows}

    retry = button(RETRY_LABEL, encode("q", attempt.attempt_id, "n"))
    bad_set = button("🚩 The questions were wrong", encode("q", attempt.attempt_id, "b"))
    skip = button(SKIP_LABEL, encode("i", attempt.run_id, attempt.item_id, "k"))

    if result.repeated:
        # Retry stops being the obvious thing after three failures. It is still
        # there -- taking the escape away is how a gate gets muted -- but it is
        # no longer what the thumb lands on.
        rows.append([bad_set])
        rows.append([skip, retry])
    else:
        rows.append([retry, skip])
    return {"inline_keyboard": rows}


def _shorten(text: str, limit: int = 28) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def no_quiz_line(reason: str, kind: str) -> str:
    """Why there is no quiz, in terms that say what to do about it.

    Eight causes, eight messages. A summary that cannot distinguish "come back
    tomorrow" from "go and fix the key" is a defect in itself, and this is the
    place that would otherwise collapse them into "the quiz failed".
    """
    heads = {
        "not-delivered": "⚠ This lecture has not been delivered yet",
        "not-readable": "⚠ I have not read all of this lecture yet",
        "no-text": "⚠ There is no extracted text on this post",
        "quota": "⏳ The day's model quota is spent",
        "rate-limited": "⏳ The model is rate limited right now",
        "timeout": "⏳ The model did not answer in time",
        "refused": "⚠ The model would not write usable questions",
        "model": "⚠ The configured model is unavailable",
        "auth": "⚠ The model API key is missing or rejected",
    }
    head = heads.get(kind, "⚠ No quiz right now")
    return (
        f"{head} — {escape(reason)}.\n"
        f"This counts as <b>read, not verified</b>."
    )
