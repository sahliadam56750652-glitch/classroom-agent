"""Digest composer: urgency ordering, grouping, escaping, and silence."""

from __future__ import annotations

import json

from agent.digest import composer

TZ = "Africa/Tunis"


def event(
    event_id,
    event_type,
    *,
    entity_type="coursework",
    entity_id=None,
    course_name="Databases",
    course_id="c1",
    payload=None,
    created_at="2026-08-27T12:00:00Z",
    as_json=True,
):
    """One event row shaped like the one store.list_events() returns."""
    body = payload or {}
    return {
        "id": event_id,
        "type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id or f"e{event_id}",
        "course_id": course_id,
        "course_name": course_name,
        "payload": json.dumps(body) if as_json else body,
        "created_at": created_at,
        "notified_at": None,
    }


# --------------------------------------------------------------------------
# silence
# --------------------------------------------------------------------------

def test_no_events_returns_none():
    """Silence is the correct output for a quiet day."""
    assert composer.compose([], timezone_name=TZ) is None


def test_no_events_returns_no_blocks():
    assert composer.compose_blocks([], timezone_name=TZ) == []


def test_never_emits_a_nothing_new_message():
    """The one thing the composer must never do is fill an empty digest."""
    result = composer.compose([], timezone_name=TZ)
    assert result is None
    assert result != ""


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------

def test_ordered_by_urgency_not_chronology():
    """A grade from this morning must not bury an assignment due in three hours."""
    rows = [
        event(1, "announcement_updated", created_at="2026-08-27T09:00:00Z"),
        event(2, "grade_posted", entity_type="submission",
              created_at="2026-08-27T08:00:00Z"),
        event(3, "deadline_t3", payload={"hours_before": 3, "due_at": "2026-08-27T15:00:00Z"},
              created_at="2026-08-27T12:00:00Z"),
        event(4, "new_coursework", created_at="2026-08-27T07:00:00Z"),
    ]

    text = composer.compose(rows, timezone_name=TZ)
    lines = [line for line in text.split("\n") if not line.startswith("<b>")]

    assert lines[0].startswith("⏰")   # deadline
    assert lines[1].startswith("📊")   # grade
    assert lines[2].startswith("🆕")   # new content
    assert lines[3].startswith("✏️")   # edit


def test_deadlines_ordered_t3_then_t24_then_t72():
    rows = [
        event(1, "deadline_t72", payload={"hours_before": 72, "due_at": "2026-09-01T12:00:00Z"}),
        event(2, "deadline_t3", payload={"hours_before": 3, "due_at": "2026-08-27T15:00:00Z"}),
        event(3, "deadline_t24", payload={"hours_before": 24, "due_at": "2026-08-28T12:00:00Z"}),
    ]

    text = composer.compose(rows, timezone_name=TZ)

    assert text.index("under 3 h") < text.index("under 24 h") < text.index("under 72 h")


def test_a_moved_due_date_sorts_with_the_deadlines():
    """A rescheduled hand-in is deadline news, not an edit buried at the bottom."""
    rows = [
        event(1, "grade_posted", entity_type="submission"),
        event(2, "due_date_changed", payload={"before": "2026-09-01T12:00:00Z",
                                              "after": "2026-08-28T12:00:00Z"}),
    ]

    text = composer.compose(rows, timezone_name=TZ)

    assert text.index("📅") < text.index("📊")


def test_the_most_urgent_course_leads_the_briefing():
    rows = [
        event(1, "announcement_updated", course_name="Operating Systems", course_id="c2"),
        event(2, "deadline_t3", course_name="Databases", course_id="c1",
              payload={"hours_before": 3, "due_at": "2026-08-27T15:00:00Z"}),
    ]

    blocks = composer.compose_blocks(rows, timezone_name=TZ)

    assert [block.course_name for block in blocks] == ["Databases", "Operating Systems"]


# --------------------------------------------------------------------------
# grouping
# --------------------------------------------------------------------------

def test_grouped_by_course_name_not_id():
    rows = [
        event(1, "new_material", course_name="Databases", course_id="c1"),
        event(2, "new_material", course_name="Operating Systems", course_id="c2"),
        event(3, "new_material", course_name="Databases", course_id="c1"),
    ]

    blocks = composer.compose_blocks(rows, timezone_name=TZ)

    assert len(blocks) == 2
    by_name = {block.course_name: block for block in blocks}
    assert by_name["Databases"].event_ids == [1, 3]
    assert "<b>Databases</b>" in by_name["Databases"].html
    assert "c1" not in by_name["Databases"].html


