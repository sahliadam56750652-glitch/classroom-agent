---
name: google-classroom-api
description: Resource shapes, scopes, pagination rules and known traps for the Google Classroom API from a STUDENT's read-only perspective, plus fetching attachment bytes via Drive. Use this whenever the task touches courses, coursework, assignments, announcements, course materials, student submissions, grades, due dates, attachments, or Classroom OAuth scopes — even for a small change like adding one field or one list call, because the field shapes and the timezone handling are easy to get subtly wrong.
---

# Google Classroom API — student, read-only

The caller is always a student reading their own data. `userId` and
`studentId` are always the literal string `"me"`. Nothing here writes.

## Scopes

```
classroom.courses.readonly
classroom.coursework.me
classroom.courseworkmaterials.readonly
classroom.announcements.readonly
classroom.student-submissions.me.readonly
drive.readonly
```

`coursework.me` is what exposes coursework and the grades on your own
submissions. It is read-write, and there is no usable read-only alternative —
see the first trap below, which is the single most expensive thing to get
wrong here. `drive.readonly` is a restricted scope and is only needed to fetch
attachment *bytes* — Classroom itself returns file IDs, never content.

After the OAuth flow, always compare granted scopes against requested scopes
and fail loudly on a mismatch. Google can drop one silently and the resulting
403s look like missing data rather than missing permission.

## Traps that cause real bugs

**`classroom.coursework.me.readonly` cannot actually be granted.** The scope is
real and documented, but it is not selectable in the Google Cloud console's
scope picker, and the manual "Pasted Scopes" box silently ignores it — paste it
in and it simply is not there afterwards. Google then strips it from the grant
with no error: consent succeeds, every other scope arrives, and
`courses.courseWork.list` returns 403 on every course. That reads like missing
data or an admin block, which sends you hunting in entirely the wrong place.

The only workable scope for a student to read coursework is
`classroom.coursework.me`, which is **read-write**. The consequence is
structural: read-only is enforced in code, not by the scope. Never call
`create`, `patch`, `delete`, `modifyAttachments`, `turnIn`, `reclaim` or
`return` on any Classroom resource. Every call in this project is `.list()` or
`.get()`, and that is now the only thing keeping the read-only guarantee true.

Two habits keep a scope drop visible instead of silent:

- Set `OAUTHLIB_RELAX_TOKEN_SCOPE=1` before anything imports oauthlib, which
  arrives indirectly via `google_auth_oauthlib` → `requests_oauthlib`.
  Otherwise oauthlib raises its "Scope has changed" warning *as an exception*
  out of the token exchange, so a mismatch you could have reported and worked
  around instead kills the whole auth step.
- Read granted scopes from `creds.granted_scopes`, never `creds.scopes`.
  `creds.scopes` just echoes back what you requested, so "requested minus
  granted" computed from it is always empty — a clean bill of health in exactly
  the case you are checking for. If `granted_scopes` is empty (older
  `google-auth` does not always persist it through `token.json`), fall back to
  the `oauth2.googleapis.com/tokeninfo` endpoint, not to `creds.scopes`.

**Archived courses are excluded unless asked for.** Pass `courseStates`
explicitly on `courses.list` — `["ACTIVE", "ARCHIVED"]` — or last year's
courses vanish. Store `courseState` and let callers filter; never filter at
fetch time.

**`courseState` is not a term indicator.** `ACTIVE` means exactly one thing: no
teacher has archived the course. It says nothing about whether the term is
running or whether you still attend. Measured: all 18 `ACTIVE` courses on this
account are from an academic year that has already finished — the teachers
simply never archived them.

So "currently enrolled this term" cannot be inferred from `courseState`, and
anything that tries will sync last year's courses as though they were live. The
set of courses to sync comes from an explicit user-curated allowlist in config.
`courseState` is stored as metadata and drives nothing.

**Due dates are UTC and split in two.** `dueDate` is `{year, month, day}` and
`dueTime` is a separate `{hours, minutes, seconds}`, both UTC. `dueTime` is
often absent, meaning end-of-day. An assignment with no `dueDate` at all has
no deadline. Combine into a single UTC datetime at parse time and convert to
`Africa/Tunis` only for display — a 1-hour offset silently shifts a deadline
across midnight.

**Fetch all submissions per course in one call.** On
`courses.courseWork.studentSubmissions.list`, pass `courseWorkId="-"` to get
every submission in the course at once. Doing one call per assignment wastes
quota and hits rate limits on a full sync.

