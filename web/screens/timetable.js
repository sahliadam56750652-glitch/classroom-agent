// The timetable, and recording what a professor did to one session.
//
// This is the Phase 5 requirement that started the adjustments layer: "a moved
// or cancelled session must be recordable from the app", because on a phone at
// 22:00 hand-editing timetable.yaml does not happen, and the gate then prepares
// me for a lecture that is not taking place.
//
// Three rules from PLAN.md are visible in this screen:
//
//   The FILE is never written. This records a dated fact and the gate resolves
//   the two together. There is no edit-the-pattern control here and there must
//   not be one.
//
//   A departed session is shown CROSSED OUT rather than removed. A day that is
//   silently shorter than the printed timetable is indistinguishable from a bug
//   in the resolver.
//
//   An orphan is always printed. Never applied, never dropped.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, describe } from "/api.js";
import { shortDate } from "/format.js";

function today() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(
    now.getDate()
  ).padStart(2, "0")}`;
}

function Session({ session, departed, onCancel, onMove }) {
  const subjects = session.parts.map((part) => part.subject).join(" + ");
  const where = session.parts
    .map((part) => part.room)
    .filter(Boolean)
    .join(" / ");

  return html`
    <li class=${`session ${departed ? "departed" : ""}`}>
      <span class="session-when">${session.start}</span>
      <span class="session-what">
        <b>${subjects}</b>
        <span class="faint">
          ${`${session.kind}${where ? ` · ${where}` : ""}`}
        </span>
        ${session.note
          ? html`<span class="session-note">${session.note}</span>`
          : null}
      </span>
      ${departed
        ? null
        : html`<span class="session-do">
            <button class="plain" onClick=${() => onCancel(session)}>
              Cancel
            </button>
            <button class="plain" onClick=${() => onMove(session)}>Move</button>
          </span>`}
    </li>
  `;
}

export function Timetable() {
  const [data, setData] = useState(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState("");
  const [moving, setMoving] = useState(null);

  const load = () => {
    const from = today();
    api
      .get(`/api/timetable?from=${from}`)
      .then(setData)
      .catch((err) => setProblem(describe(err)));
  };

  useEffect(load, []);

  async function record(spec) {
    setBusy(true);
    setProblem("");
    setSaid("");
    try {
      const done = await api.post("/api/adjustments", spec);
      // What it does, said back -- including which evening now gates it, which
      // is the part I would otherwise have to work out.
      setSaid(
        `${done.kind} ${done.subject} at ${done.session_start}. ${done.note}`
      );
      load();
    } catch (err) {
      setProblem(describe(err));
    } finally {
      setBusy(false);
    }
  }

  const cancel = (day) => (session) =>
    record({
      kind: "cancelled",
      subject: session.parts[0].subject,
      on: day.date,
      at: session.start,
    });

  // Inline, never `prompt()`. A modal browser dialog blocks the page, looks
  // nothing like the rest of the app, and is suppressed outright in an
  // installed PWA on some platforms -- a Move button that silently does nothing
  // is worse than no Move button.
  const move = (day) => (session) =>
    setMoving({ day: day.date, start: session.start,
                subject: session.parts[0].subject, to: session.start });

  if (problem && !data) {
    return html`<main class="pad"><p class="problem">${problem}</p></main>`;
  }
  if (!data) return html`<main class="pad"><p class="faint">…</p></main>`;

  const withSomething = data.days.filter(
    (day) => day.sessions.length || day.departed.length
  );

  return html`
    <main class="pad">
      <h1>Timetable</h1>
      ${said ? html`<p class="said">${said}</p>` : null}
      ${problem ? html`<p class="problem">${problem}</p>` : null}

      ${moving
        ? html`
            <form
              class="move-form"
              onSubmit=${(event) => {
                event.preventDefault();
                const spec = {
                  kind: "moved",
                  subject: moving.subject,
                  on: moving.day,
                  at: moving.start,
                  to_time: moving.to,
                };
                setMoving(null);
                record(spec);
              }}
            >
              <label>
                ${`Move ${moving.subject} from ${moving.start} on ${moving.day} to`}
                <input
                  type="time"
                  required
                  value=${moving.to}
                  onInput=${(event) =>
                    setMoving({ ...moving, to: event.target.value })}
                />
              </label>
              <button type="submit">Record the move</button>
              <button type="button" class="plain" onClick=${() => setMoving(null)}>
                Cancel
              </button>
            </form>
          `
        : null}

      ${data.orphans.length
        ? html`
            <!--
              Always printed. An adjustment that names nothing the file still
              contains is never applied and never silently dropped, so it has to
              be visible or it is simply lost.
            -->
            <section>
              <h2>Orphaned adjustments</h2>
              <ul class="facts">
                ${data.orphans.map(
                  (orphan) => html`
                    <li key=${orphan.id}>
                      <span>
                        ${`${orphan.applies_on} ${orphan.session_start} ${orphan.subject}`}
                      </span>
                      <b class="faint">${orphan.why}</b>
                    </li>
                  `
                )}
              </ul>
              <p class="faint">
                Moved only with <code>agent adjust --repoint</code>. Matching
                approximately is what adjusts the wrong session while looking
                like it worked.
              </p>
            </section>
          `
        : null}

      ${withSomething.map(
        (day) => html`
          <section key=${day.date}>
            <h2>${shortDate(day.date)}</h2>
            ${day.exception
              ? html`<p class="faint">${day.exception}</p>`
              : null}
            <ul class="sessions">
              ${day.sessions.map(
                (session) => html`<${Session}
                  key=${`${day.date}-${session.start}`}
                  session=${session}
                  onCancel=${cancel(day)}
                  onMove=${move(day)}
                />`
              )}
              ${day.departed.map(
                (session) => html`<${Session}
                  key=${`${day.date}-gone-${session.start}`}
                  session=${session}
                  departed=${true}
                />`
              )}
            </ul>
          </section>
        `
      )}

      <p class="faint">
        ${`${data.path} is never written by this app. A professor moving one ` +
        `Tuesday is one dated fact about one Tuesday; a permanent change is a ` +
        `new version in the file, by hand.`}
      </p>
      ${busy ? html`<p class="quiet">…</p>` : null}
    </main>
  `;
}
