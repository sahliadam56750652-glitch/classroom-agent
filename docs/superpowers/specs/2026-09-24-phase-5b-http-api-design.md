# Phase 5b — the HTTP API behind the web dashboard

**Status:** approved 2026-09-24. Extends `PLAN.md` Phase 5; supersedes nothing.

`CLAUDE.md` says how to build. `PLAN.md` says what and in what order.
`DESIGN.md` says how the client should feel. This says what the API between
them is, and what it must never be able to do.

---

## 1. Intent

One HTTP API over the existing store layer, so the web client of Phase 5c has
something to read. It runs on the laptop at `localhost` today and moves to the
Oracle box at 5a as a directory copy, not a rewrite.

**The governing constraint: no new business logic.** Every figure the API
returns is computed by a function the CLI already calls. Where that function
currently lives inside `cli.py`, it moves down rather than being reimplemented —
two implementations of "which session does this adjustment name" is how the CLI
and the API come to disagree, and a disagreement between them is
indistinguishable from either one working.

### Success criteria

1. Every number DESIGN.md's screens need is reachable through an endpoint, and
   none of them is a percentage of a deficit.
2. `agent serve` refuses to start without a token. There is no unauthenticated
   mode, on localhost or anywhere else.
3. A byte range of a 92-page PDF returns `206`, so the reader never downloads a
   document whole.
4. `verified` is unreachable from the API, proven by test rather than asserted.
5. The API fires no sync, no OCR, no model call, and no gate.
6. Deploying to the box is a fifth systemd unit and a Caddy stanza. No code
   changes, no absolute paths.

---

## 2. Scope

### In

- Reads for all seven DESIGN.md screen groups.
- Writes for: manual subjects, logged sessions, manual tasks, projects,
  milestones, timetable adjustments, uploads, and read positions.
- Token-for-cookie authentication with server-side sessions and rate limiting.
- PDF and pack file serving with range requests and conditional GET.

### Out, deliberately

- **The gate's own actions.** Deliver, mark-read, skip, snooze and the quiz stay
  in `agent bot` through 5b and 5c. See section 11 and the 5d note in `PLAN.md`.
- **`verified`.** Not merely unimplemented — unreachable, and tested for.
- **Every pipeline stage.** No sync, fetch, extract, OCR, packs *build*,
  deadline *scan*, or notify. The API reads what the scheduled pipeline wrote.
- **Model calls.** `llm/` is not importable from `agent/api/`.
- **Writing `timetable.yaml`.** The file holds the weekly pattern and keeps one
  writer. A one-off change is an adjustment row.
- **TLS.** Caddy terminates it on the box. The app binds loopback.
- **Serving the static client.** One `StaticFiles` line in 5c. The API is
  designed for one origin so CORS never enters the picture, but 5b ships no
  client.

---

## 3. Architecture

```
src/agent/
  entries.py              NEW  validation + orchestration for hand-entered records
  api/                    NEW
    __init__.py
    app.py                     create_app(config) -> FastAPI
    deps.py                    per-request connection, auth dependency
    auth.py                    token exchange, sessions, rate limiting
    files.py                   path resolution, range streaming, ETag
    schemas.py                 response models
    routes/
      meta.py   study.py   subjects.py   library.py
      deadlines.py   projects.py   timetable.py   entries.py
```

`agent serve [--config P] [--host 127.0.0.1] [--port 8000]` joins `agent bot`
as a long-running listener.

### Dependencies

`fastapi`, `uvicorn`, `python-multipart`. **Plain `uvicorn`, never
`uvicorn[standard]`** — the extras pull `httptools` and `uvloop`, which need a C
toolchain on ARM. This is the same rule that chose `python-docx` and
`python-pptx`, and it belongs in the same comment block in `pyproject.toml`.

### Every route is `def`, never `async def`

A sync route runs in Starlette's threadpool, where a per-request sync `sqlite3`
connection is correct and safe. One `async def` route touching the store blocks
the event loop and puts an async framework around a sync store — the stated
reason `python-telegram-bot` was rejected twice. Pinned by a test that walks
`app.routes` and asserts no endpoint is a coroutine function.

### Why FastAPI is a different call from that rejection

