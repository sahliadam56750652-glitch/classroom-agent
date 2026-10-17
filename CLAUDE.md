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
  config.py  auth.py  cli.py  scope.py  manual.py  entries.py  backup.py
  health.py  locks.py
  api/         app.py  deps.py  auth.py  files.py  schemas.py  idempotency.py  routes/
  classroom/   client.py  models.py
  db/          schema.sql  store.py
  sync/        poller.py  differ.py  deadlines.py
  files/       drive.py  extract.py  render.py  packs.py  ocr.py
  llm/         provider.py
  notify/      telegram.py  dispatch.py
  digest/      composer.py
  gate/        timetable.py  scheduler.py  messages.py  bot.py  quiz.py
               quizgen.py  actions.py  sections.py  adjustments.py
tests/
deploy/        fingerprint.py  register-listeners.ps1
data/          academic.db  token.json  library/  logs/
config.yaml    timetable.yaml          (both hand-edited, both gitignored)
```

`deploy/register-listeners.ps1` makes the Task Scheduler tasks for `agent bot`
and `agent serve` (see Scheduling). `deploy/fingerprint.py` prints a comparable summary of a `DATA_DIR` -- row
counts, the figures that cannot be rebuilt from the API, and one hash over the
whole library -- so that "the data arrived whole" after a copy to a new machine
is a `diff` rather than a belief. It imports nothing from `agent` on purpose: a
broken install and a truncated copy must not look alike. (Phase 5a, the server,
was cancelled on 2026-10-13; the backend lives on this PC. See `PLAN.md`.)

The timetable is a file, not a table. It is versioned, semester-scoped
configuration with joint sessions and per-session teachers, none of which the
old `timetable` table could express, and mirroring it into SQLite would add a
second source of truth to keep in step. See `gate/timetable.py` and the note
where the table used to be in `db/schema.sql`.

**The file holds the weekly PATTERN; one-off changes are data.** A professor
moving Tuesday's lecture is not a change to the pattern, it is one dated fact
about one Tuesday -- so `timetable_adjustments` holds it and
`gate/adjustments.py` resolves the two together. The file is read and never
written by any command, which is what keeps it single-writer. See the settled
decision in `PLAN.md`.

## Conventions

- All timestamps stored as UTC ISO-8601 strings. Convert to `Africa/Tunis`
  only at the moment of display.
- There is no migration framework, and `_migrate_2_to_3` in `db/store.py` is
  not the start of one -- it is one hand-written step for the one change that
  had to alter an existing table. Adding a table needs nothing. Altering one
  means bumping `schema_version`, writing the step, and taking a backup first.
  Four things in this project cannot be rebuilt from the API:
  `events.notified_at`, `study_items`, `timetable_adjustments`, and
  `read_positions`. Everything entered by hand is irreplaceable by the same
  argument -- there is no API to re-ask -- which is why `agent backup` covers it
  and `deploy/fingerprint.py` counts it. So does `timetable.yaml`: hand-written,
  gitignored, and rebuildable from nothing, so the snapshot carries a checksummed
  copy, and a restore never overwrites one that is there -- a differing copy is
  written beside it (2026-10-16, after one save replaced the whole file).
  `agent run`'s backup stage takes one snapshot per Tunis day and keeps the
  newest 14 (`backup.DAILY_KEEP`); it deletes only directories it would have
  named itself, `manual-<UTC>` with a MANIFEST, and never one written with
  `--out`. A project's uploaded files under `DATA_DIR/projects/` are the only
  copy anywhere, so the snapshot carries them too, hard-linked to the previous
  snapshot's copy when unchanged. `api_sessions` and `client_ops` are
  the two tables deliberately left OUT of the backup: live cookies, and the
  receipts of writes the phone already sent.
- Every long-running command takes `--dry-run`.
- **`agent run`'s `studyitems` stage is strictly in scope**, which is narrower
  than the `agent studyitems` command's `local`. Typed by hand it is a
  deliberate act with `--seed` and `--force` available; run unattended twice a
  day it must never widen the backlog on its own, or the gate claims a backlog
  I do not have. It never seeds, it is idempotent, and an unreadable timetable
  makes it create nothing rather than everything.
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
- **The quiz is two modules, and the split is a rule.** `gate/quiz.py` grades:
  questions as data, the cache lookup, readiness, the attempt, `settle`. It
  imports no provider. `gate/quizgen.py` writes questions -- the prompt,
  `generate`, the bot's `begin`, and `prepare` for the scheduled run -- and is
  the only module in `gate/` that imports `llm/provider.py`. The API imports
  the first and a test asserts its process never loads the second.
- **Read and Skip have one implementation**, `gate/actions.py`, called by the
  bot's buttons and the API's routes alike. A skip always records where it came
  from ("skipped in the web app", "skipped at delivery, gate run 41") and keeps
  my reason, if I gave one, after that floor.
- **A question set is cached against a hash of the text it came from**, not
  against a timestamp -- the same rule as invariant 2. A retry, a restart or a
  second look costs zero requests; a transcription landing changes the hash and
  correctly earns a new set. `PROMPT_VERSION` is part of that hash, so editing
  the prompt retires every stored set.
- The free tier is ~20 requests a day and it binds. `ocr.run_limit` is 6 and
  `agent run` fires twice, so OCR takes 12; `quiz.prepare_limit` is 2, so sets
  written ahead take 4; the bot has ~4 left for quizzes written on demand.
  Raising either limit is spending the other's allowance; the arithmetic is
  written out in `config.example.yaml` so that is a deliberate choice rather
  than a surprise.
- **A subject with no Classroom keeps a `manual-` course id, as identity only.**
  Manual LOGGING -- uploads, session logs, minting new manual subjects -- was
  cancelled on 2026-10-13. The four existing ids stay because they are the only
  thing a task or a project for Calculus III, Algebra III, Philosophy or
  Communication can be filed against (`entries.subject_course`); those subjects
  stay in `timetable.yaml` as never gated. The prefix is still enforced, not
  trusted: `courses.tracked` refuses one, `soft_delete_missing` never stamps one,
  and `drive_references` never offers one to Drive. `manual_sessions` stays as a
  table, empty, still covered by `agent backup`.

- **An adjustment names a session as (date, subject, start time)** -- how the
  printed timetable identifies one to a person. It survives the file's sessions
  being reordered and deliberately does NOT survive the subject being renamed:
  matching approximately is what adjusts the wrong session while looking like
  it worked. A row that matches nothing is an ORPHAN, never applied, never
  dropped, always printed, and moved only by `agent adjust --repoint`.

- **`tracked` and `in scope` are two different lists.** Tracked is the poller's
  allowlist -- which Classroom courses to fetch, curated by hand in
  `config.yaml`. In scope is what this semester is about: every course id
  `timetable.yaml` names. `scope.py` holds both and their union, `local`, which
  is what every stage reading material off this disk takes -- study items,
  packs, the gate. They were one list only because every course came from
  Classroom, and they stop being one list the moment a subject does not.

- **At 12 pages a day the OCR ORDER is the whole feature.** The queue is sorted
  by (tier, posting date descending, drive_id): in scope first, then merely
  tracked, then everything else, and newest material first within each.
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
- **The API adds no business logic.** `agent/api/` returns what a CLI command
  already computes, so the two cannot disagree; where that computation lived in
  `cli.py` it moved down into `entries.py` or `gate/adjustments.py` rather than
  being written twice. `manual.py` keeps identity and minting; `entries.py` owns
  validation and orchestration of hand-entered records.
- **Every API route is `def`, never `async def`**, so a per-request sync
  `sqlite3` connection stays correct and the event loop is never blocked. One
  place commits -- the `get_db` dependency -- because store writes do not commit
  and a route that forgets returns 200 for a write that never landed.
- **The API moves a study item only the ways the bot does, and never fires the
  gate.** Since 5d it calls `gate/actions.py` for Read and Skip, and reaches
  `verified` through `quiz.settle` on an attempt rebuilt from its row -- in
  `routes/quiz.py` and nowhere else. It never names `verify_study_item` or
  `advance_study_item`, never imports `quizgen` or a provider, and never writes
  a `gate_runs` row: computing a plan and recording that a prompt was sent are
  different acts. All of it is pinned by tests, not by convention.
- **The phone's writes go through one outbox, and only these nine.**
  `web/outbox.js` stores Read, Skip, a task or milestone done, a timetable
  change, a reading position, and -- on a project -- adding a link, pinning a
  library document and removing either, on the phone first and sends them
  second, oldest first, before every sync, each with an `Idempotency-Key` that
  `api/idempotency.py` answers once. Creating a task, project or milestone and
  uploading a project file need the PC and say so -- the upload BEFORE a file
  is picked. A waiting write is
  shown as waiting, never as done, and one the PC refuses stays under "Not sent"
  with its sentence until I retry or discard it. A test holds the client's
  queued routes and the server's idempotent routes to the same list.
- **The API never writes a question.** A quiz whose set does not exist yet is a
  row in `quiz_requests`. `agent bot` answers it between polls; the quizzes
  stage of `agent run` answers it first if the bot is not running. The screen
  says which, from the bot's heartbeat in `bot_state` -- measured, not assumed.
  An open attempt is sent without its answers or explanations; those exist only
  in the review of a finished one.
- Every external call has explicit retry with exponential backoff on 429 and
  5xx. Never a bare `except:`.
- Both SQLite pragmas that matter are connection-scoped, so both are set in
  `db/store.py:connect` and neither can live in `schema.sql`: `foreign_keys`,
  and `busy_timeout` at `BUSY_TIMEOUT_MS`. Two writers still serialise under
  WAL, and on the server `agent run` and `agent bot` genuinely overlap -- the
  default 5 seconds is short enough for a sync burst to exceed, and the loser
  gets `database is locked`, which for the bot is a tapped button that does
  nothing.
- Secrets come from `.env` (via `python-dotenv`) and never from source:
  `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `WEB_API_TOKEN`.
  `.env`, `credentials.json`, `token.json`, `data/` are all gitignored.
