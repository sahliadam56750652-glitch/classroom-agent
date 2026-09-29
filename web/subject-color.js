// A subject's colour, derived from its name and nothing else.
//
// Derived rather than stored, so it needs no table, no setting and no sync: the
// same name is the same colour on every screen, on every device, after every
// restart. DESIGN.md section 8 has the swatches and why there are twelve.
//
// Twelve colours for any number of subjects means two can share one -- with
// twelve subjects that is more likely than not. That is acceptable only because
// a colour never appears without the name beside it: the name identifies, the
// colour only helps the eye find the same subject again on the next screen.
//
// No red, no orange. The one warm alarm in this app is a passed deadline, and a
// subject that happened to hash to red would read as a subject in trouble.

export const SWATCHES = [
  "#c9ae6d",
  "#b4b672",
  "#9cbd82",
  "#83c297",
  "#6ec4af",
  "#64c3c6",
  "#6abfd9",
  "#7db8e6",
  "#94b1eb",
  "#aca9e8",
  "#c1a2de",
  "#d29ccc",
];

/**
 * FNV-1a over the name's UTF-8 bytes.
 *
 * Bytes rather than UTF-16 code units so that "Probabilités" hashes the same
 * here as anywhere else that ever needs to agree with it.
 */
function fnv1a(text) {
  let hash = 0x811c9dc5;
  for (const byte of new TextEncoder().encode(text)) {
    hash ^= byte;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

/** The swatch for a subject name; neutral for none. */
export function subjectColor(name) {
  if (!name) return "var(--edge)";
  return SWATCHES[fnv1a(String(name).trim()) % SWATCHES.length];
}

/** A style object that sets --subject, for any element that carries one. */
export function subjectStyle(name) {
  return { "--subject": subjectColor(name) };
}
