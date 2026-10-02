// The timetable, and recording what a professor did to one session.
//
// This is the Phase 5 requirement that started the adjustments layer: "a moved
// or cancelled session must be recordable from the app", because on a phone at
// 22:00 hand-editing timetable.yaml does not happen, and the gate then prepares
// me for a lecture that is not taking place.
//
// Two layouts of the same week, chosen by width rather than by device:
//
//   At 1024px and above, a week grid like the one pinned above a desk -- days
//   across, hours down, each session a block filled with its subject's colour
//   and placed by its time, so a crowded Tuesday looks crowded.
//
//   Below it, one day at a time: a row of days to choose from, and that day's
//   sessions as tinted cards.
//
// Each session has ONE menu, opened from the session itself, holding Cancel and
// Move. A row of buttons on every line made the week read as a form.
//
// Three rules from PLAN.md are visible here:
//
//   The FILE is never written. This records a dated fact and the gate resolves
//   the two together. There is no edit-the-pattern control and there must not
//   be one.
//
//   A departed session is shown CROSSED OUT rather than removed. A day that is
//   silently shorter than the printed timetable is indistinguishable from a bug
//   in the resolver.
//
//   An orphan is always printed. Never applied, never dropped.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, describe } from "/api.js";
import { Icon } from "/icons.js";
import { Chip, Empty, Problem, ScreenHeader, Skeleton } from "/ui.js";
import { subjectStyle } from "/subject-color.js";

const WIDE = "(min-width: 1024px)";
const DAY_MS = 86400000;
// How tall a minute is on the week grid. Ten hours is then 660px: a working
// day fits one desktop screen without the blocks becoming slivers.
const PX_PER_MIN = 1.1;

function iso(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function parse(day) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d);
}

/** The Monday of the week a date falls in, locally. */
function mondayOf(date) {
  const copy = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const back = (copy.getDay() + 6) % 7;
  copy.setDate(copy.getDate() - back);
  return copy;
}

function minutes(clock) {
  const [h, m] = clock.split(":").map(Number);
  return h * 60 + m;
}

function weekday(day, style = "short") {
  return parse(day).toLocaleDateString(undefined, { weekday: style });
}

function dayNumber(day) {
  return parse(day).getDate();
}

function subjectsOf(session) {
  return (session.parts || []).map((part) => part.subject);
}

function kindWord(session) {
  const kind = (session.kind || "").toLowerCase();
  return { lec: "Lecture", tut: "Tutorial", lab: "Lab" }[kind] || session.kind || "";
}

function roomOf(session) {
  return (session.parts || []).map((part) => part.room).filter(Boolean).join(" / ");
}

/** Whether the window is wide enough for the week grid, kept current. */
function useWide() {
  const [wide, setWide] = useState(() => matchMedia(WIDE).matches);
  useEffect(() => {
    const query = matchMedia(WIDE);
    const changed = () => setWide(query.matches);
    query.addEventListener("change", changed);
    return () => query.removeEventListener("change", changed);
  }, []);
  return wide;
}

// ---------------------------------------------------------------- the menu

/**
 * Cancel or Move, for one session, behind one button.
 *
 * Inline, never `prompt()`. A modal browser dialog blocks the page, looks
 * nothing like the rest of the app, and is suppressed outright in an installed
 * PWA on some platforms -- a Move that silently does nothing is worse than none.
 */
