// Everything entered by hand: an upload, a task, a session that happened, a
// project.
//
// This is the screen Phase 6 exists for. A third of my week has no Classroom at
// all -- Calculus III, Algebra III, Philosophy -- and a gate covering three
// subjects out of twelve is a gate I stop believing. For those subjects a
// photographed board is the main way material arrives, so the upload opens the
// CAMERA in one tap, with the gallery and files as the second door.
//
// One rule governs every form here: **a failure is visible and keeps what I
// typed.** Nothing is optimistically shown as saved, and nothing is queued
// silently. A task that looks recorded and is not is the silent-success failure
// this project ranks worst, and it would be worst precisely here, where the row
// exists nowhere else.

import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "/html.js";
import { Offline, api, describe } from "/api.js";
import { Icon } from "/icons.js";
import { ScreenHeader } from "/ui.js";
import { subjectStyle } from "/subject-color.js";

// The subject chosen last time, remembered on this device: a board is usually
// photographed straight after the lecture it belongs to, so it is usually the
// same subject twice in a row. A convenience, never state anything relies on.
const LAST_SUBJECT = "lectern.add.subject";

function remembered() {
  try {
    return localStorage.getItem(LAST_SUBJECT) || "";
  } catch {
    return "";
  }
}

function remember(name) {
  try {
    localStorage.setItem(LAST_SUBJECT, name);
  } catch {
    // Not remembered; nothing else changes.
  }
}

function useSubjects() {
  const [subjects, setSubjects] = useState([]);
  useEffect(() => {
    api
      .get("/api/subjects")
      .then((rows) => setSubjects(rows.map((row) => row.name)))
      .catch(() => setSubjects([]));
  }, []);
  return subjects;
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
          ? "No connection, so this was not recorded. Nothing has been lost; try again when you are back."
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

/**
 * The upload: the camera first, in one tap.
 *
 * Two real <input type=file>s behind two labels. `capture="environment"` on the
 * first opens the back camera directly on a phone -- the board, now -- and the
 * second has no capture, so it offers the gallery and files: the photo taken an
 * hour ago, a PDF someone sent. Both feed the same form.
 */
function Upload({ subjects }) {
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState("");
  const [subject, setSubject] = useState(remembered());
  const camera = useRef(null);
  const gallery = useRef(null);

  useEffect(() => {
    if (!file || !file.type.startsWith("image/")) {
      setPreview("");
      return undefined;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const picked = (event) => {
    const found = event.target.files && event.target.files[0];
    if (found) setFile(found);
  };

  return html`<${Form}
    id="upload"
    class="add-upload"
    title="Upload a board or a handout"
    note="A photographed board, a classmate's notes, an emailed handout. It is read, transcribed and quizzed like anything from Classroom."
    submitLabel="Upload"
    ready=${Boolean(file && subject)}
    onSubmit=${async (data, form) => {
      data.set("file", file);
      data.set("subject", subject);
      const body = await api.postForm("/api/uploads", data);
      remember(subject);
      setFile(null);
      form.reset();
      return `${body.title} is in ${body.course_name}. It is read at the next sync.`;
    }}
  >
    <div class="pick">
      <label class="button primary-button pick-camera">
        <${Icon} name="camera" /> Photograph a board
        <input
          ref=${camera}
          class="visually-hidden"
          type="file"
          accept="image/*"
          capture="environment"
          onChange=${picked}
        />
      </label>
      <label class="button secondary-button pick-file">
        <${Icon} name="image" /> Choose a photo or file
        <input
          ref=${gallery}
          class="visually-hidden"
          type="file"
          accept="image/*,.pdf,.docx,.pptx,.txt,.md"
          onChange=${picked}
        />
      </label>
    </div>
    ${file
      ? html`<div class="picked">
          ${preview ? html`<img class="picked-image" src=${preview} alt="The photo to upload" />` : null}
          <span class="t-meta">${file.name}</span>
        </div>`
      : null}
    <${SubjectChoice} subjects=${subjects} value=${subject} onChange=${setSubject} />
    <label class="field">
      <span>Title <span class="hint">optional, the file's own name otherwise</span></span>
      <input type="text" name="title" />
    </label>
  </${Form}>`;
}

export function Add() {
  const subjects = useSubjects();
  const [taskSubject, setTaskSubject] = useState("");
  const [sessionSubject, setSessionSubject] = useState("");
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
      What Classroom doesn't know about: a board, a task from class, a session, a project.
    </${ScreenHeader}>

    <${Upload} subjects=${subjects} />

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
        id="session"
        title="Log a session"
        note="The timetable says a session was scheduled. This says one took place."
        submitLabel="Log it"
        ready=${Boolean(sessionSubject)}
        onSubmit=${async (data, form) => {
          const body = await api.post("/api/sessions", {
            subject: sessionSubject,
            kind: data.get("kind") || "LEC",
            on: data.get("on") || null,
            covered: data.get("covered") || null,
          });
          form.reset();
          return `${body.course_name || body.course_id}, ${body.held_on}: logged.`;
        }}
      >
        <${SubjectChoice} subjects=${subjects} value=${sessionSubject} onChange=${setSessionSubject} />
        <div class="field-row">
          <label class="field">
            <span>Kind</span>
            <select name="kind">
              <option value="LEC">Lecture</option>
              <option value="TUT">Tutorial</option>
              <option value="LAB">Lab</option>
            </select>
          </label>
          <label class="field">
            <span>Held <span class="hint">today if empty</span></span>
            <input type="date" name="on" />
          </label>
        </div>
        <label class="field">
          <span>Covered <span class="hint">optional</span></span>
          <input type="text" name="covered" placeholder="Integration by parts" />
        </label>
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
