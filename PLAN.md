# classroom-agent — roadmap

The durable record of what is built, what comes next, and which decisions are
already settled. `CLAUDE.md` says how to build; this file says what and in what
order. If a decision appears under **Settled decisions**, it is closed — reopen
it only with new information, not with a fresh opinion.

---

## Status

**Phase 0 — complete.** OAuth end to end against the real account, seven scopes
granted (`classroom.courses.readonly`, `classroom.coursework.me`,
`classroom.courseworkmaterials.readonly`, `classroom.announcements.readonly`,
`classroom.student-submissions.me.readonly`, `classroom.topics.readonly`,
`drive.readonly`). `probe.py` walked the whole corpus and the shapes and volumes
it measured are recorded in `data/dump.json` and in the `google-classroom-api`
skill: 25 courses, 46 courseWork, 93 courseWorkMaterials, 246 announcements,
375 attachments, of which announcements carry 211 — more than coursework and
posted material combined. Check `dump.json` before guessing at a field shape.

**Phase 1 — complete.** `config.py`, `auth.py` with a granted-scope check, the
SQLite store and schema, the Classroom client, poller, hash differ, deadline
scanner, digest composer, and the Telegram notifier. `agent sync`,
`agent events`, `agent deadlines`, `agent notify` and `agent run` all exist,
each long-running one with `--dry-run`, plus `--seed` for the first-run backlog.
Runs twice daily under Windows Task Scheduler. Verified end to end against real
course data: a second consecutive sync emits zero events, and every event
carries `notified_at` exactly once.

**Phase 2 — complete.** Drive fetch, text extraction, per-page OCR through
Gemini vision, study packs, and a real pipeline: `agent run` is now
sync → fetch → extract → ocr → packs → deadlines → notify, with every stage
before notify isolated so no failure can suppress the briefing. `agent fetch`,
`agent extract`, `agent ocr`, `agent packs`, `agent studyitems` and
`agent missing` all exist alongside the Phase 1 commands.

**Phase 3 — complete, in three commits.** The LLM layer and the readiness
gate. `llm/provider.py` already existed and was in production use for OCR, so
Phase 3 inherited a working, tested provider rather than starting one.

- **3a — complete.** Schema v3, the timetable, and the course mapping. No LLM,
  no bot, nothing sends.
- **3b — complete.** The evening gate, delivery, skip, snooze, and the
  listening bot. Deterministic throughout: the gate works, it just never says
  `verified`.
- **3c — complete.** Quiz generation, question caching, grading, and the 🚩
  flag button. The LLM enters here and nowhere else.

**`study_items` is seeded: 67 rows, every one `skipped` with
`skip_source = 'seed'`.** (An earlier version of this file said the table held
zero rows. It was stale.) All five tracked courses are a finished academic
year, so creating them as `pending` would have opened the gate believing I am
~90 lectures behind and made Phase 4's coverage figure meaningless from its
first day.

The consequence for 3b and 3c is that **there are no `pending` items at all**,
so the gate has nothing to serve and cannot be exercised against a real backlog
until September's courses land. `agent studyitems --reopen <id>` is the only
way out of `skipped` and exists mainly so the gate can be tested before then.

`quiz_attempts` is in use from 3c, alongside `quiz_questions` (the cache) and
`quiz_flags` (what I marked as a bad question). The `timetable` table is gone;
see the settled decision below.

---

## Phases

### Phase 2 — files (complete)

Turn attachment references into local, readable text. Walk the `materials` rows,
resolve each `driveFile` through `files.get` (asking for `trashed` and
`md5Checksum` in `fields`), download binaries with `get_media` and Google-native
documents with `export_media(mimeType="application/pdf")`, and store the bytes
under `DATA_DIR/library/`. Extract text with PyMuPDF, measure characters per
page, and fall back to OCR below roughly 100 — the decision is per file, never
per course, because the probe found native text in some PDFs and scans in others
within the same library. Assemble the extracted text into per-lecture study
packs keyed to `study_items`. Roughly 10% of attachment references are dead —
30 trashed and 8 hard 404s out of 363 measured — so skip and record them rather
than retrying into a wall.

**Done when** every tracked course's attachments are either downloaded and
extracted, or recorded with a reason for being skipped; re-running the
downloader re-fetches nothing (md5 match); a scanned PDF and a native-text PDF
both come out as usable text; and the pack builder has a `--dry-run` that prints
what it would build without touching Drive.

