"""The readiness gate: what to revise, before which session, and did I.

This is the feature the project exists for, and the reason it is trustworthy is
that almost none of it is a language model.

What is deterministic, and must stay so:

  * reading the timetable and deciding which day is being prepared for
  * which subjects meet that day, and in what order
  * which study item to serve (oldest unreviewed post first)
  * whether an item is readable enough to quiz on (page arithmetic over
    `extractions`, see the scheduler)
  * grading, and the pass threshold
  * every state transition, every timestamp, every skip
  * whether to send anything at all

What the model does, and nothing more: write the quiz questions, and write the
one-paragraph summary of a lecture. Both have plain-template fallbacks, because
invariant 4 says an LLM outage degrades the briefing rather than suppressing
it -- and the gate is the briefing that matters most.

One difference from the sync worth stating out loud, because it looks like a
violation of invariant 1 and is not. The sync is catch-up safe: it never
assumes it ran yesterday, and a week of silence produces a week of events. The
gate is deliberately NOT. A prompt for a lecture that already happened is
noise, so a gate that has not run for three days fires once, for tomorrow, and
not three times. What stays catch-up safe is the backlog itself: study_items
never expire, so a week of missed prompts still shows up as a week of
unreviewed items in the next one.
"""
