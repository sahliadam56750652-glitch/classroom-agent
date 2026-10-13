// Everything entered by hand: a task, a project.
//
// Two forms, and that is deliberate. Uploads and session logging were cancelled
// on 2026-10-13 with the rest of manual logging; tasks and projects stay, filed
// under any subject -- including the four with no Classroom, through the
// `manual-` ids they already have.
//
// One rule governs every form here: **a failure is visible and keeps what I
// typed.** Nothing is optimistically shown as saved, and nothing is queued
// silently. A task that looks recorded and is not is the silent-success failure
// this project ranks worst, and it would be worst precisely here, where the row
// exists nowhere else. Creating needs the PC, and offline these say so.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { Offline, api, describe } from "/api.js";
import { syncNow, useLocal } from "/sync.js";
import { Icon } from "/icons.js";
import { ScreenHeader } from "/ui.js";
import { subjectStyle } from "/subject-color.js";

/** The subjects to file under, from the phone's snapshot. */
function useSubjects() {
  return (useLocal("/api/subjects").data || []).map((row) => row.name);
}

/** Which subject, as a row of chips: one tap, the colour beside the name. */
function SubjectChoice({ subjects, value, onChange }) {
  return html`<fieldset class="subject-choice">
    <legend>Subject</legend>
    <div class="filter-chips">
      ${subjects.map(
        (name) => html`<button
          key=${name}
          type="button"
          class="filter-chip"
          style=${subjectStyle(name)}
          aria-pressed=${value === name ? "true" : "false"}
          onClick=${() => onChange(name)}
        >${name}</button>`
      )}
    </div>
  </fieldset>`;
}

/** A form card that says what happened, and never pretends. */
function Form({ id, title, note, children, onSubmit, submitLabel, ready = true, class: extra = "" }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [said, setSaid] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setProblem("");
    setSaid("");
    try {
      setSaid(await onSubmit(new FormData(event.target), event.target));
    } catch (err) {
      // The fields are NOT cleared on failure. Retyping a brief because a
      // request timed out is how a feature stops being used.
      setProblem(
        err instanceof Offline
          ? "The PC is not reachable, so this was not recorded. What you typed is still here; try again when the PC is on."
          : describe(err)
      );
    } finally {
      setBusy(false);
    }
  }

  return html`<section class=${`card add-card ${extra}`} id=${id} aria-labelledby=${`${id}-title`}>
    <form class="card-body add-form" onSubmit=${submit}>
      <h2 class="t-lead" id=${`${id}-title`}>${title}</h2>
      ${note ? html`<p class="t-meta">${note}</p>` : null}
      ${children}
      <button class="button primary-button" type="submit" disabled=${busy || !ready}>
        ${busy ? "Saving…" : submitLabel}
      </button>
      ${said ? html`<p class="notice" role="status">${said}</p>` : null}
      ${problem ? html`<p class="problem" role="alert">${problem}</p>` : null}
    </form>
  </section>`;
}

export function Add() {
  const subjects = useSubjects();
  const [taskSubject, setTaskSubject] = useState("");
  const [projectSubject, setProjectSubject] = useState("");

  // /add#project, from Work's "New project": open on that form.
  useEffect(() => {
    if (location.hash) {
      const target = document.getElementById(location.hash.slice(1));
      if (target) target.scrollIntoView({ block: "start" });
    }
  }, []);

  return html`<div class="screen add">
    <${ScreenHeader} title="Add">
      What Classroom doesn't know about: a task from class, or a project.
    </${ScreenHeader}>

    <div class="add-grid">
      <${Form}
        id="task"
        title="Record a task"
        note="An exercise sheet, a tutorial to prepare. It is alerted on like any deadline."
        submitLabel="Record it"
        ready=${Boolean(taskSubject)}
        onSubmit=${async (data, form) => {
          const body = await api.post("/api/tasks", {
            subject: taskSubject,
            title: data.get("title"),
            kind: data.get("kind") || "exercise_sheet",
            due: data.get("due") || null,
          });
          form.reset();
          syncNow();
          return body.due_at
            ? `${body.title} is recorded.`
            : `${body.title} is recorded with no date, so nothing will remind you of it.`;
        }}
      >
        <${SubjectChoice} subjects=${subjects} value=${taskSubject} onChange=${setTaskSubject} />
        <label class="field">
          <span>Title</span>
          <input type="text" name="title" required placeholder="TD3, graphes" />
        </label>
        <div class="field-row">
          <label class="field">
            <span>Kind</span>
            <select name="kind">
              <option value="exercise_sheet">Exercise sheet</option>
              <option value="tutorial">Tutorial</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label class="field">
            <span>Due <span class="hint">optional</span></span>
            <input type="date" name="due" />
          </label>
        </div>
      </${Form}>

      <${Form}
        id="project"
        title="Start a project"
        note="Progress is counted from milestones and from nothing else."
        submitLabel="Start it"
        ready=${Boolean(projectSubject)}
        onSubmit=${async (data, form) => {
          const milestones = String(data.get("milestones") || "")
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean);
          const body = await api.post("/api/projects", {
            subject: projectSubject,
            title: data.get("title"),
            deadline: data.get("deadline") || null,
            milestones,
          });
          form.reset();
          syncNow();
          return `${body.title} is started, with ${body.milestones_total} milestone${body.milestones_total === 1 ? "" : "s"}.`;
        }}
      >
        <${SubjectChoice} subjects=${subjects} value=${projectSubject} onChange=${setProjectSubject} />
        <label class="field">
          <span>Title</span>
          <input type="text" name="title" required placeholder="Route planner" />
        </label>
        <label class="field">
          <span>Deadline <span class="hint">optional</span></span>
          <input type="date" name="deadline" />
        </label>
        <label class="field">
          <span>Milestones <span class="hint">one per line</span></span>
          <textarea name="milestones" rows="4"></textarea>
        </label>
      </${Form}>
    </div>
  </div>`;
}