- Log structured lines to `data/logs/` and record each sync in `sync_runs`.
  A failure that produces no visible output is the worst failure mode here.

## Scheduling

Three separate entries, because they have three different cadences and one of
them must not inherit another's.

| when | how | command | why |
|---|---|---|---|
| 07:30, 19:30 | Task Scheduler | `agent run` | sync → fetch → extract → render → ocr → studyitems → quizzes → packs → deadlines → backup → notify |
| 20:00 | Task Scheduler | `agent gate` | tomorrow's revision prompt, after the 19:30 sync has pulled the day's material |
| at logon, then every minute | Task Scheduler → `pythonw.exe` | `agent bot` | the long-poll listener; restart-safe, so killing it is harmless |
| at logon, then every minute | Task Scheduler → `pythonw.exe` | `agent serve` | the HTTP API behind the web client; holds no state a restart could lose |

The two listeners are tasks too since 2026-10-17 (`deploy/register-listeners.ps1`
makes them). As this user, only while logged on, no time limit, battery
allowed, and repeating every minute with "do not start a new instance": the
repetition IS the restart, because Task Scheduler's "restart on failure" was
measured not to fire for a pythonw process that exits non-zero. pythonw has no
console, so no window shows and no admin rights are needed. `agent bot` holds
`DATA_DIR/bot.lock`, so a second bot leaves at once; `agent serve` cannot bind
a second time. Both write `DATA_DIR/logs/<command>.log` when started without a
console, and both beat into `bot_state` -- `health.py` reads the beats, and the
gate prompt and the briefing say in one line when either is down.

