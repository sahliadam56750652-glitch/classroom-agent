-- classroom-agent schema. Applied idempotently on every open_db(); there is no
-- migration framework, so every statement here is CREATE ... IF NOT EXISTS.
--
-- Conventions:
--   * Google IDs are the primary keys. They are stable and globally unique, so
--     an upsert keyed on them is naturally idempotent.
--   * Every timestamp is a UTC ISO-8601 string ('2025-03-07T23:59:59Z'), with
--     no exceptions -- the timetable used to be one and now lives in YAML.
--   * Content tables carry content_hash and first_seen_at. The hash covers only
--     the fields worth notifying about, so a teacher fixing a typo in a field we
--     do not care about produces no event. See the sync-and-diff skill.
--   * first_seen_at is written on insert and never updated.
--
-- store.open_db() enables PRAGMA foreign_keys per connection -- the pragma is
-- connection-scoped and a no-op inside a transaction, so it cannot live here.
--
-- Deliberately absent: a last_synced_at on content tables. Catch-up safety
-- (invariant 1) forbids computing "what's new" from a wall-clock window, and a
-- per-row sync timestamp is the obvious thing to reach for when doing exactly
-- that. Deletions are found by comparing the full live ID set against the
-- stored one, which needs no timestamp.
--
-- On adding columns before they are needed: there is no migration framework, so
-- a column added later means deleting the database and starting over. That is
-- cheap for the Classroom mirror, which just re-syncs from the API -- but
-- events.notified_at, study_items and quiz_attempts cannot be reconstructed
-- from anywhere. Losing them loses which alerts were already sent and
-- everything actually revised. A nullable column carried unused for a phase is
-- the cheap side of that trade.
--
-- deleted_at is a soft delete on the four resource tables plus materials. The
-- differ reconciles the full live ID set for a course against the stored one
-- and stamps whatever has vanished; rows are never removed. A teacher deleting
-- a post should not erase the fact that I already revised it, and a 404 from a
-- flaky call must not look like a deletion the next sync silently resurrects.
-- Re-seeing an item clears the stamp.
--
-- Topics are fetched (classroom.client.list_topics) but deliberately not
-- stored. Measured at 17 across 25 courses, they are far too sparse to group
-- material by, and no consumer exists yet. coursework.topic_id and
-- coursework_materials.topic_id keep the raw value when the API supplies one;
-- that is as far as topics go.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL
);

