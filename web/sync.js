// Keeping the phone's copy current, and handing it to screens. Phase 5e.
//
// The PC is on only some of the time. So screens never wait on it: they read the
// snapshot this phone already holds (local.js), and this module refreshes that
// snapshot whenever the PC can be reached -- on open, when the connection comes
// back, when the app returns to the foreground, and once a minute while it is
// open and the PC is not answering. Nothing syncs while the app is closed; the
// app says so rather than implying otherwise.
//
// A sync replaces the snapshot WHOLE, and only once a complete one has arrived.
// An interrupted sync leaves the previous snapshot exactly as it was.
//
// Every screen shows when its data was synced (DESIGN.md section 6). That date is
// the snapshot's own `synced_at`, stamped by the PC -- not the time this phone
// last tried, which would make a failed attempt look like fresh data.

import { useEffect, useState } from "preact/hooks";
import { api, describe, NotSignedIn, Offline } from "/api.js";
import { getKey, setKey } from "/local.js";

const RETRY_MS = 60 * 1000;
const REFRESH_MS = 10 * 60 * 1000;

let snapshot = null;
let loaded = null;
let loadDone = false;
let inflight = null;
let timer = null;

// What the shell's one line says about the connection.
const state = {
  syncing: false,
  // null: not tried yet. true: the PC answered the last attempt. false: it did not.
  reachable: null,
  lastAttempt: 0,
  error: "",
};

const listeners = new Set();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The snapshot on this phone, read once from IndexedDB. Null if none yet. */
export function loadSnapshot() {
  if (!loaded) {
    loaded = getKey("snapshot")
      .catch(() => null)
      .then((found) => {
        if (found && !snapshot) snapshot = found;
        loadDone = true;
        emit();
      });
  }
  // The snapshot as it is NOW -- a sync may have landed since the first read.
  return loaded.then(() => snapshot);
}

/** The snapshot as held right now, without waiting. */
export function currentSnapshot() {
  return snapshot;
}

/**
 * Fetch a fresh snapshot from the PC and keep it. Never throws.
 *
 * A 401 is passed on by api.js to the app (which shows the sign-in screen) and
 * changes NOTHING here: the snapshot stays, the documents stay. Being signed
 * out stops syncing; it never takes away what is already on the phone.
 */
export function syncNow() {
  if (inflight) return inflight;
  state.syncing = true;
  emit();
  inflight = (async () => {
    try {
      const body = await api.get("/api/sync");
      if (!body || typeof body !== "object" || !body.entries) {
        throw new Error("the PC answered with something that is not a snapshot");
      }
      // Shown at once, saved right after: a slow write to the phone's storage
      // must not keep a complete answer off the screen. If the save fails, the
      // line says so -- the next open would otherwise show an older copy.
      snapshot = body;
      state.reachable = true;
      state.error = "";
      emit();
      try {
        await setKey("snapshot", body);
      } catch {
        state.error = "This sync could not be saved on the phone. It shows now, and may not after a restart.";
      }
      window.dispatchEvent(new Event("lectern:synced"));
      // The documents follow the snapshot that lists them, in the background.
      syncDocuments();
    } catch (err) {
      if (err instanceof Offline) {
        state.reachable = false;
      } else if (err instanceof NotSignedIn) {
        state.reachable = true;
      } else {
        state.reachable = true;
        state.error = describe(err);
      }
    } finally {
      state.syncing = false;
      state.lastAttempt = Date.now();
      inflight = null;
      emit();
    }
  })();
  return inflight;
}

/** Start keeping the snapshot current. Called once, when the shell appears. */
export function startSyncing() {
  if (timer) return;
  loadSnapshot().then(() => refreshDocumentCounts());
  syncNow();
  addEventListener("online", () => syncNow());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") syncNow();
  });
  timer = setInterval(() => {
    if (document.visibilityState !== "visible") return;
    const since = Date.now() - state.lastAttempt;
    if (state.reachable === false ? since >= RETRY_MS : since >= REFRESH_MS) syncNow();
  }, 15 * 1000);
}

// ---------------------------------------------------------------- hooks

function useVersion() {
  const [, setTick] = useState(0);
  useEffect(() => subscribe(() => setTick((n) => n + 1)), []);
}

/** The state the shell's synced line reads. */
export function useSyncState() {
  useVersion();
  useEffect(() => {
    loadSnapshot();
  }, []);
  return {
    syncedAt: snapshot ? snapshot.synced_at : null,
    reachable: state.reachable,
    syncing: state.syncing,
    error: state.error,
    loaded: loadDone,
  };
}

