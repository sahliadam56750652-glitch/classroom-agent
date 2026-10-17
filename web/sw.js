// The service worker. DESIGN.md section 6, as amended for Phase 5e.
//
// The backend is a PC that is on only some of the time, so the phone holds its
// own copy: the snapshot in IndexedDB (local.js, sync.js) and the documents in
// one cache, LIBRARY. Three policies:
//
//   SHELL      precache, cache-first, VERSIONED. A new app replaces it.
//   LIBRARY    documents, UNVERSIONED. Nothing in this file ever deletes it --
//              not an update, not a sign-out, not a 401. Losing the library is
//              the one failure here that cannot be undone while the PC is off.
//   API        not cached here at all. Reads come from the snapshot; writes are
//              the page's problem.
//
// One hard constraint shapes the documents: **the Cache API cannot store a
// 206.** A kept document is fetched WHOLE, once, on purpose, and reading online
// still uses range requests.

// A hash of every file in SHELL_FILES, stamped by `agent webcontract`. Never
// edited by hand: the shell is served cache-first, so a changed file reaches an
// installed app ONLY when this changes, and a forgotten bump looks exactly like
// a fix that did not work. A test fails when it is stale.
const VERSION = "d0e78ac31697";
const SHELL = `shell-${VERSION}`;

// The documents' cache. Its name carries NO version, on purpose: every cache
// named after a version is deleted when the next version activates, and that
// is exactly how an app update used to wipe every kept document.
// tests/test_web_assets.py refuses a VERSION anywhere in this name.
const LIBRARY = "library";

// Only caches of these kinds are ever deleted, and only old versions of them.
// `api-` caches are from before 5e, when API reads were cached here; the
// snapshot replaced them. A `docs-` cache from before 5e is never deleted: it may
// hold a document I kept, and caches.match() still finds it.
const DISPOSABLE = /^(shell|api)-/;

// Everything needed to render something rather than nothing. The PDF.js worker
// is NOT here: 1.4 MB that only the reader needs, and precaching it would make
// every first load pay for a screen most opens never reach.
const SHELL_FILES = [
  "/",
  "/index.html",
  "/app.css",
  "/fonts/atkinson-next-latin.woff2",
  "/fonts/atkinson-next-latin-ext.woff2",
  "/fonts/bricolage-latin.woff2",
  "/fonts/bricolage-latin-ext.woff2",
  "/app.js",
  "/api.js",
  "/local.js",
  "/sync.js",
  "/synced.js",
  "/html.js",
  "/format.js",
  "/router.js",
  "/contract.js",
  "/icons.js",
  "/ui.js",
  "/subject-color.js",
  "/theme.js",
  "/greeting.js",
  "/styles/more.css",
  "/styles/now.css",
  "/styles/subjects.css",
  "/styles/quiz.css",
  "/styles/timetable.css",
  "/styles/work.css",
  "/styles/library.css",
  "/styles/add.css",
  "/styles/status.css",
  "/styles/reader.css",
  "/screens/now.js",
  "/screens/subjects.js",
  "/screens/status.js",
  "/screens/library.js",
  "/screens/timetable.js",
  "/screens/work.js",
  "/screens/add.js",
  "/screens/more.js",
  "/screens/quiz.js",
  "/outbox.js",
  "/reader/reader.js",
  "/reader/zoom.js",
  "/icon.svg",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-192-maskable.png",
  "/icon-512-maskable.png",
  "/manifest.webmanifest",
  "/vendor/preact.module.js",
  "/vendor/preact-hooks.module.js",
  "/vendor/htm.module.js",
];

