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

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, Offline } from "/api.js";
import { age, plural } from "/format.js";
import { linkProps } from "/router.js";

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

function Standing({ subject }) {
  const said = SAYS[subject.state];
  const item = subject.next_item;

  return html`
    <li class="subject">
      <a class="subject-name" ...${linkProps(
        `/subjects/${encodeURIComponent(subject.name)}`
      )}>
        ${subject.name}
      </a>
      ${said
        ? html`<span class="subject-said">
            ${said}${subject.dead_files
              ? ` — ${plural(subject.dead_files, "attachment")} gone from Drive`
              : ""}
          </span>`
        : html`
            <span class="subject-said">
              ${subject.blocked
                ? `${subject.unreviewed} unreviewed, ${subject.ready} ready`
                : plural(subject.unreviewed, "unreviewed", "unreviewed")}
            </span>
            ${subject.oldest_posted_at
              ? html`<span class="subject-age">
                  ${`oldest posted ${age(subject.oldest_posted_at)}`}
                </span>`
              : null}
            ${item
              ? html`<span class="subject-next">
                  ${`next: ${item.label}`}
                </span>`
              : null}
          `}
    </li>
  `;
}

export function Subjects() {
  const [rows, setRows] = useState(null);
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
            : err.detail || err.message
        );
      });
    return () => {
      live = false;
    };
  }, []);

  if (problem) {
    return html`<main class="pad"><p class="problem">${problem}</p></main>`;
  }
  if (!rows) return html`<main class="pad"><p class="faint">…</p></main>`;

  const behind = rows.filter((row) => row.state === "behind");

  return html`
    <main class="pad">
      <h1>Subjects</h1>
      <ul class="subjects">
        ${rows.map((row) => html`<${Standing} key=${row.name} subject=${row} />`)}
      </ul>
      ${behind.length
        ? html`<p class="quiet">
            ${`${plural(behind.length, "subject")} with something unreviewed, ` +
            `${plural(
              behind.reduce((sum, row) => sum + row.unreviewed, 0),
              "item"
            )} in total.`}
          </p>`
        : html`<p class="quiet">Nothing unreviewed in any gated subject.</p>`}
    </main>
  `;
}

export function Subject({ name }) {
  const [subject, setSubject] = useState(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get(`/api/subjects/${encodeURIComponent(name)}`)
      .then((found) => live && setSubject(found))
      .catch((err) => live && setProblem(err.detail || err.message));
    return () => {
      live = false;
    };
  }, [name]);

  if (problem) {
    return html`<main class="pad">
      <p class="problem">${problem}</p>
      <p><a class="plain-link" ...${linkProps("/subjects")}>Back</a></p>
    </main>`;
  }
  if (!subject) return html`<main class="pad"><p class="faint">…</p></main>`;

  const item = subject.next_item;

  return html`
    <main class="pad">
      <p><a class="plain-link" ...${linkProps("/subjects")}>Subjects</a></p>
      <h1>${subject.name}</h1>
      <p class="quiet">
        ${SAYS[subject.state] ||
        `${subject.unreviewed} unreviewed${
          subject.blocked ? `, ${subject.ready} ready` : ""
        }`}
      </p>

      ${subject.state === "behind"
        ? html`
            <ul class="facts">
              <li>
                <span>unreviewed</span><b>${subject.unreviewed}</b>
              </li>
              <li><span>ready to quiz</span><b>${subject.ready}</b></li>
              <li>
                <span>pages not transcribed</span><b>${subject.unread_pages}</b>
              </li>
              ${subject.oldest_posted_at
                ? html`<li>
                    <span>oldest posted</span
                    ><b>${age(subject.oldest_posted_at)}</b>
                  </li>`
                : null}
            </ul>
            ${item
              ? html`<p class="quiet">${`Next: ${item.label}`}</p>`
              : null}
          `
        : null}

      ${subject.course_id
        ? html`<p>
            <a class="plain-link" ...${linkProps(
              `/library?course=${encodeURIComponent(subject.course_id)}`
            )}>
              Material held for this subject
            </a>
          </p>`
        : null}
    </main>
  `;
}
