// The library, and the deadlines.
//
// Both are navigation rather than the product, so both are lists that get out
// of the way. The one rule with teeth is on the deadlines: `due_at` is the only
// fact about time, and the client does not compute a second one.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, Offline } from "/api.js";
import { age, hasPassed, localTime, plural } from "/format.js";
import { linkProps } from "/router.js";

export function Library({ course }) {
  const [posts, setPosts] = useState(null);
  const [problem, setProblem] = useState("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    let live = true;
    const path = course
      ? `/api/library?course=${encodeURIComponent(course)}`
      : "/api/library";
    api
      .get(path)
      .then((found) => live && setPosts(found))
      .catch((err) => {
        if (!live) return;
        setProblem(
          err instanceof Offline
            ? "No connection, and the library list is not kept offline."
            : err.detail || err.message
        );
      });
    return () => {
      live = false;
    };
  }, [course]);

  if (problem) {
    return html`<main class="pad"><p class="problem">${problem}</p></main>`;
  }
  if (!posts) return html`<main class="pad"><p class="faint">…</p></main>`;

  // Filtered here rather than by re-asking the server: the whole list is
  // already in hand, and a request per keystroke on mobile data is worse than
  // no filter at all.
  const shown = query
    ? posts.filter((post) =>
        post.title.toLowerCase().includes(query.toLowerCase())
      )
    : posts;

  return html`
    <main class="pad">
      <h1>Library</h1>
      <input
        type="search"
        value=${query}
        placeholder="filter by title"
        onInput=${(event) => setQuery(event.target.value)}
      />
      ${!posts.length
        ? html`<p class="quiet">
            Nothing has been fetched and extracted yet.
          </p>`
        : html`
            <ul class="posts">
              ${shown.map(
                (post) => html`
                  <li class="post" key=${`${post.entity_type}:${post.entity_id}`}>
                    <span class="post-title">${post.title}</span>
                    <span class="post-meta">
                      ${`${post.course_name} · ${plural(post.pages, "page")}` +
                      (post.unread
                        ? ` · ${post.unread} not transcribed`
                        : "") +
                      (post.creation_time ? ` · ${age(post.creation_time)}` : "")}
                    </span>
                  </li>
                `
              )}
            </ul>
            <p class="quiet">
              ${`${plural(shown.length, "post")} held.`}
            </p>
          `}
    </main>
  `;
}

export function Deadlines() {
  const [rows, setRows] = useState(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get("/api/deadlines")
      .then((found) => live && setRows(found))
      .catch((err) => live && setProblem(err.detail || err.message));
    return () => {
      live = false;
    };
  }, []);

  if (problem) {
    return html`<main class="pad"><p class="problem">${problem}</p></main>`;
  }
  if (!rows) return html`<main class="pad"><p class="faint">…</p></main>`;

  return html`
    <main class="pad">
      <h1>Deadlines</h1>
      ${!rows.length
        ? html`<p class="quiet">Nothing due.</p>`
        : html`<ul class="posts">
            ${rows.map(
              (row) => html`
                <li class="post" key=${`${row.entity_type}:${row.entity_id}`}>
                  <span class="post-title">${row.title}</span>
                  <span
                    class=${`post-meta ${hasPassed(row.due_at) ? "passed" : ""}`}
                  >
                    <!--
                      The instant, and nothing else about time. No countdown, no
                      colour that escalates as it nears: section 7 forbids
                      manufactured urgency, and the only red in this app is a
                      deadline that has ALREADY gone.
                    -->
                    ${row.due_at ? localTime(row.due_at) : "no date"}
                    ${row.label ? ` · ${row.label}` : ""}
                  </span>
                </li>
              `
            )}
          </ul>`}
    </main>
  `;
}