def test_a_course_with_no_name_falls_back_to_its_id():
    rows = [event(1, "new_material", course_name=None, course_id="c9")]
    (block,) = composer.compose_blocks(rows, timezone_name=TZ)
    assert block.course_name == "c9"


def test_blocks_are_separated_by_a_blank_line():
    """The boundary split_message() prefers, so a long digest breaks per course."""
    rows = [
        event(1, "new_material", course_name="Databases", course_id="c1"),
        event(2, "new_material", course_name="Operating Systems", course_id="c2"),
    ]

    assert "\n\n" in composer.compose(rows, timezone_name=TZ)


def test_every_event_id_is_accounted_for_exactly_once():
    rows = [event(i, "new_material", course_name=f"C{i % 3}", course_id=f"c{i % 3}")
            for i in range(1, 10)]

    blocks = composer.compose_blocks(rows, timezone_name=TZ)
    ids = [event_id for block in blocks for event_id in block.event_ids]

    assert sorted(ids) == list(range(1, 10))


# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------

def test_a_course_title_with_angle_brackets_ampersand_and_quotes_is_escaped():
    """The required case: markup characters in real course and item titles."""
    rows = [
        event(
            1,
            "new_coursework",
            course_name='Algo & Data <Structures> "2026"',
            payload={"title": 'TD 3: <Sets> & "Relations"'},
        )
    ]

    text = composer.compose(rows, timezone_name=TZ)

    # The course header is escaped inside its own bold tag.
    assert "<b>Algo &amp; Data &lt;Structures&gt; &quot;2026&quot;</b>" not in text
    assert '<b>Algo &amp; Data &lt;Structures&gt; "2026"</b>' in text
    # The item title is escaped too.
    assert "TD 3: &lt;Sets&gt; &amp; &quot;Relations&quot;" not in text
    assert 'TD 3: &lt;Sets&gt; &amp; "Relations"' in text
    # No raw markup survived anywhere outside the tags we emit.
    assert "<Sets>" not in text
    assert "<Structures>" not in text


def test_a_link_url_with_an_ampersand_is_attribute_escaped():
    rows = [event(1, "new_coursework", entity_id="w1", payload={"title": "TD"})]
    links = {("coursework", "w1"): "https://classroom.google.com/c?a=1&b=2"}

    text = composer.compose(rows, timezone_name=TZ, links=links)

    assert 'href="https://classroom.google.com/c?a=1&amp;b=2"' in text


def test_an_announcement_body_with_markup_is_escaped():
    rows = [
        event(
            1,
            "new_announcement",
            entity_type="announcement",
            payload={"text": "Read <chapter 4> & bring the handout"},
        )
    ]

    text = composer.compose(rows, timezone_name=TZ)

    assert "Read &lt;chapter 4&gt; &amp; bring the handout" in text


# --------------------------------------------------------------------------
# links
# --------------------------------------------------------------------------

def test_every_item_links_to_its_alternate_link():
    rows = [
        event(1, "new_coursework", entity_id="w1", payload={"title": "TD 3"}),
        event(2, "new_material", entity_type="coursework_material", entity_id="m1",
              payload={"title": "Lecture 7"}),
    ]
    links = {
        ("coursework", "w1"): "https://classroom.google.com/w1",
        ("coursework_material", "m1"): "https://classroom.google.com/m1",
    }

    text = composer.compose(rows, timezone_name=TZ, links=links)

    assert '<a href="https://classroom.google.com/w1">TD 3</a>' in text
    assert '<a href="https://classroom.google.com/m1">Lecture 7</a>' in text


def test_a_deadline_uses_the_link_from_its_own_payload():
    rows = [
        event(1, "deadline_t3", entity_id="w1", payload={
            "title": "TD 3",
            "hours_before": 3,
            "due_at": "2026-08-27T15:00:00Z",
            "alternate_link": "https://classroom.google.com/w1",
        })
    ]

    text = composer.compose(rows, timezone_name=TZ)

    assert '<a href="https://classroom.google.com/w1">TD 3</a>' in text


def test_a_missing_link_degrades_to_plain_text():
    rows = [event(1, "new_coursework", entity_id="w1", payload={"title": "TD 3"})]

    text = composer.compose(rows, timezone_name=TZ, links={})

    assert "TD 3" in text
    assert "<a href" not in text


