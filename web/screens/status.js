// Status: when it last synced, and every figure the coverage number is built
// from.
//
// This screen exists because of one line in DESIGN.md section 7: "Never a state
// the app counts but will not show me. Anything feeding the Phase 4 coverage
// figure must be reachable in the UI. A number I cannot audit is a number I will
// not trust, and an untrusted coverage figure is a project that has failed."
//
// So it is deliberately complete, and it is the one screen where a zero IS
// shown: elsewhere a zero is noise, here it is an answer ("how many are
// skipped?" "none"). No summary, no health score, no green ticks -- those would be
// a judgement about the numbers, and the point is to show the numbers.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, describe } from "/api.js";
import { localTime } from "/format.js";
import { Problem, ScreenHeader, Skeleton } from "/ui.js";

// Stored states, in words -- per section, because the same stored word means
// different things in different tables: an OCR page that is "pending" is
// waiting to be read, a lecture that is "pending" has not been opened. Anything
// not named is shown as stored, so a new state appears rather than vanishing.
const WORDS = {
  lectures: {
    pending: "not opened yet",
    delivered: "opened",
    reviewed: "read, not verified",
    verified: "verified",
    skipped: "skipped",
  },
  files: {
    ok: "readable",
    fetched: "fetched, not yet read",
    missing: "gone from Drive",
    trashed: "in Drive's trash",
    unsupported: "a format that cannot be read",
    error: "failed",
  },
  pages: {
    pending: "waiting to be read",
    ok: "read",
    error: "could not be read",
  },
};

function ordered(counts, words) {
  const order = Object.keys(words || {});
  return Object.entries(counts || {}).sort(
    ([a], [b]) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99) || a.localeCompare(b)
  );
}

function Figures({ title, counts, note, words = {} }) {
  const entries = ordered(counts, words);
  return html`<section class="card status-card">
    <div class="card-body">
      <h2 class="section-title">${title}</h2>
      ${entries.length
        ? html`<ul class="figures">
            ${entries.map(
              ([name, count]) => html`<li key=${name} class="figure">
                <span class="num">${count}</span>${words[name] || name}
              </li>`
            )}
          </ul>`
        : html`<p class="t-state">None yet.</p>`}
      ${note ? html`<p class="t-meta">${note}</p>` : null}
    </div>
  </section>`;
}

export function Status() {
  const [status, setStatus] = useState(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get("/api/status")
      .then((found) => live && setStatus(found))
      .catch((err) => live && setProblem(describe(err)));
    return () => {
      live = false;
    };
  }, []);

  if (problem) return html`<div class="screen"><${Problem}>${problem}</${Problem}></div>`;
  if (!status) return html`<div class="screen"><${Skeleton} rows=${4} /></div>`;

  const sync = status.last_sync;
  const synced = sync
    ? sync.status === "error"
      ? `The last sync, ${localTime(sync.started_at)}, failed: ${sync.error || "no reason recorded"}.`
      : sync.status === "running"
      ? `A sync started ${localTime(sync.started_at)} and has not finished.`
      : `Last synced ${localTime(sync.finished_at || sync.started_at)}.`
    : "Not synced yet.";

  return html`<div class="screen status">
    <${ScreenHeader} title="Status">
      Everything the app counts, in full, zeros included.
    </${ScreenHeader}>

    <section class=${`card status-sync ${sync && sync.status === "error" ? "status-failed" : ""}`}>
      <div class="card-body">
        <p class="t-lead">${synced}</p>
        ${sync && sync.events_emitted
          ? html`<p class="t-meta">${`${sync.events_emitted} change${sync.events_emitted === 1 ? "" : "s"} found in that run.`}</p>`
          : null}
      </div>
    </section>

    <div class="status-grid">
      <${Figures} title="Lectures" counts=${status.study_items} words=${WORDS.lectures} />
      <${Figures}
        title="Files"
        counts=${status.extractions}
        words=${WORDS.files}
        note="A file gone from Drive is gone for good; it is not retried."
      />
      <${Figures}
        title="Pages read from images"
        counts=${status.ocr_pages}
        words=${WORDS.pages}
        note="A few pages are read each sync, newest material first. A lecture can be opened before every page is read, and quizzed only after."
      />
      <${Figures}
        title="Notices"
        counts=${{ sent: status.events_total - status.events_pending, "waiting to be sent": status.events_pending }}
      />
      ${status.ocr_errors && status.ocr_errors.length
        ? html`<${Figures}
            title="Pages that could not be read"
            counts=${Object.fromEntries(status.ocr_errors.map((row) => [row.error, row.pages]))}
          />`
        : null}
      <${Figures}
        title="This install"
        counts=${{ "schema version": status.schema_version, "signed-in devices": status.sessions_live }}
      />
    </div>
  </div>`;
}
