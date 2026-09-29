// Everything entered by hand: an upload, a task, a session that happened.
//
// This is the screen Phase 6 exists for. A third of my week has no Classroom at
// all -- Calculus III, Algebra III, Philosophy -- and a gate covering three
// subjects out of twelve is a gate I stop believing.
//
// One rule governs every form here: **a failure is visible and keeps what I
// typed.** Nothing is optimistically shown as saved, and nothing is queued
// silently. A task that looks recorded and is not is the silent-success failure
// this project ranks worst, and it would be worst precisely here, where the row
// exists nowhere else.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { Offline, api, describe } from "/api.js";

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

/** A form that says what happened, and never pretends. */
function Form({ title, note, children, onSubmit, submitLabel }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [said, setSaid] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setProblem("");
    setSaid("");
    try {
      setSaid(await onSubmit(new FormData(event.target)));
      event.target.reset();
    } catch (err) {
      // The fields are NOT cleared on failure. Retyping a brief because a
      // request timed out is how a feature stops being used.
      setProblem(
        err instanceof Offline
          ? "No connection, so this was not recorded. Nothing has been lost — try again when you are back."
          : describe(err)
      );
    } finally {
      setBusy(false);
    }
  }

  return html`
    <section>
      <h2>${title}</h2>
      ${note ? html`<p class="faint">${note}</p>` : null}
      <form onSubmit=${submit}>
        ${children}
        <button type="submit" disabled=${busy}>
          ${busy ? "…" : submitLabel}
        </button>
      </form>
      ${said ? html`<p class="said">${said}</p>` : null}
      ${problem ? html`<p class="problem">${problem}</p>` : null}
    </section>
  `;
}

function SubjectPicker({ subjects }) {
  return html`
    <label>
      Subject
      <select name="subject" required>
        <option value="">—</option>
        ${subjects.map(
          (name) => html`<option key=${name} value=${name}>${name}</option>`
        )}
      </select>
    </label>
  `;
}

export function Add() {
  const subjects = useSubjects();

  return html`
    <main class="pad">
      <h1>Add</h1>
      <p class="quiet">
        Material and work that no API this project can call knows about.
      </p>

      <${Form}
        title="Upload a file"
        note=${
          "A photographed board, a classmate's notes, an emailed handout. It " +
          "enters the pipeline unchanged — extract, OCR, packs, quiz, gate. " +
          "Download is the only stage an upload skips."
        }
        submitLabel="Upload"
        onSubmit=${async (data) => {
          const body = await api.postForm("/api/uploads", data);
          return `${body.title} is in ${body.course_name}. ${body.note}`;
        }}
      >
        <${SubjectPicker} subjects=${subjects} />
        <label>
          File
          <input type="file" name="file" required accept=".pdf,.jpg,.jpeg,.png,.docx,.pptx,.txt,.md" />
        </label>
        <label>
          Title <span class="faint">(optional)</span>
          <input type="text" name="title" placeholder="the file's own name" />
        </label>
        <label>
          Posted <span class="faint">(optional; the OCR queue sorts on it)</span>
          <input type="date" name="posted" />
        </label>
      </>

      <${Form}
        title="Record a task"
        note="It joins the existing T-72/24/3 scanner rather than getting a second one."
        submitLabel="Record it"
        onSubmit=${async (data) => {
          const body = await api.post("/api/tasks", {
            subject: data.get("subject"),
            title: data.get("title"),
            kind: data.get("kind") || "exercise_sheet",
            due: data.get("due") || null,
          });
          return body.due_at
            ? `${body.title} is due ${body.due_at}.`
            : `${body.title} recorded with no date, so the scanner will never alert on it.`;
        }}
      >
        <${SubjectPicker} subjects=${subjects} />
        <label>
          Title
          <input type="text" name="title" required placeholder="TD3 — graphes" />
        </label>
        <label>
          Kind
          <select name="kind">
            <option value="exercise_sheet">exercise sheet</option>
            <option value="tutorial">tutorial</option>
            <option value="reading">reading</option>
          </select>
        </label>
        <label>
          Due <span class="faint">(optional; end of day, locally)</span>
          <input type="date" name="due" />
        </label>
      </>

      <${Form}
        title="Log a session"
        note="The timetable says a session was scheduled. This says one took place."
        submitLabel="Log it"
        onSubmit=${async (data) => {
          const body = await api.post("/api/sessions", {
            subject: data.get("subject"),
            kind: data.get("kind") || "LEC",
            on: data.get("on") || null,
            covered: data.get("covered") || null,
          });
          return `${body.course_name || body.course_id} on ${body.held_on} logged.`;
        }}
      >
        <${SubjectPicker} subjects=${subjects} />
        <label>
          Kind
          <select name="kind">
            <option value="LEC">lecture</option>
            <option value="TUT">tutorial</option>
            <option value="LAB">lab</option>
          </select>
        </label>
        <label>
          Held <span class="faint">(optional; today by default)</span>
          <input type="date" name="on" />
        </label>
        <label>
          Covered <span class="faint">(optional)</span>
          <input type="text" name="covered" placeholder="integration by parts" />
        </label>
      </>
    </main>
  `;
}
