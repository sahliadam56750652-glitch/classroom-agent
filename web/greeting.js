// The greeting on Today, and the only place in this client that reads the hour.
//
// DESIGN.md section 4, as amended: the 23:00 state looks exactly like every
// other state. The greeting names the time of day and does nothing else -- no
// "it's late", no "still up", no change of colour, layout or wording anywhere
// else. At 23:00 it says "Good evening", exactly as it did at 19:00.
//
// tests/test_web_assets.py allows getHours() in this module and in no other,
// so the rule cannot spread by accident.

/**
 * "Good morning", "Good afternoon" or "Good evening". Never anything else.
 *
 * Midnight to 05:00 is still the evening. "Good morning" at 01:30 reads wrong to
 * someone who has not stopped studying since dinner -- and it would be the app
 * announcing that a new day has started on them. A browser test pins the hours.
 */
export function greeting(now) {
  const hour = now.getHours();
  if (hour >= 5 && hour < 12) return "Good morning";
  if (hour >= 12 && hour < 18) return "Good afternoon";
  return "Good evening";
}

/** "Friday 2 October" -- today, as a person would say it. */
export function longDate(now) {
  return now.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}