INSERT OR IGNORE INTO schema_version (id, version, applied_at)
VALUES (1, 3, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

-- Phase 2 added the `extractions` table and did NOT bump this number, which is
-- deliberate. The version guard exists for one failure it cannot otherwise
-- detect: CREATE TABLE IF NOT EXISTS cannot add a COLUMN to a table that
-- already exists, so an older file silently lacks something the code reads.
-- A whole new table has no such problem -- running this script against an
-- older file produces a genuinely complete one, so there is no incompatibility
-- to refuse and nothing for the user to do. That still holds: a later phase
-- adding a table does not touch this number.
--
-- Version 3 (Phase 3a) is the first change that DOES alter an existing table:
-- study_items gained the `reviewed` state, plus reviewed_at and skip_source.
-- SQLite cannot alter a CHECK constraint, so it is a create/copy/drop/rename
-- rebuild, and it lives in store._migrate_2_to_3() rather than here -- this
-- script only ever describes the destination shape. The INSERT above is still
-- OR IGNORE and deliberately carries nothing forward: an existing file's
-- version row is advanced by the migration that earned it, so a file that
-- never ran the migration keeps saying 2 and is refused rather than waved
-- through with the column-shaped mismatch the guard exists to catch.


-- ---------------------------------------------------------------- courses

CREATE TABLE IF NOT EXISTS courses (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    section         TEXT,
    room            TEXT,
    owner_id        TEXT,
    -- ACTIVE means only that no teacher archived the course. It is NOT a term
    -- indicator: all 18 ACTIVE courses measured on this account are from a
    -- finished academic year. The set of courses to sync comes from the
    -- hand-curated allowlist in config.yaml, never from this column.
    course_state    TEXT NOT NULL,
    enrollment_code TEXT,
    alternate_link  TEXT,
    creation_time   TEXT,
    update_time     TEXT,
    content_hash    TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL
);


-- ------------------------------------------------------------- coursework

CREATE TABLE IF NOT EXISTS coursework (
    id             TEXT PRIMARY KEY,
    course_id      TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    title          TEXT,
    description    TEXT,
    state          TEXT,
    work_type      TEXT,
    -- Topics are sparse (17 across 25 courses), so this is metadata only and
    -- carries no foreign key. Do not build grouping logic on it.
    topic_id       TEXT,
    max_points     REAL,
    -- One UTC datetime combined from the API's split dueDate + dueTime at parse
    -- time. NULL is normal and common: only 46% of measured coursework has one.
    due_at         TEXT,
    alternate_link TEXT,
    creation_time  TEXT,
    update_time    TEXT,
    content_hash   TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    deleted_at     TEXT
);

CREATE INDEX IF NOT EXISTS ix_coursework_course ON coursework (course_id);
-- The deadline scanner sweeps by due_at; NULLs are the majority so they are
-- excluded from the index rather than bloating it.
CREATE INDEX IF NOT EXISTS ix_coursework_due ON coursework (due_at)
    WHERE due_at IS NOT NULL;


-- --------------------------------------------------- coursework_materials

-- Posted material with nothing to submit -- a separate endpoint from
-- coursework, and where lecture slides usually live.
CREATE TABLE IF NOT EXISTS coursework_materials (
    id             TEXT PRIMARY KEY,
    course_id      TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    title          TEXT,
    description    TEXT,
    state          TEXT,
    topic_id       TEXT,
    alternate_link TEXT,
    creation_time  TEXT,
    update_time    TEXT,
    content_hash   TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    deleted_at     TEXT
);

CREATE INDEX IF NOT EXISTS ix_coursework_materials_course
    ON coursework_materials (course_id);


-- ---------------------------------------------------------- announcements

CREATE TABLE IF NOT EXISTS announcements (
    id             TEXT PRIMARY KEY,
    course_id      TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    text           TEXT,
    state          TEXT,
    alternate_link TEXT,
    creation_time  TEXT,
    update_time    TEXT,
    content_hash   TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    deleted_at     TEXT
);

CREATE INDEX IF NOT EXISTS ix_announcements_course ON announcements (course_id);


-- ------------------------------------------------------------ submissions

CREATE TABLE IF NOT EXISTS submissions (
    id             TEXT PRIMARY KEY,
    course_id      TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    -- Submissions arrive via courseWorkId='-', so the parent coursework row
    -- must be upserted before its submissions or this constraint fires.
    coursework_id  TEXT NOT NULL REFERENCES coursework (id) ON DELETE CASCADE,
    state          TEXT,
    late           INTEGER NOT NULL DEFAULT 0,
    -- assignedGrade is the released grade. draftGrade is deliberately not
    -- stored: it is not visible to the student in Classroom, so reacting to it
    -- would leak a grade the teacher has not released. A column that must never
    -- be read is a trap, so there is no column.
    assigned_grade REAL,
    alternate_link TEXT,
    creation_time  TEXT,
    update_time    TEXT,
    content_hash   TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    deleted_at     TEXT
);

CREATE INDEX IF NOT EXISTS ix_submissions_coursework ON submissions (coursework_id);
CREATE INDEX IF NOT EXISTS ix_submissions_course ON submissions (course_id);


-- -------------------------------------------------------------- materials

-- Polymorphic over its parent. Announcements are a first-class parent, not an
-- afterthought: measured at 211 attachments against 131 on coursework_material
-- and 33 on coursework, they carry more than the other two combined.
--
-- Attachments have no Classroom ID of their own, so `id` is synthetic and
-- built by models.material_id() as parent_type:parent_id:kind:ref. Keying on
-- `ref` rather than list position keeps the ID stable when a teacher reorders
-- or removes a sibling attachment.
CREATE TABLE IF NOT EXISTS materials (
    id           TEXT PRIMARY KEY,
    parent_type  TEXT NOT NULL
        CHECK (parent_type IN ('coursework', 'coursework_material', 'announcement')),
    parent_id    TEXT NOT NULL,
    course_id    TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL
        CHECK (kind IN ('driveFile', 'link', 'youTube', 'form')),
    -- Stable identity within the parent: Drive file ID, YouTube video ID, link
    -- URL or form URL depending on kind.
    ref          TEXT NOT NULL,
    drive_id     TEXT,
    title        TEXT,
    url          TEXT,
    -- Roughly 10% of driveFile references are dead: measured 30 pointing at
    -- trashed files and 8 returning 404. Both are steady-state conditions, not
    -- transient errors, so they are recorded rather than retried.
    trashed      INTEGER NOT NULL DEFAULT 0,
    fetch_error  TEXT,
    -- Filled in Phase 2, when attachment bytes are actually fetched from Drive.
    -- Nothing populates these yet, so a NULL here is the expected state and not
    -- a bug. They exist now only because adding them later would cost a
    -- database rebuild -- see the note on adding columns early, above.
    mime_type    TEXT,
    md5_checksum TEXT,
    local_path   TEXT,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    deleted_at    TEXT
);

CREATE INDEX IF NOT EXISTS ix_materials_parent ON materials (parent_type, parent_id);
CREATE INDEX IF NOT EXISTS ix_materials_course ON materials (course_id);
CREATE INDEX IF NOT EXISTS ix_materials_drive ON materials (drive_id)
    WHERE drive_id IS NOT NULL;


-- ----------------------------------------------------------- extractions

-- One row per Drive FILE: what it is, where its bytes landed, and what came
-- out of it as text. Everything Phase 2 discovers that Classroom cannot tell
-- us lives here.
--
-- Keyed on drive_id rather than materials.id because this describes the file,
-- not the reference to it. The materials PK is composite
-- (parent_type:parent_id:kind:ref), so one file attached to two posts is two
-- material rows -- measured 118 distinct drive_ids across 118 driveFile rows
-- today, so it has not happened yet, but keying on file identity means the
-- download happens once when it does.
--
-- status is the honest record of the ~17% of references that cannot become
-- text (measured: 16 trashed + 4 hard 404 of 118 in the tracked courses).
-- Nothing here raises on those; a fetcher that aborts on the first dead file
-- never reaches the end of the library.
--
--   fetched     bytes are on disk, nothing has read them yet
--   ok          text extracted
--   trashed     the file is in the owner's Drive trash
--   missing     404 -- deleted outright
--   unsupported a format we do not read (zip, legacy .ppt, a folder)
--   error       anything else, with the reason in `error`
--
-- fetch and extract are separate commands, so 'fetched' is a real resting
-- state and not a transient: a download that succeeds while the extractor is
-- broken must leave evidence that the bytes arrived.
CREATE TABLE IF NOT EXISTS extractions (
    drive_id      TEXT PRIMARY KEY,
    status        TEXT NOT NULL
        CHECK (status IN ('fetched', 'ok', 'trashed', 'missing', 'unsupported', 'error')),
    mime_type     TEXT,
    size_bytes    INTEGER,
    -- Drive omits md5Checksum for Google-native files, and the Phase 0 probe
    -- never requested the field, so its availability is unmeasured. Change
    -- detection falls back to modified_time when it is absent.
    md5_checksum  TEXT,
    modified_time TEXT,
    -- Both relative to config.library_dir. Invariant 5: no absolute paths in
    -- the database either, or moving DATA_DIR to the cloud VM is a rewrite.
    local_path    TEXT,
    text_path     TEXT,
    method        TEXT,
    pages         INTEGER,
    chars         INTEGER,
    -- scan_pages: pages that carry an image and almost no text, so their
    -- content is only reachable through OCR. ocr_pages: how many of those were
    -- actually read. The gap between the two is the honest measure of what the
    -- library is still missing, and summed across the corpus it is the number
    -- that decides whether OCR is worth wiring up at all.
    scan_pages    INTEGER NOT NULL DEFAULT 0,
    ocr_pages     INTEGER NOT NULL DEFAULT 0,
    fetched_at    TEXT,
    extracted_at  TEXT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS ix_extractions_status ON extractions (status);


-- ------------------------------------------------------------- ocr_pages

-- One row per page that could only be read by a vision model, and the text it
-- read. This is the quota-protection mechanism: transcription is the only part
-- of this project that costs money per unit of work, so a page is sent once
-- and its answer kept forever.
--
-- Keyed on (drive_id, page_index) because that is where the text has to be
-- spliced back in, with page_hash -- sha256 of the rendered image bytes -- as
-- the cache key. A page whose hash still matches is never sent again, and a
-- page whose hash has changed is a page the teacher actually edited.
--
-- The hash is also the dedupe key across files: the same diagram reused on two
-- decks is transcribed once and copied to the second.
--
--   ok       transcribed, text is here
--   pending  quota was spent or the API was down. Nothing was lost; the next
--            run picks it up. This is an expected resting state, not a fault.
--   error    the model refused or returned nothing usable for this page
--
-- `text` is model-generated, which nothing else in this database is. Phase 3
-- reads `status` and `model` before treating any of it as source material for
-- a quiz question.
CREATE TABLE IF NOT EXISTS ocr_pages (
    drive_id   TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    page_hash  TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('ok', 'pending', 'error')),
    text       TEXT,
    model      TEXT,
    chars      INTEGER,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    error      TEXT,
    PRIMARY KEY (drive_id, page_index)
);

-- Partial: only a successful transcription is worth reusing by content.
CREATE INDEX IF NOT EXISTS ix_ocr_pages_hash ON ocr_pages (page_hash)
    WHERE status = 'ok';
CREATE INDEX IF NOT EXISTS ix_ocr_pages_status ON ocr_pages (status);


-- ----------------------------------------------------------- ocr_sources

-- Which revision of a file the ocr_pages hashes above were computed from.
--
-- Rasterising a page is 98% of the cost of an OCR run -- measured at 18.7s
-- against 0.4s to classify the same pages -- and the hash cannot be known
-- without doing it. So a run that re-rendered everything to look up its own
-- cache spent minutes preparing pages it had already read: a --limit 2 run
-- cost 315 seconds to make two API calls.
--
-- The file's md5 settles it without rendering anything. If the bytes are
-- identical to when the pages were read, no page inside them can have changed,
-- so the stored hashes still describe the file and rendering is pointless.
-- A changed md5 invalidates every page of that file at once.
--
-- Separate from ocr_pages on purpose: this is bookkeeping about rendering, and
-- the transcriptions next door are paid for and must not be disturbed by it.
CREATE TABLE IF NOT EXISTS ocr_sources (
    drive_id   TEXT PRIMARY KEY,
    source_md5 TEXT NOT NULL,
    updated_at TEXT NOT NULL
);


-- ---------------------------------------------------------------- packs

-- One row per course whose study pack has been written, and a hash of exactly
-- the extraction state that produced it.
--
-- The hash is what makes a rebuild conditional rather than unconditional: a
-- pack is only rewritten when the text going into it actually differs, the
-- same rule the differ applies to Classroom content (invariant 2). Rebuilding
-- regardless would rewrite every file on every run, and these are meant to sit
-- in a synced folder -- an unchanged pack rewritten twice a day is a sync
-- storm and a stack of pointless file versions.
CREATE TABLE IF NOT EXISTS packs (
    course_id    TEXT PRIMARY KEY REFERENCES courses (id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    sources      INTEGER NOT NULL,
    built_at     TEXT NOT NULL
);


-- ------------------------------------------------------------ study_items

-- The revision gate's view of one piece of material: has it been delivered to
-- me, and did I verify it with a quiz? Skips are recorded as skips and never
-- as verifications -- a gate that quietly forgives makes the coverage a lie.
--
-- The four states are four different facts, and `reviewed` is here because
-- collapsing it into `delivered` would lose one of them. "The bot sent me the
-- slides and I never opened Telegram" and "I read them but the quiz could not
-- run" are not the same, and a coverage figure that cannot tell them apart is
-- wrong in the optimistic direction -- which is the direction that lets me
-- walk into a lecture believing I am ready.
--
--   pending    the gate has never sent this
--   delivered  the material message was sent successfully
--   reviewed   I said I read it, or a quiz started. No quiz has passed.
--   verified   a quiz attempt passed. The ONLY way in is a passed attempt.
--   skipped    I opted out. Always available, always logged, never verified.
--
-- Nothing decays: no transition is ever inferred from time passing, and the
-- only way back out of `skipped` is `agent studyitems --reopen`.
--
-- skip_source separates the historical backlog recorded by
-- `agent studyitems --seed` from a skip I actually chose. Phase 4's coverage
-- denominator has to exclude a finished academic year without excluding the
-- times I ducked the gate, and matching on skip_reason text to tell them apart
-- would be a string comparison holding up an honesty guarantee.
CREATE TABLE IF NOT EXISTS study_items (
    id           INTEGER PRIMARY KEY,
    entity_type  TEXT NOT NULL
        CHECK (entity_type IN ('coursework', 'coursework_material', 'announcement')),
    entity_id    TEXT NOT NULL,
    course_id    TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    state        TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'reviewed', 'verified', 'skipped')),
    skip_reason  TEXT,
    skip_source  TEXT CHECK (skip_source IN ('seed', 'user')),
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    reviewed_at  TEXT,
    verified_at  TEXT,
    UNIQUE (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS ix_study_items_course_state
    ON study_items (course_id, state);


-- ----------------------------------------------------------------- events

-- Append-only. One row per real change, never updated except to stamp
-- notified_at. Selection for sending is always WHERE notified_at IS NULL.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    course_id   TEXT,
    payload     TEXT NOT NULL,   -- JSON: before/after for changes
    created_at  TEXT NOT NULL,
    notified_at TEXT
);

-- The deadline scanner is stateless: it recomputes candidates every run and
-- leans on this index to avoid emitting the same alert twice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_dedupe
    ON events (type, entity_type, entity_id, created_at);

CREATE INDEX IF NOT EXISTS ix_events_unnotified ON events (created_at)
    WHERE notified_at IS NULL;


-- -------------------------------------------------------------- timetable
--
-- There is deliberately no timetable table. It was declared here through
-- Phases 1 and 2 and never held a row, and Phase 3 measured what the real
-- timetable is: sessions with a kind (LEC/TUT/LAB/Project), a teacher that
-- belongs to the session rather than the subject, JOINT sessions that serve
-- two subjects with two teachers and sometimes two rooms, and versions with
-- effective dates because a provisional timetable gets revised mid-term.
--
-- The old shape -- (course_id, weekday, start_time, end_time, location) UNIQUE
-- on (course_id, weekday, start_time) -- could express none of that, and a
-- JOINT session in it is two rows that have lost the fact that they are one
-- session. Making it fit meant altering a table, which is the expensive
-- operation in a project with no migration framework.
--
-- So the timetable is a hand-edited YAML file (see timetable.example.yaml and
-- gate/timetable.py), which is what it always was: versioned, semester-scoped
-- configuration. Mirroring it into SQLite would buy nothing and add a second
-- source of truth to keep in step. Nothing queries it relationally -- ~20
-- sessions a week is a list, not a join.
--
-- The empty table is left in place in databases that already have it. This
-- script does not DROP it: a DROP here would be a statement that runs on every
-- open, against every future file, for no benefit today.


-- ---------------------------------------------------------- quiz_attempts

-- One row per quiz I actually sat. `questions` carries the whole live state of
-- the attempt as JSON, and that is deliberate rather than lazy: the columns
-- this table was declared with in Phase 1 are enough, and adding
-- `current_index` and `message_id` as real columns would mean altering an
-- existing table -- the expensive operation here (see the note above
-- study_items). The shape is:
--
--   {"model": "gemini:...", "source_hash": "...", "run_id": 12,
--    "message_id": 340, "index": 2, "pass_ratio": 0.75,
--    "questions": [{question, options[4], correct, explanation,
--                   source_file, source_page}],
--    "answers":   [{"chosen": 1, "at": "...Z"} | null, ...],
--    "flags":     [0, 1, 0, 0]}
--
-- An attempt with finished_at IS NULL is in progress. That is what a restart
-- resumes from: the bot holds nothing in memory, so killing it mid-quiz and
-- starting it again reads this row and carries on at `index`.
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id            INTEGER PRIMARY KEY,
    study_item_id INTEGER NOT NULL REFERENCES study_items (id) ON DELETE CASCADE,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    score         REAL,
    passed        INTEGER,
    questions     TEXT,   -- JSON: the generated questions and my answers
    -- Bad generated questions are the main risk to trusting the gate, and a
    -- flag from me is the only way to find them.
    flagged       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_quiz_attempts_item ON quiz_attempts (study_item_id);

-- An attempt that has not finished. Read on every restart and on every retry.
CREATE INDEX IF NOT EXISTS ix_quiz_attempts_open ON quiz_attempts (study_item_id)
    WHERE finished_at IS NULL;


-- --------------------------------------------------------- quiz_questions

-- The generated question set for one study item, cached against the text it
-- was generated from. This is the quota mechanism, and it is the same instinct
-- as ocr_pages: the free tier allows roughly 20 requests a day, so a set of
-- questions is paid for once and then reused for every retry, every restart
-- and every second look at the same lecture.
--
-- source_hash is a hash of the identity of the item's extracted text -- the
-- same fields packs._fingerprint uses, and for the same reason as invariant 2:
-- regenerate when the text actually changed, not when a timestamp moved. A
-- transcription landing on one attachment changes ocr_pages, changes the hash,
-- and correctly earns a new set of questions, because the material the quiz
-- would be asking about has genuinely grown.
--
-- `flagged` means I marked something in this set as a bad question. A flagged
-- set is never served again: the next attempt regenerates, which is the only
-- way the flag button can actually improve anything. What was wrong with it is
-- preserved in quiz_flags below, because regeneration overwrites this row.
CREATE TABLE IF NOT EXISTS quiz_questions (
    id            INTEGER PRIMARY KEY,
    study_item_id INTEGER NOT NULL REFERENCES study_items (id) ON DELETE CASCADE,
    source_hash   TEXT NOT NULL,
    model         TEXT NOT NULL,
    questions     TEXT NOT NULL,   -- JSON array, validated before it is stored
    created_at    TEXT NOT NULL,
    flagged       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (study_item_id, source_hash)
);


-- ------------------------------------------------------------- quiz_flags

-- Every question I ever flagged, kept verbatim.
--
-- Without this table the flag button is write-only: quiz_questions.flagged
-- causes the set to be regenerated and the bad question is gone before anyone
-- can look at it. Bad generated questions are the main risk to trusting the
-- gate, so the record of one has to outlive the regeneration it triggers.
--
-- Nothing reads this automatically. `agent flagged` prints it, and that is the
-- point: a flag is evidence for me, not an input to the generator.
CREATE TABLE IF NOT EXISTS quiz_flags (
    id            INTEGER PRIMARY KEY,
    study_item_id INTEGER NOT NULL REFERENCES study_items (id) ON DELETE CASCADE,
    attempt_id    INTEGER REFERENCES quiz_attempts (id) ON DELETE SET NULL,
    source_hash   TEXT,
    model         TEXT,
    question_index INTEGER NOT NULL,
    question      TEXT NOT NULL,   -- JSON: the question exactly as it was asked
    source_file   TEXT,
    source_page   INTEGER,
    flagged_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_quiz_flags_item ON quiz_flags (study_item_id);


-- --------------------------------------------------------------- gate_runs

-- One row per evening the gate prepared for a day. This is what makes the
-- prompt fire once instead of once per session: my week is ~20 sessions across
-- ~11 subjects, and a per-session prompt would arrive three times a day and be
-- muted inside a fortnight.
--
-- for_date is a LOCAL calendar date (Africa/Tunis), not an instant, and it is
-- the day being prepared FOR rather than the evening the row was written. It
-- is UNIQUE, and that is the whole idempotency mechanism -- the same instinct
-- as ux_events_dedupe on events. A second `agent gate` the same evening finds
-- the row and does nothing.
--
-- sent_at is stamped only after the message actually lands, so a crash between
-- composing and sending leaves a row that the next run will re-send. Same
-- ordering as notify/dispatch.py: send, then stamp, then commit.
--
-- `plan` is the composed plan as JSON: the day's sessions, and the subjects in
-- the order they appear on the keyboard. That order is why it is stored rather
-- than recomputed -- callback_data carries a subject INDEX into this array, so
-- a button tapped tomorrow morning must resolve to the subject it named last
-- night even if the backlog has moved underneath it.
--
-- Unlike the sync, the gate is deliberately not catch-up safe. A prompt for a
-- lecture that already happened is noise, so a gate that has not run for three
-- days fires once, for tomorrow, not three times. The backlog stays catch-up
-- safe because study_items never expire.
CREATE TABLE IF NOT EXISTS gate_runs (
    id            INTEGER PRIMARY KEY,
    for_date      TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    sent_at       TEXT,
    message_id    INTEGER,
    version_label TEXT,
    -- Set by the snooze button. The bot re-sends when it passes, and refuses
    -- to set it past the morning of for_date -- a snooze that steps over the
    -- lecture it was preparing me for is not a snooze, it is a silent skip.
    snoozed_until TEXT,
    closed_at     TEXT,
    plan          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_gate_runs_open ON gate_runs (snoozed_until)
    WHERE closed_at IS NULL;


-- --------------------------------------------------------------- bot_state

-- Small durable key/value for the long-poll loop, holding exactly one thing
-- today: the last update_id acknowledged to getUpdates.
--
-- It has to survive a restart rather than live in the process, because
-- Telegram keeps an unacknowledged update for 24 hours and re-offers it. A bot
-- that forgot its offset would either replay every button I pressed yesterday
-- or skip the one I pressed while it was down.
CREATE TABLE IF NOT EXISTS bot_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);


-- ---------------------------------------------------------- telegram_files

-- What Telegram called a file after we uploaded it. Sending the same document
-- again passes this id instead of the bytes: no upload, no 50 MB ceiling on
-- the repeat, and near-instant delivery when I re-open a lecture.
--
-- Keyed on drive_id rather than materials.id for the same reason extractions
-- is: this describes the FILE, not one post's reference to it, so a deck
-- attached to two announcements uploads once.
--
-- Nothing here is precious. If Telegram ever rejects a stale id the row is
-- deleted and the bytes are sent again, which is why there is no foreign key
-- and no attempt to keep it in step with anything.
CREATE TABLE IF NOT EXISTS telegram_files (
    drive_id    TEXT PRIMARY KEY,
    file_id     TEXT NOT NULL,
    file_size   INTEGER,
    uploaded_at TEXT NOT NULL
);


-- ------------------------------------------------------------- sync_runs

-- A sync that fails silently is worse than one that crashes, because the whole
-- promise of the system is that silence means nothing happened.
CREATE TABLE IF NOT EXISTS sync_runs (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error')),
    items_seen     TEXT,   -- JSON: {"courses": 25, "coursework": 46, ...}
    events_emitted INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS ix_sync_runs_started ON sync_runs (started_at);
