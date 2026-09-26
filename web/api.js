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
    super(detail || `${route} failed with ${status}`);
    this.status = status;
    this.detail = detail;
    this.route = route;
  }
}

/** Thrown on a 401. The app shows sign-in; nothing else handles it. */
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

async function request(method, path, { body, form, signal } = {}) {
  const route = `${method} ${templateOf(path)}`;
  let response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      signal,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: form ? form : body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new Offline(route);
  }

  if (response.status === 401) throw new NotSignedIn(401, null, route);

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
 * Whether there is a live session, asked the only way available: try something
 * that needs one. The cookie is HttpOnly, so there is nothing to inspect.
 */
export async function haveSession() {
  try {
    await api.get("/api/status");
    return true;
  } catch (err) {
    if (err instanceof NotSignedIn) return false;
    throw err;
  }
}
