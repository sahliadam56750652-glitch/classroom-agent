// The entry point: work out whether there is a session, then render.
//
// "Is there a session" is only answerable by asking for something that needs one.
// The cookie is HttpOnly, which is the point -- there is nothing here to inspect,
// so a 401 is the answer and the sign-in screen is the response.

import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import {
  api,
  describe,
  haveSession,
  NotSignedIn,
  Offline,
  onSignedOut,
  signIn,
  signOut,
} from "/api.js";
import { Now } from "/screens/now.js";
import { Reader } from "/reader/reader.js";
import { Subject, Subjects } from "/screens/subjects.js";
import { Status } from "/screens/status.js";
import { Library } from "/screens/library.js";
import { Timetable } from "/screens/timetable.js";
import { ProjectPage, Work } from "/screens/work.js";
import { Add } from "/screens/add.js";
import { More, SECONDARY } from "/screens/more.js";
import { PastAttempt, Quiz, Quizzes } from "/screens/quiz.js";
import { Icon } from "/icons.js";
import { Brand } from "/ui.js";
import { linkProps, match, navigate, useRoute } from "/router.js";
import { loadSnapshot, startSyncing, useSyncState } from "/sync.js";
import { syncedLine } from "/synced.js";

/**
 * The token, once every 90 days.
 *
 * `spellcheck=${false}` rather than `spellcheck="false"`: Preact sets a boolean
 * attribute from truthiness and the STRING "false" is truthy, so the quoted form
 * turns spellcheck ON -- which then underlines a random 43-character secret. Found
 * by rendering the page, not by reading it.
 */
function SignIn({ onDone, onLocal, hasLocal }) {
  const [token, setToken] = useState("");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setProblem("");
    try {
      await signIn(token.trim());
    } catch (err) {
      // A wrong token is a 401 from the exchange itself, and says so in its
      // detail; everything else goes through describe() like any screen.
      setProblem(
        err instanceof NotSignedIn
          ? "That token is not the one in .env."
          : err instanceof Offline
          ? "No connection, so there is nothing to sign in to yet."
          : describe(err)
      );
      setBusy(false);
      return;
    }

    // The exchange said yes. Whether the BROWSER kept the cookie is a separate
    // question, and the only way to ask it is to make a call that needs one.
    // If that is a 401, the POST worked and the cookie was thrown away -- and
    // the common reason has a name, so the screen uses it.
    const now = await haveSession().catch(() => "unreachable");
    setBusy(false);
    if (now === "in") {
      onDone();
      return;
    }
    if (now === "out") {
      setProblem(
        "Signed in, but this browser did not keep the session cookie, so every " +
          "request after this one is refused. This usually means " +
          "api.secure_cookie is true while the app is served over plain http to " +
          "something other than localhost — a Secure cookie is only accepted " +
          "over http for localhost. Set api.secure_cookie: false in config.yaml " +
          "for a phone on this network, or serve the app over HTTPS."
      );
      return;
    }
    setProblem("Signed in, then the server stopped answering. Try again in a moment.");
  }

  return html`
    <main class="signin">
      <div class="signin-card">
        <${Brand} />
        <p class="t-state">
          Paste <code>WEB_API_TOKEN</code> from <code>.env</code>. Once every 90
          days.
        </p>
        <form onSubmit=${submit}>
          <label class="field">
            <span>Token</span>
            <input
              type="password"
              value=${token}
              autocomplete="current-password"
              spellcheck=${false}
              onInput=${(e) => setToken(e.target.value)}
            />
          </label>
          <button class="button primary-button" type="submit" disabled=${busy || !token.trim()}>
            ${busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        ${problem && html`<p class="problem" role="alert">${problem}</p>`}
        ${hasLocal
          ? html`<p class="t-meta signin-local">
              Everything synced to this phone is still here.${" "}
              <button class="button quiet-button" type="button" onClick=${onLocal}>
                Read what's on this phone
              </button>
            </p>`
          : null}
      </div>
    </main>
  `;
}

/**
 * Who is signed in -- and, since Phase 5e, whether that even matters right now.
 *
 * The phone holds a snapshot of everything (sync.js), so the shell renders from
 * it at once, without waiting for the PC. The server is asked in the background:
 *
 *   "in"     the PC answered, or the PC is not reachable but the phone has a
 *            snapshot to show -- being offline is not being signed out
 *   "out"    the PC answered 401: the sign-in screen, with no nav
 *   "local"  signed out, and reading what is on the phone anyway; nothing syncs
 *
 * A 401 from ANY request lands here through `onSignedOut`. It never touches the
 * snapshot or the documents. Neither does Sign out: it ends the session on the
 * PC, and the library stays on the phone. tests/test_web_render.py holds both.
 */
