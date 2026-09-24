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
sync → fetch → extract → ocr → studyitems → packs → deadlines → notify, with
every stage before notify isolated so no failure can suppress the briefing. `agent fetch`,
`agent extract`, `agent ocr`, `agent packs`, `agent studyitems` and
`agent missing` all exist alongside the Phase 1 commands.

**Phase 3 — complete, in three commits.** The LLM layer and the readiness
gate: timetable configuration with versions, exceptions and joint sessions; the
evening gate batching by subject with Telegram buttons; and quiz generation,
caching, grading and flagging. `llm/provider.py` already existed and was in
production use for OCR, so Phase 3 inherited a working, tested provider rather
than starting one.

The feature the project exists for now runs end to end: the prompt arrives the
evening before, the material is delivered on a tap, and the only way to
`verified` is a quiz passed against the lecture's own text.

- **3a — complete.** Schema v3, the timetable, and the course mapping. No LLM,
  no bot, nothing sends.
- **3b — complete.** The evening gate, delivery, skip, snooze, and the
  listening bot. Deterministic throughout: the gate works, it just never says
  `verified`.
- **3c — complete.** Quiz generation, question caching, grading, and the 🚩
  flag button. The LLM enters here and nowhere else.
- **3d — approach settled, stage 1 built.** How a gate is scoped to a session's
  worth when a post is a 92-page chapter. `gate/sections.py` and
  `agent sections` compute and print the windows; nothing yet acts on them. See
  the open problem at the end of this file for what was measured, what was
  chosen, and what is deliberately waiting for September.

**Phase 6 — complete, backend and CLI.** Manual entries: material that is
real in my week and absent from every API this project can call. Seven stages,
seven commits, no `schema_version` bump — four new tables and one reserved id
prefix were enough.