That rejection weighed a framework against ~120 sync lines. This surface is ~30
endpoints with multipart parsing, range requests, conditional GET and schema
validation. That is not 120 lines, and the paradigm objection is answered by the
`def`-only rule above rather than ignored.

---

## 4. Slice 0 — the lift out of `cli.py`

`cli.py` is 3,586 lines. For the pipeline stages it already splits computation
from display (`_do_sync`/`_print_sync`, `_do_extract`/`_print_extract`), and
those are exactly the stages the API does not need.

The write paths the API *does* need have their validation and orchestration in
`cli.py`, shaped around `argparse.Namespace`, `print` and exit codes:

| moves from | to | becomes |
|---|---|---|
| `cli.py:_one_session` | `gate/adjustments.py` | `resolve_one_session(table, day, subject, at)` |
| `cli.py:_record_adjustment` | `gate/adjustments.py` | `record(conn, table, spec) -> Recorded` |
| `cli.py:_repoint_adjustment` | `gate/adjustments.py` | `repoint(conn, table, adjustment_id, spec)` |
| `cli.py:_add_project` | `entries.py` | `add_project(conn, table, spec) -> int` |
| `cli.py:_complete_milestone` | `entries.py` | `complete_milestone(conn, milestone_id)` |
| `cli.py:_close_project` | `entries.py` | `close_project(conn, project_id)` |
| `cli.py:_log_session` | `entries.py` | `log_session(conn, table, spec)` |
| `cli.py:_add_task` | `entries.py` | `add_task(conn, table, spec) -> int` |
| `cli.py:_complete_task` | `entries.py` | `complete_task(conn, task_id)` |
| `cli.py:_subject_course` | `entries.py` | `subject_course(config, table, name)` |
| `cli.py:_due_at`, `_date_arg`, `_clock_arg` | `entries.py` | `parse_due`, `parse_date`, `parse_clock` |
| `sync/deadlines.py:_candidates` | same module | `candidates` (rename only) |

`entries.py` owns validation and orchestration of hand-entered records.
`manual.py` keeps identity and minting, unchanged — the `manual-` prefix
namespace is its whole job, and widening it would blur that.

Each lifted function takes typed parameters and **raises** instead of printing.
`cli.py` keeps argparse-to-typed translation and printing, and nothing else.

**The acceptance test for slice 0 is that no test changes.**
`test_adjust_cli.py`, `test_projects.py`, `test_manual_entries.py` and the rest
pass byte-identically. A lift that needed a test edited is a lift that changed
behaviour.

Slice 0 has no visible payoff and every chance of breaking working code, so it
is its own commit on top of a clean checkpoint. If it goes wrong it is reverted,
not debugged halfway.

---

## 5. Endpoints

Grouped by the DESIGN.md screen each serves. All paths under `/api`.

### 5.1 What to study now — DESIGN.md sections 2 and 4

```
GET  /api/now                      the default screen
GET  /api/gate?date=YYYY-MM-DD     the whole plan for a date (default tomorrow)
GET  /api/study-items/{id}         one item: files, window, readiness
```

Backed by `scheduler.plan_for(conn, scope.local(config, table), table, date)` —
the same call `cmd_gate` makes. `GatePlan` already serialises.

`/api/now` returns the single next item: subject, title, the window being asked
for, how much of it is readable, `ready`, and `blocked_reason` when it is not.
The deficit is one subordinate field, not a screen.

When `plan.worth_sending` is false it returns `{"waiting": false,
"next_session": {...}}` — the empty state as the *absence* of an item, which is
`composer.compose() -> None` in HTTP form. Never an empty list dressed up as
content.

> **Computing a plan is not firing the gate.** The API must never call
> `create_gate_run`, `replace_gate_plan`, `mark_gate_sent`, `snooze_gate_run` or
> `close_gate_run`. If opening the dashboard at 06:00 created a `gate_runs` row,
> `gate_runs.for_date` would silently swallow that evening's real prompt — the
> identical failure that keeps `agent gate` out of `agent run`. Pinned by the
> route-whitelist test and by a test that calls `/api/now` and asserts
> `gate_runs` is unchanged.

### 5.2 Subjects and coverage — DESIGN.md section 3

```
GET  /api/subjects
GET  /api/subjects/{name}
```

Each subject carries its state as an explicit enum, mirroring
`messages._subject_line`, which already distinguishes all four:

