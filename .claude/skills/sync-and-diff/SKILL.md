---
name: sync-and-diff
description: Correctness patterns for the poll → store → diff → notify pipeline — idempotent upserts, content-hash change detection, the events table, once-only notification, and deadline event generation. Use this for any work on the poller, differ, deadline scanner, store layer, database schema, or notification dispatch, and any time the question "is this new?" or "did this change?" comes up, because the failure modes here are silent duplicates and silent misses rather than exceptions.
---

# Sync and diff

The value of this system is that I trust it. A duplicate alert is annoying; a
missed deadline destroys the whole thing. Every rule below exists to protect
one of those two properties.

## The pipeline

```
poll (fetch live state)
  → upsert (store raw current state)
    → diff (compare against previous snapshot)
      → events (append-only row per real change)
        → notify (send, then stamp notified_at)
```

Each stage is separately runnable and separately testable. Never fuse them.

## Catch-up safety

**Never compute "what's new" from a time window.** No `WHERE updateTime >
last_run`. The differ compares the full live state against the full stored
state, so a sync that hasn't run in ten days produces exactly the same events
as ten daily syncs would have — no gaps, no repeats. My laptop will be closed
for days at a time and the system has to survive that without me thinking
about it.

## Idempotent upserts

Google IDs are stable and globally unique — use them as primary keys.

```sql
INSERT INTO coursework (id, course_id, title, ...)
VALUES (?, ?, ?, ...)
ON CONFLICT(id) DO UPDATE SET
  title = excluded.title, ...
```

Running a sync twice in a row must produce zero events the second time. This
is the single most useful test in the project — write it first.

## Diff on content hashes

`updateTime` changes when a teacher fixes a typo, reorders material, or
touches a course in ways that never reach a student. Timestamp-based diffing
turns that into notification spam and trains me to ignore the bot.

Store a `content_hash` per row: a stable hash over only the fields that
matter for notification.

- **coursework** — title, description, due datetime, maxPoints, state,
  material IDs
- **materials** — title, description, material IDs
- **announcements** — text, material IDs
- **submissions** — state, late, assignedGrade

Hash canonically: sort keys, normalise `None` vs missing, then hash the JSON.
Two syncs of unchanged data must produce a byte-identical hash.

Emit a change event only when the stored hash differs from the computed one.
Then compare individual fields to decide *which* event type — a due-date move
and a title fix are not the same news.

## The events table

Append-only. One row per real change. Never updated except to stamp
`notified_at`.

```sql
CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  type        TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id   TEXT NOT NULL,
  course_id   TEXT,
  payload     TEXT NOT NULL,   -- JSON: before/after for changes
  created_at  TEXT NOT NULL,
  notified_at TEXT
);
CREATE UNIQUE INDEX ux_events_dedupe
  ON events(type, entity_type, entity_id, created_at);
```

Types: `new_coursework`, `new_material`, `new_announcement`,
`coursework_updated`, `announcement_updated`, `material_updated`,
`due_date_changed`, `grade_posted`, `grade_changed`,
`submission_state_changed`, `deadline_t72`, `deadline_t24`, `deadline_t3`.

`announcement_updated` and `material_updated` are not optional polish:
announcements are the primary attachment channel in this account, carrying 211
of 375 measured attachments — more than coursework and posted material
combined — so a file added to an existing announcement is a main way content
arrives, not an edge case.

The payload carries before/after values so the digest can say what actually
changed without re-querying. For the two `*_updated` attachment-bearing types
that means an `attachments_added` boolean alongside `added_count`,
`removed_count` and `text_changed`: new files are worth reading, a reworded
sentence is not, and the digest must be able to tell them apart without going
back to the database.

## Notify exactly once

Selection is always `WHERE notified_at IS NULL`. Stamp it in the same
transaction as a *successful* send. If the send fails, leave it null and let
the next run retry — an event delivered twice is a bug, an event delivered
late is not.

## First run

The first sync would otherwise emit an event for every item in every course
and produce an unreadable wall of text. Support a `--seed` mode that
populates the tables and marks all generated events as already notified.
Everything after that is genuinely new.

## Deadline events

Deadlines are derived, not fetched, and the scanner is stateless — it
recomputes candidates on every run and relies on the events unique index to
avoid duplicates.

Emit `deadline_t72` / `t24` / `t3` only when **all** of these hold:

- the coursework has a due datetime
- the submission state is not `TURNED_IN` and not `RETURNED`
- now is past the threshold and before the deadline
- no event of that type exists for that coursework

A deadline that has already passed generates nothing. Missing that condition
means being told at 03:00 about something due last month.

## Grade events

Distinguish two cases against the previously stored value:

- previous `assignedGrade` was null, now set → `grade_posted`
- previous was set and differs → `grade_changed` (a regrade — always worth
  telling me about)

Ignore `draftGrade` entirely. It isn't visible to students and reacting to it
would leak information the teacher hasn't released.

## Observability

Every run writes a `sync_runs` row: started, finished, status, items seen per
resource type, events emitted, error text. A sync that fails silently is
worse than one that crashes, because the system's whole promise is that
silence means nothing happened.
