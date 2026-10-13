# Phase 5e — offline-first Lectern (plan only, no code)

## Context

There is no cloud server: the backend runs on this PC and only while it is on (Part A, `f2e6ee8`).
The new requirement: the Android app stays fully usable while the PC is off, from whatever it last
synced, including the course PDFs. It receives updates only while the PC is on. Today's client
can't do that:

- Every screen fetches `/api/...` live.
- The service worker keeps only tonight's document and anything I explicitly keep.
- The documents cache is named `docs-${VERSION}`, and `activate` deletes every other cache. So
  **every app update wipes every kept document** (`web/sw.js:112-124`).
- A 401 purges the API and documents caches (`web/sw.js:142-147`).
- The phone reaches the PC over plain http on the LAN, which is not a secure context, so offline
  and install don't work at all from there.

## Measured 2026-10-13 against the live DB

| | |
|---|---|
| In-scope courses | 10: 6 ACTIVE with material, 4 MANUAL with none. Archived courses are never synced. |
| In-scope files (status ok) | **60 files, 161.8 MB, 1,334 pages.** Largest 42.0 MB. NET Comp 2026 alone is 134.7 MB. |
| By type | **PPTX: 19 files, 133.5 MB (82%)**. PDF: 27 files, 26.9 MB. DOCX: 13, 1.3 MB. JPEG: 1. |
| Tasks, projects, manual sessions, uploads | 0, 0, 0, 0 |
| `read_positions` | 0. Evidence for 3d stage 2 starts accruing now. |
| `study_items` | 39 pending · 1 reviewed · 66 skipped |
| Last sync | 2026-10-13 12:27Z, ok |

Library size is not the binding problem. File format is: **the reader is PDF.js and cannot open
82% of this semester's bytes.** `DocumentOut.readable` is `true` for PPTX today
(`api/convert.py:160`), so tapping one already fails. That is an existing defect, and offline makes
it the main one.

## Architecture

```
phone (installed PWA, origin https://<pc>.<tailnet>.ts.net)
  IndexedDB  snapshot       one JSON, replaced whole on each sync, with synced_at
             outbox         writes made offline, replayed in order
             meta           last_sync, last_reachable, persist granted?, storage estimate
  Cache API  "library"      UNVERSIONED. One whole file per document, keyed by content hash
             shell-<VER>    versioned as now
  SW         serves the shell offline, and answers PDF.js Range requests by slicing the cached blob
  sync.js    the only module that talks to /api for reads; screens read the snapshot

PC (only while on)
  tailscale serve :443  ->  agent serve on 127.0.0.1:8000
  GET  /api/sync               snapshot: everything small, computed by existing functions
  GET  /api/documents/{id}/pdf  the PDF to read: the original, or one converted on the PC
  POST writes with Idempotency-Key  ->  client_ops table, so a replay applies once
```

### Secure context: agree with Tailscale

`tailscale serve` gives a real Let's Encrypt certificate on a private `ts.net` name. The API stays
bound to 127.0.0.1 and nothing is public. Better than the alternatives:

- **Self-signed certificate on the LAN:** fragile to install a CA on Android, and works at home only.
- **Cloudflare Tunnel:** public.

Three details matter:

- **The origin is the storage.** If the machine name or tailnet name changes, the phone treats it
  as a new site and the whole library is gone. Pin the machine name and record it in config. Status
  shows the origin.
- **Offline needs no Tailscale.** The installed app serves itself from its own storage. The VPN
  matters only for syncing.
- **Small fixes:**
  - `api.secure_cookie: true` works over ts.net.
  - Set `api.origin` to the ts.net URL.
  - The sign-in rate limiter keys on `request.client.host`, which behind `tailscale serve` is
    always 127.0.0.1. That is harmless with one user, but the comment should say so.

### Persistent storage

On first sync, and from the More screen, the app calls `navigator.storage.persist()`. Status always
shows whether storage is "Kept (persistent)" or "May be cleared by Android", plus
`storage.estimate()` usage against quota.