/**
 * One GET's answer, from the phone.
 *
 * Returns { data, syncedAt, status } where status is:
 *   "loading"  the snapshot has not been read from the phone yet
 *   "ready"    data is the answer the PC gave at syncedAt
 *   "absent"   there is no snapshot, or it does not hold this path
 */
export function useLocal(path) {
  useVersion();
  const [, setReady] = useState(false);
  useEffect(() => {
    loadSnapshot().then(() => setReady(true));
  }, []);
  return lookup(path);
}

export function lookup(path) {
  if (!snapshot) {
    return { data: null, syncedAt: null, status: loadDone ? "absent" : "loading", reason: "" };
  }
  const entries = snapshot.entries || {};
  if (path in entries) {
    return { data: entries[path], syncedAt: snapshot.synced_at, status: "ready", reason: "" };
  }
  return {
    data: null,
    syncedAt: snapshot.synced_at,
    status: "absent",
    reason: (snapshot.missing || {})[path] || "",
  };
}

/**
 * A slice of the synced timetable, as /api/timetable would have answered for
 * that range. `covered` is false when part of the range is outside what was
 * synced -- the caller says so instead of showing an empty week as if it were
 * real.
 */
export function useTimetable(from, to) {
  const found = useLocal("/api/timetable");
  if (found.status !== "ready") return { ...found, covered: false };
  const whole = found.data;
  const days = whole.days.filter((day) => day.date >= from && day.date <= to);
  const covered = from >= whole.from_date && to <= whole.to_date;
  return { ...found, data: { ...whole, from_date: from, to_date: to, days }, covered };
}

/** One document's details from the synced library, or null. */
export async function documentFromSnapshot(driveId) {
  await loadSnapshot();
  const posts = (snapshot && snapshot.entries && snapshot.entries["/api/library"]) || [];
  for (const post of posts) {
    for (const doc of post.documents || []) {
      if (doc.drive_id === driveId) return doc;
    }
  }
  return null;
}

// ================================================================ documents
//
// Every readable document of this semester's courses, kept on the phone in the
// Cache API under `library` -- the one cache nothing in this app ever deletes
// wholesale (sw.js). Downloaded one at a time, WHOLE, after the snapshot that
// lists them has been saved, and only when its content key has changed: a
// 42 MB deck is fetched once, not on every sync.
//
// A copy is removed only when the current snapshot no longer lists it (the
// file left the semester, or was replaced by a new version, which is fetched
// first). Never because a request failed, never because of a sign-out.
//
// Mobile data: by default documents download on Wi-Fi only, where the browser
// can say which it is on (navigator.connection, on Android). The setting is per
// device and lives in More.

const LIBRARY = "library";
const MANIFEST = "library-manifest";
const ANYWHERE = "lectern.docs.anywhere";

const documents = {
  total: 0,
  held: 0,
  heldBytes: 0,
  wantedBytes: 0,
  downloading: null,
  waitingForWifi: false,
  error: "",
  persisted: null,
  usage: null,
  quota: null,
};

let documentsInflight = null;

export function documentsOnMobileData() {
  try {
    return localStorage.getItem(ANYWHERE) === "yes";
  } catch {
    return false;
  }
}

export function setDocumentsOnMobileData(allowed) {
  try {
    if (allowed) localStorage.setItem(ANYWHERE, "yes");
    else localStorage.removeItem(ANYWHERE);
  } catch {
    // Not remembered; this visit still follows the choice.
  }
  emit();
  if (allowed) syncDocuments();
}

function onMobileData() {
  const link = navigator.connection;
  return Boolean(link && link.type === "cellular");
}

/** The documents the current snapshot says this phone should hold. */
export function wantedDocuments(from = snapshot) {
  if (!from || !from.entries) return [];
  const courses = new Set(from.offline_courses || []);
  const posts = from.entries["/api/library"] || [];
  const found = new Map();
  for (const post of posts) {
    if (!courses.has(post.course_id)) continue;
    for (const doc of post.documents || []) {
      if (doc.readable && doc.pdf_url && doc.pdf_key) found.set(doc.drive_id, doc);
    }
  }
  return [...found.values()];
}

/**
 * Ask Android to keep this site's storage. Without it, the browser may clear the
 * synced library under storage pressure -- silently, which is the worst failure
 * here. Asked once documents start arriving; the answer is shown on Status.
 */
export async function askToPersist() {
  try {
    if (!navigator.storage) return null;
    documents.persisted = (await navigator.storage.persisted()) || (await navigator.storage.persist());
    const estimate = await navigator.storage.estimate();
    documents.usage = estimate.usage ?? null;
    documents.quota = estimate.quota ?? null;
  } catch {
    documents.persisted = null;
  }
  emit();
  return documents.persisted;
}