# --------------------------------------------------------------------------
# the *_updated branch
# --------------------------------------------------------------------------

def test_files_arriving_on_an_existing_post_reads_as_real_news():
    """Announcements carry 211 of 375 attachments; this is a main delivery path."""
    rows = [
        event(1, "announcement_updated", entity_type="announcement", payload={
            "text": "Week 4",
            "attachments_added": True,
            "added_count": 2,
            "removed_count": 0,
            "text_changed": False,
        })
    ]

    text = composer.compose(rows, timezone_name=TZ)

    assert "📎 2 new files added to" in text


def test_one_new_file_is_singular():
    rows = [
        event(1, "material_updated", entity_type="coursework_material", payload={
            "title": "Lecture 7", "attachments_added": True, "added_count": 1,
        })
    ]

    assert "📎 1 new file added to" in composer.compose(rows, timezone_name=TZ)


def test_a_text_only_edit_is_a_one_liner():
    rows = [
        event(1, "announcement_updated", entity_type="announcement", payload={
            "text": "Week 4 (corrected)",
            "attachments_added": False,
            "added_count": 0,
            "removed_count": 0,
            "text_changed": True,
        })
    ]

    text = composer.compose(rows, timezone_name=TZ)
    body = [line for line in text.split("\n") if not line.startswith("<b>")]

    assert len(body) == 1
    assert body[0].startswith("✏️")
    assert "new file" not in body[0]


def test_all_three_updated_types_share_the_attachment_branch():
    """One code path, because the differ gives all three the same payload keys."""
    for event_type, entity_type in (
        ("coursework_updated", "coursework"),
        ("material_updated", "coursework_material"),
        ("announcement_updated", "announcement"),
    ):
        rows = [event(1, event_type, entity_type=entity_type, payload={
            "title": "X", "text": "X", "attachments_added": True, "added_count": 3,
        })]
        assert "📎 3 new files added to" in composer.compose(rows, timezone_name=TZ)


def test_removed_files_are_reported_distinctly():
    rows = [
        event(1, "material_updated", entity_type="coursework_material", payload={
            "title": "Lecture 7", "attachments_added": False,
            "added_count": 0, "removed_count": 2, "text_changed": False,
        })
    ]

    assert "✂️ 2 files removed from" in composer.compose(rows, timezone_name=TZ)


# --------------------------------------------------------------------------
# rendering detail
# --------------------------------------------------------------------------

def test_times_are_displayed_in_the_configured_zone():
    """Stored UTC, displayed Africa/Tunis -- one hour ahead in summer."""
    rows = [
        event(1, "deadline_t3", payload={
            "hours_before": 3, "due_at": "2026-08-27T15:00:00Z",
        })
    ]

    text = composer.compose(rows, timezone_name=TZ)

    assert "16:00" in text
    assert "15:00" not in text


def test_utc_is_used_when_the_zone_cannot_be_resolved():
    """A bad zone must not suppress the briefing."""
    rows = [
        event(1, "deadline_t3", payload={
            "hours_before": 3, "due_at": "2026-08-27T15:00:00Z",
        })
    ]

    text = composer.compose(rows, timezone_name="Not/AZone")

    assert "15:00" in text


def test_grades_lose_a_trailing_zero():
    rows = [event(1, "grade_posted", entity_type="submission",
                  payload={"title": "TD 2", "after": 15.0})]

    assert "graded 15 —" in composer.compose(rows, timezone_name=TZ)


def test_a_regrade_shows_both_values():
    rows = [event(1, "grade_changed", entity_type="submission",
                  payload={"title": "TD 2", "before": 12.0, "after": 15.5})]

    assert "regraded 12 → 15.5" in composer.compose(rows, timezone_name=TZ)


def test_an_announcement_without_a_title_uses_its_first_line():
    rows = [
        event(1, "new_announcement", entity_type="announcement",
              payload={"text": "Exam moved to Friday\nBring your notes"})
    ]

    text = composer.compose(rows, timezone_name=TZ)

    assert "Exam moved to Friday" in text
    assert "Bring your notes" not in text


def test_a_long_announcement_body_is_truncated():
    rows = [event(1, "new_announcement", entity_type="announcement",
                  payload={"text": "x" * 200})]

    text = composer.compose(rows, timezone_name=TZ)

    assert "..." in text
    assert "x" * 200 not in text