`no_course` | `no_readable_material` | `up_to_date` | `behind`

with counts alongside: `verified`, `read`, `unreviewed`, `ready`,
`unread_pages`, `oldest_posted_at`, `dead_files`.

**There is no percentage field, and its absence is the enforcement.** DESIGN.md
section 7 forbids a percentage of a deficit and forbids `0%`/`100%` for a
subject with no readable material. A client cannot render a field the server
never sends. Probability & Statistics returns `no_readable_material` with its
dead-file count, never a number.

### 5.3 Library and reader — DESIGN.md section 6

```
GET  /api/library?course=&q=
GET  /api/documents/{drive_id}                metadata, windows, position
GET  /api/documents/{drive_id}/file           bytes, with Range
GET  /api/documents/{drive_id}/text?page=     extracted / transcribed text
GET  /api/documents/{drive_id}/position
PUT  /api/documents/{drive_id}/position       the one reader write
GET  /api/packs/{course_id}                   a built study pack
```

Windows come from `sections.windows`, read-only, which is what 3d stage 1 is
for.

`/text` exists beyond accessibility: it is the material a quiz is grounded in,
and DESIGN.md section 7 forbids "a state the app counts but will not show me."

### 5.4 Deadlines

```
GET  /api/deadlines
GET  /api/events?since=
```

Merged across coursework, manual tasks and projects through
`deadlines.candidates()` — the computing half only. The API never runs a scan
and never writes an event.

**`due_at` is a UTC instant and there is no other field about time.** No
`severity`, no `urgency`, no countdown, no hours-remaining. DESIGN.md section 7
forbids manufactured urgency; red-on-passed is a client rule applied to a
timestamp.

### 5.5 Projects

```
GET  /api/projects                 POST /api/projects
GET  /api/projects/{id}            POST /api/projects/{id}/milestones
POST /api/milestones/{id}/complete
POST /api/projects/{id}/close
```

Progress is `milestones_done` and `milestones_total`, two integers. No
percentage, for the reason the milestone design exists: a number I can quietly
revise upward is exactly what DESIGN.md's closing constraint forbids.

### 5.6 Timetable and adjustments

```
GET    /api/timetable?from=&to=        resolved days + orphans
GET    /api/timetable/file             the raw pattern, read-only
POST   /api/adjustments
DELETE /api/adjustments/{id}
POST   /api/adjustments/{id}/repoint
```

Each resolved session carries the `note` saying why it is where it is
(`adjustments.departure_note`). **Orphans appear on every timetable response**,
per the settled decision that an orphan is never applied, never dropped, always
printed.

`PUT /api/timetable/file` returns **405 with a body explaining why** — that the
file holds the pattern and keeps one writer, and that a one-off change is a
`POST /api/adjustments`. A 404 would read as a missing feature and invite
someone to add it.

### 5.7 Manual entries and upload

```
GET/POST /api/subjects/manual
GET/POST /api/sessions
GET/POST /api/tasks        POST /api/tasks/{id}/complete
POST     /api/uploads      multipart; ?dry_run=true returns the plan
```

### 5.8 Meta

```
POST   /api/session        the token exchange. The only unauthenticated route.
DELETE /api/session        sign out, revoking the row
GET    /api/health         unauthenticated: {"ok": true} and nothing else
GET    /api/status         authenticated: sync runs, OCR counts, item counts
```

`health` and `status` are split deliberately. A liveness probe that needs auth
is awkward for systemd and Caddy; a liveness probe that leaks backlog figures on
a public box is a small but real leak.

---

## 6. Authentication

A long random token in `.env`, exchanged once for a session cookie.

### Why a cookie rather than a bearer header

**The cookie is load-bearing for file serving, not a style preference.** A
browser's own PDF viewer issuing range requests from an `iframe` or `embed`, and
any `img` tag, cannot attach an `Authorization` header. A bearer token would
force the reader to fetch the whole PDF through JS into a Blob to attach one —
downloading the 92-page document whole, which is the exact failure range
requests exist to prevent. Cookies ride on subresource and range requests
automatically.

### The flow

`POST /api/session` with the token in the body sets an opaque random session id:

```
HttpOnly; Secure; SameSite=Lax; Path=/
```

