// The entry point: work out whether there is a session, then render.
//
// "Is there a session" is only answerable by asking for something that needs one.
// The cookie is HttpOnly, which is the point -- there is nothing here to inspect,
// so a 401 is the answer and the sign-in screen is the response.

import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, haveSession, Offline, signIn, signOut } from "/api.js";
import { Now } from "/screens/now.js";
import { Reader } from "/reader/reader.js";
import { Subject, Subjects } from "/screens/subjects.js";
import { Status } from "/screens/status.js";
import { Deadlines, Library } from "/screens/library.js";
import { Timetable } from "/screens/timetable.js";
import { linkProps, match, useRoute } from "/router.js";
import { flushWhenOnline } from "/queue.js";

/**
 * The token, once every 90 days.
 *
 * `spellcheck=${false}` rather than `spellcheck="false"`: Preact sets a boolean
 * attribute from truthiness and the STRING "false" is truthy, so the quoted form
 * turns spellcheck ON -- which then underlines a random 43-character secret. Found
 * by rendering the page, not by reading it.
 */
function SignIn({ onDone }) {
  const [token, setToken] = useState("");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setProblem("");
    try {
      await signIn(token.trim());
      onDone();
    } catch (err) {
      setProblem(
        err instanceof Offline
          ? "No connection, so there is nothing to sign in to yet."
          : err.detail || err.message
      );
    } finally {
      setBusy(false);
    }
  }

  return html`
    <main class="pad">
      <h1>classroom-agent</h1>
      <p class="quiet">
        Paste <code>WEB_API_TOKEN</code> from <code>.env</code>. Once every
        90 days.
      </p>
      <form onSubmit=${submit}>
        <input
          type="password"
          value=${token}
          autocomplete="current-password"
          spellcheck=${false}
          placeholder="token"
          onInput=${(e) => setToken(e.target.value)}
        />
        <button type="submit" disabled=${busy || !token.trim()}>
          ${busy ? "…" : "Sign in"}
        </button>
      </form>
      ${problem && html`<p class="problem">${problem}</p>`}
    </main>
  `;
}

function App() {
  // null = not yet known. Distinguished from false, so the app does not flash
  // the sign-in screen at someone who is already signed in.
  const [signedIn, setSignedIn] = useState(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    haveSession()
      .then(setSignedIn)
      .catch((err) => {
        if (err instanceof Offline) setOffline(true);
        setSignedIn(false);
      });
  }, []);

  if (signedIn === null) {
    return html`<main class="pad"><p class="quiet">…</p></main>`;
  }
  if (!signedIn) {
    return html`
      ${offline &&
      html`<p class="pad problem">No connection.</p>`}
      <${SignIn} onDone=${() => setSignedIn(true)} />
    `;
  }
  return html`<${Shell}
    onSignOut=${async () => {
      await signOut();
      setSignedIn(false);
    }}
  />`;
}


/**
 * Everything behind a live session.
 *
 * `/api/status` is fetched once here rather than per screen, because the one
 * thing the default screen needs from it -- the bot username, for the primary
 * action -- does not change between navigations, and asking again on every render
 * would be a request per screen for a value that is configuration.
 */
function Shell({ onSignOut }) {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    api.get("/api/status").then(setStatus).catch(() => setStatus({}));
  }, []);

  const route = useRoute();
  const reading = match("/read/", route.path);

  // The reader is full-screen and owns the viewport, so it renders alone --
  // no nav, no chrome but its own. Section 6: everything else is navigation.
  if (reading) return html`<${Reader} driveId=${reading} />`;

  return html`
    <${Screen} route=${route} status=${status} />
    <${Nav} here=${route.path} />
    <footer class="pad">
      <button class="plain" onClick=${onSignOut}>Sign out</button>
    </footer>
  `;
}

/** Which screen this path is. */
function Screen({ route, status }) {
  const path = route.path;
  const subject = match("/subjects/", path);
  if (subject) return html`<${Subject} name=${subject} />`;
  if (path === "/subjects") return html`<${Subjects} />`;
  if (path === "/library")
    return html`<${Library} course=${route.query.get("course")} />`;
  if (path === "/deadlines") return html`<${Deadlines} />`;
  if (path === "/timetable") return html`<${Timetable} />`;
  if (path === "/status") return html`<${Status} />`;
  return html`<${Now} status=${status} />`;
}

/**
 * The whole picture, one tap away and never the front door.
 *
 * A row at the END of the content rather than a fixed tab bar. Section 2 is
 * explicit that the default screen is one item and that everything else is
 * behind it -- a bar pinned over the bottom of the screen would put four other
 * destinations in front of the one thing I opened the app to do.
 */
function Nav({ here }) {
  const places = [
    ["/", "Now"],
    ["/subjects", "Subjects"],
    ["/library", "Library"],
    ["/deadlines", "Deadlines"],
    ["/timetable", "Timetable"],
    ["/status", "Counted"],
  ];
  return html`
    <nav class="nav pad">
      ${places.map(
        ([to, label]) =>
          html`<a
            key=${to}
            class=${here === to ? "nav-here" : ""}
            aria-current=${here === to ? "page" : null}
            ...${linkProps(to)}
          >
            ${label}
          </a>`
      )}
    </nav>
  `;
}

// The service worker, registered after load so it never competes with the first
// paint. Failure is not reported: without it the app simply needs a connection,
// which is a smaller problem than a message about it on the screen that matters.
if ("serviceWorker" in navigator) {
  addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js")
      .catch((err) => console.warn("[app] no service worker:", err));
  });
}

// Positions saved while offline go out when the connection returns.
flushWhenOnline(api);

const root = document.getElementById("app");
// Cleared first. Preact's `render` APPENDS into a container holding children it
// did not create, so the loading line stayed on screen above the app -- visible
// only in a rendered DOM, which is why this was found by rendering it.
root.textContent = "";
render(html`<${App} />`, root);
