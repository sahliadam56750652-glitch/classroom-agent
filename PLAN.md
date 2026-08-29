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