// Set by the page when it knows tonight's document. Kept in a cache entry rather
// than in a variable, because a service worker is terminated between events and
// a variable would not survive.
const KEEP_MARKER = "/__keep__";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      // Individually, not `addAll`: one missing file fails the whole batch and
      // leaves NOTHING precached, which turns a typo in this list into an app
      // that is simply never available offline.
      .then((cache) =>
        Promise.all(
          SHELL_FILES.map((path) =>
            // `reload` skips the HTTP cache. Without it a new worker could
            // precache the OLD app.js from the browser's own cache, under the
            // new version's name, and the update would install and change
            // nothing.
            cache.add(new Request(path, { cache: "reload" })).catch((err) => {
              console.warn("[sw] could not precache", path, err);
            })
          )
        )
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => DISPOSABLE.test(name) && name !== SHELL)
            .map((name) => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

// No messages. The library has one writer, the document sync in sync.js, which
// keeps a manifest of what it stored -- a second writer here would leave copies
// the sync does not know about and would never remove. And there is no
// "signed-out" message: signing out ends the session on the PC; it does not
// take the library off the phone (Phase 5e).

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return; // writes are the page's problem
  const url = new URL(request.url);
  if (url.origin !== location.origin) return;

  // A library document, or a file uploaded to a project (2026-10-16): both are
  // kept whole in LIBRARY by the sync, and both are read from there.
  if (
    (url.pathname.startsWith("/api/documents/") &&
      (url.pathname.endsWith("/file") || url.pathname.endsWith("/pdf"))) ||
    /^\/api\/projects\/\d+\/files\/\d+$/.test(url.pathname)
  ) {
    event.respondWith(document_(request));
    return;
  }
  if (request.headers.get("X-Agent-Probe")) {
    // "Is there a session" must be answered by the server or not at all. A
    // cached 200 would say "there was one", and the shell used to render on
    // exactly that for a session that no longer existed.
    event.respondWith(fetch(request));
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    // Straight to the network. The page reads the snapshot first and treats a
    // failed request as "the PC is not reachable", which is the truth.
    return;
  }
  event.respondWith(shell(request, event));
});

/**
 * App shell: cache first, because it never changes without a new VERSION and a
 * round trip for it is a round trip before anything is on screen.
 */
async function shell(request, event) {
  const cached = await caches.match(request, { ignoreSearch: true });
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      // `waitUntil`, not fire-and-forget: a service worker may be terminated as
      // soon as the response it was asked for resolves, and an un-awaited
      // cache.put is then cancelled part-way. The symptom is a cache that is
      // sometimes populated, which is worse than one that never is.
      const copy = response.clone();
      event.waitUntil(caches.open(SHELL).then((cache) => cache.put(request, copy)));
    }
    return response;
  } catch (err) {
    // A navigation to any path is the app; that is what the server does too.
    if (request.mode === "navigate") {
      const index = await caches.match("/index.html");
      if (index) return index;
    }
    throw err;
  }
}

/**
 * Documents: the phone's copy first, the PC second.
 *
 * Phase 5e: every readable document of this semester is synced into LIBRARY
 * (sync.js), whole. The reader asks for ranges -- PDF.js opens a 92-page deck
 * on page 1 without the rest -- so a range is answered by slicing the stored
 * file: a 206 built here, from the phone, with the PC off. Nothing here writes
 * to a cache; only the sync does, deliberately.
 */
async function document_(request) {
  const cached = await caches.match(request, { ignoreVary: true, ignoreSearch: true });
  if (cached) return fromCopy(cached, request.headers.get("range"));
  return fetch(request);
}

/** A stored whole file, or the slice of it a Range header asks for. */
async function fromCopy(cached, range) {
  const type = cached.headers.get("Content-Type") || "application/pdf";
  const blob = await cached.blob();
  const base = { "Content-Type": type, "Accept-Ranges": "bytes", "X-Lectern-From": "library" };
  const asked = range && /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  if (!asked) {
    return new Response(blob, { status: 200, headers: { ...base, "Content-Length": String(blob.size) } });
  }
  let start = asked[1] === "" ? null : Number(asked[1]);
  let end = asked[2] === "" ? null : Number(asked[2]);
  if (start === null) {
    // "bytes=-500": the last 500 bytes.
    start = Math.max(0, blob.size - (end || 0));
    end = blob.size - 1;
  } else if (end === null || end >= blob.size) {
    end = blob.size - 1;
  }
  if (start >= blob.size || start > end) {
    return new Response(null, { status: 416, headers: { ...base, "Content-Range": `bytes */${blob.size}` } });
  }
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    headers: {
      ...base,
      "Content-Range": `bytes ${start}-${end}/${blob.size}`,
      "Content-Length": String(end - start + 1),
    },
  });
}