function App() {
  const [state, setState] = useState("checking");
  const [hasLocal, setHasLocal] = useState(false);
  const route = useRoute();

  useEffect(() => {
    let live = true;
    loadSnapshot().then((found) => {
      if (!live) return;
      setHasLocal(Boolean(found));
      if (found) setState((now) => (now === "checking" ? "in" : now));
    });
    haveSession()
      .then((answer) => {
        if (!live) return;
        setState(answer === "out" ? "out" : "in");
      })
      .catch(() => live && setState("in"));
    const stop = onSignedOut(() => {
      setState((now) => (now === "local" ? "local" : "out"));
      loadSnapshot().then((found) => setHasLocal(Boolean(found)));
    });
    return () => {
      live = false;
      stop();
    };
  }, []);

  // Syncing starts once there is a shell to sync for, and only when signed in.
  useEffect(() => {
    if (state === "in") startSyncing();
  }, [state]);

  // Keep the URL honest about the state: signed out is /signin, and /signin
  // is not a place a signed-in person should be left standing.
  useEffect(() => {
    if (state === "out" && route.path !== "/signin") {
      navigate("/signin", { replace: true });
    }
    if ((state === "in" || state === "local") && route.path === "/signin") {
      navigate("/", { replace: true });
    }
  }, [state, route.path]);

  if (state === "checking") {
    return html`<main class="booting" aria-busy="true"><p class="t-meta">…</p></main>`;
  }
  if (state === "out") {
    return html`<${SignIn}
      hasLocal=${hasLocal}
      onDone=${() => setState("in")}
      onLocal=${() => setState("local")}
    />`;
  }
  return html`<${Shell}
    local=${state === "local"}
    onSignIn=${() => setState("out")}
    onSignOut=${async () => {
      // The session ends on the PC. The snapshot and the documents stay on the
      // phone: losing the library by signing out must be impossible.
      await signOut().catch(() => {});
      setState("out");
    }}
  />`;
}

/** The one line every screen carries: when its data was synced. */
function SyncedLine({ local, onSignIn }) {
  const sync = useSyncState();
  if (local) {
    return html`<p class="synced-line" role="status">
      ${syncedLine(sync.syncedAt, false, false)}. Signed out, so nothing syncs.${" "}
      <button class="button quiet-button" type="button" onClick=${onSignIn}>Sign in</button>
    </p>`;
  }
  return html`<p class="synced-line" role="status">
    ${syncedLine(sync.syncedAt, sync.reachable, sync.syncing)}${sync.error ? ` · ${sync.error}` : ""}
  </p>`;
}

/**
 * Everything behind a live session -- or behind none, when reading what the
 * phone holds while signed out.
 */
function Shell({ onSignOut, onSignIn, local }) {
  const route = useRoute();
  const reading = match("/read/", route.path);

  // The reader is full-screen and owns the viewport, so it renders alone --
  // no nav, no chrome but its own. Section 6: everything else is navigation.
  if (reading) return html`<${Reader} driveId=${reading} />`;

  // The content first and the navigation after it, in the document as on the
  // screen: section 2 says the front door is one item, and everything else is
  // one tap behind it.
  return html`
    <div class="shell">
      <main class="shell-main" id="main">
        <${SyncedLine} local=${local} onSignIn=${onSignIn} />
        <${Screen} route=${route} onSignOut=${onSignOut} />
      </main>
      <${Sidebar} here=${route.path} onSignOut=${onSignOut} />
      <${TabBar} here=${route.path} />
    </div>
  `;
}

/** Which screen this path is. */
function Screen({ route, onSignOut }) {
  const [, screen] = pick(route, onSignOut);
  return screen;
}

