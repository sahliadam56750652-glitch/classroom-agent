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
  loadSnapshot();
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
