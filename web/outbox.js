// Writes made while the PC is off. Phase 5e, the outbox.
//
// Six kinds of write wait here when the PC cannot be reached, and are sent when
// it can: Read, Skip, marking a task or a milestone done, a timetable change,
// and where I stopped reading. Each answers something about a row that ALREADY exists, so
// sending it late changes nothing about what it means.
//
// Creating a task or a project is never queued. Creation is where a silent loss
// would hurt most -- a task that looks recorded and is not -- so those forms need
// the PC, say so, and keep what I typed.
//
// The rules, each pinned by a test:
//
//   * Everything goes through the queue, online or not. A write is stored first
//     and sent second, so the order on the PC is the order I acted in, and a
//     crash between the two loses nothing.
//   * Each entry carries an `op_id`, sent as `Idempotency-Key`. A reply lost on
//     the way back is sent again and applied once (api/idempotency.py).
//   * A waiting write is shown as WAITING -- "Read, waiting to send" -- never as
//     done. The snapshot is the PC's word; this is mine, not yet heard.
//   * Nothing is dropped silently. A write the PC refuses stays, with its
//     sentence, under "Not sent" on Status and on its own screen, until I retry
//     or discard it. Two exceptions, both the same fact twice: a milestone
//     already complete answers 200, and a position for a document that is gone
//     costs only a place in it.
//   * A 401 stops sending and keeps everything. So does an unreachable PC.

import { useEffect, useState } from "preact/hooks";
import { api, describe, NotSignedIn, Offline } from "/api.js";
import { outboxAdd, outboxAll, outboxDelete, outboxPut } from "/local.js";

// What a waiting write is called, by kind. The screen shows "<label>, waiting to send".
const WORDS = {
  read: "Read",
  skip: "Skipped",
  task: "Task done",
  milestone: "Milestone done",
  adjustment: "Timetable change",
  position: "Place in a document",
};

let entries = [];
let loaded = null;
let running = null;
let again = false;
// op_ids a screen is awaiting right now. A refusal of one of these goes back to
// that screen as an error, the way an online write always has; only a refusal
// nobody is waiting for becomes a "Not sent" entry.
const awaited = new Map();

const listeners = new Set();

function emit() {
  for (const listener of listeners) listener();
}

async function reload() {
  try {
    entries = (await outboxAll()).sort((a, b) => a.seq - b.seq);
  } catch {
    entries = [];
  }
  emit();
}

function load() {
  if (!loaded) loaded = adoptOldQueue().then(reload);
  return loaded;
}

// Before 5e, positions that could not be sent waited in localStorage (queue.js).
// Any still there join the outbox once, and the old key goes.
const OLD_QUEUE = "agent.pending.positions";

async function adoptOldQueue() {
  let old = {};
  try {
    old = JSON.parse(localStorage.getItem(OLD_QUEUE) || "{}") || {};
  } catch {
    return;
  }
  try {
    for (const [driveId, held] of Object.entries(old)) {
      await outboxAdd({
        op_id: newId(), kind: "position", method: "PUT",
        path: `/api/documents/${encodeURIComponent(driveId)}/position`,
        body: { page_index: held.page_index }, target: `doc:${driveId}`, label: "",
        made_at: new Date(held.at || Date.now()).toISOString(), status: "waiting", detail: "",
      });
    }
    localStorage.removeItem(OLD_QUEUE);
  } catch {
    // Left where it was; tried again on the next open.
  }
}

function newId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // randomUUID needs a secure context; the phone always has one (ts.net), a
  // test page on plain http may not.
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Send a write through the queue.
 *
 *   kind     one of WORDS
 *   method   "POST" or "PUT"
 *   path     the API path
 *   body     JSON body, or null
 *   target   what the write is about -- "item:12", "milestone:3", "doc:abc" --
 *            so a screen can find its own waiting writes
 *   label    a few words naming the row, for the "Not sent" list
 *
 * Resolves to { sent: true, value } when the PC answered, or { sent: false }
 * when it is queued. Throws the PC's refusal, like any API call, when the PC
 * answered and said no -- nothing is left queued in that case.
 */