function SessionMenu({ day, session, onRecord, onClose }) {
  const [moving, setMoving] = useState(false);
  const [to, setTo] = useState(session.start);
  const [toDay, setToDay] = useState(day);
  const subject = subjectsOf(session)[0];

  if (moving) {
    return html`<form
      class="session-menu"
      onSubmit=${(event) => {
        event.preventDefault();
        onRecord({
          kind: "moved",
          subject,
          on: day,
          at: session.start,
          to_time: to,
          to_date: toDay !== day ? toDay : null,
        });
      }}
    >
      <p class="t-body session-menu-title">${`Move ${subject}`}</p>
      <div class="session-menu-fields">
        <label class="field">
          <span>Day</span>
          <input type="date" required value=${toDay} onInput=${(e) => setToDay(e.target.value)} />
        </label>
        <label class="field">
          <span>Starts</span>
          <input type="time" required value=${to} onInput=${(e) => setTo(e.target.value)} />
        </label>
      </div>
      <div class="session-menu-actions">
        <button class="button primary-button" type="submit">Record the move</button>
        <button class="button quiet-button" type="button" onClick=${onClose}>Keep it</button>
      </div>
    </form>`;
  }

  return html`<div class="session-menu" role="menu" aria-label=${`${subject} at ${session.start}`}>
    <button
      class="session-menu-item"
      type="button"
      role="menuitem"
      onClick=${() => onRecord({ kind: "cancelled", subject, on: day, at: session.start })}
    >
      <${Icon} name="close" /> Cancel this session
    </button>
    <button class="session-menu-item" type="button" role="menuitem" onClick=${() => setMoving(true)}>
      <${Icon} name="timetable" /> Move it to another time
    </button>
    <button class="session-menu-item quiet" type="button" role="menuitem" onClick=${onClose}>
      Close
    </button>
  </div>`;
}

// ---------------------------------------------------------------- one block

function Block({ day, session, departed, open, onOpen, onRecord, onClose, style }) {
  const names = subjectsOf(session);
  const label = `${names.join(" + ")}, ${kindWord(session)} ${session.start} to ${session.end}`;
  return html`<div
    class=${`tt-block ${departed ? "departed" : ""} ${open ? "open" : ""}`}
    style=${{ ...subjectStyle(names[0]), ...style }}
  >
    <button
      class="tt-block-button session-menu-button"
      type="button"
      disabled=${departed}
      aria-expanded=${open ? "true" : "false"}
      aria-label=${departed ? `${label}, ${session.note || "not taking place"}` : `${label}. Cancel or move`}
      onClick=${onOpen}
    >
      <span class="tt-name">${names.join(" + ")}</span>
      <span class="time tt-time">${`${session.start}–${session.end}`}</span>
      <span class="tt-where">${[kindWord(session), roomOf(session)].filter(Boolean).join(" · ")}</span>
      ${session.note ? html`<span class="tt-note">${session.note}</span>` : null}
    </button>
    ${open
      ? html`<${SessionMenu} day=${day} session=${session} onRecord=${onRecord} onClose=${onClose} />`
      : null}
  </div>`;
}

// ---------------------------------------------------------------- the week grid