90-day sliding expiry, refreshed on use. Long, because a session expiring at
23:00 mid-chapter costs a reading session and buys nothing against a single-user
tool.

### Sessions are rows, not a signed cookie

New table `api_sessions` (id, created_at, last_used_at, expires_at,
user_agent). Adding a table costs nothing here — no `schema_version` bump, no
migration step. What it buys is **revocation**, which costs everything the day
the phone is lost and which a stateless signed cookie cannot do. It also makes
the cookie value opaque, so nothing about the token is derivable from it.

### The token

`WEB_API_TOKEN` in `.env`, beside `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY`.
Compared with `hmac.compare_digest`. Minimum 32 characters, refused shorter.

**`agent serve` refuses to start without it.** There is no "localhost needs
none" path. An unset token silently meaning "open" is two states that look
identical from outside, which `PLAN.md`'s recurring lesson ranks as its own
defect. The refusal names the variable and prints the one-liner to mint one.

### Rate limiting the exchange

`POST /api/session` is the only unauthenticated route that does work, so it is
the only brute-force surface. Logging without a limit is a record of the attack
rather than a defence.

- **5 failures per IP per minute**, then `429` with `Retry-After`.
- **Sustained failure locks out**: 20 failures inside 15 minutes blocks that IP
  for 15 minutes, extending on continued attempts.
- In-memory, per-process. Not a table: the state is worthless across a restart,
  and a restart is not an attack. A dict keyed by IP with a pruning sweep.
- A successful exchange clears that IP's counter.
- Every failure is logged with the IP and the reason. On a public box the
  failure *rate* is the signal.
- Tested: a burst of 6 gets `429` on the 6th; a success resets the counter; the
  sweep does not grow without bound.

### CSRF

`SameSite=Lax` already blocks cross-site form POSTs. On top: an `Origin` check
on every non-GET request against the configured origin. Two lines, and it is the
honest belt-and-braces for a single-origin app.

### `Secure` on `http://localhost`

Chrome treats localhost as a trustworthy origin and accepts `Secure` cookies
there; not every browser has always agreed. `api.secure_cookie` in
`config.yaml`, default `true` — cheap insurance against an afternoon spent on a
cookie that is silently not set.

### Binding

Default `127.0.0.1`. `--host` is explicit. On the box Caddy terminates TLS and
proxies to loopback, so **5b writes no TLS code** and going public is a
reverse-proxy stanza rather than a code path. That is what keeps the move a
copy.

---

## 7. File serving

### The client never sends a path

It sends a `drive_id`. The server resolves `extractions.local_path`, joins it to
`config.library_dir`, calls `.resolve()`, and asserts the result is under
`library_dir.resolve()`.

A row whose path escapes returns **500 and a log line, not 404**. It means the
database is wrong, and that must not be indistinguishable from a file that was
never fetched.

### Range requests

**Verified against Starlette 1.7.0 rather than assumed, and the check paid for
itself.** Two findings, one in each direction:

- **Ranges work natively.** `FileResponse` answers a `Range` header with `206`, a
  correct `Content-Range`, `Accept-Ranges: bytes`, and `416` for an unsatisfiable
  range. No range handler is needed, and the ~40-line fallback was not.
- **`If-None-Match` is NOT honoured.** Starlette generates an `ETag` and then
  ignores the request header, returning `200` with the whole body. So conditional
  GET is written out in `api/files.py:not_modified`. Without it, DESIGN.md's
  "what has been delivered is readable offline" means re-pulling a 40 MB deck on
  every open.

Both are pinned by tests: bytes 0-1023 of a real PDF must return `206` with the
right `Content-Range`, and a second request carrying the ETag must return `304`
with an empty body. A silent fall back to `200` in either case means the whole
document downloads and the only symptom is "the reader feels slow" — a misreport,
not an error.

### Naming

`Content-Disposition: inline` with an RFC 5987 filename, from
`messages.document_filename(title, local_path)` — the same function Telegram
delivery uses, so a document has one name everywhere. `inline`, because the
reader displays it rather than saving it.

### Conditional GET

`ETag` from `get_ocr_source_md5` where present, else size and mtime.
`If-None-Match` returns `304`. This is what makes DESIGN.md section 6's "what
has been delivered is readable offline" true in practice, and keeps a phone from
re-pulling a 40 MB deck.

