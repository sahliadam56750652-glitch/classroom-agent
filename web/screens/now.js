// The default screen. DESIGN.md sections 2 and 4.
//
// Two states, and the app is in the second one most of the time:
//
//   Nothing waiting -- one line, then get out of the way.
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
import { api, Offline } from "/api.js";
import { plural, relativeDay, sessionName } from "/format.js";

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

/** Nothing waiting. One line, then nothing. */
function Clear({ body }) {
  const session = body.next_session;
  return html`
    <main class="pad now">
      <p class="clear-line">Nothing waiting.</p>
      ${session
        ? html`<p class="quiet">
            ${`Next session: ${sessionName(session)}, ${relativeDay(
              body.for_date
            )} ${session.start}.`}
          </p>`
        : /*
           * No session to name. Said, rather than left as an unexplained blank:
           * a holiday, a date no timetable version covers and a day whose
           * sessions were all moved away are three different facts, and the
           * server already distinguishes them.
           */
          html`<p class="quiet">${body.silent_because || "Nothing scheduled."}</p>`}
    </main>
  `;
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

/** The state to design first, because it is the one the app is in most often. */
function Waiting({ body, botUsername }) {
  const { item, subject, windows } = body;
  const window = windows && windows.length ? windows[0] : null;
  const pages = item.pages || 0;

  return html`
    <main class="pad now">
      <!-- The deficit. One subordinate line, above the item, and nowhere else. -->
      <p class="deficit">
        ${subject.name} · ${plural(subject.unreviewed, "unreviewed", "unreviewed")}
      </p>

      <h1 class="item-title">${item.label}</h1>

      <p class="scope">
        ${pages ? html`${plural(pages, "page")}` : null}
        ${window && pages
          ? html`<span class="sep"> · </span>${`an evening is about ${window.label}`}`
          : null}
      </p>

      <${PageRuler} windows=${windows} first=${window ? window.index : 0} />

      ${item.unread ? html`<p class="not-read">${notRead(item, windows)}</p>` : null}

      <${Actions} item=${item} botUsername=${botUsername} />
    </main>
  `;
}

/**
 * One door, and it says what is behind it.
 *
 * DESIGN.md section 4 wants Read and Skip side by side at equal weight, with Skip
 * stating what it records. Neither is possible here: 5b is read-only over
 * study_items, so the taps that change state live in Telegram until 5d.
 *
 * Two buttons that both open the same conversation would be a lie of a different
 * kind -- two doors that are one door. So there is one, and the line under it
 * names both actions and where they are. The honesty invariant survives intact
 * for the reason that matters: the honest path and the flattering one cost
 * exactly the same single tap, which is the property section 4 is protecting.
 */
function Actions({ item, botUsername }) {
  if (!botUsername) {
    return html`
      <p class="problem">
        Set <code>telegram.bot_username</code> in <code>config.yaml</code> to
        reach Read and Skip from here. Until then they are in the Telegram
        conversation.
      </p>
    `;
  }
  return html`
    <div class="actions">
      <a class="primary" href=${`https://t.me/${botUsername}`} rel="noopener">
        Open in Telegram
      </a>
      <p class="quiet actions-note">
        Read and Skip are both there, at the same weight. They move back here
        when the app can write them itself.
      </p>
    </div>
  `;
}

export function Now({ status }) {
  const [body, setBody] = useState(null);
  const [problem, setProblem] = useState("");
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    let live = true;
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
        else setProblem(err.detail || err.message);
      });
    return () => {
      live = false;
    };
  }, []);

  if (problem) {
    return html`<main class="pad"><p class="problem">${problem}</p></main>`;
  }
  if (offline) {
    return html`<main class="pad">
      <p class="problem">No connection, so this is not tonight's answer yet.</p>
    </main>`;
  }
  if (!body) {
    // No spinner. At 23:00 on mobile data the gap is real, and a spinner in it
    // is the app performing busyness before it knows anything.
    return html`<main class="pad"><p class="faint">…</p></main>`;
  }

  return body.waiting
    ? html`<${Waiting}
        body=${body}
        botUsername=${status && status.telegram_bot_username}
      />`
    : html`<${Clear} body=${body} />`;
}
