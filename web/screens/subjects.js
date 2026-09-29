// Subjects and coverage. DESIGN.md section 3.
//
// "Absolute counts of finished work, yes. Percentages of a deficit, never.
// Streaks, never."
//
// The server sends no percentage, and this screen does not compute one. That is
// not belt-and-braces: the enforcement only works if BOTH halves hold, and
// `unreviewed / (unreviewed + verified)` is one line away at any time. There is
// no such line here and there must not be.
//
// Four states are what section 3 names; six is what the data actually
// distinguishes, and the two extra exist because collapsing them would say
// something false. A subject awaiting a Classroom id is not one whose course
// this install holds nothing for. A manual subject with nothing entered is not
// "no readable material" -- nothing is missing and nothing is broken.
//
// The list is in the order of each subject's next session, because the one I
// have a lecture in tomorrow is the one I came to look at. Subjects with nothing
// in them yet are gathered at the bottom, each still saying exactly what it is.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { Offline, api, describe } from "/api.js";
import { age, plural, relativeDay } from "/format.js";
import { linkProps } from "/router.js";
import { Card, Problem, ScreenHeader, Skeleton, SubjectName } from "/ui.js";

// What each state says, and whether there is anything to do about it. The
// wording is the client's half of `messages._subject_line`; the classification
// is the server's, from `Subject.state`, so the two cannot drift.
const SAYS = {
  awaiting_course: "no Classroom course yet, never gated",
  no_material_here: "mapped to a course with no material here",
  nothing_entered: "nothing entered yet",
  no_readable_material: "no readable material",
  up_to_date: "up to date",
  behind: null, // counted, not described
};

// Subjects with nothing in them to act on, gathered into one group at the
// bottom. Not hidden -- every one keeps its exact state line inside the group --
// but a subject whose course does not exist yet should not stand between me and
// one I have a lecture in tomorrow. `no_readable_material` stays out of this
// group on purpose: dead attachments are a fact worth seeing at full size.
const INACTIVE = new Set(["nothing_entered", "awaiting_course"]);

/** The state line for a subject, the same words on the list and its page. */
function standing(subject) {
  const said = SAYS[subject.state];
  if (said) {
    return (
      said +
      (subject.dead_files
        ? ` — ${plural(subject.dead_files, "attachment")} gone from Drive`
        : "")
    );
  }
  return subject.blocked
    ? `${subject.unreviewed} unreviewed, ${subject.ready} ready`
    : plural(subject.unreviewed, "unreviewed", "unreviewed");
}

/** "Mon 5 Oct 08:30" -- when a session is, as the instant it is. */
function when(found) {
  if (!found) return "";
  return `${relativeDay(found.date)} ${found.start}`;
}

/**
 * Each subject's next session in the timetable's window, by name.
 *
 * The timetable names subjects the same way the subject list does, because both
 * come from timetable.yaml. A subject with no session in the window has no
 * entry and sorts after every one that does.
 */
function nextSessions(timetable) {
  const next = {};
  for (const day of (timetable && timetable.days) || []) {
    for (const session of day.sessions || []) {
      for (const part of session.parts || []) {
        if (!next[part.subject]) {
          next[part.subject] = { date: day.date, start: session.start, session };
        }
      }
    }
  }
  return next;
}

function order(rows, next) {
  const key = (row) => {
    const found = next[row.name];
    return found ? `${found.date} ${found.start}` : "~";
  };
  // Stable: subjects with no session keep the server's order among themselves.
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const ka = key(a.row);
      const kb = key(b.row);
      return ka < kb ? -1 : ka > kb ? 1 : a.index - b.index;
    })
    .map(({ row }) => row);
}

function Standing({ subject, next }) {
  const item = subject.next_item;
  const meta = [
    next ? `Next session ${when(next)}` : "",
    subject.state === "behind" && subject.oldest_posted_at
      ? `oldest posted ${age(subject.oldest_posted_at)}`
      : "",
  ].filter(Boolean);

  return html`<li class="subject">
    <${Card}
      to=${`/subjects/${encodeURIComponent(subject.name)}`}
      subject=${subject.name}
    >
      <span class="t-lead"><${SubjectName} name=${subject.name} /></span>
      <span class="t-state subject-said">${standing(subject)}</span>
      ${subject.state === "behind" && item
        ? html`<span class="subject-next">${`Next: ${item.label}`}</span>`
        : null}
      ${meta.length ? html`<span class="t-meta">${meta.join(" · ")}</span>` : null}
    </${Card}>
  </li>`;
}

function Inactive({ rows }) {
  if (!rows.length) return null;
  return html`<details class="section inactive">
    <summary class="inactive-summary">
      <span class="t-body">Nothing here yet</span>
      <span class="t-meta">${plural(rows.length, "subject")}</span>
    </summary>
    <ul class="inactive-list">
      ${rows.map(
        (row) => html`<li key=${row.name}>
          <a class="inactive-row" ...${linkProps(`/subjects/${encodeURIComponent(row.name)}`)}>
            <${SubjectName} name=${row.name} />
            <span class="t-meta">${standing(row)}</span>
          </a>
        </li>`
      )}
    </ul>
  </details>`;
}

