// Icons, drawn here as SVG paths rather than fetched from an icon font.
//
// A font would be one more file to self-host and precache for fourteen glyphs,
// and an emoji renders differently on every phone. One stroke weight, one grid
// (24), round joins; every icon inherits the colour of the text beside it.
// Decorative by default -- the label next to it is what a screen reader reads.

import { html } from "/html.js";

const PATHS = {
  now: html`<circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" />`,
  subjects: html`<path d="M5 4.5h9.5a2 2 0 0 1 2 2v13H7a2 2 0 0 1-2-2z" /><path d="M5 17.5a2 2 0 0 1 2-2h9.5" /><path d="M19 7v12.5" />`,
  timetable: html`<rect x="4" y="5.5" width="16" height="14" rx="2.5" /><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4" />`,
  library: html`<path d="M5 4.5v15M9.5 4.5v15" /><path d="m13.5 5.2 3.9-1 3.6 14.6-3.9 1z" /><path d="M3.5 19.5h17" />`,
  more: html`<circle cx="6" cy="12" r="1.3" /><circle cx="12" cy="12" r="1.3" /><circle cx="18" cy="12" r="1.3" />`,
  deadlines: html`<path d="M6 20.5V4" /><path d="M6 4.5h11l-2.2 4 2.2 4H6" />`,
  projects: html`<path d="m12 4 8 4-8 4-8-4z" /><path d="m4 12 8 4 8-4" /><path d="m4 16 8 4 8-4" />`,
  add: html`<path d="M12 5v14M5 12h14" />`,
  status: html`<path d="M5 19.5V11M10 19.5V5M15 19.5v-6M20 19.5V8.5" />`,
  signout: html`<path d="M14 4.5H6.5a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2H14" /><path d="M10.5 12H20m-3.5-3.5L20 12l-3.5 3.5" />`,
  chevron: html`<path d="m9.5 6 6 6-6 6" />`,
  back: html`<path d="M14.5 6 8.5 12l6 6" />`,
  send: html`<path d="M20.5 4 3.5 11l6.5 2.5L12.5 20z" /><path d="m10 13.5 4.5-4.5" />`,
  document: html`<path d="M7 3.5h7l4.5 4.5v12.5H7a1.5 1.5 0 0 1-1.5-1.5V5A1.5 1.5 0 0 1 7 3.5z" /><path d="M14 3.5V8h4.5M9 12.5h6M9 16h6" />`,
  external: html`<path d="M13.5 4.5h6v6M19.5 4.5 11 13" /><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" />`,
  search: html`<circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" />`,
  close: html`<path d="M6 6l12 12M18 6 6 18" />`,
  quiz: html`<rect x="4.5" y="4.5" width="15" height="15" rx="3" /><path d="m8.5 12 2.5 2.5 4.5-5" />`,
  flag: html`<path d="M6 20.5V4" /><path d="M6 4.5h11l-2.2 4 2.2 4H6" />`,
  skip: html`<path d="m6 6.5 6 5.5-6 5.5z" /><path d="M13 6.5 19 12l-6 5.5" />`,
  check: html`<path d="m5 12.5 4.5 4.5L19 7.5" />`,
};

/**
 * <${Icon} name="now" /> -- aria-hidden unless given a label, because it
 * almost always sits beside words that already say what it is.
 */
export function Icon({ name, label, class: extra = "" }) {
  return html`<svg
    class=${`icon ${extra}`}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.75"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden=${label ? null : "true"}
    role=${label ? "img" : null}
    aria-label=${label || null}
    focusable="false"
  >
    ${PATHS[name] || null}
  </svg>`;
}

/**
 * The mark: a page with its margin rule. DESIGN.md section 8.
 *
 * Drawn from the theme's own tokens, so it is ink on paper by day and lit ink on
 * charcoal at night. web/icon.svg is the same geometry in fixed colours, for the
 * places a theme cannot reach -- the favicon and the installed app's icon.
 */
export function Mark({ class: extra = "" }) {
  return html`<svg class=${`mark ${extra}`} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
    <rect x="5" y="3" width="22" height="26" rx="4" fill="var(--raised)" stroke="var(--ink)" stroke-width="1.6" />
    <path d="M11 3.8v24.4" stroke="var(--s0-fg)" stroke-width="1.8" />
    <path d="M14.5 11h8M14.5 16h8M14.5 21h5" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round" />
  </svg>`;
}
