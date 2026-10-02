// The default screen. DESIGN.md sections 2 and 4.
//
// Two states, and the app is in the second one most of the time:
//
//   Nothing to review -- one line, then get out of the way.
//   23:00, nothing done, a lecture at 08:30 -- identical in tone, colour and
//   layout to any other state. The app has no information at 23:00 that it
//   lacked at 19:00, so behaving differently would be theatre.
//
// What is deliberately NOT here: any count that is not on section 2's list. The
// deficit is one subordinate line above the item and nowhere else, and the
// subject's age, its ready count and its unread page total all belong on the
// subject screen. A deficit with no affordance attached is not information.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { Offline, api, describe } from "/api.js";
import { localTime, plural, relativeDay, sessionName, shortDate } from "/format.js";
import { linkProps } from "/router.js";
import { Icon } from "/icons.js";
import { Card, Chip, Empty, Problem, Skeleton, useSubjectNames } from "/ui.js";
import { subjectStyle } from "/subject-color.js";
import { greeting, longDate } from "/greeting.js";
import { QuizEntry } from "/screens/quiz.js";

/**
 * The shape of the document, with tonight's range marked.
 *
 * Section 3 permits a bar for one item -- `window 3 of 6` inside a 92-page
 * chapter is a finite thing that finishes tonight. But it must not be a PROGRESS
 * bar, and that is not a stylistic preference: nothing records which windows have
 * been read, because window-scoped state is 3d stage 2 and is not built. A filled
 * bar would claim windows 1 and 2 were done.
 *
 * So this draws the document, not my progress through it. Every window is a tick;
 * the one an evening would start with is marked; a window whose pages are not all
 * transcribed is hollow, because that is a fact about the material. Nothing here
 * says anything about me.
 *
 * `unread_located` false means OCR has never run over the file, so which pages are
 * images is known only in aggregate. Then no tick is drawn hollow -- drawing them
 * all solid would say "every window is complete" when the truth is "we do not know
 * which ones are not", and those are different things. The caller says so in words
 * instead.
 */
function PageRuler({ windows, first }) {
  if (!windows || windows.length < 2) return null;
  const total = windows.reduce((sum, w) => sum + w.pages, 0) || 1;
  const located = windows.every((w) => w.unread_located);

  return html`
    <div
      class="ruler"
      role="img"
      aria-label=${`${windows.length} evening-sized windows across ${total} pages`}
    >
      ${windows.map(
        (w) => html`
          <span
            class=${[
              "tick",
              w.index === first ? "tick-now" : "",
              located && w.unread ? "tick-unread" : "",
            ]
              .filter(Boolean)
              .join(" ")}
            style=${`flex-grow:${w.pages}`}
            title=${`${w.label}${w.title ? ` — ${w.title}` : ""}${
              w.unread ? ` · ${w.unread} not transcribed` : ""
            }`}
          ></span>
        `
      )}
    </div>
  `;
}

/**
 * Nothing to review. One line, then nothing -- the empty state, not the hero,
 * because there is no action to make large.
 *
 * "Nothing to review" rather than "Nothing waiting": Today also lists what is
 * due, and "nothing waiting" above a homework due on Thursday said two
 * contradictory things. What is empty is the reading queue.
 */
function Clear({ body }) {
  const session = body.next_session;
  return html`<${Empty} title="Nothing to review.">
    ${session
      ? `Next session: ${sessionName(session)}, ${relativeDay(body.for_date)} ${session.start}.`
      : /*
         * No session to name. Said, rather than left as an unexplained blank: a
         * holiday, a date no timetable version covers and a day whose sessions
         * were all moved away are three different facts, and the server already
         * distinguishes them.
         */
        capitalised(body.silent_because || "Nothing scheduled.")}
  </${Empty}>`;
}

