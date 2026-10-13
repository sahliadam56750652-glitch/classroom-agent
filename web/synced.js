// "Synced 07:31" -- the words for when the phone's copy was made.
//
// DESIGN.md section 7: never a state the app will not show me, and stale data
// presented as current is the app inventing one. So the line always carries the
// time of the snapshot it is showing, and says plainly when the PC is not
// answering. It is a fact, in the same neutral ink as everything else: being
// offline is not an alarm.

/** "07:31", "yesterday 19:30", or "Sat 3 Oct, 19:30". */
export function whenSynced(iso) {
  const at = new Date(iso);
  const now = new Date();
  const clock = at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  const sameDay = (a, b) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(at, now)) return clock;
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (sameDay(at, yesterday)) return `yesterday ${clock}`;
  const day = at.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  return `${day}, ${clock}`;
}

/**
 * The whole line. `reachable` is null (not tried yet), true, or false.
 */
export function syncedLine(syncedAt, reachable, syncing) {
  if (!syncedAt) {
    if (syncing) return "Syncing for the first time…";
    if (reachable === false) return "Nothing synced to this phone yet. It syncs when the PC is on";
    return "Nothing synced to this phone yet";
  }
  const when = `Synced ${whenSynced(syncedAt)}`;
  if (syncing) return `${when} · syncing…`;
  if (reachable === false) return `${when} · PC not reachable`;
  return when;
}
