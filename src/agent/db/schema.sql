-- classroom-agent schema. Applied idempotently on every open_db(); there is no
-- migration framework, so every statement here is CREATE ... IF NOT EXISTS.
--
-- Conventions:
--   * Google IDs are the primary keys. They are stable and globally unique, so
--     an upsert keyed on them is naturally idempotent.
--   * Every timestamp is a UTC ISO-8601 string ('2025-03-07T23:59:59Z').
--     The one exception is timetable, which is local wall-clock by nature.
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
VALUES (1, 2, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));


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


-- ------------------------------------------------------------ study_items

-- The revision gate's view of one piece of material: has it been delivered to
-- me, and did I verify it with a quiz? Skips are recorded as skips and never
-- as verifications -- a gate that quietly forgives makes the coverage a lie.
CREATE TABLE IF NOT EXISTS study_items (
    id           INTEGER PRIMARY KEY,
    entity_type  TEXT NOT NULL
        CHECK (entity_type IN ('coursework', 'coursework_material', 'announcement')),
    entity_id    TEXT NOT NULL,
    course_id    TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    state        TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'verified', 'skipped')),
    skip_reason  TEXT,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
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

-- When each subject actually meets, used by the gate to decide what to revise
-- before which session. Times here are LOCAL wall-clock (Africa/Tunis), not
-- UTC -- a lecture at 08:00 stays at 08:00 across a DST change, so storing it
-- as an instant would be wrong.
CREATE TABLE IF NOT EXISTS timetable (
    id         INTEGER PRIMARY KEY,
    course_id  TEXT NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- Monday = 0
    start_time TEXT NOT NULL,   -- 'HH:MM' local
    end_time   TEXT,            -- 'HH:MM' local
    location   TEXT,
    UNIQUE (course_id, weekday, start_time)
);


-- ---------------------------------------------------------- quiz_attempts

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
