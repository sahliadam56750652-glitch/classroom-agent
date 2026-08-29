# classroom-agent

A single-user personal assistant that syncs my Google Classroom, tracks how far
behind I am in each subject, and gates new course material behind a revision
quiz on the material I haven't reviewed yet.

I am a **student**, not a teacher. Every Classroom call is read-only and
concerns my own data — though see invariant 6: one granted scope is not. There is exactly one user. Do not build multi-tenancy,
user tables, or role systems.

## Stack

- Python 3.13 (pyproject pins >=3.13), standard venv, `pyproject.toml`
- SQLite (single file) — no ORM, plain `sqlite3` with a thin repository layer
- `google-api-python-client`, `google-auth-oauthlib` for Classroom + Drive
- Telegram over plain `urllib` in `notify/telegram.py` for the interface.
  `python-telegram-bot` was reconsidered at Phase 3 and rejected again: the
  gate needs long-poll `getUpdates` and a router over five one-letter
  callback verbs, which is ~120 sync lines against the client that already
  exists, and an async framework around a sync `sqlite3` store would mean
  two paradigms for one user
- PyMuPDF (`import pymupdf`; the `fitz` alias is deprecated) for PDF text extraction
- Google Gemini for the LLM, always behind the `llm/provider.py` interface

## Invariants

These are the properties that make the system trustworthy. Breaking one is a
bug even if the tests pass.

1. **Catch-up safe.** The sync compares stored state against live state. It
   never assumes it ran yesterday. If it hasn't run for a week, one run
   reports everything that changed in that week — no gaps, no duplicates.
   Never compute "what's new" from a wall-clock window.

2. **Diff on content hashes, not timestamps.** Classroom bumps `updateTime`
   on trivial edits. A change event fires only when a hash of the fields we
   care about actually differs.

3. **Notify exactly once.** Events carry a `notified_at`. Nothing with a
   non-null `notified_at` is ever sent again.

4. **Deterministic core, LLM at the edges.** Detecting changes, computing
   deadlines, and deciding what to report are pure code. The LLM only writes
   prose and generates quiz questions. An LLM outage must degrade the
   briefing to a plain template, never suppress it.

5. **Everything under `DATA_DIR`.** Database, OAuth token, PDF library, logs,
   config. Read it from the environment, default to `./data`. No absolute
   paths anywhere in the source — the project moves from my laptop to a cloud
   VM at Phase 3 and that must be a copy, not a rewrite.

6. **Read-only against Google.** This project never writes to Classroom or
   Drive. NOTE: the granted OAuth scope `classroom.coursework.me` is
   read-write, because Google's console does not allow registering the
   `.readonly` variant. The restriction is therefore enforced in code, not
   by the scope. Never call `create`, `patch`, `delete`, `modifyAttachments`,
   `turnIn`, `reclaim`, or `return` on any Classroom resource.

## Layout

```
src/agent/
  config.py  auth.py  cli.py
  classroom/   client.py  models.py
  db/          schema.sql  store.py
  sync/        poller.py  differ.py  deadlines.py
  files/       drive.py  extract.py  packs.py  ocr.py
  llm/         provider.py
  notify/      telegram.py  dispatch.py
  digest/      composer.py
  gate/        timetable.py  scheduler.py  messages.py  bot.py  quiz.py
               sections.py
tests/
data/          academic.db  token.json  library/  logs/
config.yaml    timetable.yaml          (both hand-edited, both gitignored)
```

The timetable is a file, not a table. It is versioned, semester-scoped
configuration with joint sessions and per-session teachers, none of which the
old `timetable` table could express, and mirroring it into SQLite would add a
second source of truth to keep in step. See `gate/timetable.py` and the note
where the table used to be in `db/schema.sql`.

## Conventions

- All timestamps stored as UTC ISO-8601 strings. Convert to `Africa/Tunis`
  only at the moment of display.
- There is no migration framework, and `_migrate_2_to_3` in `db/store.py` is
  not the start of one -- it is one hand-written step for the one change that
  had to alter an existing table. Adding a table needs nothing. Altering one
  means bumping `schema_version`, writing the step, and taking a backup first:
  `events.notified_at` and `study_items` are the only things in this project
  that cannot be rebuilt from the API.
- Every long-running command takes `--dry-run`.
- The gate is the one part that is deliberately NOT catch-up safe. Invariant 1
  is about the sync: a prompt for a lecture that already happened is noise, so
  a gate that has not run for three days fires once, for tomorrow, and not
  three times. The backlog stays catch-up safe because `study_items` never
  expire.
- `callback_data` is ids and one-letter verbs, never content. A subject is
  addressed by its index in the stored plan, which is why the plan is stored;
  a quiz option is addressed by its index for the same reason.
  `gate/messages.py:encode` refuses anything over 64 bytes rather than
  truncating it into a button that resolves to the wrong row.
