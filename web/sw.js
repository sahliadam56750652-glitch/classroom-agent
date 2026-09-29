// The service worker. DESIGN.md section 6, as amended when 5a retired its
// original justification.
//
// The promise is "what has been delivered is readable offline", and "delivered"
// is doing real work: the library is local to the SERVER now, and 116 MB is not
// going to live on a phone. So four policies, and the third one is the whole
// design:
//
//   SHELL      precache, cache-first. Opening offline must never be blank.
//   API GETs   network-first, cache fallback, and the fallback is LABELLED.
//   DOCUMENTS  never automatic. Tonight's gate item is kept deliberately;
//              anything else only if I ask. A 40 MB deck must not be cached
//              because I scrolled past it.
//   WRITES     not touched here. The page queues them; see queue.js.
//
// One hard constraint shapes this: **the Cache API cannot store a 206.**
// `Cache.put` rejects a partial response by spec. The reader gets its speed from
// range requests, so reading online and keeping offline are necessarily two
// different requests -- a kept document is fetched WHOLE, once, on purpose.

// A hash of every file in SHELL_FILES, stamped by `agent webcontract`. Never
// edited by hand: the shell is served cache-first, so a changed file reaches an
// installed app ONLY when this changes, and a forgotten bump looks exactly like
// a fix that did not work. A test fails when it is stale.
const VERSION = "82daa310514e";
const SHELL = `shell-${VERSION}`;
const API = `api-${VERSION}`;
const DOCS = `docs-${VERSION}`;

// Everything needed to render something rather than nothing. The PDF.js worker
// is NOT here: 1.4 MB that only the reader needs, and precaching it would make
// every first load pay for a screen most opens never reach.
const SHELL_FILES = [
  "/",
  "/index.html",
  "/app.css",
  "/app.js",
  "/api.js",
  "/html.js",
  "/format.js",
  "/router.js",
  "/contract.js",
  "/screens/now.js",
  "/screens/subjects.js",
  "/screens/status.js",
  "/screens/library.js",
  "/screens/timetable.js",
  "/screens/projects.js",
  "/screens/add.js",
  "/queue.js",
  "/reader/reader.js",
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
            .filter((name) => ![SHELL, API, DOCS].includes(name))
            .map((name) => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "keep" && data.url) {
    // Fetched WHOLE and without a Range header, because a 206 cannot be cached.
    event.waitUntil(
      caches
        .open(DOCS)
        .then((cache) => cache.add(new Request(data.url, { credentials: "same-origin" })))
        .catch((err) => console.warn("[sw] could not keep", data.url, err))
    );
  }
  if (data.type === "forget" && data.url) {
    event.waitUntil(caches.open(DOCS).then((cache) => cache.delete(data.url)));
  }
  if (data.type === "signed-out") {
    // Signing out means the cached screens and the kept documents go too. They
    // are personal data, and "signed out" that still shows last night's backlog
    // offline is not signed out. The shell stays: it holds nothing of mine.
    event.waitUntil(Promise.all([caches.delete(API), caches.delete(DOCS)]));
  }
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return; // writes are the page's problem
  const url = new URL(request.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith("/api/documents/") && url.pathname.endsWith("/file")) {
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
    event.respondWith(api(request, event));
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
 * API reads: the network, and the last known answer when there is none.
 *
 * The fallback carries `X-Agent-Cached-At`, which is what lets the page say "as
 * of 21:40" rather than presenting old figures as current. Section 7 forbids the
 * app inventing a state, and stale data shown as fresh is exactly that.
 */
async function api(request, event) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      const when = new Date().toISOString();
      event.waitUntil(
        withStamp(copy, when).then((stamped) =>
          caches.open(API).then((cache) => cache.put(request, stamped))
        )
      );
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw err;
  }
}

/**
 * Documents: only what was deliberately kept.
 *
 * No network fallback INTO the cache -- a document is never cached as a side
 * effect of reading it, because the reader requests ranges and a 206 cannot be
 * stored anyway. Kept copies are whole responses put here by the `keep` message.
 */
async function document_(request) {
  const cached = await caches.match(request, { ignoreVary: true, ignoreSearch: true });
  // A kept copy is the whole file, so a Range request can be answered from it
  // only by the browser's own machinery -- which it will not do for a cached
  // 200. Returning the whole thing is correct and is what PDF.js falls back to.
  if (cached && !request.headers.get("range")) return cached;
  try {
    return await fetch(request);
  } catch (err) {
    if (cached) return cached;
    throw err;
  }
}

/** A copy of a response carrying when it was stored. */
async function withStamp(response, when) {
  const headers = new Headers(response.headers);
  headers.set("X-Agent-Cached-At", when);
  return new Response(await response.blob(), {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