export async function send({ kind, method, path, body = null, target, label = "" }) {
  await load();
  if (kind === "position") {
    // A cursor, not a history: one waiting position per document, the latest.
    for (const old of entries.filter((e) => e.kind === "position" && e.target === target && e.status === "waiting")) {
      await outboxDelete(old.seq);
    }
  }
  const entry = {
    op_id: newId(),
    kind,
    method,
    path,
    body,
    target,
    label,
    made_at: new Date().toISOString(),
    status: "waiting",
    detail: "",
  };
  entry.seq = await outboxAdd(entry);
  await reload();

  const outcome = new Promise((resolve) => awaited.set(entry.op_id, resolve));
  await flush();
  const result = awaited.has(entry.op_id) ? null : await outcome;
  awaited.delete(entry.op_id);
  if (!result) return { sent: false };
  if (result.refused) throw result.refused;
  return { sent: true, value: result.value };
}

/**
 * Send everything waiting, oldest first. Safe to call often; one run at a time,
 * and a call during a run asks for one more pass after it. Resolves to how many
 * the PC accepted.
 */
export function flush() {
  if (running) {
    again = true;
    return running;
  }
  running = (async () => {
    let sent = 0;
    try {
      do {
        again = false;
        await load();
        sent += await pass();
      } while (again);
      return sent;
    } finally {
      running = null;
      emit();
    }
  })();
  return running;
}

async function pass() {
  let sent = 0;
  const waiting = entries.filter((e) => e.status === "waiting");
  for (const entry of waiting) {
    let value;
    try {
      value = await api[entry.method === "PUT" ? "put" : "post"](entry.path, entry.body, {
        headers: { "Idempotency-Key": entry.op_id },
      });
    } catch (err) {
      if (err instanceof Offline || err instanceof NotSignedIn || !(err.status >= 400 && err.status < 500)) {
        // Not reachable, not signed in, or the PC failed: stop, keep it all,
        // and try again later. Sending the next one would reorder them.
        await reload();
        return sent;
      }
      await refused(entry, err);
      continue;
    }
    const resolve = awaited.get(entry.op_id);
    awaited.delete(entry.op_id);
    const notice = resolve ? "" : noticeFor(entry, value);
    if (notice) {
      await outboxPut({ ...entry, status: "notice", detail: notice });
    } else {
      await outboxDelete(entry.seq);
    }
    if (resolve) resolve({ value });
    sent += 1;
  }
  await reload();
  return sent;
}

async function refused(entry, err) {
  const resolve = awaited.get(entry.op_id);
  awaited.delete(entry.op_id);
  if (resolve || entry.kind === "position") {
    // A screen is waiting for this answer and shows it itself; or it is a place
    // in a document the PC no longer has.
    await outboxDelete(entry.seq);
    if (resolve) resolve({ refused: err });
    return;
  }
  await outboxPut({ ...entry, status: "refused", detail: describe(err) });
}

/**
 * Read or Skip that arrived after the item had already moved on elsewhere --
 * verified in Telegram, read on the PC. Nothing went wrong and nothing is lost,
 * but my answer did not land, and I am told once.
 */
function noticeFor(entry, value) {
  if ((entry.kind === "read" || entry.kind === "skip") && value && value.moved === false) {
    const verb = entry.kind === "read" ? "Read" : "skip";
    return `It was already ${value.state} by the time the PC heard it, so your ${verb} was not applied.`;
  }
  return "";
}

/** Try a refused write again. */
export async function retry(seq) {
  const entry = entries.find((e) => e.seq === seq);
  if (!entry) return;
  await outboxPut({ ...entry, status: "waiting", detail: "" });
  await reload();
  await flush();
}

/** Let a refused write go, or clear a notice I have read. */
export async function discard(seq) {
  await outboxDelete(seq);
  await reload();
}

/** Everything in the queue, oldest first. */
export function outboxEntries() {
  return entries;
}

/** The waiting write about `target` of one of `kinds`, if there is one. */
export function waitingFor(target, kinds) {
  return entries.find((e) => e.status === "waiting" && e.target === target && (!kinds || kinds.includes(e.kind))) || null;
}

/** What a waiting write is called on screen. */
export function wordsFor(entry) {
  return WORDS[entry.kind] || "A change";
}

/** The queue, kept current. Re-renders on every change. */
export function useOutbox() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const listener = () => setTick((n) => n + 1);
    listeners.add(listener);
    load();
    return () => listeners.delete(listener);
  }, []);
  return entries;
}