**Every list call paginates.** `courses`, `courseWork`, `courseWorkMaterials`,
`announcements`, `studentSubmissions`, `topics` — all return `nextPageToken`.
A missing loop looks like working code that quietly drops your later courses.

**Students only ever see `PUBLISHED` items.** Don't build UI around `DRAFT`.

**Dead attachment references are normal.** Measured on this account: of 363
`driveFile` references, 30 pointed at files already in the Drive trash and 8
returned HTTP 404 outright — roughly 10% dead. Teachers delete or move files
after posting and Classroom goes on serving the stale reference indefinitely,
so this is a steady state, not a transient glitch to retry through.

- Ask for `trashed` in the `fields` on `drive.files.get` and skip trashed files
  instead of spending a download on them. A trashed file still returns valid
  metadata, so nothing else in the response tells you it is gone.
- Treat a 404 on an attachment that appeared in a `materials[]` list as a soft
  delete: record it and move on. At ~10% prevalence, a sync that aborts on the
  first dead reference will essentially never run to completion.

**Private comments do not exist in the API.** Settled, not pending: Classroom
REST v1 has no field for private comments or class comments anywhere in its
schema. `StudentSubmission` carries only `submissionHistory`, which holds
`stateHistory` and `gradeHistory` entries — who changed state or grade and
when, never any text a teacher wrote. Comments exist in the Classroom UI only.
Do not plan features around them.

## Measured shape of my data (Phase 0 probe)

Real counts from a clean probe run against this account — measured, not
estimated. Re-measure after a new academic year rather than trusting them
forever, but until then treat them as ground truth for sizing decisions.

**Volume.** 25 courses (18 `ACTIVE`, 7 `ARCHIVED`), 46 courseWork, 93
courseWorkMaterials, 246 announcements, 17 topics, 375 attachments.

**Announcements carry the most material.** Attachments by parent:

- `announcement` — 211
- `courseWorkMaterial` — 131
- `courseWork` — 33

This is the most consequential number here. Announcements hold more attachments
than the other two sources combined, so an ingestion path that walks
`courseWork` and `courseWorkMaterials` but skips announcement `materials[]`
misses over half the course content — and misses it without erroring, which is
the worst way to lose data.

**Attachment types.** `driveFile` 363, `link` 10, `youtubeVideo` 2. Almost
everything is a Drive file. The other branches of the union are rounding errors
but still have to be branched on rather than assumed away.

**Drive mimeTypes.** PDF 230, `text/plain` 31, `.pptx` 16, `.docx` 15, JPEG 10,
MP4 6, SQL 5, Google Sheets 5, plus a long tail. PDF is the majority but not the
whole story — an extraction path that handles only PDF leaves roughly a third of
the Drive files untouched.

**Due dates are sparse.** Only 21 of 46 courseWork items carry a `dueDate`
(46%), and they sit in 10 of 25 courses — the other 15 courses yield no
deadlines at all. Deadline tracking is inherently partial: more than half of all
assignments have nothing to track. Whatever answers "how far behind am I" has to
degrade gracefully instead of assuming a due date exists.

**The first sync would emit hundreds of events.** 46 courseWork + 93
courseWorkMaterials + 246 announcements across 25 mostly-historical courses is
several hundred change events on the very first run, every one of them about
material that is already months old. A `--seed` mode that walks the whole
corpus and records it as already-notified — writing `notified_at` without
sending anything — is mandatory, not a nicety. Without it the first run floods
the notification channel with a year of backlog, which is both useless and the
fastest way to stop trusting the tool.

## Resource shapes worth knowing

**Course** — `id`, `name`, `section`, `room`, `ownerId`, `courseState`,
`enrollmentCode`, `alternateLink`, `creationTime`, `updateTime`.

**CourseWork** (assignments) — `id`, `courseId`, `title`, `description`,
`materials[]`, `state`, `alternateLink`, `creationTime`, `updateTime`,
`dueDate`, `dueTime`, `maxPoints`, `workType`
(`ASSIGNMENT` | `SHORT_ANSWER_QUESTION` | `MULTIPLE_CHOICE_QUESTION`),
`topicId`.

**CourseWorkMaterial** (posted material with no submission) — same shape
minus the grading and due-date fields. This is where lecture slides usually
live, and it is a *separate endpoint* from courseWork. Missing it means
missing most course content.

**Announcement** — `id`, `courseId`, `text`, `materials[]`, `state`,
`alternateLink`, `creationTime`, `updateTime`. Professors often attach files
here instead of using materials, so scan announcement `materials[]` too.