`Cache-Control: private` — one user, and no shared cache should ever hold this.

### Packs

`GET /api/packs/{course_id}` serves the built pack, located through
`store.get_pack`.

**This closes the `PLAN.md` open question "how study packs reach me once the
server is the host"** in favour of serving them over HTTP. It costs nothing
beyond the file serving this phase already needs, and it means **no second
exception to invariant 6 is ever required** — the `drive.file` alternative would
have been a write scope requested because the project means to write, which is
the opposite of the `classroom.coursework.me` case and would have had to be
recorded as a deliberate second exception.

---

## 8. Writes

### One place commits

A `get_db` dependency yields a connection, commits on clean return, rolls back
on any exception.

This is not tidiness. Store write functions **do not commit** — 4 `commit()`
calls in 2,257 lines, none in `add_project`, `add_milestone`, `add_adjustment`,
`log_manual_session` or `add_manual_task` — and `cli.py` commits in 23 separate
places. In a request handler a forgotten commit is a `200` for a write that
never landed: silent success, which this project ranks as its worst failure
mode. Centralising makes forgetting impossible rather than unlikely.

### Multi-statement writes are wrapped

`POST /api/projects` with milestones is one insert plus N more. A failure
partway would leave a project with three of five milestones. `with conn:` — the
pattern already used at `store.py:198`.

### `verified` is unreachable, proven three ways

1. **Import guard.** `agent/api/` never imports `verify_study_item`,
   `advance_study_item` or `gate/quiz.py`. A test asserts those names do not
   appear anywhere in the package, with a control so deleting the guard cannot
   leave the suite green.
2. **Route whitelist.** A test enumerates every route and asserts the set of
   non-GET paths equals an expected literal list. A new write route is a failing
   test, so adding one is a deliberate act. This is also where "never fires the
   gate" is pinned.
3. **Behavioural.** A test exercises every write route and asserts
   `count_study_items_by_state()['verified']` stays zero — mirroring the existing
   test that presses every Telegram button and asserts the same.

### Import guard, in full

`agent/api/` may not import: `sync/poller.py`, `sync/differ.py`,
`files/drive.py`, `files/ocr.py`, `files/extract.py`, `files/packs.py`, `llm/`,
`notify/`, `gate/quiz.py`.

It may import: `config`, `db/store`, `scope`, `manual`, `entries`,
`gate/timetable`, `gate/adjustments`, `gate/scheduler`, `gate/sections`,
`gate/messages` (for `document_filename`), `sync/deadlines` (for `candidates`),
`files/upload`.

`sync/deadlines` is the sharp edge — importing it for `candidates()` leaves
`scan()` one attribute away. The route whitelist covers it.

### Uploads

`upload.read_payload(path)` splits in two:

- `validate_payload(payload: bytes, filename: str) -> tuple[bytes, str]` — the
  size ceiling, suffix check, magic-byte sniff, and the "renaming a file does
  not convert it" refusal. Everything real lives here and is shared.
- `read_payload(path)` — reads the file and calls it. `agent upload` is
  unchanged.

The API checks `Content-Length` against `MAX_BYTES` **before** buffering, and
spools to a temp file **under `DATA_DIR`**, not the system temp, per invariant 5.
Then `prepare` then `commit`: the same two functions `agent upload` calls, which
gives `?dry_run=true` for free.

### Read positions

New table `read_positions` (drive_id, page_hash, page_index, updated_at).

It stores **the content hash of the last page seen**, never an index alone — the
same anchor rule as the 3d cursor, using `sections.anchor`.

`GET` returns `{"page": n}`, or `{"page": null, "changed": true, "note": "..."}`
when the hash is absent because the document was re-uploaded. Said out loud, per
DESIGN.md section 6 and per the recurring lesson: a silent fall back to page one
is a misreport.

> **This makes four things a fresh sync cannot rebuild**, not three:
> `events.notified_at`, `study_items`, `timetable_adjustments`, and now
> `read_positions`. `agent backup` includes it and `deploy/fingerprint.py`
> counts it. `api_sessions` is the opposite and is **excluded** from backup:
> restoring live sessions onto a new box is a liability for no benefit.

---

## 9. Store-layer changes

Nine assumptions of a single-threaded CLI process were surveyed. Five need code.

