// The library: every post this install holds, newest first. Part of Study.
//
// Navigation rather than the product -- the reader is the product -- so this is
// a list that gets out of the way: a search, a row of subject chips to narrow
// it, and each post a tinted card that opens its file in the reader in one tap.
//
// Filtered here rather than by re-asking the server: the whole list is already
// in hand, and a request per keystroke on mobile data is worse than no filter.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { Offline, api, describe } from "/api.js";
import { age, plural } from "/format.js";
import { Icon } from "/icons.js";
import { navigate } from "/router.js";
import {
  Card,
  Chip,
  Empty,
  Problem,
  ScreenHeader,
  SectionLinks,
  Skeleton,
  STUDY_PARTS,
  useSubjectNames,
} from "/ui.js";
import { subjectStyle } from "/subject-color.js";

const STATE = {
  pending: "Not opened yet",
  delivered: "Opened",
  reviewed: "Read, not verified",
  verified: "Verified",
  skipped: "Skipped",
};

function Post({ post, subject }) {
  const file = (post.documents || []).find((found) => found.readable);
  const meta = [
    plural(post.pages, "page"),
    post.unread ? `${post.unread} still to transcribe` : "",
    post.creation_time ? `posted ${age(post.creation_time)}` : "",
  ].filter(Boolean);
  const body = html`
    <${Chip} name=${subject} />
    <span class="t-lead library-title">${post.title}</span>
    ${post.study_item_state
      ? html`<span class="t-state">${STATE[post.study_item_state] || post.study_item_state}</span>`
      : null}
    <span class="t-meta">${meta.join(" · ")}</span>
  `;
  return html`<li>
    ${file
      ? html`<${Card} to=${`/read/${encodeURIComponent(file.drive_id)}`} subject=${subject}>${body}</${Card}>`
      : html`<${Card} subject=${subject}>
          ${body}
          <span class="t-meta">No readable file is held for this yet.</span>
        </${Card}>`}
  </li>`;
}

export function Library({ course }) {
  const names = useSubjectNames();
  const [posts, setPosts] = useState(null);
  const [problem, setProblem] = useState("");
  const [query, setQuery] = useState("");
  const [only, setOnly] = useState(course || "");

  useEffect(() => {
    let live = true;
    api
      .get("/api/library")
      .then((found) => live && setPosts(found))
      .catch((err) => {
        if (!live) return;
        setProblem(
          err instanceof Offline
            ? "No connection, and the library list is not kept offline."
            : describe(err)
        );
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => setOnly(course || ""), [course]);

  const subjectOf = (post) => names[post.course_id] || post.course_name;

  const header = html`
    <${ScreenHeader} title="Study" />
    <${SectionLinks} links=${STUDY_PARTS} here="/library" />
  `;

  if (problem) return html`<div class="screen">${header}<${Problem}>${problem}</${Problem}></div>`;
  if (!posts) return html`<div class="screen">${header}<${Skeleton} rows=${4} /></div>`;

  const courses = [...new Map(posts.map((post) => [post.course_id, subjectOf(post)])).entries()].sort(
    (a, b) => a[1].localeCompare(b[1])
  );
  const wanted = query.trim().toLowerCase();
  const shown = posts.filter(
    (post) =>
      (!only || post.course_id === only) &&
      (!wanted || post.title.toLowerCase().includes(wanted))
  );

  return html`<div class="screen library">
    ${header}
    <div class="library-tools">
      <label class="field library-search">
        <span class="visually-hidden">Search the library</span>
        <span class="search-box">
          <${Icon} name="search" />
          <input
            type="search"
            value=${query}
            placeholder="Search titles"
            onInput=${(event) => setQuery(event.target.value)}
          />
        </span>
      </label>
      <div class="filter-chips" role="group" aria-label="Show one subject">
        <button
          type="button"
          class="filter-chip"
          aria-pressed=${only ? "false" : "true"}
          onClick=${() => {
            setOnly("");
            if (course) navigate("/library", { replace: true });
          }}
        >All</button>
        ${courses.map(
          ([id, name]) => html`<button
            key=${id}
            type="button"
            class="filter-chip"
            style=${subjectStyle(name)}
            aria-pressed=${only === id ? "true" : "false"}
            onClick=${() => setOnly(only === id ? "" : id)}
          >${name}</button>`
        )}
      </div>
    </div>

    ${!posts.length
      ? html`<${Empty} title="Nothing here yet.">Material appears once it is fetched, or uploaded from Add.</${Empty}>`
      : shown.length
      ? html`<ul class="grid">
          ${shown.map(
            (post) => html`<${Post}
              key=${`${post.entity_type}:${post.entity_id}`}
              post=${post}
              subject=${subjectOf(post)}
            />`
          )}
        </ul>`
      : html`<${Empty} title="Nothing matches.">Try fewer letters, or another subject.</${Empty}>`}
  </div>`;
}