/** Download what changed, drop what left. Never throws. */
export function syncDocuments() {
  if (documentsInflight) return documentsInflight;
  documentsInflight = (async () => {
    try {
      const wanted = wantedDocuments();
      const manifest = (await getKey(MANIFEST).catch(() => null)) || {};
      const cache = await caches.open(LIBRARY);
      documents.total = wanted.length;
      documents.wantedBytes = wanted.reduce((sum, doc) => sum + (doc.pdf_bytes || 0), 0);
      documents.error = "";

      const stale = wanted.filter((doc) => {
        const held = manifest[doc.drive_id];
        return !held || held.key !== doc.pdf_key || held.url !== doc.pdf_url;
      });
      count(wanted, manifest);
      documents.waitingForWifi = stale.length > 0 && onMobileData() && !documentsOnMobileData();
      documents.error = "";
      emit();

      if (stale.length && !documents.waitingForWifi) await askToPersist();

      for (const doc of documents.waitingForWifi ? [] : stale) {
        documents.downloading = doc.title;
        emit();
        const response = await fetch(doc.pdf_url, { credentials: "same-origin" });
        if (!response.ok) {
          // Said in words, naming the document; the status code stays out of
          // the screen. One refusal does not stop the others.
          documents.error = `${doc.title} could not be downloaded from the PC. It is tried again at the next sync.`;
          continue;
        }
        const previous = manifest[doc.drive_id];
        await cache.put(doc.pdf_url, response);
        if (previous && previous.url !== doc.pdf_url) await cache.delete(previous.url);
        manifest[doc.drive_id] = { url: doc.pdf_url, key: doc.pdf_key, bytes: doc.pdf_bytes || 0, at: Date.now() };
        await setKey(MANIFEST, manifest);
        count(wanted, manifest);
        emit();
      }

      // Only now, with every current file in place: drop what the snapshot no
      // longer lists.
      const keep = new Set(wanted.map((doc) => doc.drive_id));
      for (const [driveId, held] of Object.entries(manifest)) {
        if (keep.has(driveId)) continue;
        await cache.delete(held.url);
        delete manifest[driveId];
      }
      await setKey(MANIFEST, manifest);
      count(wanted, manifest);
    } catch (err) {
      documents.error =
        err instanceof TypeError
          ? "The PC stopped answering part-way. The rest download at the next sync."
          : "A document could not be kept on the phone. It is tried again at the next sync.";
    } finally {
      documents.downloading = null;
      documentsInflight = null;
      if (navigator.storage && navigator.storage.estimate) {
        navigator.storage
          .estimate()
          .then((estimate) => {
            documents.usage = estimate.usage ?? null;
            documents.quota = estimate.quota ?? null;
            emit();
          })
          .catch(() => {});
      }
      emit();
    }
  })();
  return documentsInflight;
}

function count(wanted, manifest) {
  let held = 0;
  let bytes = 0;
  for (const doc of wanted) {
    const copy = manifest[doc.drive_id];
    if (copy && copy.key === doc.pdf_key) {
      held += 1;
      bytes += copy.bytes || 0;
    }
  }
  documents.held = held;
  documents.heldBytes = bytes;
}

/** What Status and More show about the documents on this phone. */
export function useDocuments() {
  useVersion();
  return { ...documents, onMobileDataAllowed: documentsOnMobileData() };
}

/** Whether one document's current version is on the phone. */
export async function isHeld(doc) {
  if (!doc || !doc.pdf_key) return false;
  const manifest = (await getKey(MANIFEST).catch(() => null)) || {};
  const copy = manifest[doc.drive_id];
  return Boolean(copy && copy.key === doc.pdf_key);
}

/** Counts from the manifest alone, for a phone that cannot reach the PC. */
export async function refreshDocumentCounts() {
  await loadSnapshot();
  const wanted = wantedDocuments();
  const manifest = (await getKey(MANIFEST).catch(() => null)) || {};
  documents.total = wanted.length;
  documents.wantedBytes = wanted.reduce((sum, doc) => sum + (doc.pdf_bytes || 0), 0);
  count(wanted, manifest);
  try {
    if (navigator.storage) {
      documents.persisted = await navigator.storage.persisted();
      const estimate = await navigator.storage.estimate();
      documents.usage = estimate.usage ?? null;
      documents.quota = estimate.quota ?? null;
    }
  } catch {
    // Unknown is shown as unknown.
  }
  emit();
}
