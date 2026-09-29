// The components every screen shares. DESIGN.md section 8, "Component set".
//
// Deliberately few. A screen that needs something not here builds it in its own
// module first; it moves here when a second screen needs the same thing.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api } from "/api.js";
import { linkProps } from "/router.js";
import { Icon } from "/icons.js";
import { subjectStyle } from "/subject-color.js";

/** The top of a screen: its name, and one sentence about its state. */
export function ScreenHeader({ title, children }) {
  return html`<header class="screen-header">
    <h1 class="t-title">${title}</h1>
    ${children ? html`<p class="t-state">${children}</p>` : null}
  </header>`;
}

/** A subject's name with its colour beside it. The colour is never alone. */
export function SubjectName({ name, class: extra = "" }) {
  return html`<span class=${`named ${extra}`} style=${subjectStyle(name)}>
    <span class="pip" aria-hidden="true"></span>${name}
  </span>`;
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
// and the name both come from the subject. One request for the whole session,
// shared by every screen that asks.

let names = null;

function loadNames() {
  if (!names) {
    names = api
      .get("/api/subjects")
      .then((subjects) => {
        const byCourse = {};
        for (const s of subjects || []) {
          if (s.course_id) byCourse[s.course_id] = s.name;
          if (s.mapped_course) byCourse[s.mapped_course] = byCourse[s.mapped_course] || s.name;
        }
        return byCourse;
      })
      .catch(() => {
        names = null;
        return {};
      });
  }
  return names;
}

/** course id -> subject name, or {} until it arrives. */
export function useSubjectNames() {
  const [found, setFound] = useState({});
  useEffect(() => {
    let live = true;
    loadNames().then((byCourse) => live && setFound(byCourse));
    return () => {
      live = false;
    };
  }, []);
  return found;
}