| # | finding | change |
|---|---|---|
| 1 | `connect()` applies the whole schema on every call (`store.py:89`) — re-reads `schema.sql`, re-runs every `CREATE TABLE IF NOT EXISTS`, and runs `INSERT OR IGNORE INTO schema_version`, which is a write | `connect(db_path, *, initialise: bool = True)`. The app calls `open_db` once at startup, so the version guard fires loudly at boot; the per-request dependency passes `initialise=False`. CLI unchanged. |
| 2 | `sqlite3.connect()` defaults to `check_same_thread=True` (`store.py:83`) | Connection per request from a dependency. **Not** `check_same_thread=False` with a shared connection — that trades a loud error for interleaved transactions, which is worse and harder to see. |
| 3 | Store writes do not commit; the caller does | Commit in the dependency, never in a route. |
| 4 | `add_milestone` is a read-modify-write (`SELECT MAX(position)` then `INSERT`) | Fails safe already — `UNIQUE (project_id, position)` makes it `IntegrityError`, not a duplicate. The API translates that to a retry and then `409`, never a `500`. |
| 5 | Default `isolation_level` of `""` — multi-statement writes are not atomic | `with conn:` in routes that write more than once. |
| 6 | `load_config()` does filesystem work every call (`config.py:387-417`) | Loaded once at startup and held. `agent serve --config` is explicit so the systemd unit is unambiguous, rather than relying on `REPO_ROOT`. |
| 7 | `timetable_mod.load()` re-parses the YAML every call | **Deliberately not cached**, with a comment saying so. The file is hand-edited; a cache means editing it does nothing until restart, which is a silent failure of its own. |
| 8 | `files/ocr.py:531` prints to stdout | Not on any API path. Noted so the survey is complete; no change. |
| 9 | No `threading`, `asyncio`, `signal`, module-level mutable globals, `lru_cache`, `input()`, `os.chdir`, or absolute paths anywhere in `src/agent/` | No change. The 5a survey's conclusion holds for 5b. `BUSY_TIMEOUT_MS` of 30 seconds already anticipates the API as a third writer. |

---

## 10. Deployment

A fifth systemd unit, `classroom-agent-api.service`: `Restart=always` and
`StartLimitIntervalSec=0`, for the same reason the bot has them — a dead
listener is a dashboard that silently stops answering.

Caddy proxies one origin to `127.0.0.1:8000`, serving the API and (at 5c) the
static client, so CORS never enters the picture.

`deploy/README.md`, its migration table, and `deploy/fingerprint.py` all learn
the fifth unit and `read_positions` as the fourth un-rebuildable table.
`fingerprint.py` still imports nothing from `agent`.

---

## 11. What this leaves open

**The gate gap.** 5b is read-only over `study_items`, so DESIGN.md section 2's
default screen — "the next single action", one primary action — renders an item
the client can show and not act on. A dashboard whose primary action cannot be
performed fails its own brief on the first screen.

This is recorded in `PLAN.md` as **Phase 5d, sequenced immediately after 5c and
counted as part of operational rather than as an extra.** In the interim, 5c's
primary action deep-links into Telegram, so it is never a dead end.

---

## 12. Slices

| slice | deliverable | done when |
|---|---|---|
| **checkpoint** | this spec, `PLAN.md`, `CLAUDE.md` | committed, suite green |
| **0** | the lift out of `cli.py`; `entries.py`; `candidates` rename | **no test file changed**, suite green |
| **1** | `agent serve`, auth, sessions, rate limiting, health/status, the connection dependency, `initialise=False`, the three guard tests | token exchanges for a cookie; no token refuses to start; range support verified |
| **2** | reads: now, gate, study items, subjects, deadlines, events, timetable | every DESIGN.md figure reachable; no percentage field exists |
| **3** | files: documents, range, ETag, text, `read_positions`, packs | bytes 0-1023 return `206`; packs question closed |
| **4** | writes: adjustments, projects, milestones, tasks, sessions, manual subjects, uploads | `verified` still zero after every write route |
| **5** | deployment: fifth unit, Caddy, `deploy/README.md`, `fingerprint.py`, `agent backup` | runbook covers five units and four un-rebuildable tables |

Tests alongside the code, not after the slice.