function pick(route, onSignOut) {
  const path = route.path;
  // Study. /subjects/... is the old address of a subject and still works, so a
  // link saved before the redesign is not a dead end.
  const subject = match("/study/", path) || match("/subjects/", path);
  if (subject) return ["subject", html`<${Subject} name=${subject} />`];
  if (path === "/study" || path === "/subjects") return ["subjects", html`<${Subjects} />`];
  const past = match("/quiz/attempt/", path);
  if (past) return ["quiz", html`<${PastAttempt} attemptId=${past} />`];
  const quizItem = match("/quiz/", path);
  if (quizItem) return ["quiz", html`<${Quiz} itemId=${quizItem} />`];
  if (path === "/quizzes") return ["quizzes", html`<${Quizzes} />`];
  if (path === "/library")
    return ["library", html`<${Library} course=${route.query.get("course")} />`];
  // Work: homework and projects together. /projects and /deadlines are older
  // addresses of the same screen, kept so a saved link is not a dead end.
  const project = match("/projects/", path);
  if (project) return ["project", html`<${ProjectPage} id=${project} />`];
  if (path === "/work" || path === "/projects" || path === "/deadlines")
    return ["work", html`<${Work} />`];
  if (path === "/timetable") return ["timetable", html`<${Timetable} />`];
  if (path === "/add") return ["add", html`<${Add} />`];
  if (path === "/status") return ["status", html`<${Status} />`];
  if (path === "/more") return ["more", html`<${More} onSignOut=${onSignOut} />`];
  return ["now", html`<${Now} />`];
}

// The five places, DESIGN.md section 8. Four here and More, which the tab bar
// adds and the sidebar does not need.
const PLACES = [
  ["/", "Today", "now"],
  ["/study", "Study", "subjects"],
  ["/work", "Work", "projects"],
  ["/timetable", "Timetable", "timetable"],
];

// Each place's own parts, listed under it in the sidebar and as section links
// on the screen itself.
const PARTS = {
  "/study": [
    ["/quizzes", "Quizzes", "quiz"],
    ["/library", "Library", "library"],
  ],
};

/** Which of the five places a path belongs to, so a subject page lights Study. */
function placeOf(path) {
  if (
    path.startsWith("/study") ||
    path.startsWith("/subjects") ||
    path.startsWith("/quiz") ||
    path.startsWith("/library")
  )
    return "/study";
  if (path === "/work" || path.startsWith("/projects") || path === "/deadlines") return "/work";
  if (path === "/timetable") return "/timetable";
  if (path === "/") return "/";
  return "/more";
}

/** The exact destination a path is, for the sidebar's finer marking. */
function partOf(path) {
  if (path.startsWith("/quiz")) return "/quizzes";
  if (path.startsWith("/subjects") || path.startsWith("/study")) return "/study";
  if (path === "/deadlines") return "/work";
  return path;
}

/**
 * The tab bar, below 1024px. Five places and no counts: DESIGN.md section 7
 * forbids a badge counting what I have not done, and a number on a tab is
 * exactly that badge.
 */
function TabBar({ here }) {
  const place = placeOf(here);
  const tabs = [...PLACES, ["/more", "More", "more"]];
  return html`
    <nav class="tabbar" aria-label="Places">
      ${tabs.map(
        ([to, label, icon]) => html`<a
          key=${to}
          class="tab"
          aria-current=${place === to ? "page" : null}
          ...${linkProps(to)}
        >
          <${Icon} name=${icon} />
          <span>${label}</span>
        </a>`
      )}
    </nav>
  `;
}

/** The sidebar, at 1024px and above: every destination, and Sign out. */
function Sidebar({ here, onSignOut }) {
  const exact = partOf(here);
  const link = ([to, label, icon], sub = false) => html`<a
    key=${to}
    class=${`side-link ${sub ? "side-sub" : ""}`}
    aria-current=${exact === to ? "page" : null}
    ...${linkProps(to)}
  >
    <${Icon} name=${icon} />
    <span>${label}</span>
  </a>`;
  return html`
    <nav class="sidebar" aria-label="All places">
      <${Brand} />
      <div class="sidebar-group">
        ${PLACES.map(
          (place) => html`${link(place)}${(PARTS[place[0]] || []).map((part) => link(part, true))}`
        )}
      </div>
      <div class="sidebar-group">${SECONDARY.map((entry) => link(entry))}</div>
      <div class="sidebar-foot">
        <button class="side-link" type="button" onClick=${onSignOut}>
          <${Icon} name="signout" />
          <span>Sign out</span>
        </button>
      </div>
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

const root = document.getElementById("app");
// Cleared first. Preact's `render` APPENDS into a container holding children it
// did not create, so the loading line stayed on screen above the app -- visible
// only in a rendered DOM, which is why this was found by rendering it.
root.textContent = "";
render(html`<${App} />`, root);
