// Work: homework and projects, together. DESIGN.md section 8.
//
// Homework is Classroom coursework with my submission's state, and the tasks I
// entered by hand, in one list by due date -- from /api/homework, which is the
// same function `agent homework` prints. Two rules with teeth:
//
//   The due date is an instant and nothing else. No countdown, no colour that
//   escalates as it nears (section 7). The one red in this app is a deadline
//   that has ALREADY gone and is still not handed in.
//
//   A Classroom item is never handed in from here. Invariant 6: this project
//   never writes to Classroom, and turning work in is a write -- so the card
//   links to its page there. A task is mine and can be marked done.
//
// Projects are counted from milestones and from nothing else: "2 of 4
// milestones", never a share. The page for one project is the checklist.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, describe } from "/api.js";
import { hasPassed, plural } from "/format.js";
import { Icon } from "/icons.js";
import { linkProps } from "/router.js";
import { Card, Chip, Empty, Problem, ScreenHeader, Skeleton, useSubjectNames } from "/ui.js";
import { subjectStyle } from "/subject-color.js";

const DAY = 86400000;

/** "Tue 6 Oct, 22:59" -- the instant, in the display face. */
export function dueAt(stamp) {
  const at = new Date(stamp);
  const day = at.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  const clock = at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${day}, ${clock}`;
}

// What a Classroom submission state says, in words a person uses.
const SUBMISSION = {
  null: "Not started",
  NEW: "Not handed in",
  CREATED: "Not handed in",
  RECLAIMED_BY_STUDENT: "Taken back, not handed in",
  TURNED_IN: "Handed in",
  RETURNED: "Returned",
};

const TASK_KIND = { tutorial: "Tutorial", exercise_sheet: "Exercise sheet", other: "Task" };

/**
 * Group by when, soonest first: what has gone, this week, later, no date.
 * "Gone" is first because it is the one thing here that is late -- and the
 * only red in the app -- not because the screen is meant to alarm.
 */
function groups(rows) {
  const now = Date.now();
  const week = now + 7 * DAY;
  const out = [
    ["Past its date", []],
    ["This week", []],
    ["Later", []],
    ["No date", []],
  ];
  for (const row of rows) {
    if (!row.due_at) out[3][1].push(row);
    else if (hasPassed(row.due_at)) out[0][1].push(row);
    else if (Date.parse(row.due_at) <= week) out[1][1].push(row);
    else out[2][1].push(row);
  }
  return out.filter(([, found]) => found.length);
}

function Homework({ row, subject, onDone }) {
  const [busy, setBusy] = useState(false);
  const passed = row.due_at && hasPassed(row.due_at) && !row.done;
  const what =
    row.kind === "classroom"
      ? SUBMISSION[row.submission_state] || row.submission_state || "Not started"
      : TASK_KIND[row.task_kind] || "Task";

  return html`<li>
    <div class="card has-subject homework" style=${subjectStyle(subject)}>
      <div class="card-body">
        <div class="homework-top">
          <${Chip} name=${subject} />
          <span class="t-meta">${what}${row.late ? ", late" : ""}</span>
        </div>
        <span class="t-lead homework-title">${row.title}</span>
        ${row.due_at
          ? html`<span class=${`t-meta homework-due ${passed ? "passed" : ""}`}>
              ${passed ? "Was due " : "Due "}<span class="time">${dueAt(row.due_at)}</span>
            </span>`
          : html`<span class="t-meta">No date given</span>`}
        <div class="homework-doors">
          ${row.kind === "classroom" && row.link
            ? html`<a class="button quiet-button" href=${row.link} target="_blank" rel="noopener">
                <${Icon} name="external" /> Open in Classroom
              </a>`
            : null}
          ${row.kind === "task" && !row.done
            ? html`<button
                class="button secondary-button"
                type="button"
                disabled=${busy}
                onClick=${async () => {
                  setBusy(true);
                  await onDone(row.id);
                  setBusy(false);
                }}
              >
                <${Icon} name="check" /> Mark done
              </button>`
            : null}
        </div>
      </div>
    </div>
  </li>`;
}

/** One project as a card: its subject, its deadline, and two integers. */
export function ProjectCard({ project, subject }) {
  const passed = project.deadline_at && hasPassed(project.deadline_at) && !project.closed_at;
  return html`<li>
    <${Card} to=${`/projects/${project.id}`} subject=${subject}>
      <${Chip} name=${subject} />
      <span class="t-lead">${project.title}</span>
      <span class="t-state">
        <span class="num">${project.milestones_done}</span> of
        <span class="num"> ${project.milestones_total}</span> milestones
      </span>
      ${project.deadline_at
        ? html`<span class=${`t-meta ${passed ? "passed" : ""}`}>
            ${passed ? "Was due " : "Due "}<span class="time">${dueAt(project.deadline_at)}</span>
          </span>`
        : html`<span class="t-meta">No deadline</span>`}
    </${Card}>
  </li>`;
}

export function Work() {
  const names = useSubjectNames();
  const [rows, setRows] = useState(null);
  const [projects, setProjects] = useState(null);
  const [problem, setProblem] = useState("");

  const load = () => {
    api.get("/api/homework").then(setRows).catch((err) => setProblem(describe(err)));
    api.get("/api/projects").then(setProjects).catch(() => setProjects([]));
  };
  useEffect(load, []);

  const subjectOf = (row) => names[row.course_id] || row.course_name;

  async function done(taskId) {
    setProblem("");
    try {
      await api.post(`/api/tasks/${taskId}/complete`);
      load();
    } catch (err) {
      setProblem(describe(err));
    }
  }

  if (problem && !rows) return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  if (!rows) return html`<div class="screen"><${Skeleton} rows=${4} /></div>`;

  return html`<div class="screen work">
    <${ScreenHeader} title="Work" />
    ${problem ? html`<${Problem}>${problem}</${Problem}>` : null}
    <div class="split">
      <section class="section" aria-labelledby="work-homework">
        <h2 class="section-title" id="work-homework">Homework</h2>
        ${rows.length
          ? groups(rows).map(
              ([title, found]) => html`<div class="day-group" key=${title}>
                <h3 class="day-heading">${title}</h3>
                <ul class="list">
                  ${found.map(
                    (row) => html`<${Homework}
                      key=${`${row.kind}-${row.id}`}
                      row=${row}
                      subject=${subjectOf(row)}
                      onDone=${done}
                    />`
                  )}
                </ul>
              </div>`
            )
          : html`<${Empty} title="Nothing to hand in.">Coursework from Classroom and tasks you add appear here.</${Empty}>`}
      </section>

      <section class="section" aria-labelledby="work-projects">
        <div class="section-head">
          <h2 class="section-title" id="work-projects">Projects</h2>
          <a class="button quiet-button" ...${linkProps("/add#project")}>
            <${Icon} name="add" /> New project
          </a>
        </div>
        ${projects === null
          ? html`<${Skeleton} rows=${1} />`
          : projects.length
          ? html`<ul class="list">
              ${projects.map(
                (project) => html`<${ProjectCard}
                  key=${project.id}
                  project=${project}
                  subject=${subjectOf(project)}
                />`
              )}
            </ul>`
          : html`<${Empty} title="No projects.">A project reaches Classroom late or never, so it is entered by hand.</${Empty}>`}
      </section>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- one project

/**
 * /projects/:id -- the checklist. A milestone either happened or it did not, and
 * I cannot round it, so progress is the count of ticked boxes and nothing else.
 * A ticked milestone stays ticked: there is no un-complete in the API, because a
 * thing that happened did not un-happen.
 */
export function ProjectPage({ id }) {
  const names = useSubjectNames();
  const [project, setProject] = useState(null);
  const [problem, setProblem] = useState("");
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () =>
    api
      .get(`/api/projects/${encodeURIComponent(id)}`)
      .then(setProject)
      .catch((err) => setProblem(describe(err)));
  useEffect(load, [id]);

  async function act(work) {
    setBusy(true);
    setProblem("");
    try {
      await work();
      await load();
    } catch (err) {
      setProblem(describe(err));
    }
    setBusy(false);
  }

  if (problem && !project) return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  if (!project) return html`<div class="screen"><${Skeleton} rows=${3} /></div>`;

  const subject = names[project.course_id] || project.course_name;
  const passed = project.deadline_at && hasPassed(project.deadline_at) && !project.closed_at;
  const steps = project.milestones || [];

  return html`<div class="screen project-page" style=${subjectStyle(subject)}>
    <header class="subject-band">
      <a class="back-link t-meta" ...${linkProps("/work")}>Work</a>
      <${Chip} name=${subject} />
      <h1 class="t-title subject-title">${project.title}</h1>
      <ul class="figures">
        <li class="figure">
          <span class="num">${project.milestones_done}</span>of
          <span class="num">${project.milestones_total}</span>milestones
        </li>
        ${project.deadline_at
          ? html`<li class=${`figure ${passed ? "passed" : ""}`}>
              ${passed ? "was due" : "due"} <span class="time">${dueAt(project.deadline_at)}</span>
            </li>`
          : html`<li class="figure">no deadline</li>`}
        ${project.closed_at ? html`<li class="figure">closed</li>` : null}
      </ul>
    </header>

    ${problem ? html`<${Problem}>${problem}</${Problem}>` : null}

    <div class="split">
      <section class="section" aria-labelledby="project-steps">
        <h2 class="section-title" id="project-steps">Milestones</h2>
        ${steps.length
          ? html`<ul class="checklist">
              ${steps.map(
                (step) => html`<li key=${step.id}>
                  <button
                    class=${`check-row ${step.done_at ? "done" : ""}`}
                    type="button"
                    role="checkbox"
                    aria-checked=${step.done_at ? "true" : "false"}
                    disabled=${busy || Boolean(step.done_at) || Boolean(project.closed_at)}
                    onClick=${() => act(() => api.post(`/api/milestones/${step.id}/complete`))}
                  >
                    <span class="check-box" aria-hidden="true">
                      ${step.done_at ? html`<${Icon} name="check" />` : null}
                    </span>
                    <span class="check-title">${step.title}</span>
                  </button>
                </li>`
              )}
            </ul>`
          : html`<${Empty} title="No milestones yet.">Progress is counted from them and nothing else.</${Empty}>`}

        ${project.closed_at
          ? null
          : html`<form
              class="add-step"
              onSubmit=${(event) => {
                event.preventDefault();
                const title = adding.trim();
                if (!title) return;
                act(async () => {
                  await api.post(`/api/projects/${project.id}/milestones`, { title });
                  setAdding("");
                });
              }}
            >
              <label class="field add-step-field">
                <span class="visually-hidden">A new milestone</span>
                <input
                  type="text"
                  value=${adding}
                  placeholder="Add a milestone"
                  onInput=${(e) => setAdding(e.target.value)}
                />
              </label>
              <button class="button secondary-button" type="submit" disabled=${busy || !adding.trim()}>
                <${Icon} name="add" /> Add
              </button>
            </form>`}
      </section>

      <aside class="section">
        ${project.deliverables && project.deliverables.length
          ? html`<div class="card"><div class="card-body">
              <h2 class="section-title">To hand in</h2>
              <ul class="plain-list">${project.deliverables.map((line) => html`<li key=${line}>${line}</li>`)}</ul>
            </div></div>`
          : null}
        ${project.team && project.team.length
          ? html`<div class="card"><div class="card-body">
              <h2 class="section-title">Team</h2>
              <ul class="plain-list">${project.team.map((line) => html`<li key=${line}>${line}</li>`)}</ul>
            </div></div>`
          : null}
        ${project.closed_at
          ? null
          : html`<button
              class="button quiet-button close-project"
              type="button"
              disabled=${busy}
              onClick=${() => act(() => api.post(`/api/projects/${project.id}/close`))}
            >
              Close this project
            </button>`}
      </aside>
    </div>
  </div>`;
}