If persistence is not granted, Today shows one neutral notice with the reason ("Android may clear
the synced library"). The library is never deleted silently: a document missing from the cache
when the snapshot says it should be there shows as "not on this phone; it downloads at the next
sync". It is never shown as readable.

### What the phone stores, and delta vs full

**The snapshot (full each sync).** It holds:

- `now` plans for the next 7 dates, with the ordered candidate items per date, not just the first
- subjects with standing
- homework
- projects with milestones
- timetable for 14 days with adjustments and orphans
- library posts and document metadata
- quizzes list
- status figures
- `synced_at`

All of it is computed by the functions the GETs already call (`scheduler.plan_for`,
`entries.homework`, `deadlines.candidates`, `adjust.sessions_on`, and others), so the snapshot
cannot disagree with them. A test compares the snapshot against the individual GETs.

Full rather than delta, because it is small (tens of KB) and replacing it whole means no merge
logic, which is where sync bugs live. It is atomic: an interrupted sync leaves the previous
snapshot intact.

**Documents (delta by content hash).** The snapshot lists the in-scope documents with
`{drive_id, title, sha, bytes, pdf_url}`. The phone downloads only the hashes it lacks, one at a
time, with progress shown. It deletes cached files that are out of scope or superseded only
**after** the new snapshot is saved. Delta, because this is 160 MB and growing.

The hash is Drive's `md5_checksum`, or for a converted PDF the sha of the converted bytes.

**When it syncs.**

- On app open.
- On the `online` event.
- On returning to the foreground.
- Every 60 seconds while the app is visible and the PC is unreachable. A `/api/health` probe is
  cheap.

There is no background sync while the app is closed. Android's Periodic Background Sync is limited
to roughly once every 12 hours and is unreliable, so nothing depends on it. The app says
"Syncs while open and the PC is on."

### Writes made offline: the outbox

**In the outbox:** read, skip (with optional reason), milestone complete, timetable cancel and
move, and reading positions (last write wins per document; folds in today's `queue.js`).

**Online only, failing visibly and keeping what I typed:** creating tasks and projects, adding
milestones, and quizzes. Creation, not acting on existing rows, is where silent loss would hurt
most.

**How a queued write behaves:**

- **Each entry** stores `{op_id (uuid), kind, path, body, made_at, label}`.
- **Replay** runs in order on reconnect, before the sync. Every request carries `Idempotency-Key`.
  The server records `op_id → response` in a new `client_ops` table (a new table needs no
  migration), so a replay that loses its reply is safe to send again.
- **While pending**, the affected card shows the change as **"Read — waiting to send"** (for
  example), never as done. Today's plan skips locally-pending items: the client filters the
  server's ordered candidate list. It never re-orders it.

**Conflicts**, each shown in a "Not sent" list on Status and as a notice on the affected screen,
with Retry or Discard and never dropped silently:

| Case | Shown as |
|---|---|
| Read or skip for an item already past that state (`moved: false`) | "Already verified in Telegram; your skip was not applied." Shown once, then cleared, because it is information rather than an error. |
| Skip after the item was verified elsewhere | `advance_study_item` refuses; same message. |
| Adjustment for a session that no longer matches (file changed, or already cancelled) | The server's refusal sentence. Stays until discarded. |
| Milestone already complete | Idempotent. Cleared silently, because it is the same fact. |
| 401 during replay | The outbox is kept and the token asked for. The library and outbox are **never** purged on a 401. |

### Quizzes

Grading stays on the PC. Offline, the quiz door says "Quizzes are graded on your PC. This one
opens when the PC is reachable." An open attempt says the same. No questions are cached and no
answers are queued. Queuing answers would keep grading on the server, but it adds a Telegram/phone
conflict on one attempt; it can be a later slice if wanted.

### Staleness

Every screen header carries one line from the snapshot: "Synced 07:31 today" or "Synced Sat
19:30 · PC not reachable". Today's plans run out after 7 days, and then Today says "No plan beyond
Tue 20 Oct; last synced Tue 13 Oct" rather than showing an old item as current.

### The APK: the installed PWA first, a wrapper only if needed

**The installed PWA** gives an icon, a standalone window and offline use.

- Two things to **verify on the S25**:
  1. Whether Chrome mints a WebAPK from a ts.net origin, or falls back to a shortcut. The WebAPK
     minting server may need to fetch the manifest.
  2. Whether `persist()` is granted. Chrome usually grants it to installed PWAs.
- If both hold, there is no wrapper.

**TWA** — not recommended.
- Digital Asset Links are verified by fetching `/.well-known/assetlinks.json` through Google's
  servers. A ts.net name is not publicly resolvable, so verification almost certainly fails and the
  TWA shows a browser bar.
- Bubblewrap also needs Node.

**Wrapper option (only if persistence or the WebAPK fails).** A hand-written Android project:
- one Activity, a WebView, and androidx `WebViewAssetLoader` serving a copy of `web/` from APK
  assets
- built with the Gradle wrapper and the Android SDK, without Node (Capacitor's CLI would need it)
- it opens with no server at all, and its IndexedDB is app data, cleared only by uninstall

Costs:
- the API must accept a bearer token alongside the cookie, because a cross-origin WebView cookie
  is unreliable
- CORS for one origin
- an APK rebuild for each client change
- a second shell to keep in step

The toolchain on this machine was not verified (that probe was declined), so check `java -version`
and the SDK first.

### The evening gate and the bot

They run only while the PC is on.

- **A missed 20:00:**
  - If the PC comes on before midnight with Task Scheduler's "run as soon as possible after a
    missed start" enabled, the prompt for tomorrow goes out late. That is useful.
  - If it comes on after midnight, `for_date` is now the day after, so that day's prompt goes out
    early. The 20:00 run that evening then finds the `gate_runs` row and does nothing. Today's
    lecture's prompt is never sent, which is the gate's deliberate non-catch-up.
- **The phone covers the gap:** its Today comes from the 7-day plans, so the next action is there
  without the bot.
- **The bot:** Telegram holds button taps for up to 24 hours. Taps made while the PC is off are
  handled when `agent bot` starts, and older ones are lost.
- **Deadline alerts:** T-72/24/3 go out only while the PC is on. The scanner's suppression already
  sends only the most urgent missed threshold, and the phone shows every deadline offline.

## Slices, each one commit with the full suite green

**0. Origin and install.**
- Tailscale on PC and phone; `tailscale serve`.
- `api.origin`, plus a `pwa.origin` record shown on Status.
- A short runbook in `docs/`.
- Status shows persistence and storage.
- *Done when* the phone opens the ts.net URL, the PWA installs, and we have recorded whether it is
  a WebAPK and whether `persist()` was granted.

**1. Remove manual logging, keep manual identity.**
- Delete `/api/uploads`, `/api/sessions`, `/api/subjects/manual`, `agent upload`, `agent sessions`
  and `agent subjects --add`, along with the Add screen's upload and session forms and their tests.
- Move `resolve_subject` from `files/upload.py` into `entries.py` and delete `upload.py`.
- Keep the four `MANUAL` course rows and the `manual-` prefix guards, so tasks and projects still
  file under Calculus III and the others.
- Rename the `nothing_entered` state's words to "no Classroom; never gated".
- Leave the `manual_sessions` table empty in place.
- *Done when* a task and a project can be filed under Calculus III, `agent timetable --check` still
  says those subjects are never gated, the route whitelist and guards are updated, and nothing
  uploads.

**2. Documents the reader can open.**
- A `render` step in `agent run`, after `extract`, converts in-scope PPTX and DOCX to PDF under
  `library/pdf/`, keyed by the source md5 and skipped when unchanged.
- `DocumentOut.readable` means "a PDF exists". New fields: `pdf_url`, `sha`, `bytes`.
- Images get an `<img>` view and text gets `<pre>`.
- **Converter decision:** LibreOffice headless (recommended; needs installing, and is
  cross-platform) or PowerPoint through COM (installed, but Windows-only and would need pywin32).
- *Done when* all 19 PPTX open in the reader, and the size of the offline set is measured and
  recorded in PLAN.md.

**3. Sync API.**
- `GET /api/sync` built from existing functions; `GET /api/documents/{id}/pdf`.
- `Idempotency-Key` plus `client_ops` on the outbox's write routes.
- *Done when* a test shows the snapshot matches the individual GETs, a replayed write applies once,
  and the import and whitelist guards pass.

**4. Local store and sync engine.**
- `web/sync.js` with IndexedDB; SW rework: an unversioned library cache, Range-from-blob,
  no purge on 401.
- *Done when*, with the API killed, every screen renders from the snapshot, every in-scope
  document opens in the reader, and a VERSION bump keeps the library (render tests, server stopped
  mid-test).

**5. Screens read the snapshot.**
- Synced-at line on every header.
- A guard test: no screen module calls `/api` except `sync.js`, the quiz, and online-only forms.
- *Done when* render tests with the server down show every screen and its stamp, and stale is never
  shown as current.

**6. Outbox.**
- Read, skip, milestone, adjustments and positions; pending states; the "Not sent" list.
- *Done when* a test covers each conflict row in the table above, and a pending write is never
  rendered as done.

**7. Honest offline edges.**
- Quiz offline sentence.
- Today beyond its 7 days.
- Gate and bot behaviour written into CLAUDE.md scheduling.
- *Done when* the render tests cover these.

**8. Wrapper.** Only if slice 0 showed eviction risk or no WebAPK. Re-planned then.

Then **3d stage 2**, once `read_positions` has a week or two of real sittings.

## What breaks with my real data

- **82% of in-scope bytes are PPTX the reader can't open.** This is already broken today and
  becomes the main gap offline. Fixed in slice 2.
- **One course is 135 MB** and the largest file 42 MB. The first sync over mobile data is heavy:
  sync documents on Wi-Fi only by default, using `navigator.connection` where available, with a
  toggle.
- **Converted PDFs may be larger or smaller than the PPTX.** Unknown until measured, which slice 2
  does.
- **The 4 MANUAL courses have no material** and now never will. They sync as subjects with no
  documents.
- **`read_positions` is empty**, so the window size for 3d is still unmeasured for a while.

## What in DESIGN.md or the code cannot survive

- **`sw.js`:**
  - the versioned `DOCS` cache deleted on `activate`
  - the `signed-out` purge on a 401
  - per-URL network-first API caching with `X-Agent-Cached-At` (replaced by one snapshot stamp)
  - the `keep` / `forget` messages (replaced by sync)
- **`queue.js`** "exactly one kind of write is queued": replaced by the outbox. Its rule — creation
  fails visibly and keeps what I typed — survives for task and project creation.
- **`app.js`** asks the server before rendering the shell. Offline and with a snapshot, it must
  render. "Signed out" means only a real 401 from a reachable server, and it never wipes data.
- **Every screen's direct `api.get`:** `now.js` (TodaySessions, DueSoon), `subjects.js`, `work.js`,
  `timetable.js`, `library.js`, `status.js`, `quiz.js` (QuizEntry), and `useSubjectNames` in
  `ui.js`.
- **The reader:** PDF.js over network ranges becomes ranges served from the cached blob, and only
  PDFs.
- **The Add screen's camera upload**, built a week ago, goes with manual logging.
- **DESIGN.md §6**, "a document kept offline is fetched whole, two different requests": already
  amended in Part A. DESIGN.md §2/§4 are unchanged, though Today offline now depends on 7-day plans.
- **PLAN.md's Phase 5 "Done when"** (a public origin, a verified TWA): superseded, as noted in
  Part A.

## Verification

- `pytest`, the full suite, before every commit. New render tests that stop `agent serve` partway
  through and assert every screen still renders with its synced-at line.
- A guard test that the library cache name contains no VERSION, and a render test that bumps
  VERSION and finds the documents still cached.
- **On the S25** (cannot be automated here):
  1. Install from ts.net.
  2. Sync on Wi-Fi.
  3. Turn on airplane mode.
  4. Open three documents, including a converted PPTX.
  5. Mark one read and skip one.
  6. Turn the PC on and reconnect, and see both land.
  7. Check Status for persistence and the synced-at line.