**Status — complete.** `agent fetch`, `agent extract`, `agent ocr`,
`agent packs` and `agent studyitems` are built and tested, and `agent run` is
now sync → fetch → extract → ocr → packs → deadlines → notify, with every
stage before notify best-effort so no failure can suppress the briefing.

Several decisions here departed from what this section originally described,
every one driven by measurement rather than preference. They are recorded under
**Settled decisions** below rather than here, because they are closed.

Two facts about the resulting library that Phase 3 has to build on:

- **A study item is one per parent post**, in the schema's `pending` state. The
  schema already settled this; a per-attachment key would have meant rebuilding
  the database and losing `notified_at` on 113 events.
- **Roughly a sixth of the library is permanently gone** — 20 of 118 tracked
  attachments return 404, all of them in Probability & Statistics.
  `agent missing` lists them, and that course's pack is correctly empty rather
  than misleadingly thin.

`agent ocr --status` reports transcription progress per subject, which is the
figure Phase 3 needs before it can honestly generate a quiz on a subject.

### Phase 3 — LLM and the readiness gate

The first phase where the LLM appears, and it stays at the edges: prose and quiz
questions only, behind `llm/provider.py`, with a plain-template fallback so an
outage degrades the briefing rather than suppressing it. Build the timetable
(which subject meets when), the scheduler that fires a pre-session prompt, and
the quiz flow — a question generated from the study pack for the oldest
unreviewed item, one question per message edited in place, a 🚩 flag button on
every question, and a `study_items` state machine of `pending → delivered →
verified` with `skipped` always available and always logged. Quiz state lives in
the database, so a bot restart resumes a half-finished quiz. This is also the
phase where hosting moves to the Oracle box.

**Done when** a pre-session prompt arrives before a real lecture, the gate can
be completed in a few taps on a phone, a killed and restarted bot resumes
mid-quiz with no progress lost, skipping is recorded as `skipped` and never as
`verified`, and the digest still sends with the LLM provider stubbed out to
raise on every call.

**3a — complete.** `schema_version` is 3: `study_items` was rebuilt to carry a
`reviewed` state alongside `reviewed_at` and `skip_source`, the migration ran
against the real database with all 67 items and all 113 `notified_at` stamps
intact, and it takes a `.bak-v2` copy before touching anything. The timetable
moved to `timetable.yaml` with `gate/timetable.py` behind it, and
`agent timetable [--check|--on DATE]` and `agent studyitems --reopen` exist.

**Done, measured against the list above:** the prompt arrives the evening
before, the gate completes in a few taps, a killed bot resumes mid-quiz with no
progress lost (there is no resume path -- every button carries ids and every
state is a row), skipping is recorded as `skipped` and never as `verified`, and
the digest still sends with the provider stubbed to raise on every call. The
one item outstanding is hosting: the move to the Oracle box has not happened,
and nothing in the code depends on it (invariant 5).

**3b — complete.** `agent gate [--dry-run] [--on DATE] [--force]` composes one
message for tomorrow and `agent bot [--once]` listens for the buttons.
`gate_runs`, `bot_state` and `telegram_files` were added as new tables, so no
version bump was needed. Verified against the real account: the prompt for
Tue 15 Sep sent as one message with a working keyboard, a second run the same
evening sent nothing, and all three silent paths (Sunday, the 15 Oct
exception, a date before the term) produced no message.

What 3b settled:

- **Batching is per subject, per day.** A subject meeting twice tomorrow is one
  entry with both sessions shown; a joint session expands to both its subjects.
  Confirmed against the real timetable: the Wednesday Database+OS lab gates
  both.
- **Restart safety is a property of the shape, not a feature.** Every button
  carries ids only, every piece of state is a row, and the message survives on
  the phone — so there is no resume path to write and none that could be
  subtly wrong. The only thing that must persist is the getUpdates offset,
  which lives in `bot_state`.
- **A snooze is refused past 07:00 on the gated day.** Two hours at a time from
  an evening prompt reaches morning in five taps, and a snooze that outlives
  the lecture is a skip nobody recorded.
- **`verified` is unreachable from every button in 3b**, pinned by a test that
  presses all four and asserts the count stays zero.

