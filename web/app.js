// The entry point: work out whether there is a session, then render.
//
// Slice 0 renders a scaffold check rather than a blank page, because a scaffold
// that renders nothing cannot be told apart from a broken one. Slice 1 replaces
// `Scaffold` with the default screen.

import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { GENERATED_AT, ROUTES, SUBJECT_STATES } from "/contract.js";
import { api, haveSession, Offline, signIn, signOut } from "/api.js";

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
          spellcheck="false"
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

function Scaffold({ onSignOut }) {
  const [status, setStatus] = useState(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    api
      .get("/api/status")
      .then(setStatus)
      .catch((err) => setProblem(err.message));
  }, []);

  return html`
    <main class="pad">
      <h1>scaffold</h1>
      <p class="quiet">
        Slice 0. The default screen lands in slice 1; this exists so a working
        scaffold can be told apart from a broken one.
      </p>
      <ul class="checks">
        <li>modules resolved — Preact, hooks and htm loaded</li>
        <li>contract loaded — ${Object.keys(ROUTES).length} routes,
          ${SUBJECT_STATES.length} subject states</li>
        <li>generated ${GENERATED_AT}</li>
        <li>
          session live —
          ${status
            ? `schema v${status.schema_version}, ${
                status.sessions_live
              } session(s)`
            : problem || "…"}
        </li>
        <li>
          telegram deep link —
          ${status
            ? status.telegram_bot_username
              ? `t.me/${status.telegram_bot_username}`
              : "not configured; the primary action will be hidden"
            : "…"}
        </li>
      </ul>
      <button class="plain" onClick=${onSignOut}>Sign out</button>
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
  return html`<${Scaffold}
    onSignOut=${async () => {
      await signOut();
      setSignedIn(false);
    }}
  />`;
}

render(html`<${App} />`, document.getElementById("app"));
