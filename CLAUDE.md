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
- `python-telegram-bot` (async) for the interface
- PyMuPDF (`fitz`) for PDF text extraction
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
  files/       drive.py  extract.py  packs.py
  llm/         provider.py  gemini.py
  notify/      telegram.py
  digest/      composer.py
  gate/        timetable.py  scheduler.py  quiz.py
tests/
data/          academic.db  token.json  library/  logs/
```

## Conventions

- All timestamps stored as UTC ISO-8601 strings. Convert to `Africa/Tunis`
  only at the moment of display.
- Every long-running command takes `--dry-run`.
- Every external call has explicit retry with exponential backoff on 429 and
  5xx. Never a bare `except:`.
- Secrets come from `.env` (via `python-dotenv`) and never from source.
  `.env`, `credentials.json`, `token.json`, `data/` are all gitignored.
- Log structured lines to `data/logs/` and record each sync in `sync_runs`.
  A failure that produces no visible output is the worst failure mode here.

## Working style

- Build one deliverable per session. Do not scaffold ahead into future phases.
- Prefer boring, readable, procedural code over clever abstractions. This is
  a personal tool maintained by one person between lectures.
- When Classroom API behaviour is uncertain, say so and check `data/dump.json`
  (captured by the Phase 0 probe against real course data) rather than
  guessing at field shapes.
- Write the test alongside the code, not after the phase.
