// Every request to the API goes through here.
//
// Three things this centralises, each because scattering it would eventually get
// one caller wrong:
//
//   `credentials: "same-origin"` on every request, so the HttpOnly session cookie
//   rides along. The client can never read that cookie -- which is the point -- so
//   "am I signed in" is only ever answered by a 401.
//
//   A 401 is not an error to display. It means the session went away, and the only
//   correct response is the sign-in screen. Handled once, here.
//
//   Every response is passed through `contract.expect`, so a field the server
//   stopped sending is a named console complaint rather than a blank line.

import { expect } from "/contract.js";

export class ApiError extends Error {
  constructor(status, detail, route) {
    // A sentence, never "GET /api/x failed with 500". That string used to be
    // this default and it reached the screen whenever a response had no detail;
    // the route stays on the object for the console, where it is useful.
    super(detail || "The server refused that request.");
    this.status = status;
    this.detail = detail;
    this.route = route;
  }
}

/**
 * Thrown on a 401, and never shown. A 401 anywhere means signed out, so the
 * request that hit it also raises the app-wide signal below; the screen that
 * made the call does not get to decide what a missing session looks like.
 */
export class NotSignedIn extends ApiError {}

/**
 * Thrown when the network is unreachable -- distinct from an API that answered
 * with an error, because the two want different screens: one says "no connection"
 * and the other says what went wrong. Collapsing them is how "offline" ends up
 * displayed as "something went wrong".
 */
export class Offline extends Error {
  constructor(route) {
    super(`offline: ${route}`);
    this.route = route;
  }
}

// Anything that wants to know the session went away. The app listens once and
// routes to /signin; no screen handles a 401 itself.
const signedOutListeners = new Set();

/** Subscribe to "the server says there is no session". Returns an unsubscribe. */
export function onSignedOut(listener) {
  signedOutListeners.add(listener);
  return () => signedOutListeners.delete(listener);
}

async function request(method, path, { body, form, signal, probe } = {}) {
  const route = `${method} ${templateOf(path)}`;
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  // Tells the service worker this request must reach the server or fail. A
  // cached 200 for /api/status answers "was I signed in last time", which is
  // not the question -- and treating it as the answer is how the shell rendered
  // for a session that no longer existed.
  if (probe) headers["X-Agent-Probe"] = "1";

  let response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      signal,
      headers,
      body: form ? form : body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new Offline(route);
  }

  if (response.status === 401) {
    // The sign-in exchange itself answers 401 for a wrong token, and that is a
    // message for the form, not a sign-out.
    if (route !== "POST /api/session") {
      for (const listener of signedOutListeners) listener();
    }
    throw new NotSignedIn(401, await detailOf(response), route);
  }

  if (!response.ok) {
    throw new ApiError(response.status, await detailOf(response), route);
  }
  if (response.status === 204) return null;

  const type = response.headers.get("content-type") || "";
  if (!type.includes("json")) return response;

  // `parsed`, not `body` -- this function already has a `body` parameter, and
  // shadowing it is a SyntaxError that blanks the whole app.
  const parsed = expect(route, await response.json());

  // The service worker stamps a cached fallback. Carried onto the parsed value
  // so a screen can say "as of 21:40" rather than presenting old figures as
  // current -- section 7 forbids the app inventing a state, and stale data shown
  // as fresh is exactly that. Non-enumerable, so it never reaches a form or a
  // request built from this object.
  const cachedAt = response.headers.get("X-Agent-Cached-At");
  if (cachedAt && parsed && typeof parsed === "object") {
    Object.defineProperty(parsed, "__cachedAt", {
      value: cachedAt,
      enumerable: false,
    });
  }
  return parsed;
}

async function detailOf(response) {
  // FastAPI puts the message on `detail`, and a validation failure puts a list
  // there instead. Both are surfaced as text rather than as "[object Object]",
  // which is what a naive read produces and what makes a real error unreadable.
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    }
    return JSON.stringify(body);
  } catch {
    return null;
  }
}

/**
 * The path with its ids put back as placeholders, so it matches a key in
 * ROUTES. "/api/documents/abc/file" -> "/api/documents/{drive_id}/file".
 */
function templateOf(path) {
  const clean = path.split("?")[0];
  return clean
    .replace(/\/documents\/[^/]+/, "/documents/{drive_id}")
    .replace(/\/study-items\/\d+/, "/study-items/{item_id}")
    .replace(/\/subjects\/(?!manual)[^/]+/, "/subjects/{name}")
    .replace(/\/projects\/\d+/, "/projects/{project_id}")
    .replace(/\/milestones\/\d+/, "/milestones/{milestone_id}")
    .replace(/\/tasks\/\d+/, "/tasks/{task_id}")
    .replace(/\/adjustments\/\d+/, "/adjustments/{adjustment_id}")
    .replace(/\/packs\/[^/]+/, "/packs/{course_id}");
}

export const api = {
  get: (path, options) => request("GET", path, options),
  post: (path, body, options) => request("POST", path, { body, ...options }),
  put: (path, body, options) => request("PUT", path, { body, ...options }),
  del: (path, options) => request("DELETE", path, options),
  postForm: (path, form, options) => request("POST", path, { form, ...options }),
};

/** Trade the token for a session cookie. The only unauthenticated call. */
export async function signIn(token) {
  return request("POST", "/api/session", { body: { token } });
}

export async function signOut() {
  return request("DELETE", "/api/session");
}

/**
 * Whether there is a live session, asked of the SERVER.
 *
 * Three answers, because there are three states and collapsing any two of them
 * is how the reported bug happened:
 *
 *   "in"           the server answered 200
 *   "out"          the server answered 401
 *   "unreachable"  nothing answered -- offline, or the server still starting
 *
 * The probe bypasses the service worker's cache (see `request`), and it retries
 * once after a pause, because the app is opened at logon at the same moment the
 * Startup script is starting `agent serve`.
 */
export async function haveSession() {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await request("GET", "/api/status", { probe: true });
      return "in";
    } catch (err) {
      if (err instanceof NotSignedIn) return "out";
      if (!(err instanceof Offline)) throw err;
      if (attempt === 0) await new Promise((r) => setTimeout(r, 1500));
    }
  }
  return "unreachable";
}

/**
 * Any error as a sentence a person can act on. The only thing a screen may show.
 *
 * Never a status line, never a stack, never `[object Object]`. The API's own
 * `detail` strings are already sentences -- "no study item with id 12." -- so
 * they pass through; everything else is named by what it means.
 */
export function describe(err) {
  if (err instanceof Offline) {
    return "No connection, so this can't be loaded right now.";
  }
  if (err instanceof NotSignedIn) return "Signed out.";
  if (err instanceof ApiError) {
    if (err.status >= 500) {
      return "The server hit an error and could not answer. The detail is in its log.";
    }
    if (err.status === 404 && !err.detail) {
      return "That isn't on the server. It may have been removed.";
    }
    return err.detail || "The server refused that request.";
  }
  return "Something in the app itself went wrong. Reloading usually clears it.";
}

/** Tell the service worker to forget cached personal data. Sign-out only. */
export function forgetCachedData() {
  const worker = navigator.serviceWorker && navigator.serviceWorker.controller;
  if (worker) worker.postMessage({ type: "signed-out" });
}