**3c — complete.** `agent quiz --item N [--dry-run]` and `agent flagged` exist,
`LLMProvider` gained `generate_json`, and the quiz reaches `verified` from a
button. `quiz_questions` and `quiz_flags` were added as new tables, so again no
version bump. Verified against the real account and real lecture text: study
item 8 (Chapter 1, 92 pages, 14 of 14 transcribed) generated four grounded
questions citing pages 14, 26, 55 and 90, the second run of the same command
cost zero requests, and the quiz arrived on the phone as one message that edits
in place.

What 3c settled:

- **`verified` has exactly one writer**, `store.verify_study_item`, and it
  re-reads the attempt rather than trusting the caller. `advance_study_item`
  still cannot reach `verified` at all. This is the guarantee Phase 4's
  coverage figure rests on, so it is enforced by a query and not by care.
- **The cache key is the text's identity plus the prompt version.** A retry, a
  restart, or a second look costs nothing; a transcription landing changes the
  hash and earns a fresh set, because the material genuinely grew. Editing
  `PROMPT_VERSION` retires every stored set, which is what makes the prompt
  something that can be changed and tested.
- **A flagged question leaves the denominator rather than counting as wrong.**
  Otherwise flagging one is a choice between honesty and my own coverage
  figure, and I would stop flagging them. Flagging *every* question cannot
  pass: that is a statement that the set was bad, not a way through.
- **`quiz_flags` exists because retiring a set destroys the evidence.**
  `quiz_questions.flagged` makes the next attempt regenerate, which overwrites
  the bad question — so it is copied out verbatim first, and `agent flagged`
  reads it. A flag button with no reader does nothing.
- **`ocr.run_limit` dropped from 8 to 6.** 6 x 2 runs = 12 of ~20 requests a
  day, leaving ~8 for the gate. The arithmetic is written into
  `config.example.yaml` so the next person to raise it sees what they spend.
- **Every model failure has its own message.** Quota, rate limit, timeout,
  refusal, retirement, a bad key, and nothing readable are seven distinct
  outcomes, and in every one of them the item stays `reviewed`. The recurring
  lesson below, applied before it could be relearned.

Three things 3a settled that the plan did not anticipate:

- **The gate is deliberately not catch-up safe**, unlike the sync. A prompt for
  a lecture that already happened is noise, so a gate that has not run for
  three days fires once, for tomorrow, not three times. The backlog stays
  catch-up safe because `study_items` never expire.
- **`for_date` is always tomorrow**, and a tomorrow with no sessions sends
  nothing. Anything cleverer leaves a hole: under "the evening before", Sunday
  evening is Monday's only chance.
- **Holidays needed their own mechanism.** Effective dates version a timetable;
  they do not say "no classes this week", and a gate that quizzes me the night
  before a public holiday gets muted. Hence `exceptions:`.

### Phase 4 — coverage, grades, exam mode

Reporting built on what the earlier phases record. Coverage tracking answers
"how far behind am I in each subject" from `study_items` states rather than from
guesswork, and it has to degrade gracefully: only 21 of 46 coursework items
carry a due date, and they sit in 10 of 25 courses, so more than half the corpus
has no deadline to be behind on. Grade trends read from the `submissions`
history that Phase 1 already stores. Exam mode reweights the gate toward a
declared exam date, front-loading unreviewed material in that subject.

**Done when** `/status` gives a per-subject coverage figure with an honest
denominator, subjects with no due dates report as untracked rather than as
complete, a grade change shows up as a trend and not just as a one-off alert,
and exam mode measurably reorders what the gate serves.

### Phase 5 — PWA and Android APK

Replace the Telegram-only interface with a web client: a PWA served over HTTPS,
wrapped as an installable Android APK via Bubblewrap/TWA. One codebase serves
desktop web, the installed PWA, and a real APK with a home-screen icon. The API
and the static client are served from one origin so CORS never enters the
picture. Telegram does not go away — see the settled decision below.

**Done when** the PWA installs on the S25 Ultra from a `*.vercel.app` or
`*.pages.dev` origin, the APK verifies its Digital Asset Links against that
origin and opens with no browser chrome, the same build works on desktop, and
deadline alerts still arrive by Telegram independently of the web client.

---

## Settled decisions — do not revisit

- **Telegram is the interface through Phase 4.** WhatsApp was rejected:
  business-initiated messages require pre-approved templates and cost money per
  conversation.