- **`verified` has exactly one writer.** `db/store.py:verify_study_item`, and
  it re-reads the attempt it is handed and refuses one that did not pass.
  `verified` is absent from every value in `_TRANSITIONS`, so
  `advance_study_item` cannot reach it however it is called. Grading is an
  integer compared against an integer; no model is consulted and there is no
  code path from `quiz.settle` to a provider.
- **A question set is cached against a hash of the text it came from**, not
  against a timestamp -- the same rule as invariant 2. A retry, a restart or a
  second look costs zero requests; a transcription landing changes the hash and
  correctly earns a new set. `PROMPT_VERSION` is part of that hash, so editing
  the prompt retires every stored set.
- The free tier is ~20 requests a day and it binds. `ocr.run_limit` is 6 and
  `agent run` fires twice, so OCR takes 12 and the gate has ~8. Raising the OCR
  limit is spending the quiz's allowance; the arithmetic is written out in
  `config.example.yaml` so that is a deliberate choice rather than a surprise.
- **At 12 pages a day the OCR ORDER is the whole feature.** The queue is sorted
  by (tier, posting date descending, drive_id): tracked-and-timetabled first,
  then tracked, then everything else, and newest material first within each.
  Otherwise a backlog of archived courses starves a new term's slides for a
  fortnight, and the gate cannot quiz on pages nothing has read. The order is a
  function of the material only -- never of the clock or of OCR progress -- so a
  queue drained six pages at a time resumes where it left off. `agent ocr
  --status` prints the head of it, and `agent ocr --course <id|subject>` forces
  one subject when the gate needs it. See `files/ocr.py:queue`.
  Quiz LENGTH is not part of that trade: one request returns the whole set
  whatever `quiz.question_count` says, so a longer quiz costs my time and not
  quota. Six questions, five to pass -- `quiz.pass_threshold` is a fraction
  because a flagged question leaves the denominator.
- A document is delivered under its Drive title, not its Drive id
  (`gate/messages.py:document_filename`). A `file_id` keeps the filename it was
  uploaded with, so changing that rule means `DELETE FROM telegram_files`.
- Every external call has explicit retry with exponential backoff on 429 and
  5xx. Never a bare `except:`.
- Secrets come from `.env` (via `python-dotenv`) and never from source.
  `.env`, `credentials.json`, `token.json`, `data/` are all gitignored.
- Log structured lines to `data/logs/` and record each sync in `sync_runs`.
  A failure that produces no visible output is the worst failure mode here.

## Scheduling

Three separate entries, because they have three different cadences and one of
them must not inherit another's.

| when | how | command | why |
|---|---|---|---|
| 07:30, 19:30 | Task Scheduler | `agent run` | sync → fetch → extract → ocr → packs → deadlines → notify |
| 20:00 | Task Scheduler | `agent gate` | tomorrow's revision prompt, after the 19:30 sync has pulled the day's material |
| at logon | Startup `.vbs` → `pythonw.exe` | `agent bot` | the long-poll listener; restart-safe, so killing it is harmless |

The bot is the odd one out, and deliberately. A Task Scheduler entry that runs
without a visible console window needs the S4U logon type, which requires
administrator rights this account does not have -- so the listener starts from a
`.vbs` in the Startup folder calling `pythonw.exe`, which is windowless and runs
as the ordinary user. It is a crude mechanism and it costs nothing, because
`agent bot` holds no state a restart could lose.

The quiz has no entry of its own. It is reached by tapping a button, so it
lives inside `agent bot` -- and generation is lazy, at the moment the button is
pressed, which is what keeps it to one or two requests an evening.
`agent quiz --item N --dry-run` prints a set to stdout for judging by eye, and
`agent flagged` lists everything I have marked as a bad question.

`agent sections --item N [--pages N]` has no entry either, and nothing acts on
it yet. It prints how a long post would be cut into evening-sized windows -- a
measurement command in the family of `agent extract --dry-run` and
`agent ocr --status`, there so the boundaries can be judged by eye before
anything is built on them. See `gate/sections.py` and the open problem at the
end of `PLAN.md`.

`agent gate` is deliberately **not** a stage of `agent run`. `agent run` fires
twice a day and the gate must fire once — folding it in would send the prompt
every morning as well, and `gate_runs.for_date` would silently swallow the
second one, which is a fix that hides the mistake rather than preventing it.

## Working style

- Build one deliverable per session. Do not scaffold ahead into future phases.
- Prefer boring, readable, procedural code over clever abstractions. This is
  a personal tool maintained by one person between lectures.
- When Classroom API behaviour is uncertain, say so and check `data/dump.json`
  (captured by the Phase 0 probe against real course data) rather than
  guessing at field shapes.
- Write the test alongside the code, not after the phase.
