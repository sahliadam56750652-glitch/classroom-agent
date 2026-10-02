// A subject's colour, derived from its name and nothing else.
//
// Derived rather than stored, so it needs no table, no setting and no sync: the
// same name is the same colour on every screen, on every device, after every
// restart, in both themes. DESIGN.md section 8 has the hues and why there are
// twelve.
//
// What this returns is not a colour but an INDEX into twelve sets of tokens in
// app.css -- --s0-fg, --s0-tint, --s0-fill and so on -- each a light-dark()
// pair. So a theme change recolours everything at once without a re-render, and
// the values that have to pass contrast live in the one file the test reads.
//
// Twelve hues for any number of subjects means two can share one. That is
// acceptable only because the colour never appears without the name in it.
//
// No red, no orange. The one alarm in this app is a passed deadline, and a
// subject that hashed to red would read as a subject in trouble.

export const HUES = 12;

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

/** Which of the twelve a subject is; null for none. */
export function subjectIndex(name) {
  if (!name) return null;
  return fnv1a(String(name).trim()) % HUES;
}

/** The three custom properties anything belonging to a subject sets. */
export function subjectStyle(name) {
  const index = subjectIndex(name);
  if (index == null) return {};
  return {
    "--subject": `var(--s${index}-fg)`,
    "--subject-tint": `var(--s${index}-tint)`,
    "--subject-fill": `var(--s${index}-fill)`,
  };
}
