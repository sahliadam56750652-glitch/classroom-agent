// What this phone holds. Phase 5e, DESIGN.md section 6 as amended.
//
// The backend is a PC that is on only some of the time, so the phone is the copy
// I study from. Two IndexedDB stores, one database:
//
//   kv       "snapshot" -- the last GET /api/sync, replaced whole -- and small
//            facts about this device: when it last reached the PC.
//   outbox   writes made while the PC was not reachable (Phase 5e slice 6).
//
// IndexedDB rather than localStorage: the snapshot is tens of kilobytes and
// growing, localStorage is synchronous on the main thread, and the outbox needs
// ordered keys. Document BYTES live in the Cache API instead (see sw.js), where
// a Response can be stored and sliced for range requests without copying.
//
// Nothing in this module deletes the snapshot or the documents. Not signing
// out, not a 401, not an app update. The test suite holds that line.

const DB_NAME = "lectern";
const DB_VERSION = 1;

let opening = null;

function open() {
  if (!opening) {
    opening = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
        if (!db.objectStoreNames.contains("outbox")) {
          db.createObjectStore("outbox", { keyPath: "seq", autoIncrement: true });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => {
        opening = null;
        reject(request.error);
      };
    });
  }
  return opening;
}

/** One transaction. `work` returns the IDBRequest whose result is the answer. */
function run(storeName, mode, work) {
  return open().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, mode);
        const request = work(tx.objectStore(storeName));
        tx.oncomplete = () => resolve(request ? request.result : undefined);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      })
  );
}

export function getKey(key) {
  return run("kv", "readonly", (store) => store.get(key));
}

export function setKey(key, value) {
  return run("kv", "readwrite", (store) => store.put(value, key));
}

// ------------------------------------------------------------------ outbox

export function outboxAll() {
  return run("outbox", "readonly", (store) => store.getAll());
}

export function outboxAdd(entry) {
  return run("outbox", "readwrite", (store) => store.add(entry));
}

export function outboxPut(entry) {
  return run("outbox", "readwrite", (store) => store.put(entry));
}

export function outboxDelete(seq) {
  return run("outbox", "readwrite", (store) => store.delete(seq));
}