export function Subjects() {
  const [rows, setRows] = useState(null);
  const [next, setNext] = useState({});
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get("/api/subjects")
      .then((found) => live && setRows(found))
      .catch((err) => {
        if (!live) return;
        setProblem(
          err instanceof Offline
            ? "No connection, and nothing has been kept for this screen."
            : describe(err)
        );
      });
    // The order, not the content: without the timetable the list still renders,
    // in the server's order.
    api
      .get("/api/timetable")
      .then((found) => live && setNext(nextSessions(found)))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  if (problem) {
    return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  }
  if (!rows) return html`<div class="screen"><${Skeleton} rows=${5} /></div>`;

  const behind = rows.filter((row) => row.state === "behind");
  const active = order(rows.filter((row) => !INACTIVE.has(row.state)), next);
  const inactive = rows.filter((row) => INACTIVE.has(row.state));

  return html`
    <div class="screen">
      <${ScreenHeader} title="Subjects">
        ${behind.length
          ? `${plural(behind.length, "subject")} with something unreviewed, ` +
            `${plural(
              behind.reduce((sum, row) => sum + row.unreviewed, 0),
              "item"
            )} in total. In the order of their next session.`
          : "Nothing unreviewed in any gated subject."}
      </${ScreenHeader}>
      <ul class="grid subjects">
        ${active.map(
          (row) => html`<${Standing} key=${row.name} subject=${row} next=${next[row.name]} />`
        )}
      </ul>
      <${Inactive} rows=${inactive} />
    </div>
  `;
}

/** This subject's sessions in the timetable's window. */
function sessionsOf(name, timetable) {
  const found = [];
  for (const day of (timetable && timetable.days) || []) {
    for (const session of day.sessions || []) {
      if ((session.parts || []).some((part) => part.subject === name)) {
        found.push({ date: day.date, session });
      }
    }
  }
  return found;
}

function kindOf(session) {
  const kind = (session.kind || "").toLowerCase();
  return { lec: "lecture", tut: "tutorial", lab: "lab" }[kind] || kind;
}

export function Subject({ name }) {
  const [subject, setSubject] = useState(null);
  const [sessions, setSessions] = useState(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get(`/api/subjects/${encodeURIComponent(name)}`)
      .then((found) => live && setSubject(found))
      .catch((err) => live && setProblem(describe(err)));
    api
      .get("/api/timetable")
      .then((found) => live && setSessions(sessionsOf(name, found)))
      .catch(() => live && setSessions([]));
    return () => {
      live = false;
    };
  }, [name]);

  if (problem) {
    return html`<div class="screen">
      <${Problem}>${problem}</${Problem}>
      <p><a class="button quiet-button" ...${linkProps("/subjects")}>All subjects</a></p>
    </div>`;
  }
  if (!subject) return html`<div class="screen"><${Skeleton} rows=${3} /></div>`;

  const item = subject.next_item;

  return html`
    <div class="screen subject-page">
      <header class="screen-header">
        <a class="back-link t-meta" ...${linkProps("/subjects")}>Subjects</a>
        <h1 class="t-title"><${SubjectName} name=${subject.name} /></h1>
        <p class="t-state">${standing(subject)}</p>
      </header>

      <div class="split">
        <div class="section">
          ${subject.state === "behind"
            ? html`
                <dl class="card fact-list subject-facts">
                  <dt>unreviewed</dt>
                  <dd class="num">${subject.unreviewed}</dd>
                  <dt>ready to quiz</dt>
                  <dd class="num">${subject.ready}</dd>
                  <dt>pages not transcribed</dt>
                  <dd class="num">${subject.unread_pages}</dd>
                  ${subject.oldest_posted_at
                    ? html`<dt>oldest posted</dt>
                        <dd>${age(subject.oldest_posted_at)}</dd>`
                    : null}
                </dl>
                ${item
                  ? html`<div class="section">
                      <h2 class="section-title">Next</h2>
                      <${Card} subject=${subject.name}>
                        <span class="t-lead">${item.label}</span>
                        ${item.pages
                          ? html`<span class="t-meta">${plural(item.pages, "page")}</span>`
                          : null}
                      </${Card}>
                    </div>`
                  : null}
              `
            : null}

          ${subject.course_id
            ? html`<${Card}
                to=${`/library?course=${encodeURIComponent(subject.course_id)}`}
              >
                <span class="t-lead">Material held for this subject</span>
                <span class="t-meta">Every post and file this install has for it.</span>
              </${Card}>`
            : null}
        </div>

        ${sessions && sessions.length
          ? html`<section class="section">
              <h2 class="section-title">Sessions, next two weeks</h2>
              <ul class="list">
                ${sessions.map(
                  ({ date, session }) => html`<li key=${`${date}-${session.start}`}>
                    <div class="card subject-session">
                      <span class="t-meta num session-at">${`${relativeDay(date)} ${session.start}`}</span>
                      <div class="card-body">
                        <span>${[kindOf(session), session.parts[0] && session.parts[0].room]
                          .filter(Boolean)
                          .join(", ")}</span>
                        ${session.note ? html`<span class="t-meta">${session.note}</span>` : null}
                      </div>
                    </div>
                  </li>`
                )}
              </ul>
            </section>`
          : null}
      </div>
    </div>
  `;
}
