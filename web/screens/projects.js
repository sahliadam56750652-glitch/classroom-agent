// Projects. Phase 6, milestone-based.
//
// "Milestone-based progress rather than a self-reported percentage" is not a UI
// preference and this screen is where it is either honoured or quietly undone.
// A percentage is a feeling typed into a box; a milestone either happened or it
// did not, and I cannot round it. So progress renders as two integers and there
// is no slider, no percent field, and nothing that computes one.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, describe } from "/api.js";
import { hasPassed, localTime } from "/format.js";

function Project({ project, onComplete, onClose }) {
  const { milestones = [] } = project;

  return html`
    <li class="project">
      <div class="project-head">
        <b>${project.title}</b>
        <span class="faint">${project.course_name || project.course_id}</span>
      </div>

      <div class="post-meta ${hasPassed(project.deadline_at) ? "passed" : ""}">
        ${project.deadline_at ? localTime(project.deadline_at) : "no deadline"}
        ${project.deadline_at ? "" : " — the scanner will never alert on this"}
      </div>

      ${milestones.length
        ? html`
            <!--
              Two integers, never a share. The count is what I can move: four
              becoming five is visibly a thing I did, and 80% becoming 100% is
              the same feeling twice.
            -->
            <div class="quiet">
              ${`${project.milestones_done} of ${project.milestones_total} milestones`}
            </div>
            <ul class="milestones">
              ${milestones.map(
                (step) => html`
                  <li key=${step.id}>
                    ${step.done_at
                      ? html`<span class="done">${`✓ ${step.title}`}</span>`
                      : html`<button
                          class="plain"
                          onClick=${() => onComplete(step.id)}
                        >
                          ${step.title}
                        </button>`}
                  </li>
                `
              )}
            </ul>
          `
        : html`<p class="faint">
            No milestones. Progress is counted from them and from nothing else,
            so a project with none has none to report.
          </p>`}

      ${project.closed_at
        ? html`<span class="faint">closed</span>`
        : html`<button class="plain" onClick=${() => onClose(project.id)}>
            Close it
          </button>`}
    </li>
  `;
}

export function Projects() {
  const [rows, setRows] = useState(null);
  const [problem, setProblem] = useState("");

  const load = () =>
    api
      .get("/api/projects")
      .then(setRows)
      .catch((err) => setProblem(describe(err)));

  useEffect(load, []);

  async function act(work) {
    setProblem("");
    try {
      await work();
      await load();
    } catch (err) {
      setProblem(describe(err));
    }
  }

  if (problem && !rows) {
    return html`<main class="pad"><p class="problem">${problem}</p></main>`;
  }
  if (!rows) return html`<main class="pad"><p class="faint">…</p></main>`;

  return html`
    <main class="pad">
      <h1>Projects</h1>
      ${problem ? html`<p class="problem">${problem}</p>` : null}
      ${!rows.length
        ? html`<p class="quiet">
            Nothing recorded. A project reaches Classroom late or never, which
            is why it is entered by hand.
          </p>`
        : html`<ul class="projects">
            ${rows.map(
              (project) => html`<${Project}
                key=${project.id}
                project=${project}
                onComplete=${(id) =>
                  act(() => api.post(`/api/milestones/${id}/complete`))}
                onClose=${(id) =>
                  act(() => api.post(`/api/projects/${id}/close`))}
              />`
            )}
          </ul>`}
    </main>
  `;
}