- **The Phase 5 client is a PWA, not a native app**, wrapped as an installable
  Android APK via Bubblewrap/TWA. Native was rejected on two grounds: the sync
  runs server-side, so on-device background work buys nothing, and a native
  Android app loses the desktop.

- **The target device is a Samsung S25 Ultra, not an iPhone.** No Apple
  Developer account is needed. If S Pen annotation ever matters, the TWA shell
  can host native surfaces without a rewrite.

- **Telegram keeps deadline alerts even after the PWA exists.** Web push is for
  softer nudges. A missed deadline alert is the one failure this project cannot
  afford, so it stays on the channel that has already proven it delivers.

- **Hosting: this laptop through Phase 2, then an Oracle Cloud Always Free ARM
  instance from Phase 3.** The free tier was cut to 2 OCPU / 12 GB in 2026,
  still far more than this needs. Two known hazards: "Out of host capacity" on
  instance creation, and idle reclamation below roughly 10% CPU and low network
  over 7 days — a twice-daily sync is close to idle, so it needs a keepalive
  cron or a Pay-As-You-Go upgrade.

- **No custom domain.** A free `*.vercel.app` or `*.pages.dev` subdomain serves
  the PWA and the `assetlinks.json` the APK needs. The likely final shape is
  Caddy on the Oracle box serving both the API and the static PWA from one
  origin, avoiding CORS entirely.

- **Portability is why invariant 5 exists.** Moving to a paid VPS must be
  copying one directory, not a rebuild. No absolute paths, ever.

- **OCR uses a vision model, not Tesseract.** The pages needing OCR are not
  scanned documents — they are images embedded *inside* otherwise-native slide
  decks: diagrams, equations, code screenshots and photographed boards.
  Tesseract is at its worst on exactly that content and fails silently, giving
  back line noise that looks like text. Verified on real material, the vision
  model preserves LaTeX, transcribes queue traces step by step, and recovers
  weighted edge lists from graph images. A quarter of this library was too much
  to lose to plausible-looking garbage.

- **The OCR decision is PER PAGE, not per file.** Measured: 71 of 72 PDFs
  average over 100 chars/page, while 322 individual pages have almost no text.
  A per-file average classifies all but one document as native and silently
  loses a quarter of the material. The `google-classroom-api` skill said "per
  file" and was corrected.

- **The timetable is a YAML file, not a SQLite table.** The declared-but-empty
  `timetable` table was removed in Phase 3a. It could not express any of what
  the real timetable is: session kind (LEC/TUT/LAB/Project), a teacher that
  belongs to the session rather than the subject, JOINT sessions serving two
  subjects with two teachers and sometimes two rooms, or versions with
  effective dates. A JOINT session in that shape was two rows that had lost the
  fact that they were one session, and `UNIQUE (course_id, weekday,
  start_time)` actively fought it. Nothing queries a timetable relationally —
  ~20 sessions a week is a list, not a join — so mirroring hand-edited
  configuration into the database would have bought nothing and added a second
  source of truth. Existing databases keep the empty table, orphaned; the
  schema does not `DROP` it, because that would be a statement running on every
  open of every future file to tidy one empty table once.

- **Subject-to-course mapping is explicit and never fuzzy.** "Database" vs
  "Database GA 2026". A session naming a subject absent from the `subjects:`
  map is a load error, not a near-match, because a wrong match gates the wrong
  subject and looks exactly like the gate working. A subject mapped to `null`
  is legal and normal — six of eleven have no Classroom course — and is listed
  in the schedule but never gated.

- **Study packs are local files only.** `drive.readonly` cannot upload and
  invariant 6 forbids writing to Drive. Pointing `packs_dir` at a Drive-synced
  folder is how they reach NotebookLM — which has no API either, so a pack
  arrives there as a file whatever this project does.

- **The Gemini free tier is ~20 requests/day, and that binds the backfill
  rather than the code.** The remaining pages drain through scheduled runs at
  the configured `ocr.run_limit`. Not worth paying to accelerate: the corpus is
  an archived academic year, so nothing in it is urgent, and the per-page cache
  means the cost is paid once and never again.

- **Network errors must be caught as `OSError` AND
  `http.client.HTTPException`, never as `URLError`.** `TimeoutError`,
  `ConnectionResetError`, `socket.gaierror` and `ssl.SSLError` are *not*
  `URLError` subclasses — they are only wrapped in one during connect, not
  during read. `IncompleteRead` and `BadStatusLine` are not even `OSError`,
  which is why both trees have to be caught. A read timeout escaping an
  `except URLError` is what killed a twenty-page run with a traceback.

