// Turning stored values into the words DESIGN.md asks for.
//
// One rule governs everything here, and it is the difference between two things
// that look alike:
//
//   AGE counts up and is honest. Section 5 asks for it by name -- "oldest posted
//   3 weeks ago" -- because the age of unreviewed material is the real signal.
//
//   TIME REMAINING counts down and is manufactured urgency, which section 7
//   forbids. So there is no `timeUntil` in this file and there must not be one.
//   A deadline renders as the instant it is, and the T-72/24/3 phrasing comes
//   from the event the scanner already emitted -- never from arithmetic here.

const DAY = 86400000;

/**
 * A stored ISO date as "tomorrow", "today", or a plain date.
 *
 * Compared in local wall-clock days, because "tomorrow" is a wall-clock fact --
 * at 20:00 Africa/Tunis the local day and the UTC day already disagree for part
 * of the year, which is the same reason the gate computes its date locally.
 */
export function relativeDay(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  const then = new Date(y, m - 1, d);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((then - today) / DAY);

  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  return then.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

/** "Mon 21 Sep", for a date that is not near enough to name relatively. */
export function shortDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

/**
 * How long ago something was posted, in the coarsest true unit.
 *
 * Coarse on purpose: "3 weeks" is the signal, and "23 days" invites arithmetic
 * about which day it was, which is not a question the age is answering.
 */
export function age(iso) {
  if (!iso) return "";
  const days = Math.floor((Date.now() - Date.parse(iso)) / DAY);
  if (!Number.isFinite(days) || days < 0) return "";
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 14) return `${days} days ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 9) return `${weeks} weeks ago`;
  const months = Math.floor(days / 30);
  return `${months} month${months === 1 ? "" : "s"} ago`;
}

/**
 * A stored UTC instant as a local time. Display only, per the convention that
 * conversion happens at the moment of display and nowhere earlier.
 */
export function localTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Whether a deadline has already gone. The only thing in this app that is red. */
export function hasPassed(iso) {
  return Boolean(iso) && Date.parse(iso) < Date.now();
}

/** "OS lab" -- what a session is, said the way the printed timetable says it. */
export function sessionName(session) {
  if (!session) return "";
  const subjects = (session.parts || []).map((p) => p.subject).join(" + ");
  const kind = (session.kind || "").toLowerCase();
  const spoken = { lec: "lecture", tut: "tutorial", lab: "lab" }[kind] || kind;
  return spoken ? `${subjects} ${spoken}` : subjects;
}

/** "6 unreviewed", "1 unreviewed". Counts, never a percentage. */
export function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many || one + "s"}`;
}
