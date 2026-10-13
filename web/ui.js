// The components every screen shares. DESIGN.md section 8, "Component set".
//
// Deliberately few. A screen that needs something not here builds it in its own
// module first; it moves here when a second screen needs the same thing.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { useLocal } from "/sync.js";
import { linkProps } from "/router.js";
import { Icon, Mark } from "/icons.js";
import { subjectStyle } from "/subject-color.js";
import { discard, retry, useOutbox, wordsFor } from "/outbox.js";

/** The top of a screen: its name, and one sentence about its state. */
export function ScreenHeader({ title, children }) {
  return html`<header class="screen-header">
    <h1 class="t-title">${title}</h1>
    ${children ? html`<p class="t-state">${children}</p>` : null}
  </header>`;
}

/** A screen's own parts, as links; the one this is marked. */
export function SectionLinks({ links, here }) {
  return html`<nav class="section-links" aria-label="Parts of this screen">
    ${links.map(
      ([to, label]) => html`<a
        key=${to}
        aria-current=${here === to ? "page" : null}
        ...${linkProps(to)}
      >${label}</a>`
    )}
  </nav>`;
}

/** Study's three parts, the same row on each of them. */
export const STUDY_PARTS = [
  ["/study", "Subjects"],
  ["/quizzes", "Quizzes"],
  ["/library", "Library"],
];

/** A subject's name on its fill: the chip. The colour is never alone. */
export function Chip({ name, class: extra = "" }) {
  if (!name) return null;
  return html`<span class=${`chip ${extra}`} style=${subjectStyle(name)}>${name}</span>`;
}

/** The mark and the name, linking home. */
export function Brand() {
  return html`<a class="brand" ...${linkProps("/")} aria-label="Lectern, Today">
    <${Mark} />
    <span class="brand-name">Lectern</span>
  </a>`;
}

/**
 * An empty state: the mark, small, above one line that says what is true, and
 * at most one more. Not a mood -- section 4 -- just the fact, designed.
 */
export function Empty({ title, children }) {
  return html`<div class="empty">
    <${Mark} />
    <p class="t-lead">${title}</p>
    ${children ? html`<p class="t-state">${children}</p>` : null}
  </div>`;
}


/**
 * A card. With `to` the whole card is a link and carries a chevron; without,
 * it is a surface. With `subject`, its left edge is that subject's colour.
 */
export function Card({ to, subject, class: extra = "", children, label }) {
  const classes = `card ${subject ? "has-subject" : ""} ${extra}`;
  const style = subject ? subjectStyle(subject) : null;
  if (to) {
    return html`<a class=${classes} style=${style} aria-label=${label || null} ...${linkProps(to)}>
      <div class="card-body">${children}</div>
      <${Icon} name="chevron" class="chevron" />
    </a>`;
  }
  return html`<div class=${classes} style=${style}>
    <div class="card-body">${children}</div>
  </div>`;
}

/**
 * The shape of what is loading. Static -- a loop is not an answer to anything
 * I did -- and hidden from screen readers, which get the status line instead.
 */
export function Skeleton({ hero = false, rows = 3 }) {
  return html`<div class="skeleton-stack" aria-busy="true">
    <p class="visually-hidden" role="status">Loading</p>
    <div aria-hidden="true" class="skeleton-stack">
      <span class="skeleton-line" style=${{ width: "38%", height: "22px" }}></span>
      ${hero
        ? html`<span class="skeleton" style=${{ height: "220px", borderRadius: "var(--r-hero)" }}></span>`
        : null}
      ${Array.from(
        { length: rows },
        (_, n) =>
          html`<span key=${n} class="skeleton" style=${{ height: "76px" }}></span>`
      )}
    </div>
  </div>`;
}

/** A plain sentence about something that went wrong. Never alarm-coloured. */
export function Problem({ children }) {
  return html`<p class="problem" role="alert">${children}</p>`;
}

// ---------------------------------------------------------------- subject names
//
// Library documents and deadlines carry a course id, not a subject; the colour
// and the name both come from the subject. Read from the phone's snapshot, so a
// name is known with the PC off.

/** course id -> subject name, from the synced subjects; {} before a sync. */
export function useSubjectNames() {
  const subjects = useLocal("/api/subjects").data || [];
  const byCourse = {};
  for (const s of subjects) {
    if (s.course_id) byCourse[s.course_id] = s.name;
    if (s.mapped_course) byCourse[s.mapped_course] = byCourse[s.mapped_course] || s.name;
  }
  return byCourse;
}

/**
 * What a screen says when the phone holds nothing for it yet. Never an error:
 * the PC is simply not the thing being looked at.
 */
export function NotSynced({ reason }) {
  return html`<${Empty} title="Not on this phone yet.">
    ${reason
      ? reason
      : "It arrives with the next sync, while the PC is on."}
  </${Empty}>`;
}

// ---------------------------------------------------------------- the outbox
//
// Phase 5e. A write made with the PC off waits on the phone (outbox.js). These
// are the two ways a screen shows that: a waiting write on its own row, and the
// writes the PC refused, each with Retry and Discard. Never a count in a badge:
// DESIGN.md forbids counting undone things at me.

/** "Read, waiting to send." On the row the write is about, instead of "done". */
export function WaitingToSend({ entry }) {
  return html`<p class="waiting t-meta" role="status">
    <${Icon} name="now" />
    <span>${wordsFor(entry)}, waiting to send. It goes when the PC is reachable.</span>
  </p>`;
}

/**
 * Writes the PC refused, and Read or Skip that arrived after the item had moved
 * on. Only the given kinds, or every kind on Status.
 */
export function NotSent({ kinds, title = "" }) {
  const entries = useOutbox().filter(
    (e) => (e.status === "refused" || e.status === "notice") && (!kinds || kinds.includes(e.kind))
  );
  if (!entries.length) return null;
  return html`<section class="not-sent" aria-label="Not sent">
    ${title ? html`<h2 class="section-title">${title}</h2>` : null}
    <ul class="not-sent-list">
      ${entries.map(
        (entry) => html`<li key=${entry.seq} class="notice not-sent-row">
          <p class="t-lead">${wordsFor(entry)}${entry.label ? html`: ${entry.label}` : null}</p>
          <p class="t-meta">${entry.detail}</p>
          <div class="not-sent-doors">
            ${entry.status === "refused"
              ? html`<button class="button secondary-button" type="button" onClick=${() => retry(entry.seq)}>
                    Try again
                  </button>
                  <button class="button quiet-button" type="button" onClick=${() => discard(entry.seq)}>
                    Discard it
                  </button>`
              : html`<button class="button quiet-button" type="button" onClick=${() => discard(entry.seq)}>
                  Understood
                </button>`}
          </div>
        </li>`
      )}
    </ul>
  </section>`;
}