---

## Recurring lesson — misreporting is its own defect

Four separate incidents in Phase 2 cost real time, and all four were failures
**misreported rather than mishandled**. In every case the underlying code did
roughly the right thing; what went wrong is that the output described it as
something else.

- A **per-minute rate limit** was reported as a daily cap, because the
  classifier read only the `quotaId` and the free tier's violation names no
  window. The response said "please retry in 16.27s" — plainly not a limit
  that resets tomorrow.
- A **retired model** (a hard 404 on every request) surfaced as quota
  exhaustion, because a 404 was folded into a generic API error and the caller
  turned that into 323 pages marked "pending".
- An **unattempted page was indistinguishable from a failed one**: both read
  as `pending`, so `--limit 1`, `--limit 10` and `--limit 100` produced
  byte-identical output and a dead API key, a retired model and a broken TLS
  chain all looked exactly alike.
- A **socket timeout escaped as a crash** rather than a report, because it is
  not a `URLError` and nothing else caught it.

The rule this leaves behind: **when a summary cannot distinguish two states,
that is a defect in itself**, and it deserves the same weight as a wrong
result. The states have to be separated at the point where they are known —
which usually means the error type, not the log line. Concretely, that is why
the run summary now reports calls attempted apart from pages pending, why
`ocr_pages.error` is surfaced with counts instead of sitting unread in the
database, and why quota, rate limit, timeout, refusal, retirement and auth
failure are six distinct exception types rather than one.

This is the same instinct as the conventions in `CLAUDE.md`: a failure that
produces no visible output is the worst failure mode here. A failure that
produces *misleading* output is the second worst, and it is harder to notice.

---

## Current data situation

All 25 courses belong to a finished academic year and are being archived. No
live courses are expected until roughly mid-September 2026.

Archiving does not break the sync. The poller fetches by tracked course ID, and
archived courses stay fully readable — `courses.list` is called with
`courseStates` of both `ACTIVE` and `ARCHIVED`, and `courseState` is stored as
metadata that drives nothing. Expect silence from the bot until new courses
appear; silence means nothing changed, which is the intended behaviour and not a
fault to debug.

When the new term starts: re-run `agent courses`, curate `courses.tracked` in
`config.yaml` by hand, and seed the new courses so the first sync does not
deliver a wall of backlog. `courseState: ACTIVE` still does not mean the course
is running — the tracked list is curated by hand and always will be.

---

## Open questions

- ~~**Which LLM provider for Phase 3.**~~ **Answered by Phase 2.** Gemini is in
  production use for OCR behind `llm/provider.py`, which now has a tested retry
  policy, quota handling and error taxonomy. Phase 3 inherits it rather than
  choosing. The interface still exists so the answer can change without
  touching the gate — and the model *name* is already configurable via
  `GEMINI_MODEL`, because a model being retired underneath this project has
  happened once and will happen again.

- **Whether NotebookLM stays the study surface.** Still open, but narrower:
  packs are built and land wherever `packs_dir` points, so nothing in the code
  depends on the answer. If NotebookLM stays, Phase 3 skips a vector store
  entirely. If it does not, retrieval becomes part of Phase 3 and the packs are
  the corpus it would index. Resolve before writing any retrieval code.

- ~~**How much of the library the gate should require before it starts.**~~
  **Answered, and the framing was wrong.** Readiness is a property of the
  **study item**, not the subject: within DSA, one item is 14/14 transcribed
  and fully quizzable while another is 0/26, so blocking the whole subject
  would block one that is mostly ready. The rule is strict per item — any
  untranscribed page means the item is delivered and can reach `reviewed`, but
  never `verified`, and the message says which pages are missing. Subject-level
  readiness is only the aggregate, for display.

  What this costs, measured: 184 of AI's 212 scan pages are unread, and OS has
  41 of 41 — OCR has never touched that course. Under the strict rule almost
  every AI and OS item is unquizzable until the backlog drains at ~16/day. That
  is the right answer and it must not read as a bug.

  A separate case the strict rule does not cover: **Probability & Statistics
  has no study items at all**, because all 20 of its attachments are 404. An
  empty subject must render as "no readable material" and never as "up to
  date".
