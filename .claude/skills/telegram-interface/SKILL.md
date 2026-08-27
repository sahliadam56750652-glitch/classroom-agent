---
name: telegram-interface
description: Telegram Bot API constraints and interaction patterns for this project — message and callback_data size limits, HTML vs MarkdownV2 escaping, inline keyboards, callback query handling, document delivery, and the quiz/gate conversation flow. Use this for any work on notify/, the digest sender, the readiness gate prompts, quiz delivery, or anything involving buttons, chat commands, or sending files to me, because the Bot API's silent limits truncate or reject messages rather than raising anything useful.
---

# Telegram interface

The bot is my only interface until the Phase 5 web app. It carries briefings,
deadline alerts, the gate prompts, quizzes, and Q&A.

## Hard limits that bite

- **Message text: 4096 characters.** A long briefing needs splitting on
  logical boundaries (per course), not mid-sentence. Write a `send_long`
  helper once and route everything through it.
- **`callback_data`: 64 bytes.** Never pack content into it. Store a row and
  send its id: `q:1734:a` (quiz 1734, answer a). Serialising a question into
  callback data will silently fail on real content.
- **Caption on a document: 1024 characters.** Longer explanations go as a
  separate follow-up message.
- **Bot uploads: 50 MB; bot downloads: 20 MB.** Lecture slide decks can
  exceed 50 MB. When they do, send the Drive `alternateLink` and a locally
  generated summary instead of failing.
- **Inline keyboard: keep to 8 or fewer buttons.** More becomes unusable on
  a phone screen.

## Use HTML, not MarkdownV2

MarkdownV2 requires escaping `_ * [ ] ( ) ~ \` > # + - = | { } . !` — and
course titles, filenames and LaTeX-ish lecture text contain all of those. One
missed escape returns a 400 and the whole briefing silently fails to arrive.

Use `parse_mode="HTML"`, escape with `html.escape()` on every interpolated
value, and stick to `<b>`, `<i>`, `<code>`, `<pre>`, `<a href="">`.

## File caching

`send_document` returns a `file_id`. Store it against the material row. Every
later send of the same document uses the `file_id` instead of re-uploading —
much faster and it avoids the upload limit entirely on repeat delivery.

## Callback queries

Always call `answer_callback_query`, even with no text. Otherwise the button
shows a loading spinner until it times out and the interface feels broken.

Prefer editing the existing message over sending a new one when a state
changes — a quiz that edits in place reads as one coherent exercise instead
of flooding the chat.

## Gate conversation flow

The gate is the core interaction. It must be finishable in a few taps while
walking to class.

```
[pre-session prompt]
  "Databases tomorrow 10:00. 1 unreviewed lecture."
  [ Start catch-up ] [ Snooze 2h ] [ Skip — log it ]

[delivery]  document + generated summary   → state: delivered
[quiz]      one question per message, edited in place as answered
[result]    pass → state: verified, next item unlocks
            fail → show what was missed, offer a retry
```

Rules that keep it honest:

- **Skip is always available and always logged.** Record it as `skipped`
  with a reason, never as `verified`. A gate with no escape gets abandoned
  in a week; a gate that quietly forgives makes the coverage number a lie.
- **Never lose progress to a restart.** Quiz state lives in the database, not
  in process memory. The bot restarts; a half-finished quiz must resume.
- **Every quiz question carries a 🚩 flag button.** Bad generated questions
  are the main risk to trusting the gate, and flagged questions are the only
  way to find them.

## Commands

Keep the surface small: `/today`, `/status`, `/subject <name>`, `/catchup`,
`/ask <question>`, `/skip`. Register them with `set_my_commands` so they
autocomplete.

## Delivery discipline

Silence when there is nothing to say. A bot that sends "nothing new today"
twice a day trains me to swipe the notification away without reading, and
then the one message that mattered gets swiped away too.