function capitalised(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

/**
 * What the app has not read, said out loud before any button.
 *
 * Quizzing on a lecture nothing has read is the failure the gate exists to
 * prevent, and a button that will apologise is worse than no button.
 *
 * Built as a plain string rather than as markup, for two reasons found the hard
 * way. htm drops the whitespace between adjacent `${}` on separate lines, which
 * rendered "12 pagesare not transcribed". And a comment CANNOT live inside an htm
 * template if it contains a dollar-brace, because that is live interpolation
 * syntax inside a template literal, not text -- an empty one is a SyntaxError
 * that takes the whole module down and leaves the page blank.
 *
 * The second sentence only appears when the unread pages have no known position:
 * "no window is short" and "we do not know which windows are short" are different
 * facts, and the ruler cannot draw the second one.
 */
function notRead(item, windows) {
  const count = `${item.unread} page${item.unread === 1 ? "" : "s"}`;
  const verb = item.unread === 1 ? "is" : "are";
  let text =
    `I have not read all of this lecture yet — ${count} ${verb} not ` +
    "transcribed. This counts as read, not verified.";
  const located =
    !windows || windows.length < 2 || windows.every((w) => w.unread_located);
  if (!located) {
    text +=
      " Which pages is not known yet, so the marks above do not show where" +
      " they fall.";
  }
  return text;
}

/**
 * What tomorrow's session is, said once inside the hero: the reason these
 * pages rather than others. A plain sentence -- no countdown to it.
 */
function forSession(subject, forDate) {
  const session = (subject.sessions || [])[0];
  if (!session) return "";
  const where = session.parts && session.parts[0] && session.parts[0].room;
  return (
    `For the ${sessionName(session)}, ${relativeDay(forDate)} ${session.start}` +
    (where ? `, ${where}.` : ".")
  );
}

/**
 * The state to design first, because it is the one the app is in most often.
 *
 * One hero card: the deficit as its first, smallest line (section 2 -- above the
 * item and nowhere else), the title as the largest thing on the screen, the
 * window, the shape of the document, what is not readable, and the two doors.
 * It looks identical at 23:00 and at 14:00, because the app knows nothing at
 * 23:00 that it did not know at 14:00.
 */
function Waiting({ body, onNext }) {
  const { item, subject, windows } = body;
  const window = windows && windows.length ? windows[0] : null;
  const pages = item.pages || 0;
  const why = forSession(subject, body.for_date);
  // Each part kept whole on its own line, so "pages 1-20" never breaks at the
  // hyphen on a phone.
  const scope = [
    pages ? plural(pages, "page") : "",
    window && pages ? `an evening is about ${window.label}` : "",
  ].filter(Boolean);

  return html`
        <article
          class="hero has-subject now-hero"
          style=${subjectStyle(subject.name)}
          aria-labelledby="now-title"
        >
          <!-- The deficit. One subordinate line, above the item, and nowhere
               else. The separator is for a screen reader, which reads the line
               the way DESIGN.md writes it: "Database · 6 unreviewed". -->
          <p class="t-state deficit">
            <${Chip} name=${subject.name} /><span class="visually-hidden"> · </span>
            <span>${plural(subject.unreviewed, "unreviewed", "unreviewed")}</span>
          </p>

          <div class="now-what">
            <h1 class="t-hero item-title" id="now-title">${item.label}</h1>
            ${scope.length
              ? html`<p class="t-state">
                  ${scope.map(
                    (part, n) =>
                      html`<span key=${n} class="nowrap">${part}${
                        n < scope.length - 1 ? " ·" : ""
                      }</span>${" "}`
                  )}
                </p>`
              : null}
          </div>

          <${PageRuler} windows=${windows} first=${window ? window.index : 0} />

          ${why ? html`<p class="t-meta now-why">${why}</p>` : null}

          ${item.unread
            ? html`<p class="notice not-read">${notRead(item, windows)}</p>`
            : null}

          <${Actions}
            key=${item.item_id}
            item=${item}
            files=${body.files}
            windows=${windows}
            onNext=${onNext}
          />
        </article>
  `;
}

/** Today's date, locally, as the timetable names a day. */
function localToday() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

// How far ahead "due soon" looks. Four days: far enough to see the week's
// shape on a Thursday, near enough that the list stays short. An instant on
// each, never a countdown -- format.js has no timeUntil and must not.
const DUE_DAYS = 4;

/**
 * Today's schedule, as a timeline: each session a block in its subject's
 * colour against its time. A fact about the day, not about me -- no counts, no
 * standing (section 2 keeps the whole picture off the front door, and this is
 * not it). A session that moved or was cancelled stays, crossed out, with why.
 */
function TodaySessions() {
  const [day, setDay] = useState(null);

  useEffect(() => {
    let live = true;
    const today = localToday();
    api
      .get(`/api/timetable?from=${today}&to=${today}`)
      .then((found) => live && setDay((found.days || [])[0] || null))
      .catch(() => {
        // Context, not the answer. Without it the hero still stands alone.
      });
    return () => {
      live = false;
    };
  }, []);

  if (!day) return null;
  const items = [
    ...day.sessions.map((session) => [session, false]),
    ...day.departed.map((session) => [session, true]),
  ].sort((a, b) => (a[0].start < b[0].start ? -1 : a[0].start > b[0].start ? 1 : 0));

  return html`
    <section class="section today-part" aria-labelledby="today-sessions">
      <h2 class="section-title" id="today-sessions">Today's schedule</h2>
      ${items.length
        ? html`<ol class="timeline">
            ${items.map(([session, departed]) => html`<${Slot}
              key=${`${session.start}-${sessionName(session)}-${departed}`}
              session=${session}
              departed=${departed}
            />`)}
          </ol>`
        : html`<p class="t-state">${capitalised(day.exception || "No sessions today.")}</p>`}
    </section>
  `;
}

/** One session on the timeline. */
function Slot({ session, departed }) {
  const parts = session.parts || [];
  const names = parts.map((part) => part.subject);
  const where = [capitalised(kindOf(session)), parts[0] && parts[0].room].filter(Boolean).join(" · ");
  return html`<li class=${`slot ${departed ? "departed" : ""}`} style=${subjectStyle(names[0])}>
    <span class="slot-time">
      <span class="time slot-start">${session.start}</span>
      <span class="time slot-end">${session.end}</span>
    </span>
    <span class="slot-block">
      <span class="slot-name">${names.join(" + ")}</span>
      ${where ? html`<span class="slot-where">${where}</span>` : null}
      ${session.note ? html`<span class="slot-where">${session.note}</span>` : null}
    </span>
  </li>`;
}

/**
 * What is due in the next few days, soonest first, as the instants they are.
 *
 * Only what has not passed: a passed deadline is the one red in this app and it
 * belongs on the Work screen, where it can be dealt with, not on the front door.
 */
function DueSoon() {
  const names = useSubjectNames();
  const [rows, setRows] = useState(null);

  useEffect(() => {
    let live = true;
    api
      .get("/api/deadlines")
      .then((found) => live && setRows(found))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  if (!rows) return null;
  const now = Date.now();
  const until = now + DUE_DAYS * 86400000;
  const soon = rows.filter((row) => {
    const at = row.due_at ? Date.parse(row.due_at) : NaN;
    return !row.done && at >= now && at <= until;
  });
  if (!soon.length) return null;

  return html`
    <section class="section today-part" aria-labelledby="today-due">
      <h2 class="section-title" id="today-due">Due this week</h2>
      <ul class="list">
        ${soon.map((row) => {
          const subject = names[row.course_id] || "";
          return html`<li key=${`${row.entity_type}-${row.entity_id}`}>
            <${Card} to="/work" subject=${subject || null}>
              <${Chip} name=${subject} />
              <span class="t-body due-title">${row.title}</span>
              <span class="t-meta"><span class="time">${dueAt(row.due_at)}</span>${row.label ? `, ${row.label}` : ""}</span>
            </${Card}>
          </li>`;
        })}
      </ul>
    </section>
  `;
}

/** "Sun 4 Oct, 22:59" -- the instant, in the display face. Never a countdown. */
function dueAt(stamp) {
  const at = new Date(stamp);
  const day = at.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  const clock = at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${day}, ${clock}`;
}

/** The kind of session in words: "lecture", "lab". */
function kindOf(session) {
  const kind = (session.kind || "").toLowerCase();
  return { lec: "lecture", tut: "tutorial", lab: "lab" }[kind] || kind;
}

/**
 * Three doors, and the two that record something are the same size.
 *
 * Opening the material comes first and largest, because it is the thing the app
 * can do for me right now. Then the two answers DESIGN.md section 4 asks for, side
 * by side at equal weight: I read it, or I am skipping it. Skip is ONE tap and is
 * logged on that tap -- "a second tap to be honest is a tax on honesty" -- so the
 * optional reason is offered BEFORE it, folded away, never as a confirmation after.
 *
 * Both go through the server's gate/actions.py, the functions the bot's buttons
 * call. Neither can reach `verified`; only a passed quiz can, and the quiz door
 * appears once the lecture is marked read.
 *
 * `files` comes from `/api/now` rather than a second request, because this is
 * the screen where a round trip is felt.
 */
function Actions({ item, files, windows, onNext }) {
  const readable = (files || []).find((file) => file.readable);
  const window = windows && windows.length ? windows[0] : null;
  const [done, setDone] = useState(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");

  // Tonight's document, kept for the radio being off. One file, a few MB, and
  // precisely the one I will want at 23:00 -- which is what DESIGN.md section 6
  // means by "what has been delivered". Nothing else is cached automatically.
  useEffect(() => {
    if (!readable || !navigator.serviceWorker?.controller) return;
    navigator.serviceWorker.controller.postMessage({
      type: "keep",
      url: `/api/documents/${encodeURIComponent(readable.drive_id)}/file`,
    });
  }, [readable && readable.drive_id]);

  async function record(verb, body) {
    setBusy(true);
    setProblem("");
    try {
      await api.post(`/api/study-items/${item.item_id}/${verb}`, body);
      setDone(verb);
    } catch (err) {
      setProblem(describe(err));
    }
    setBusy(false);
  }

  const open = readable
    ? html`<a
        class="button primary-button now-open"
        ...${linkProps(`/read/${encodeURIComponent(readable.drive_id)}`)}
      >
        <${Icon} name="document" />
        ${window ? `Open ${window.label}` : "Open it"}
      </a>`
    : html`<p class="notice">
        Nothing readable is held for this post yet. ${" "}
        <code>agent fetch</code> and <code>agent extract</code> bring it here.
      </p>`;

  if (done) {
    return html`<div class="now-done" role="status">
      <p class="t-lead now-done-line">
        <${Icon} name=${done === "read" ? "check" : "skip"} />
        ${done === "read" ? "Marked as read." : "Logged as skipped."}
      </p>
      ${done === "skip"
        ? html`<p class="t-meta">
            ${reason.trim()
              ? `Recorded with your reason: ${reason.trim()}`
              : "Recorded as skipped in the web app. It is never counted as verified."}
          </p>`
        : html`<${QuizEntry} itemId=${item.item_id} />`}
      <div class="hero-actions">
        ${done === "read" ? open : null}
        <button class="button quiet-button" type="button" onClick=${onNext}>
          Show the next one
        </button>
      </div>
    </div>`;
  }

  return html`
    <div class="now-actions">
      ${open}
      <div class="now-answers">
        <button
          class="button secondary-button"
          type="button"
          disabled=${busy}
          onClick=${() => record("read")}
        >
          <${Icon} name="check" />
          I've read it
        </button>
        <button
          class="button secondary-button"
          type="button"
          disabled=${busy}
          onClick=${() => record("skip", { reason })}
        >
          <${Icon} name="skip" />
          Skip — logged
        </button>
      </div>
      ${asking
        ? html`<label class="field now-reason">
            <span>Why skip it? <span class="hint">Optional, kept with the record.</span></span>
            <input
              type="text"
              maxlength="280"
              value=${reason}
              onInput=${(e) => setReason(e.target.value)}
            />
          </label>`
        : html`<button class="button quiet-button now-reason-toggle" type="button" onClick=${() => setAsking(true)}>
            Add a reason before skipping
          </button>`}
      ${problem ? html`<p class="problem" role="alert">${problem}</p>` : null}
    </div>
  `;
}

export function Now({ status }) {
  const [body, setBody] = useState(null);
  const [problem, setProblem] = useState("");
  const [offline, setOffline] = useState(false);

  const [round, setRound] = useState(0);

  useEffect(() => {
    let live = true;
    setBody(null);
    // `?date=` is passed straight through. DESIGN.md's test of this screen is
    // answerable by looking, and the empty state cannot be looked at on a day
    // that has material -- so a quiet date makes the rarer state inspectable.
    const asked = new URLSearchParams(location.search).get("date");
    api
      .get(asked ? `/api/now?date=${encodeURIComponent(asked)}` : "/api/now")
      .then((found) => live && setBody(found))
      .catch((err) => {
        if (!live) return;
        if (err instanceof Offline) setOffline(true);
        else setProblem(describe(err));
      });
    return () => {
      live = false;
    };
  }, [round]);

  if (problem) {
    return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  }
  if (offline) {
    return html`<div class="screen">
      <${Problem}>No connection, so this is not tonight's answer yet.</${Problem}>
    </div>`;
  }
  if (!body) {
    // No spinner. At 23:00 on mobile data the gap is real, and a spinner in it
    // is the app performing busyness before it knows anything. The shape of
    // the answer instead, and still.
    return html`<div class="screen"><${Skeleton} hero=${true} rows=${0} /></div>`;
  }

  const asOf = body.__cachedAt;

  // The one next action first and largest; then today's sessions, then what is
  // due soon. Beside the hero at 1024px and above, below it on a phone.
  return html`
    ${asOf
      ? html`<p class="notice now-stale" role="status">
          ${`No connection. This is as of ${new Date(asOf).toLocaleTimeString(
            undefined,
            { hour: "2-digit", minute: "2-digit" }
          )}.`}
        </p>`
      : null}
    <div class="screen today">
      <header class="today-head">
        <p class="t-greeting">${longDate(new Date())}</p>
        <p class="t-title today-greeting">${greeting(new Date())}</p>
      </header>
      <div class="today-split">
        <div class="today-main">
          ${body.waiting
            ? html`<${Waiting} body=${body} onNext=${() => setRound((n) => n + 1)} />`
            : html`<${Clear} body=${body} />`}
        </div>
        <div class="today-aside">
          <${TodaySessions} />
          <${DueSoon} />
        </div>
      </div>
    </div>
  `;
}