**The gate waits for the evening's material.** `agent run` holds
`DATA_DIR/run.lock` for its whole length and records in `pipeline_runs` when
its material stages (sync through study items) are done. `agent gate` builds
its prompt only once a run started at or after the most recent 19:30 has done
them: it waits while a run holds the lock, or, if none does, brings the material
in itself under the lock -- without OCR, which spends the day's quota, and
without notify, which stays in `agent run` so events go out exactly once. It
waits at most 30 minutes; if it gives up, or the run could not reach Classroom,
the prompt still goes out, with one line saying when material was last synced.
The lock is an OS file lock, so a process that dies releases it.

**That table is the whole of the deployment.** There is no server (Phase 5a was
cancelled on 2026-10-13), so everything above runs only while this PC is on.
The phone reaches `agent serve` through `tailscale serve` (`docs/phone.md`),
and its address is the phone's storage: it never changes.

**When the PC was off.** Both Task Scheduler entries have "run as soon as
possible after a scheduled start is missed" on (checked 2026-10-13):

- `agent run` is catch-up safe, so a missed 07:30 is reported in full by the
  run that starts at logon.
- `agent gate` is not, and its late run is what the rule allows and no more. On
  before midnight, the 20:00 prompt for tomorrow goes out late -- still useful.
  On after midnight, "tomorrow" has moved: the late run sends the NEXT day's
  prompt early, the 20:00 run that evening finds its `gate_runs` row and does
  nothing, and the lecture of the day the PC was off never had a prompt. That
  is the gate's deliberate non-catch-up, not a bug to fix.
- The phone covers the gap: Today comes from the week of plans in its snapshot,
  so the next item is there without the bot. Read and Skip wait in its outbox.
- Telegram holds a tapped button for about 24 hours. Taps made while the PC was
  off are answered when `agent bot` starts; older ones are lost.
- Deadline alerts (T-72/24/3) go out only while the PC is on; the scanner sends
  only the most urgent threshold it missed. The phone shows every deadline
  regardless.

The quiz has no entry of its own. It is sat from a button -- in Telegram or in
the web app, one open attempt per item shared by both -- and its questions are
written three ways, all inside the shared quota: ahead of time by the quizzes
stage of `agent run` (requests first, then tomorrow's subjects, at most
`quiz.prepare_limit` per run), on demand by `agent bot` when a Telegram button
needs one, and between polls by `agent bot` for a request the web app left. The
API writes none of them.
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
- **Never edit the live checkout.** `C:\dev\classroom-agent` is what the
  scheduled tasks, `agent serve` and `agent bot` import, and it stays where it
  is with `.env`, `config.yaml`, `timetable.yaml` and `data/`. On 2026-10-16
  the evening run and gate caught up at logon and imported half-finished edits.
  Development happens in the git worktree `C:\dev\classroom-agent-dev`, on
  branch `dev`, with its own `.venv`; it has no `data/`, `config.yaml` or
  `.env`, so tests cannot touch the live data (`agent webcontract` there takes
  `--config config.example.yaml` and a placeholder `WEB_API_TOKEN`). When a
  change passes the full suite in the worktree: in the live checkout,
  `git merge --ff-only dev`, push, and restart `agent serve` and/or
  `agent bot` if their code changed (end the process; its task brings it back
  within a minute). Nothing else edits tracked files in the live checkout. Dry
  runs against real data run from the live checkout, after the merge.
