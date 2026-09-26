// Writes attempted with no connection.
//
// Exactly ONE kind of write is queued: where I stopped reading. Everything else
// -- an upload, a task, an adjustment -- fails visibly and keeps what I typed,
// because a task that looks recorded and is not is the silent-success failure
// this project ranks worst. A position is different: it is small, it is idempotent,
// losing it costs my place in a 92-page chapter, and I will not notice it failing
// while I am reading.
//
// `localStorage`, not IndexedDB. There is one entry per document and the payload
// is two integers; a database for that would be more machinery than the thing it
// stores. Synchronous is fine at this size and survives a reload, which is the
// property that matters.

const KEY = "agent.pending.positions";

function read() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "{}");
  } catch {
    // Corrupt or unavailable (private mode, cleared storage). An empty queue is
    // the right answer either way -- never a thrown error on a reading screen.
    return {};
  }
}

function write(all) {
  try {
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    // Full or blocked. The position is lost, which costs a scroll.
  }
}

/** Remember a position that could not be sent. One per document, last wins. */
export function queuePosition(driveId, pageIndex) {
  const all = read();
  all[driveId] = { page_index: pageIndex, at: Date.now() };
  write(all);
}

export function pendingCount() {
  return Object.keys(read()).length;
}

/**
 * Try to send everything queued. Safe to call often; does nothing when empty.
 *
 * An entry is dropped only on success or on a 4xx -- a 404 means the document is
 * gone and retrying forever would be a queue that never drains. A network
 * failure leaves it for next time.
 */
export async function flushPositions(api) {
  const all = read();
  const ids = Object.keys(all);
  if (!ids.length) return 0;

  let sent = 0;
  for (const driveId of ids) {
    try {
      await api.put(`/api/documents/${encodeURIComponent(driveId)}/position`, {
        page_index: all[driveId].page_index,
      });
      delete all[driveId];
      sent += 1;
    } catch (err) {
      if (err && err.status >= 400 && err.status < 500) {
        delete all[driveId];
      }
      // Anything else: leave it queued and stop, because the next one will
      // almost certainly fail the same way.
      break;
    }
  }
  write(all);
  return sent;
}

/** Flush when the connection comes back, and once now. */
export function flushWhenOnline(api) {
  const attempt = () => flushPositions(api).catch(() => {});
  addEventListener("online", attempt);
  attempt();
  return () => removeEventListener("online", attempt);
}