- **6.1 — complete.** `scope.py` separates **tracked** (the poller's allowlist)
  from **in scope** (what this semester is about, which is what the OCR queue
  prioritises), with **local** as the union that every stage reading material
  off this disk takes. No current output moved: every course this timetable
  names is also tracked, so `local == tracked` today.
- **6.2 — complete.** `null` in the subjects map no longer means two things.
  A subject is *awaiting a course id* (a gap that closes) or *manual* (the
  permanent shape of a third of my week), and `agent timetable --check` says
  which. No new load-time validation, so the current all-null file stays valid.
- **6.3 — complete.** `manual.py`, `agent subjects [--add]`, and the three
  guards that keep a minted id out of the poller. Each is pinned by a test with
  a control, so deleting the guard cannot leave the suite green.
- **6.4 — complete.** `agent upload`. Download is the only stage an upload
  skips; extract, OCR, packs, quiz and the gate are untouched.
- **6.5 — complete.** `agent sessions` and `agent tasks`, joining the existing
  deadline scanner as extra candidate sources rather than as a second scanner.
- **6.6 — complete.** `agent projects`, milestone-based, with one deadline
  shared with a linked Classroom assignment.
- **6.7 — complete.** `agent backup` and a restore that is tested rather than
  assumed.

Phase 5c (the web UI) will call these same functions; nothing here is
CLI-shaped below `cli.py`.

**Phase 5a — planned, code and units built, not yet cut over.** The move to an
Oracle Always Free ARM instance, so that the backend is up when the laptop is
not. `deploy/` holds the runbook, the four systemd units and
`deploy/fingerprint.py`, which prints a comparable summary of a `DATA_DIR` so
that "the data arrived whole" can be checked by `diff` rather than believed. Two
small code changes went with it -- `store.BUSY_TIMEOUT_MS` and `auth.OAUTH_PORT`
-- both described under **Settled decisions**. Nothing has moved yet; the
instance does not exist.

A survey of `src/agent/` for platform assumptions found **none**: no
`sys.platform`, `os.name`, `platform.system()`, `os.startfile`, `winreg`,
`subprocess`, no absolute path in any module, no `date.today()` and no naive
`datetime.now()`. Stored library paths are relative and forward-slashed --
measured, 0 of 118 `extractions` rows carry a backslash. Invariant 5 turned out
to be enforced rather than aspirational, which is the whole reason 5a is a
directory copy.

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
now sync → fetch → extract → ocr → studyitems → packs → deadlines → notify,
with every stage before notify best-effort so no failure can suppress the
briefing. (`studyitems` was added straight after Phase 6 — see the settled
decision below.)

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
item 8 (Chapter 1, 92 pages, 14 of 14 transcribed) generated grounded questions
citing pages 14, 26, 55 and 90, the second run of the same command cost zero
requests, and the quiz arrived on the phone as one message that edits in place.

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
- **A quiz is six questions and a pass is five of them.** Settled after the
  first real one: four questions is not much evidence about a 92-page lecture,
  and the length is free — one request returns the whole set whatever its size,
  so the cost is my time and not the day's quota. Five of six rather than four
  is the arithmetic of guessing: through four options, four of six comes up
  3.8% of the time at random and 10% for someone who can eliminate one
  distractor, which is exactly the half-remembering state the gate exists to
  catch. Five of six is 1.8%. The threshold stays a fraction (0.75) because the
  denominator moves — a short set, or a question flagged out of the count.
- **Documents are delivered under their real title.** The library is keyed by
  Drive id, so lectures arrived as `11kqW48qFWWRMiWNUmOK69ZlkTQeKIgye.pdf`
  until `messages.document_filename` composed the Drive title with the
  extension of the bytes actually being sent. A `file_id` carries the name it
  was first uploaded under, so the fix also meant clearing `telegram_files` —
  which is free, and is why the schema note there says so.

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

**Requirement — a moved or cancelled session must be recordable from the app.**
Professors move and cancel individual sessions at short notice, and the only way
to record that today is to hand-edit `timetable.yaml`. On a phone at 22:00 that
does not happen, so the gate prepares me for a lecture that is not taking place
— the exact failure `exceptions:` exists to prevent, arriving one session at a
time rather than one day at a time.

The constraint is that **`timetable.yaml` stays the source of truth for the
stable weekly pattern.** Phase 3a removed the `timetable` table in favour of the
file deliberately (settled decision below), and an app that wrote sessions back
into the database would undo that by giving one source of truth two writers.

**Built, backend and CLI.** The shape below is no longer a proposal --
`timetable_adjustments`, `gate/adjustments.py` and `agent adjust` exist, and
the gate resolves against them. The web app at 5c is a second caller of the
same functions, not a second implementation.

- A separate **adjustments** layer in the database, holding dated one-off
  changes: a session moved to a different time or day, a session cancelled, an
  extra session added.
- The gate resolves the weekly pattern from the file, then applies any
  adjustment for that specific date. The file is read and never written.
- An adjustment names the session it modifies and the date it applies to, and
  never alters the pattern itself. Nothing in the file changes because a
  professor moved one Tuesday.
- This generalises `exceptions:`, which cancels whole days and is static
  configuration. An adjustment is per-session and entered while the semester is
  running.

Two consequences worth recording now, because both outlive whatever shape 5c
settles on:

- **Adjustments are entered by hand and cannot be rebuilt by re-syncing.** They
  join `events.notified_at` and `study_items` on the short list of things a
  fresh sync cannot restore, so they inherit the same backup obligation — and
  CLAUDE.md's "the only two such things" becomes three the day this ships.
  **Phase 5b makes it four**, with `read_positions`: where I stopped reading in a
  92-page chapter is a fact about me and not about the material, so no API can be
  re-asked for it. `api_sessions` is the counter-example and is deliberately left
  OUT of the backup — restoring live sessions onto a new box is a liability for
  no benefit.
- **Recording them as data yields a history the file could never give.** How
  often each session actually moves is a fact about the semester worth having.
  A file edited in place answers that only through `git log`, and
  `timetable.yaml` is gitignored, so it does not answer it at all.

**Done when** the PWA installs on the S25 Ultra from a `*.vercel.app` or
`*.pages.dev` origin, the APK verifies its Digital Asset Links against that
origin and opens with no browser chrome, the same build works on desktop,
deadline alerts still arrive by Telegram independently of the web client, and a
session the professor moved can be recorded in a few taps — changing what the
gate asks that evening with `timetable.yaml` untouched.

#### The sub-phases, and why 5d is not optional

- **5a — the move to the Oracle box.** Code and units built, not cut over. See
  the status section above and `deploy/README.md`.
- **5b — the HTTP API.** FastAPI over the existing store layer, no new business
  logic, read-only over `study_items`. Spec:
  `docs/superpowers/specs/2026-09-24-phase-5b-http-api-design.md`.
- **5c — the PWA and the APK.** The client DESIGN.md is the brief for.
- **5d — the gate's actions in the client.** Deliver, mark-read, skip and
  snooze, reached from the web client rather than only from Telegram.

**5d is sequenced immediately after 5c and counted as part of operational, not
as an extra.** 5b is deliberately read-only over `study_items`, which means the
client can render DESIGN.md's default screen and not act on it — and DESIGN.md's
default screen *is* the next single action. A dashboard whose primary action
cannot be performed fails its own brief on the first screen, so "the API is
read-only for now" is a decision that has to be paid off rather than lived with.

**In the interim, 5c's primary action deep-links into Telegram**, so it is never
a dead end. The button opens the conversation where the prompt for that item
already is; the tap that changes state still happens in `agent bot`. That is one
extra hop and it is honest about where the writer lives, which a button that
silently does nothing would not be.

`verified` is out of scope for 5d as much as for 5b. It has exactly one writer
and is reached by passing a quiz, and the quiz stays where generation is already
lazy and rate-limited.

### Phase 6 — manual entries

Everything before this phase assumes Google knows the work exists. Two things it
does not know about, and they are one problem: material that is real in my week
and absent from every API this project can call.

**6.1 — Projects.** HIDE is project-based and the briefs are verbal, so a
project reaches Classroom late or never. Last year three project deadlines
landed on one day with none of them ready, and nothing here could have warned
me, because nothing here knew they existed.

Requirements: title, subject, deliverables, team, deadline, **milestone-based
progress rather than a self-reported percentage**, an optional link to a
Classroom coursework id for the projects that do get posted, and deadlines that
join the existing T-72/24/3 scanner rather than getting a parallel one.

The milestone rule is not a UI preference. A percentage is a feeling typed into
a box, and DESIGN.md's closing constraint is that nothing may make honesty cost
me anything — a number I quietly revise upward is exactly that. A milestone
either happened or it did not, and I cannot round it.

Joining the existing scanner is load-bearing too. `sync/deadlines.py` is
stateless on purpose: it recomputes every candidate from stored state on every
run and has no "last scanned" timestamp anywhere in it, which is what lets a
laptop closed for four days still report every threshold it slept through. A
project deadline is a due date on a row, which is all that scanner needs, so it
inherits catch-up safety for nothing. A second scanner would have to re-earn it.

**6.2 — Subjects with no Classroom at all.** The 2026-27 timetable has twelve
subjects and three carry a course id today. Several will never carry one:
Calculus III, Algebra III, Philosophy and Communication have no Classroom and
are not going to acquire one. They appear in the schedule and are never gated —
which is the correct, designed behaviour for a `null` subject, and also means
roughly a third of my week is invisible to the feature this project exists for.

**`null` currently means two different things, and Phase 6 has to separate
them.** "Not joined yet" is a gap that closes when I paste an id. "There is no
Classroom and there never will be" is a subject that runs manually all year.
The `subjects:` map cannot tell them apart, and `agent timetable --check`
reports both as the same gap.

Requirements:

- Log that a session happened, with what was covered.
- Record a tutorial or exercise sheet as due, with a deadline.
- **Upload a file for that subject** — a photographed board, a classmate's
  notes, an emailed handout.
- **An uploaded file enters the existing pipeline unchanged**: extraction, OCR
  where the pages are images, study packs, quiz generation, the gate. This is
  the point of the feature and not a refinement of it. A gate covering three
  subjects out of twelve is a gate I stop believing.
- **A manually added subject needs a course-like identity**, so `study_items`,
  the `subjects:` map and the gate reference it exactly as they reference a
  Classroom course, without a Classroom course id.

#### What the existing code already permits — and the one place it fights back

Checked rather than assumed, because "enters the pipeline unchanged" is a claim
about code that is already written.

**The identity is free.** `courses.id` and `extractions.drive_id` are both
`TEXT PRIMARY KEY`. A locally minted id is structurally a course and
structurally a file, and nothing downstream inspects the shape of either.

**The pipeline can be joined one stage in.** `agent extract` selects
`FROM extractions WHERE status IN ('fetched', 'ok')`, and `fetched` means
exactly "the bytes are on disk and nothing has read them". An upload that writes
into `library/files/` and inserts a `fetched` row lands precisely where
`agent fetch` would have left it. **Download is the only stage an upload
skips**; extract, OCR, packs, quiz and gate read the database and the library
and cannot tell where the bytes came from.

**The sync already leaves manual rows alone, and it is worth writing down why.**
`store.soft_delete_missing` is what stamps `deleted_at` on anything the live
state stopped returning, and it runs per course id, for tracked courses only. A
manual course is not in `courses.tracked`, so it is never polled and never
compared against a live state it has no counterpart in. Hence a sharp
constraint: **a manual course id must never enter `courses.tracked`.** If one
does, the poller 404s on it and a single sync stamps `deleted_at` across
everything typed in by hand.

**And the conflict.** `ocr.queue` derives its top tier from that same
`courses.tracked` list — `_tier` returns tier 2, behind last year's archive, for
any course id not in it. So the two requirements pull in opposite directions. A
photographed board for a subject that meets tomorrow would sort behind 279 pages
of archived material and wait a fortnight at ~12 pages a day for the OCR the
gate needs before it can ask anything, which defeats the stated point of 6.2
exactly.

**So Phase 6 must separate "tracked" from "in scope".** Tracked is the poller's
allowlist: which Classroom courses to fetch. In scope is the queue's question:
which material belongs to the week I am actually having. They have been one list
so far only because every course came from Classroom. They stop being one list
the moment a subject does not.

#### Constraints

- **Manual rows cannot be rebuilt by re-syncing.** They join
  `events.notified_at`, `study_items` and Phase 5's session adjustments on the
  list of what no amount of re-running recovers — and unlike those, manual rows
  are the *bulk* of what a Classroom-less subject knows about itself. **Backup
  stops being prudent and becomes a requirement.** The weekly pull in
  `deploy/README.md` is the mechanism and it already exists; Phase 6 is what
  makes losing it expensive.
- **The sync must never treat a manual row as stale.** The protection today is
  that manual courses sit outside `courses.tracked` — a property to pin with a
  test, not one to remember.
- **Entry belongs in the web app.** Photographing a board is natural on a phone
  and miserable through Telegram, which is also why this is Phase 6 and not
  Phase 4.

**Done when** a project with milestones and a deadline alerts at T-72, T-24 and
T-3 exactly as a Classroom assignment does; a subject with no Classroom carries
logged sessions, a due exercise sheet and uploaded material; a photograph of a
board taken on Tuesday is extracted, transcribed, packed and asked about by the
gate on Wednesday evening through the same code path as a Drive PDF; and
`agent ocr --status` puts that photograph ahead of last year's archive rather
than behind it.

#### What Phase 6 settled

- **The identity is a reserved id prefix, not a parallel set of tables.**
  `manual-<slug>` for a course, `manual-post-<hash>` for a post,
  `manual-file-<sha256>` for a file. Every one is filename-safe (`:` is illegal
  on Windows and `library/files/<drive_id>.<ext>` puts a file id straight into a
  path) and cannot collide with a Classroom id, which is decimal digits. The
  alternative — `manual_posts` and `manual_materials` — needed `schema_version`
  4, a rebuild of `study_items` and `materials` to widen two CHECK constraints,
  and a fork of every query that unions the parent tables, for rows that are
  structurally identical.

- **The dangerous case is an upload for a TRACKED subject, and PLAN.md had not
  named it.** The stated constraint was "a manual course id must never enter
  `courses.tracked`", which the config guard handles. But a photographed board
  for UNIX puts a manual post *inside a Classroom course*, so the reconciler
  runs against it with a real course id and a live state that legitimately does
  not contain it — and `agent fetch` would hand `manual-file-…` to Drive, take
  the 404 as a dead reference, and rewrite a file sitting on the disk to
  `missing`. Hence `soft_delete_missing` excluding manual rows from the
  comparison, and `drive_references` omitting manual drive ids.

- **Manual deadlines are candidate sources, not a second scanner.** A
  `Candidate` type and one function per kind; the threshold logic, the
  most-urgent-speaks-for-all rule and the stateless recompute are untouched.
  `events.entity_type` carries no CHECK and no foreign key, so a new kind of
  deadline needed no schema change at all.

- **A linked project's deadline is the professor's, not mine.** The surviving
  candidate is keyed on the *coursework*, so events written months earlier keep
  deduplicating it; the posted due date wins because it is the fact, and the
  typed one is a memory of a verbal brief. When the assignment carries no
  `dueDate` — 54% of measured coursework, and the common case for a project —
  the project's own deadline is used, which is the reason the link is worth
  having. The alert names the project either way.

- **The backup carries Classroom `courses` rows as ANCHORS, and that was found
  by restoring rather than by thinking.** An exercise sheet recorded against
  UNIX is a manual row whose `course_id` is a Classroom course, and `courses`
  has a foreign key — so into an empty database it had nothing to attach to and
  the restore failed outright. No Classroom *content* is in the file; only the
  course rows the snapshot's own rows point at.

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

- **Hosting: this laptop through Phase 3, then an Oracle Cloud Always Free ARM
  instance in Phase 5a.** Deferred past Phase 3 because nothing in the code
  depended on it (invariant 5) and there was no forcing function; 5a is the
  phase because a web dashboard and an Android app are clients, and a client is
  useless against a backend that is only up when the laptop is.

  `VM.Standard.A1.Flex`, 2 OCPU / 12 GB — half the 4 OCPU / 24 GB Always Free
  allowance, deliberately, so a second free instance stays possible — on a
  **50 GB** boot volume rather than 200, because the 200 GB block allowance is a
  total across at most two volumes and the boot volume counts against it. The
  library is 116 MB.

  Two known hazards. **"Out of host capacity"** on A1 creation is real, has no
  workaround beyond scripted retries across every availability domain, and can
  take days — which is why this is scheduled before mid-September rather than
  after. **Idle reclamation** is live because the account stays Always Free: the
  documented criteria are ANDed over 7 days, 95th-percentile CPU below 20% and
  network below 20%, and a twice-daily sync plus an idle long-poll sits far under
  both. Hence `classroom-agent-keepalive.timer`, with the arithmetic written into
  the unit: on 2 OCPU one core at 100% reads as 50%, so clearing a 95th
  percentile of 20% needs more than 5% of samples above it — four 30-minute
  slices a day is 8.3% and clears it, one hour a day is 4.2% and does not.
  A Pay-As-You-Go upgrade would make the keepalive unnecessary and was
  considered and declined.

- **The 5a cutover order is fixed by one invisible failure.** Telegram hands
  each update to exactly ONE `getUpdates` caller, so two `agent bot` processes
  on one token split the button presses between them at random and the loser
  does nothing, silently. The Windows Startup `.vbs` is therefore disabled
  BEFORE the systemd service starts, never alongside it. Everything else in the
  migration fails loudly; this one does not.

- **`Persistent=true` on the run timer, `Persistent=false` on the gate.** The
  same asymmetry as invariant 1 versus the gate's deliberate exemption from it,
  expressed in systemd. The sync is catch-up safe, so a missed run should fire
  on boot and report the whole gap. The gate is not, on purpose — a prompt for a
  lecture that already happened is noise — so a box that boots at 06:00 must not
  send last night's gate. Also no `RandomizedDelaySec` on the run timer, because
  the schedule table requires 20:00 to follow the 19:30 sync and a random delay
  can reorder them.

- **The recovery artifact is `data/` plus the three root config files, not the
  VM.** `config.yaml`, `.env` and `timetable.yaml` live at the repo root, not
  under `DATA_DIR`, and all three are gitignored — so they arrive via neither
  `git clone` nor a `data/` copy, and a migration that copies only `DATA_DIR`
  produces a box that cannot start. This is the one place invariant 5's phrase
  "config" is not literally true, and it is worth knowing before a restore
  rather than during one.

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

  This decision constrains the Phase 5 web app rather than being reopened by
  it. The app must be able to record a moved or cancelled session, and it does
  that in a separate dated adjustments layer that the gate applies *on top of*
  the file — not by writing sessions back. The file keeps the weekly pattern
  and stays single-writer; the database holds only what is dated and one-off.

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

  **The Drive-synced-folder half of that dies at 5a.** It depends on a desktop
  sync client and the server does not have one, so on the box `packs_dir`
  points at a directory nobody can see. See the open question below; the rest
  of the decision stands.

- **NotebookLM has no usable API. This is a finding, not an assumption.**
  Checked September 2026: the consumer product still exposes no public API.
  Google documents notebook APIs for the **Enterprise** edition only, and the
  product has been rebranded to **Gemini Notebook**. Unofficial clients exist,
  and every one of them authenticates with Google session cookies against
  reverse-engineered internal RPC endpoints — a reasonable thing to have break
  under a hobby script, and an unreasonable thing to have break under something
  I rely on for exam deadlines. **The bridge stays a study pack added to a
  notebook by hand.** Nothing in the code depends on this, because a pack is a
  file wherever `packs_dir` points.

- **The Gemini free tier is ~20 requests/day, and that binds the backfill
  rather than the code.** The remaining pages drain through scheduled runs at
  the configured `ocr.run_limit`. Not worth paying to accelerate: the corpus is
  an archived academic year, so nothing in it is urgent, and the per-page cache
  means the cost is paid once and never again.

- **Because the allowance is ~12 pages a day, the OCR QUEUE ORDER decides what
  gets read at all.** Sorted by (tier, posting date descending, drive_id), where
  the tiers are tracked-and-in-timetable, tracked, then everything else. The
  reason is arithmetic rather than taste: 279 pages of archived material ahead
  of a new term's slides is a fortnight in which the gate cannot quiz on
  anything I am actually being taught, and an item it cannot read is an item it
  can only deliver.

  Two things the order deliberately does NOT depend on. Not the clock -- the
  timetable contributes the courses it NAMES, not the ones meeting this week,
  because a queue that reshuffles every Monday is invariant 1's mistake wearing
  a different hat. And not OCR progress -- nothing in the sort key changes as
  pages are transcribed, which is what lets `--limit 6` twice a day walk the
  whole queue instead of re-picking its head. Both are pinned by tests.

  `agent ocr --status` prints the head of the queue with the reason for each
  position, because an ordering nobody can inspect is one that can quietly stop
  working. `agent ocr --course <id|subject>` is the manual override; it resolves
  a subject name through `timetable.yaml` exactly and refuses anything it cannot
  match, for the same reason the gate's subject mapping is never fuzzy.

- **Timetable adjustments are a dated layer in the database; the file holds
  the pattern and is never written.** Built as backend and CLI, ahead of the
  5c app, because the gate needed it the moment a real semester started.

  **How a row names its session: (date, subject, start time).** That is how the
  printed timetable identifies a session to a person, and it survives the
  sessions being reordered in the file. It deliberately does NOT survive the
  subject being renamed. Re-binding by course id was considered and rejected:
  it is a second matching path, and a wrong match adjusts the wrong session and
  looks exactly like it working -- the same reason subject names are never
  matched approximately. A row that matches nothing becomes an ORPHAN, is never
  applied, never silently dropped, printed on every timetable view, and moved
  only by `agent adjust --repoint`, which is a command I type.

  **An adjustment beats `exceptions:` and beats a date no version covers.** A
  makeup class during a reading week is a real thing, and the adjustment is
  both more specific and more recent than the blanket rule. This forced
  `plan_for` to resolve before deciding to be silent -- it used to return
  silence before it ever looked at the sessions, so a session moved past the
  end of term would have been dropped with nothing said. A control test asserts
  an untouched holiday is still silent, so the rule cannot rot into
  "exceptions do nothing".

  **A move changes which evening gates it**, which falls out of resolving by
  date rather than by pattern, and `agent adjust` says which evening that now
  is when it records one.

  **Cancelling a JOINT session cancels the session.** It is one session, one
  room, one time, two subjects; the adjustment names one of them to identify
  it, either name finds it, and the command says both are affected before it
  does it.

  **Two adjustments on one session on one date are refused, not merged.** Which
  of them wins is not a question anything here can answer, and guessing
  silently discards one.

- **An adjustment is always for ONE date. There are no recurring adjustments.**
  Considered and rejected while the layer was being built, not deferred.

  "This lecture is at 16:00 for the rest of the month" is four rows today, and
  it stays four rows. A professor who moves a session permanently has not made
  four one-off changes -- they have changed the PATTERN, and the pattern has
  its own mechanism: a new version in `timetable.yaml` with an
  `effective_from`. That mechanism already exists, is already versioned, and is
  already the thing the gate reads first.

  The reason for rejecting rather than deferring is the same one that made the
  timetable a file in the first place: **two ways to express one thing, with no
  rule for which wins.** A recurring adjustment covering a date range and a new
  version covering an overlapping range would both claim the same Tuesday, and
  nothing in this project could say which was meant. Versions already refuse to
  overlap each other by name (`_check_no_overlap`) precisely because
  last-one-wins would be a silent answer to a question the file does not
  settle; a recurring adjustment would reintroduce exactly that ambiguity
  across two sources instead of within one.

  So the boundary is clean and worth stating as a rule rather than a
  preference: **if it happens again, it belongs in the file. If it happened
  once, it belongs in the table.** Four rows for four Tuesdays is the honest
  record of four separate Tuesdays, and the day it stops being four Tuesdays it
  stops being an adjustment.

- **`agent run` has a `studyitems` stage, after `ocr` and before `packs`.**
  Raised as an open question by Phase 6.4 and settled straight after it.

  Without it, a post whose material has just become readable does not become a
  *study item* until `agent studyitems` is typed by hand — and the gate cannot
  serve what has no item. So a board photographed on Tuesday was extracted and
  transcribed unattended by the 19:30 run, and then sat there.

  **The reason it is a defect and not a preference: new material that reaches
  the gate only if I remember a command is a silent failure, and this project
  treats those as defects.** It is the recurring lesson in its purest form —
  nothing errors, nothing is logged, the run reports success, and the only
  visible symptom is a gate that is quietly emptier than the library. That is
  the failure mode this whole file ranks worst.

  It was pre-existing and affected Classroom material equally; Phase 6 only
  made it visible, because a manual upload is the first material that arrives
  expecting to be gated the next evening.

  **The stage is strictly IN SCOPE, which is narrower than the command's
  `local`**, and that asymmetry is deliberate rather than an oversight:

  - Typed by hand, `agent studyitems` is a deliberate act with `--seed` and
    `--force` available and its output in front of me, so it reaches every
    course whose material is held.
  - Run unattended twice a day, it must never widen the backlog on its own. An
    archived or ignored course still has material in the library from earlier
    phases, and a course I track to fetch files from is not one I am being
    taught. Pending items for either would make the gate claim a backlog I do
    not have and Phase 4's coverage figure a lie — the same reason `--seed`
    exists at all.

  Three properties make it safe to schedule. It is **idempotent**:
  `ensure_study_item` is `INSERT … ON CONFLICT DO NOTHING`, so a second run is
  silent rather than duplicating. It **never seeds**: `--seed` is a first-run
  tool and an unattended pipeline must not reach for it. And it **fails safe**:
  an unreadable `timetable.yaml` makes `scope.in_scope` empty, so nothing is
  created rather than the whole library being filed under the wrong set. It is
  wrapped in `_stage()` like everything else, so its failure cannot suppress
  the briefing.

- **The gate fires ONCE per day, the evening before, covering tomorrow's
  subjects.** Not once per session. The real week is ~20 sessions across ~11
  subjects, so a per-session prompt would arrive three times a day and be muted
  inside a fortnight. A subject meeting twice tomorrow is one entry showing both
  times; a joint session expands to both of its subjects. This is also why the
  gate is deliberately not catch-up safe: a gate that has not run for three days
  fires once, for tomorrow, and not three times.

- **A quiz is six questions and a pass is 0.75 — five of six.** Four questions
  is not much evidence about a 92-page chapter, and the length is free: one
  request returns the whole set whatever its size, so the cost is my time and
  not the day's quota. Five rather than four is the arithmetic of guessing.
  Through four options, six questions:

  | | pure guess | one distractor eliminated |
  |---|---|---|
  | 3 of 6 | 16.9% | 32.0% |
  | 4 of 6 | 3.8% | 10.0% |
  | 5 of 6 | 0.5% | 1.8% |

  Four of six is a one-in-ten walk-through for someone who half-remembers the
  lecture well enough to discard one distractor — precisely the state this gate
  exists to catch. `0.75` expresses it without a special case, because 4/6 is
  0.667 and does not clear the bar.

- **`quiz.pass_threshold` is a fraction, not a count, because flagging shrinks
  the denominator.** A count would have to be re-derived every time the set is
  short or a question is flagged out of it, and the same 0.75 reads correctly at
  every length: 3 of 4, 4 of 5, 5 of 6, 6 of 7, 6 of 8.

- **A flagged question leaves the denominator rather than counting as wrong.**
  Flagging must never cost me the pass. If it did, every 🚩 would be a choice
  between being honest about a bad question and protecting my own coverage
  figure, and I would stop pressing it — which would lose the only signal there
  is about generation quality. Flagging *every* question cannot pass: that is a
  statement that the set was bad, not a way through, and it retires the set.

- **`verified` has exactly one writer: `store.verify_study_item`.** It re-reads
  the attempt it is handed and refuses one that did not pass, so the guarantee
  is a query rather than a convention. `verified` is absent from every value in
  `_TRANSITIONS`, so `advance_study_item` cannot reach it however it is called,
  and no button anywhere can — pinned by a test that presses all of them and
  asserts the count stays zero. Everything Phase 4's coverage figure claims
  rests on this one function.

- **A Telegram `file_id` caches the filename it was first uploaded under, and
  cannot be renamed.** Sending by id costs no upload and sidesteps the 50 MB
  ceiling, which is why it is always preferred — but the name travels with it.
  Changing the naming rule therefore means `DELETE FROM telegram_files`; each
  row then costs one re-upload and nothing else. That is exactly what was needed
  when delivery stopped naming lectures after their Drive id.

- **Timetable times MUST be quoted in `timetable.yaml`.** YAML 1.1 reads an
  unquoted `12:00` as the sexagesimal integer 720, and `13:45` as 825 — but
  `08:30` survives as a string, because the pattern will not start on a zero.
  So quoting only the mornings *looks* like it works and then fails every
  afternoon session. `gate/timetable.py` rejects an integer where a time was
  expected and names the quoted form it wanted, and there is a test pinning the
  asymmetry so nobody "simplifies" the quotes away.

- **The bot autostarts from a Startup `.vbs` calling `pythonw.exe`, not from a
  scheduled task.** A Task Scheduler entry that survives without a visible
  console needs the S4U logon type, which requires administrator rights this
  account does not have. The `.vbs` runs at logon under the normal user, and
  `pythonw.exe` keeps it windowless. `agent bot` is restart-safe by design, so
  killing it is harmless and the crude mechanism costs nothing. `agent run` and
  `agent gate` remain ordinary Task Scheduler entries.

  **Retired by Phase 5a.** systemd has no S4U problem, so the `.vbs` becomes
  `classroom-agent-bot.service` with `Restart=always`. The one setting there
  that is not a default is `StartLimitIntervalSec=0`: systemd would otherwise
  give up after five restarts in ten seconds and leave the unit dead, and a dead
  bot is a gate that silently stops answering taps.

- **Network errors must be caught as `OSError` AND
  `http.client.HTTPException`, never as `URLError`.** `TimeoutError`,
  `ConnectionResetError`, `socket.gaierror` and `ssl.SSLError` are *not*
  `URLError` subclasses — they are only wrapped in one during connect, not
  during read. `IncompleteRead` and `BadStatusLine` are not even `OSError`,
  which is why both trees have to be caught. A read timeout escaping an
  `except URLError` is what killed a twenty-page run with a traceback.

- **`store.connect` sets `busy_timeout`, not just `foreign_keys`.** Both pragmas
  are connection-scoped, so both have to be set in `connect()` rather than in
  `schema.sql`. WAL lets readers and one writer coexist, but two writers still
  serialise, and the server makes that routine in a way the laptop never did:
  the 19:30 `agent run` writes in bursts while the always-on `agent bot` is
  trying to commit a button press. Python's default is 5 seconds, which a sync
  burst can exceed, and the loser gets `database is locked` — which for the bot
  means a tap that does nothing. 30 seconds, and the wait only happens under
  contention. Pinned by a test that holds a write lock from one connection while
  another writes, plus a control proving the contention is real, so the pragma
  cannot be deleted with the suite still green.

- **`OAUTH_PORT` exists so the consent flow can be tunnelled.**
  `run_local_server(port=0)` picks a free port, which is right on a laptop and
  useless on a headless box: the browser that completes the flow is on the other
  machine, reached through `ssh -L <port>:localhost:<port>`, and a tunnel has to
  be opened before the port is known. Unset, it stays 0 and nothing changes. A
  value that is not a port is refused by name rather than passed through, because
  an OAuth flow that fails for a reason it will not state is the worst thing to
  be sitting in front of on a box with no browser.

- **The token moves; it is not re-consented.** Nothing in `token.json` is bound
  to a machine — it is a refresh token, a client id, and the `granted_scopes`
  list `_save_token` persists so `check_scopes` can run on every invocation. But
  this only works if the Cloud project's OAuth consent screen is **In
  production**: under *Testing*, refresh tokens expire after 7 days. On the
  laptop that is invisible, because `auth.py` deletes the dead token and opens a
  browser. On a headless box it is a hard stop arriving a week after cutover and
  presenting as "the sync stopped working" — the recurring lesson's exact shape,
  a failure that does not look like its cause. Check it before copying the
  token, not after.

- **The API authenticates with a token from `.env` exchanged for a session
  cookie, and there is no unauthenticated mode.** Not even on localhost. An
  unset token silently meaning "open" is two states indistinguishable from
  outside, which the recurring lesson below ranks as a defect in itself, so
  `agent serve` refuses to start without `WEB_API_TOKEN` rather than quietly
  serving the library to the network.

  **A cookie rather than a bearer header, and the reason is the PDF reader.** A
  browser's own viewer issuing range requests from an `iframe`, and any `img`
  tag, cannot attach an `Authorization` header. A bearer token would force the
  reader to pull each document whole through JS to attach one, which is the exact
  failure range requests exist to prevent. The cookie is load-bearing, not a
  preference.

  Sessions are ROWS (`api_sessions`), not a stateless signed cookie. A table
  costs nothing here and buys revocation, which costs everything the day the
  phone is lost.

- **The API never fires the gate, and computing a plan is not firing it.**
  `GET /api/now` calls `scheduler.plan_for` and writes nothing. It must never
  reach `create_gate_run`, `replace_gate_plan`, `mark_gate_sent`,
  `snooze_gate_run` or `close_gate_run` -- because `gate_runs.for_date` would
  then let opening the dashboard at 06:00 silently swallow that evening's real
  prompt. That is the same failure that keeps `agent gate` out of `agent run`,
  arriving through a GET instead of a schedule. Pinned by a route whitelist and
  by a test asserting `gate_runs` is untouched by a read.

- **Every API route is `def`, never `async def`.** A sync route runs in
  Starlette's threadpool, where a per-request sync `sqlite3` connection is
  correct. One `async def` route touching the store blocks the event loop and
  puts an async framework around a sync store, which is the stated reason
  `python-telegram-bot` was rejected twice. Pinned by a test that walks
  `app.routes`.

  FastAPI itself is not that rejection repeated: that one weighed a framework
  against ~120 sync lines, and this surface is ~30 endpoints with multipart,
  range requests and conditional GET. Plain `uvicorn`, never
  `uvicorn[standard]` -- the extras need a C toolchain on ARM, the same rule
  that chose `python-docx` and `python-pptx`.

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

- ~~**How study packs reach me once the server is the host.**~~ **Answered at
  Phase 5b: the web app serves them.** `GET /api/packs/{course_id}` locates the
  built pack through `store.get_pack` and streams it, which costs nothing beyond
  the file serving 5b already needed for the PDF reader. A pack reaches a
  notebook by being downloaded on whatever device is in my hand, which is the
  same manual step the Drive-synced folder was, minus the sync client the server
  does not have.

  **The `drive.file` alternative is therefore declined, and no second exception
  to invariant 6 is needed.** That is the part worth recording. Adding
  `drive.file` would have been the opposite of the first exception rather than
  another instance of it: `classroom.coursework.me` is read-write because Google
  refuses to register the `.readonly` variant, so the grant is wider than the
  intent and the restriction is enforced in code. `drive.file` would have been a
  write scope requested because the project means to write. Invariant 6 keeps
  exactly one exception, and it keeps the one it was always going to have.

- **How strictly "the API imports no pipeline stage" should be read.**
  *Decided during 5b with a recommendation; overrule it if you disagree.*

  The 5b spec listed modules `agent/api/` may not import as one flat set. Building
  it showed the set has two tiers, and conflating them would have meant moving
  pure helpers around for no change in what a route can do.

  **Tier one: must not LOAD at all**, transitively or otherwise --
  `llm/provider.py`, `notify/telegram.py`, `notify/dispatch.py`, `files/drive.py`,
  `files/ocr.py`, `sync/poller.py`, `gate/quiz.py`, `classroom/client.py`.
  Everything that talks to the outside world or spends quota. A subprocess test
  imports the app in a clean interpreter and asserts none of them is in
  `sys.modules`, which is a far stronger guarantee than a grep: a module never
  imported cannot be called by a route that forgot the rule.

  **Tier two: loaded, for a pure helper, and never called.** Three arrive
  transitively and are inert at import -- `gate/scheduler.py` imports
  `files/packs.py` for `packs.label`, `gate/sections.py` imports
  `files/extract.py` for `PAGE_BREAK`, and `sync/deadlines.py` imports
  `sync/differ.py` for the `Event` dataclass. `agent gate` loads all three too.
  The guard against *using* them is the AST identifier scan plus the route
  whitelist.

  Two functions moved to make tier one true rather than approximately true:
  `display_zone` from `digest/composer.py` and `document_filename` /
  `safe_filename` into the new `agent/filenames.py`, both re-exported from where
  they were. Reaching `document_filename` through `gate/messages.py` would have
  pulled `gate/quiz.py`, and through it a model client and the pack builder, into
  a process that must be unable to call either.

  **Recommendation: keep the two tiers.** The alternative is a second
  implementation of the naming rule, and CLAUDE.md is explicit that there is one
  -- "a document is delivered under its Drive title" is only true while one
  function decides it.

- **A report on how often each session actually moves.** *Accepted, deferred
  to November.* The Phase 5 note above is the argument for it -- *"how often
  each session actually moves is a fact about the semester worth having"* --
  and `timetable_adjustments` is the first thing in this project that can
  answer it. A file edited in place never could, and `timetable.yaml` is
  gitignored, so `git log` never could either.

  Deferred rather than built because **the data accrues from today whatever I
  do, and the report does not.** Writing it now means writing it against an
  empty table and judging by eye whether the output is useful, which is the
  same mistake as `gate.window_pages`: a number chosen before there was
  anything to measure. In November there will be a term of real movement in
  there and the shape of the report will be obvious from the data.

  **Trigger:** roughly November 2026, or the first time I ask "does this
  session always move?" and find myself counting rows in `agent adjust --all`.
  Likely home is Phase 4 alongside the coverage figure, since both report over
  history rather than acting on it.

- **An extra JOINT session.** *Deferred until one occurs.* `--extra` builds a
  single-subject session, so two teachers agreeing to share a slot the file
  does not contain cannot be recorded as one thing today; it would be two extra
  sessions at the same time, which reads oddly but loses nothing.

  **Trigger:** the first real one. Building for it now means guessing at a
  shape -- how two teachers, two rooms and two subjects arrive on one command
  line -- for an event that has never happened in a year of this timetable. The
  change would be additive when it does: `_build_extra` already produces a
  `Session`, which is the type that supports two parts.

- **Recording a swap as one event.** *Deferred until one occurs.* Two
  professors exchanging slots is two adjustments today. That is accurate and
  the day view reads correctly -- one session leaves each slot and one arrives
  -- but nothing records that the two rows are one arrangement, so removing one
  half silently leaves the other standing.

  **Trigger:** the first real swap, or the first time I remove half of one by
  mistake. Until then the pair costs one extra command and nothing else.

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

---

## Open problem — a study item is the wrong unit (approach settled in 3d)

**Approach settled in Phase 3d; the mechanism is deliberately not built yet.**

One study item is one Classroom post. That was settled in the schema for good
reasons -- a per-attachment key would have meant rebuilding the database and
losing `notified_at` on 113 events -- and for most posts it is the right unit.

It is the wrong unit for the posts that matter most. A post can be a 92-page
chapter that a professor teaches over a month. The gate treats it as a single
thing to be reviewed before a single session, so the prompt effectively asks for
the whole chapter in one evening. That is more than an evening allows, and a
gate that asks for something impossible is a gate that gets skipped -- which
converts an honest `skipped` into the normal case and makes the coverage figure
meaningless in the direction that flatters me.

What is NOT the answer: making readiness proportional, or lowering the pass
mark. Both would make the gate easier to satisfy without making it more
truthful, and the whole value of `verified` is that it is hard to reach
dishonestly.

### What 3d measured

Against the real library rather than by estimate:

- **The distribution is bimodal.** The median study item is **3 pages**. But 14
  items (22%) are over 30 pages and they hold **999 of 1290 pages (77%)**. So a
  mechanism that is a no-op below a threshold leaves seven items in ten exactly
  as they are, which is what makes one safe to add at all.
- **Most long documents have no structure to split on.** Five of the sixteen
  largest PDFs carry a bookmark table; eleven carry none. Where one exists it is
  PowerPoint's per-slide export, so a *run of identical titles is a topic*.
- **The obvious fallback does not work, and this was tested rather than
  assumed.** Taking the first non-empty line of each page as its title
  reproduces the bookmark table where one already exists (155 pages gave 22 runs
  either way) and collapses everywhere else: 126 runs from 129 pages, 57 from
  57, 27 from 29. It is the same signal, not a second one.
- **A page can be identified by its content.** A sha256 of a page's normalised
  text, falling back to its transcription where the page is a scan, is unique
  across **1093 of 1094 pages** in those documents -- zero collisions, one
  genuinely blank page.

### The approach

A **page-budget window snapped to a title run where one is in reach**, with the
cursor anchored by content hash. Rejected alternatives and why:

- **Elapsed time since posting** is rejected outright, not merely not chosen. It
  computes what to do from a wall-clock window, which is the shape invariant 1
  exists to forbid, and it produces a confident number unrelated to what I have
  actually read -- the failure the recurring lesson above is about.
- **Stored sections** are rejected because the boundary problem is unsolvable
  for eleven of sixteen real documents, so any design that *depends* on good
  boundaries bets on data that is not there; and stored sections would need
  reconciling against new pages after every re-fetch, which is a second source
  of truth about which pages belong together.
- **Doing nothing** loses the 22% of items holding 77% of the pages to Skip, and
  leaves the current strict readiness rule blocking the biggest documents
  permanently -- CHAPTER 2 is 155 pages with 26 scans and none transcribed.

Two constraints the mechanism must meet, both already designed for:

- **Questions stay grounded in what was presented.** `render_pages` gains an
  optional page range and stays the only splicer; `Sources.fingerprint` gains
  the window so each caches its own set; and since `_question_from` already
  parses `source_page`, a question citing a page outside the window is
  *detectable* and dropped. The guarantee is a comparison, not an instruction to
  the model.
- **The cursor survives a changed checksum**, because it stores the content hash
  of the last page covered rather than its index. Absent on a re-fetch means the
  slide was edited or deleted, and that is said out loud rather than silently
  falling back.

### What is built, and what is not

**Built (3d stage 1): `gate/sections.py` and `agent sections --item N
[--pages N]`.** Pure computation over the text and PDFs already on disk -- no
schema change, no gate change, no quiz change, no model call, no row written.
This is a measurement command in the same family as `agent extract --dry-run`
and `agent ocr --status`: it answers the one question a schema cannot, which is
whether the boundaries land where a person would have put them. On study item 8
they do -- 92 pages become six windows opening on "Introduction to C++",
"Basic instructions", "Iteration Statements", "Subprograms" and
"Time complexity".

**Not built, deliberately: everything that changes behaviour.** The cursor
table, the gate message, window-scoped `collect`, per-window readiness, and the
per-window `verified` rule all wait for September. The reason is that waiting
costs nothing structurally -- a cursor table is a *new* table, and this project
has no migration cost for those -- while the one number the design turns on, how
many pages a session actually covers, is a guess until a real session happens.
`gate.window_pages` is therefore an argument everywhere and a constant nowhere,
and 20 is a starting value rather than a measured one.

There are also no `pending` study items to exercise a gate against: all 67 are
`skipped` with `skip_source = 'seed'`. Nothing here should touch `study_items`,
`quiz.settle` or `verify_study_item` -- those are the honesty guarantees, and
they must not move for a problem whose parameters are still guessed.
