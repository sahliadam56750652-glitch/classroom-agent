"""The prompt itself: what it says, and what it never puts in a button.

Two Telegram limits are enforced rather than discovered. callback_data over 64
bytes fails silently on the button that needed it, and a message over 4096
comes back as a 400 that says nothing about length -- and a keyboard cannot be
split across messages, so the prompt has to fit.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.gate import messages
from agent.gate.scheduler import GatePlan, Item, Subject
from agent.gate.timetable import Session, SessionPart
from agent.notify.telegram import MESSAGE_LIMIT

# The real ones. A 12-digit course id is the thing that would blow the callback
# budget if it ever reached a button.
DSA = "806468143345"
DATABASE = "842149328479"
OS = "840878703017"


def part(subject, course_id=None, teacher=None, room=None):
    return SessionPart(subject=subject, course_id=course_id, teacher=teacher, room=room)


def session(start="10:30", end="12:30", kind="LAB", parts=None):
    return Session(day="tue", start=start, end=end, kind=kind,
                   parts=tuple(parts or [part("DSA", DSA, "Khelifi", "Lab1")]))


def item(item_id=8, label="Chapter 1", pages=92, unread=0, chars=31520, files=1,
         link="https://classroom.google.com/c/x"):
    return Item(
        item_id=item_id, entity_type="coursework_material", entity_id="e1",
        label=label, state="pending", alternate_link=link,
        creation_time="2026-09-01T00:00:00Z", files=files, pages=pages,
        chars=chars, unread=unread,
    )


def subject(name="DSA", course_id=DSA, items=None, has_items=True, dead=0,
            sessions=None):
    return Subject(
        name=name, course_id=course_id, course_name=f"{name} course",
        sessions=tuple(sessions or [session()]),
        items=tuple(items if items is not None else [item()]),
        dead_files=dead, has_items=has_items,
    )


def build(subjects=None, sessions=None, for_date=date(2026, 9, 15)):
    subjects = subjects if subjects is not None else [subject()]
    return GatePlan(
        for_date=for_date, version_label="S1 provisional v1", provisional=True,
        sessions=tuple(sessions or [session()]), subjects=tuple(subjects),
    )


# --------------------------------------------------------------- callback_data

def test_callback_data_stays_under_the_limit_with_real_course_ids():
    """The test the whole scheme exists to pass. Subjects are addressed by
    index precisely so that twelve digits of course id never reach a button."""
    plan = build([
        subject("Database", DATABASE),
        subject("OS", OS),
        subject("DSA", DSA),
    ])
    keyboard = messages.keyboard(plan, run_id=987654)

    for row in keyboard["inline_keyboard"]:
        for pressed in row:
            data = pressed.get("callback_data")
            if data is not None:
                assert len(data.encode("utf-8")) <= messages.CALLBACK_LIMIT, pressed


def test_no_callback_carries_a_course_id_or_a_title():
    plan = build([subject("Database", DATABASE)])
    every = [
        pressed.get("callback_data", "")
        for row in messages.keyboard(plan, 41)["inline_keyboard"]
        for pressed in row
    ]
    every += [
        pressed.get("callback_data", "")
        for row in messages.item_keyboard(41, item())["inline_keyboard"]
        for pressed in row
    ]
    for data in every:
        assert DATABASE not in data
        assert "Database" not in data
        assert "Chapter" not in data


def test_item_callbacks_stay_small_with_implausibly_large_ids():
    keyboard = messages.item_keyboard(2**31, item(item_id=2**31))
    for row in keyboard["inline_keyboard"]:
        for pressed in row:
            data = pressed.get("callback_data")
            if data is not None:
                assert len(data.encode("utf-8")) <= messages.CALLBACK_LIMIT


def test_encode_refuses_rather_than_truncating():
    """Truncating would produce a button that resolves to the wrong row."""
    with pytest.raises(messages.CallbackTooLong):
        messages.encode("g", "x" * 100)


def test_encode_and_decode_round_trip():
    assert messages.decode(messages.encode("g", 41, "s2")) == ("g", ["41", "s2"])
    assert messages.decode(messages.encode("i", 41, 912, "r")) == ("i", ["41", "912", "r"])


def test_decode_tolerates_junk():
    assert messages.decode("") == ("", [])
    assert messages.decode("garbage") == ("garbage", [])


# --------------------------------------------------------------- escaping

def test_every_interpolated_value_is_escaped():
    """Course titles and filenames in this account contain & and <. One missed
    escape is a 400 and a prompt that silently never arrives."""
    plan = build([subject("Probability & Statistics", DSA,
                          items=[item(label="Chapter <1> & 2")])])
    text = messages.compose(plan)

    assert "Probability &amp; Statistics" in text
    assert "Chapter &lt;1&gt; &amp; 2" in text
    assert "<1>" not in text


def test_a_teacher_name_with_an_ampersand_is_escaped():
    plan = build(sessions=[session(parts=[part("DSA", DSA, "Ben Salah & Co", "A&B")])])
    assert "&amp;" in messages.compose(plan)


# --------------------------------------------------------------- readiness

def test_a_subject_with_pending_ocr_says_so():
    """Flagged, never gated silently."""
    plan = build([subject(items=[item(unread=12, pages=41)])])
    text = messages.compose(plan)

    assert "⚠" in text
    assert "12 of 41 page(s) not transcribed yet" in text


def test_a_ready_subject_says_nothing_alarming():
    text = messages.compose(build([subject(items=[item(unread=0)])]))
    assert "⚠" not in text
    assert "not transcribed" not in text


def test_a_partly_ready_subject_reports_both_counts():
    plan = build([subject(items=[item(1, unread=3), item(2, unread=0)])])
    text = messages.compose(plan)
    assert "2 unreviewed, 1 ready" in text


def test_a_subject_with_no_readable_material_never_reads_as_up_to_date():
    plan = build([subject("Stats", DSA, items=[], has_items=False, dead=20)])
    text = messages.compose(plan)

    assert "no readable material" in text
    assert "20 attachment(s) are gone from Drive" in text
    assert "up to date" not in text


def test_an_up_to_date_subject_says_so():
    text = messages.compose(build([subject(items=[])]))
    assert "up to date" in text


def test_an_untracked_subject_is_listed_but_marked():
    plan = build([subject("Calculus II", None, items=[])])
    assert "no Classroom course, never gated" in messages.compose(plan)


# --------------------------------------------------------------- shape

def test_the_prompt_shows_the_timetable_version_and_its_status():
    text = messages.compose(build())
    assert "S1 provisional v1" in text
    assert "provisional" in text


def test_a_joint_session_is_one_line_with_both_subjects():
    joint = session(parts=[
        part("Database", DATABASE, "Gharbi", "Lab3"),
        part("OS", OS, "Mansour", "Lab4"),
    ])
    text = messages.compose(build([subject("Database", DATABASE),
                                   subject("OS", OS)], sessions=[joint]))

    assert "Database + OS" in text
    assert "Gharbi / Mansour" in text
    assert "Lab3 / Lab4" in text


def test_a_subject_meeting_twice_gets_one_button():
    twice = subject(sessions=[session(start="08:30", end="10:00", kind="LEC"),
                              session(start="13:45", end="15:15", kind="TUT")])
    rows = messages.keyboard(build([twice]), 1)["inline_keyboard"]
    starts = [r for r in rows if r[0]["callback_data"].endswith("s0")]
    assert len(starts) == 1


def test_the_keyboard_has_a_button_per_subject_plus_snooze_and_skip():
    plan = build([subject("Database", DATABASE), subject("OS", OS)])
    rows = messages.keyboard(plan, 1)["inline_keyboard"]

    assert len(rows) == 3
    assert rows[0][0]["text"].startswith("▶ Database")
    assert rows[1][0]["text"].startswith("▶ OS")
    assert [b["text"] for b in rows[2]] == [messages.SNOOZE_LABEL, messages.SKIP_LABEL]


def test_the_keyboard_stays_within_the_usable_button_count():
    plan = build([subject(f"S{n}", f"c{n}") for n in range(12)])
    buttons = [b for row in messages.keyboard(plan, 1)["inline_keyboard"] for b in row]
    assert len(buttons) <= 8


def test_an_up_to_date_subject_gets_no_button():
    plan = build([subject("Database", DATABASE), subject("OS", OS, items=[])])
    rows = messages.keyboard(plan, 1)["inline_keyboard"]
    assert all("OS" not in row[0]["text"] for row in rows[:-1])


def test_the_prompt_fits_one_message_even_on_an_absurd_day():
    """A keyboard attaches to exactly one message, so this cannot be split."""
    plan = build(
        [subject(f"Subject number {n}", f"c{n}",
                 items=[item(n, label="A very long lecture title " * 8)])
         for n in range(40)],
        sessions=[session(start=f"{8 + n // 6:02d}:{(n % 6) * 10:02d}",
                          parts=[part(f"Subject number {n}", f"c{n}", "Teacher " * 5)])
                  for n in range(40)],
    )
    text = messages.compose(plan)

    assert len(text) <= MESSAGE_LIMIT
    assert "omitted" in text


def test_a_normal_day_is_not_trimmed():
    text = messages.compose(build([subject("Database", DATABASE), subject("OS", OS)]))
    assert "omitted" not in text


# --------------------------------------------------------------- item message

def test_the_item_message_names_the_lecture_and_its_size():
    text = messages.item_message(subject(), item(pages=92, files=2), remaining=0)
    assert "Chapter 1" in text
    assert "2 file(s)" in text
    assert "92 page(s)" in text


def test_an_unreadable_item_says_it_cannot_be_quizzed():
    text = messages.item_message(subject(), item(unread=12, pages=41), remaining=0)
    assert "12 of 41" in text
    assert "read, not verified" in text


def test_the_item_message_says_how_many_are_left():
    text = messages.item_message(subject(), item(), remaining=3)
    assert "3 more waiting" in text


def test_the_item_keyboard_offers_read_snooze_and_skip():
    rows = messages.item_keyboard(41, item())["inline_keyboard"]
    labels = [b["text"] for row in rows for b in row]

    assert "Open in Classroom" in labels
    assert "✓ I've read it" in labels
    assert messages.SNOOZE_LABEL in labels
    assert messages.SKIP_LABEL in labels


def test_an_item_with_no_link_gets_no_link_button():
    rows = messages.item_keyboard(41, item(link=None))["inline_keyboard"]
    assert all("url" not in b for row in rows for b in row)


def test_a_document_caption_stays_under_the_limit():
    caption = messages.document_caption("x" * 4000, 92)
    assert len(caption) <= 1024


def test_an_oversized_file_offers_its_link_instead():
    line = messages.too_large_line("Deck.pdf", "https://drive/x", 78 * 1024 * 1024)
    assert "78 MB" in line
    assert "https://drive/x" in line