**StudentSubmission** — `id`, `courseId`, `courseWorkId`, `state`
(`NEW` | `CREATED` | `TURNED_IN` | `RETURNED` | `RECLAIMED_BY_STUDENT`),
`late`, `draftGrade`, `assignedGrade`, `updateTime`, `alternateLink`,
`submissionHistory[]`.

`assignedGrade` is the released grade; `draftGrade` is not yet visible to the
student and should be ignored. `submissionHistory` contains `stateHistory`
and `gradeHistory` entries — the most reliable way to detect that a grade
changed rather than first appeared.

**Topic** — fetch via `courses.topics.list` and join on `topicId`. Optional
metadata, nothing more: measured at 17 topics across 25 courses, so most
courses define none at all. Grouping logic built on topics would collapse into
one undifferentiated bucket for the majority of the material. Store `topicId`
when it is present, but derive lecture or week grouping from something else.

## Materials are a union

Each entry in `materials[]` has exactly one of these keys:

- `driveFile` → `{driveFile: {id, title, alternateLink, thumbnailUrl}, shareMode}`
- `link` → `{url, title, thumbnailUrl}`
- `youtubeVideo` → `{id, title, alternateLink, thumbnailUrl}`
- `form` → `{formUrl, responseUrl, title, thumbnailUrl}`

Branch on which key is present. Only `driveFile` is downloadable.

## Getting attachment bytes via Drive

Check `mimeType` from `drive.files.get(fileId, fields=...)` first, then:

- Binary files (`application/pdf`, images, Office formats) →
  `files.get_media(fileId=...)`
- Google-native (`application/vnd.google-apps.document` / `.presentation` /
  `.spreadsheet`) → `files.export_media(fileId=..., mimeType="application/pdf")`.
  `get_media` returns a 403 on these, which is easy to misread as a
  permissions problem.
- `application/vnd.google-apps.folder` → list children with
  `q="'<id>' in parents"` and recurse. Professors do post whole folders.

Use `md5Checksum` from the file metadata to skip re-downloading unchanged
files across syncs.

**PDFs are mixed, so OCR is required — and the decision is PER PAGE.** Extract
with PyMuPDF first, measure characters per page, and send anything under
roughly 100 to OCR. Never decide per course, never for the library as a whole,
and — the part that is easy to get wrong — **never per file.**

Measured across the tracked library: **71 of 72 PDFs average over 100
chars/page, while 322 of 1287 individual pages have almost no text.** A
per-file average classifies all but one document as native and silently loses a
quarter of the material. The Phase 0 probe's "3 of 5 files look scanned" was
measured on a five-file sample from a course that is not even tracked, and it
described the wrong shape entirely.

The real shape is not the scanned document. It is a native slide deck with
images embedded in the middle of it — diagrams, equations, code screenshots and
photographed boards — where the surrounding pages are ordinary text. Averaging
over a file hides exactly the pages that need help, because the native pages
drown them out.

Classify each page in three ways, not two:

- text at or above the threshold → **native**, keep it
- below the threshold **and the page carries an image or drawing** → **scan**,
  send this page to OCR
- below the threshold with nothing drawn on it → **blank**, a section divider.
  Do not send it. This is the difference between OCR-ing 322 pages and OCR-ing
  every sparse page in the library.

Two traps in the signal itself. The PDF producer string does not discriminate:
in the probe's five samples both the native files and the scanned ones were
written by "Microsoft: Print To PDF". And image presence alone does not either
— a genuinely native page measured 46 embedded images. Only chars/page
separates them, and image presence is a tiebreaker for the low-text pages only.

**A known limit of the threshold:** a page with a full slide of text *and* an
unreadable diagram scores above 100 and is classified native, so its diagram is
never transcribed. That is a deliberate floor on cost, not an oversight; raising
the threshold to catch those pages would send a large part of the library to a
paid API for very little gain.

**Image attachments are 100% OCR by definition.** A `image/png` or `image/jpeg`
posted as an attachment has no text layer to fall back on, so it is never
"unsupported" — it is one page, entirely unread, waiting for OCR. Route it
through the same path rather than dropping it.

## Errors

Retry with exponential backoff on `429` and `5xx`. A `403` with reason
`rateLimitExceeded` is also retryable; a `403` with an auth or policy reason
is not — surface it rather than retrying into a wall. A `404` on a resource
that appeared in a list usually means the teacher deleted it between calls;
treat it as a soft delete, not a crash.