def test_an_unknown_event_type_still_appears():
    """A silently missing event is the failure mode this project exists to avoid."""
    rows = [event(1, "something_new_in_phase_9", payload={"title": "Thing"})]

    text = composer.compose(rows, timezone_name=TZ)

    assert "Thing" in text
    assert "something new in phase 9" in text


def test_a_payload_that_is_already_a_dict_is_accepted():
    rows = [event(1, "new_coursework", payload={"title": "TD 3"}, as_json=False)]
    assert "TD 3" in composer.compose(rows, timezone_name=TZ)


def test_a_corrupt_payload_does_not_lose_the_event():
    rows = [event(1, "new_coursework")]
    rows[0]["payload"] = "{not json"

    text = composer.compose(rows, timezone_name=TZ)

    assert text is not None
    assert "untitled coursework" in text


# --------------------------------------------------------------------------
# what Phase 2 lets the briefing say
# --------------------------------------------------------------------------


def test_a_new_material_line_reports_what_the_files_amount_to():
    """"3 files" says nothing about a page of admin versus forty pages of notes."""
    rows = [event(1, "new_material", entity_type="coursework_material",
                  entity_id="m1", payload={"title": "Chapter 6", "attachment_count": 2})]
    summaries = {("coursework_material", "m1"): {"files": 2, "pages": 34,
                                                 "ocr_pages": 0, "scan_pages": 0}}

    html = composer.compose(rows, summaries=summaries)

    assert "2 files" in html
    assert "34 pages" in html


def test_a_line_says_when_a_model_had_to_read_the_pages():
    """Phase 3 quizzes from this text, so how it was obtained is worth stating."""
    rows = [event(1, "new_material", entity_type="coursework_material",
                  entity_id="m1", payload={"title": "Slides"})]
    summaries = {("coursework_material", "m1"): {"files": 1, "pages": 12,
                                                 "ocr_pages": 4, "scan_pages": 4}}

    html = composer.compose(rows, summaries=summaries)

    assert "4 read by OCR" in html
    assert "not yet readable" not in html


def test_a_line_says_when_pages_are_still_unreadable():
    rows = [event(1, "new_material", entity_type="coursework_material",
                  entity_id="m1", payload={"title": "Scanned handout"})]
    summaries = {("coursework_material", "m1"): {"files": 1, "pages": 9,
                                                 "ocr_pages": 1, "scan_pages": 5}}

    html = composer.compose(rows, summaries=summaries)

    assert "4 not yet readable" in html


def test_a_line_stays_quiet_when_nothing_has_been_fetched_yet():
    """A briefing arriving before the fetch stage should say less, not say zero."""
    rows = [event(1, "new_material", payload={"title": "Chapter 6"})]

    html = composer.compose(rows)

    assert "pages" not in html
    assert "Chapter 6" in html


def test_a_subject_with_unreadable_material_says_so():
    """Phase 3 would quiz badly on a subject whose text the agent cannot read."""
    rows = [event(1, "new_material", course_id="c1", payload={"title": "Chapter 6"})]

    html = composer.compose(rows, unread_by_course={"c1": 12})

    assert "12 pages in this subject" in html
    assert "agent ocr" in html


def test_the_unreadable_warning_is_absent_when_everything_is_readable():
    """A standing footer on every digest is noise, and noise gets swiped away."""
    rows = [event(1, "new_material", course_id="c1", payload={"title": "Chapter 6"})]

    html = composer.compose(rows, unread_by_course={"c1": 0, "c2": 40})

    assert "unreadable" not in html


def test_the_warning_only_names_subjects_actually_in_the_briefing():
    rows = [event(1, "new_material", course_id="c1", course_name="Databases",
                  payload={"title": "Chapter 6"})]

    html = composer.compose(rows, unread_by_course={"c2": 99})

    assert "unreadable" not in html


def test_page_counts_are_escaped_like_every_other_value():
    rows = [event(1, "new_material", entity_type="coursework_material",
                  entity_id="m1", payload={"title": "<b>bold</b>"})]
    summaries = {("coursework_material", "m1"): {"files": 1, "pages": 3,
                                                 "ocr_pages": 0, "scan_pages": 0}}

    html = composer.compose(rows, summaries=summaries)

    assert "&lt;b&gt;bold&lt;/b&gt;" in html