function WeekGrid({ days, today, openKey, setOpenKey, onRecord }) {
  const shown = days.filter(
    (day, index) => index < 5 || day.sessions.length || day.departed.length
  );
  const every = shown.flatMap((day) => [...day.sessions, ...day.departed]);
  const first = Math.min(8 * 60, ...every.map((s) => minutes(s.start)));
  const last = Math.max(18 * 60, ...every.map((s) => minutes(s.end)));
  const top = Math.floor(first / 60) * 60;
  const bottom = Math.ceil(last / 60) * 60;
  const hours = [];
  for (let at = top; at < bottom; at += 60) hours.push(at);
  const height = (bottom - top) * PX_PER_MIN;

  return html`<div class="tt-grid" style=${{ "--days": shown.length }}>
    <div class="tt-corner"></div>
    ${shown.map(
      (day) => html`<div key=${`h-${day.date}`} class=${`tt-day-head ${day.date === today ? "today" : ""}`}>
        <span class="tt-weekday">${weekday(day.date, "long")}</span>
        <span class="time tt-date">${dayNumber(day.date)}</span>
        ${day.exception ? html`<span class="tt-exception">${day.exception}</span>` : null}
      </div>`
    )}
    <div class="tt-hours" style=${{ height: `${height}px` }}>
      ${hours.map(
        (at) => html`<span key=${at} class="time tt-hour" style=${{ top: `${(at - top) * PX_PER_MIN}px` }}>
          ${`${String(at / 60).padStart(2, "0")}:00`}
        </span>`
      )}
    </div>
    ${shown.map(
      (day) => html`<div
        key=${`c-${day.date}`}
        class=${`tt-column ${day.date === today ? "today" : ""}`}
        style=${{ height: `${height}px`, "--hour": `${60 * PX_PER_MIN}px` }}
      >
        ${[
          ...day.sessions.map((session) => [session, false]),
          ...day.departed.map((session) => [session, true]),
        ].map(([session, departed]) => {
          const key = `${day.date}-${session.start}-${departed ? "gone" : "on"}`;
          return html`<${Block}
            key=${key}
            day=${day.date}
            session=${session}
            departed=${departed}
            open=${openKey === key}
            onOpen=${() => setOpenKey(openKey === key ? null : key)}
            onClose=${() => setOpenKey(null)}
            onRecord=${onRecord}
            style=${{
              top: `${(minutes(session.start) - top) * PX_PER_MIN}px`,
              height: `${Math.max(44, (minutes(session.end) - minutes(session.start)) * PX_PER_MIN)}px`,
            }}
          />`;
        })}
      </div>`
    )}
  </div>`;
}

// ---------------------------------------------------------------- one day

function DayList({ days, today, chosen, setChosen, openKey, setOpenKey, onRecord }) {
  const day = days.find((found) => found.date === chosen) || days[0];
  const items = [
    ...day.sessions.map((session) => [session, false]),
    ...day.departed.map((session) => [session, true]),
  ].sort((a, b) => minutes(a[0].start) - minutes(b[0].start));

  return html`<div class="tt-day">
    <div class="tt-days" role="tablist" aria-label="Days of the week">
      ${days.map(
        (found) => html`<button
          key=${found.date}
          type="button"
          role="tab"
          aria-selected=${found.date === day.date ? "true" : "false"}
          class=${`tt-day-chip ${found.date === today ? "today" : ""} ${
            found.sessions.length ? "busy" : ""
          }`}
          onClick=${() => setChosen(found.date)}
        >
          <span class="tt-day-chip-name">${weekday(found.date)}</span>
          <span class="time tt-day-chip-date">${dayNumber(found.date)}</span>
        </button>`
      )}
    </div>
    ${day.exception ? html`<p class="notice">${day.exception}</p>` : null}
    ${items.length
      ? html`<ul class="list tt-day-list">
          ${items.map(([session, departed]) => {
            const key = `${day.date}-${session.start}-${departed ? "gone" : "on"}`;
            return html`<li key=${key}>
              <${Block}
                day=${day.date}
                session=${session}
                departed=${departed}
                open=${openKey === key}
                onOpen=${() => setOpenKey(openKey === key ? null : key)}
                onClose=${() => setOpenKey(null)}
                onRecord=${onRecord}
                style=${{}}
              />
            </li>`;
          })}
        </ul>`
      : html`<${Empty} title="No sessions this day." />`}
  </div>`;
}

// ---------------------------------------------------------------- the screen

function rangeLabel(from, to) {
  const a = parse(from);
  const b = parse(to);
  const month = (d) => d.toLocaleDateString(undefined, { month: "short" });
  return a.getMonth() === b.getMonth()
    ? `${a.getDate()} – ${b.getDate()} ${month(b)}`
    : `${a.getDate()} ${month(a)} – ${b.getDate()} ${month(b)}`;
}

/** What a recorded change did, said back in one line. */
function saidBack(done, spec) {
  const what = spec.kind === "cancelled" ? "Cancelled" : "Moved";
  const when = parse(done.applies_on).toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
  const where = spec.kind === "moved" ? ` It now starts at ${spec.to_time}.` : "";
  const gate = done.gate_moves ? " The evening prompt follows the change." : "";
  return `${what}: ${done.subject}, ${when} at ${done.session_start}.${where}${gate}`;
}

export function Timetable() {
  const wide = useWide();
  const today = iso(new Date());
  const [monday, setMonday] = useState(() => iso(mondayOf(new Date())));
  const [data, setData] = useState(null);
  const [problem, setProblem] = useState("");
  const [said, setSaid] = useState("");
  const [chosen, setChosen] = useState(today);
  const [openKey, setOpenKey] = useState(null);

  const sunday = iso(new Date(parse(monday).getTime() + 6 * DAY_MS + 3600000));

  const load = () => {
    api
      .get(`/api/timetable?from=${monday}&to=${sunday}`)
      .then(setData)
      .catch((err) => setProblem(describe(err)));
  };

  useEffect(() => {
    setData(null);
    load();
  }, [monday]);

  async function record(spec) {
    setProblem("");
    setSaid("");
    setOpenKey(null);
    try {
      const done = await api.post("/api/adjustments", spec);
      setSaid(saidBack(done, spec));
      load();
    } catch (err) {
      setProblem(describe(err));
    }
  }

  const shift = (weeks) => {
    const next = new Date(parse(monday).getTime() + weeks * 7 * DAY_MS + 3600000);
    setMonday(iso(mondayOf(next)));
    setChosen(iso(mondayOf(next)));
  };

  if (problem && !data) return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  if (!data) return html`<div class="screen"><${Skeleton} rows=${4} /></div>`;

  const thisWeek = monday === iso(mondayOf(new Date()));

  return html`<div class="screen timetable">
    <header class="tt-header">
      <${ScreenHeader} title="Timetable" />
      <div class="tt-nav">
        <button class="button quiet-button" type="button" onClick=${() => shift(-1)} aria-label="The week before">
          <${Icon} name="back" />
        </button>
        <span class="t-lead tt-range">${rangeLabel(monday, sunday)}</span>
        <button class="button quiet-button" type="button" onClick=${() => shift(1)} aria-label="The week after">
          <${Icon} name="chevron" />
        </button>
        ${thisWeek
          ? null
          : html`<button class="button secondary-button tt-this-week" type="button" onClick=${() => {
              setMonday(iso(mondayOf(new Date())));
              setChosen(today);
            }}>This week</button>`}
      </div>
    </header>

    ${said ? html`<p class="notice tt-said" role="status">${said}</p>` : null}
    ${problem ? html`<${Problem}>${problem}</${Problem}>` : null}

    ${wide
      ? html`<${WeekGrid}
          days=${data.days}
          today=${today}
          openKey=${openKey}
          setOpenKey=${setOpenKey}
          onRecord=${record}
        />`
      : html`<${DayList}
          days=${data.days}
          today=${today}
          chosen=${chosen}
          setChosen=${setChosen}
          openKey=${openKey}
          setOpenKey=${setOpenKey}
          onRecord=${record}
        />`}

    ${data.orphans.length
      ? html`
          <!-- Always printed: an adjustment that names nothing the file still
               contains is never applied and never silently dropped. -->
          <section class="section">
            <h2 class="section-title">Changes that no longer match a session</h2>
            <ul class="list">
              ${data.orphans.map(
                (orphan) => html`<li key=${orphan.id} class="card">
                  <div class="card-body">
                    <${Chip} name=${orphan.subject} />
                    <span class="t-body">${`${orphan.applies_on}, ${orphan.session_start}`}</span>
                    <span class="t-meta">${orphan.why}</span>
                  </div>
                </li>`
              )}
            </ul>
            <p class="t-meta">Repointed only with <code>agent adjust --repoint</code>.</p>
          </section>
        `
      : null}

    <p class="t-meta tt-foot">
      Changes here are notes about one date. timetable.yaml is never written by this app.
    </p>
  </div>`;
}
