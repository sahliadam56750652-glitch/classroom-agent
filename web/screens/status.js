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

import { html } from "/html.js";
import { askToPersist, useDocuments, useLocal } from "/sync.js";
import { recently } from "/format.js";
import { useState } from "preact/hooks";
import { NotSent, NotSynced, ScreenHeader, Skeleton } from "/ui.js";
import { useOutbox } from "/outbox.js";

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

/** "162 MB", "48 kB": the size of what is on the phone, plainly. */
function size(bytes) {
  if (bytes == null) return "unknown";
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`;
  return `${Math.max(1, Math.round(bytes / 1e3))} kB`;
}

/**
 * What this phone holds, and whether Android has agreed to keep it. Shown first,
 * because a library the browser may clear is the one silent loss this app can
 * suffer -- so whether it is protected is said, never assumed.
 */
/** Whether this is the installed app or a browser tab: Chrome decides by it. */
function installed() {
  try {
    return matchMedia("(display-mode: standalone)").matches;
  } catch {
    return false;
  }
}

/**
 * What Android said about keeping the storage, in its own words, and a way to
 * ask again from a tap. The app asks by itself on every launch until it is
 * granted (sync.js); a tap is the strongest asking there is, so if that is
 * refused from the installed app, the refusal is real.
 */
function KeepStorage({ docs }) {
  const [busy, setBusy] = useState(false);
  if (docs.supported === false) {
    return html`<p class="t-meta">This browser cannot be asked to keep storage.</p>`;
  }
  const asked = docs.asked;
  const where = installed() ? "the installed app" : "a browser tab, not the installed app";
  return html`<div class="status-keep">
    <p class="t-meta">
      <code>navigator.storage.persisted()</code>${" returns "}<strong>${docs.persisted === null ? "nothing yet" : String(docs.persisted)}</strong>${`, asked from ${where}.`}
      ${asked
        ? ` Last asked ${recently(asked.at)}${asked.how === "tap" ? " from a tap" : asked.how === "launch" ? " at launch" : " while downloading"}: ${asked.granted ? "granted" : "refused"}.`
        : ""}
    </p>
    ${docs.persisted === true
      ? null
      : html`<button
          class="button secondary-button"
          type="button"
          disabled=${busy}
          onClick=${async () => {
            setBusy(true);
            await askToPersist("tap");
            setBusy(false);
          }}
        >Keep this storage</button>`}
  </div>`;
}

function ThisPhone() {
  const docs = useDocuments();
  const waiting = useOutbox().filter((e) => e.status === "waiting").length;
  const kept =
    docs.persisted === true
      ? "Kept: Android will not clear this storage."
      : docs.persisted === false
      ? installed()
        ? "May be cleared by Android when the phone runs low on space."
        : "May be cleared by Android when the phone runs low on space. Installing the app usually lets it be kept."
      : "Whether Android will keep it is not known on this browser.";
  return html`<section class="card status-card status-phone">
    <div class="card-body">
      <h2 class="section-title">On this phone</h2>
      <ul class="figures">
        <li class="figure"><span class="num">${docs.held}</span>of <span class="num">${docs.total}</span>documents</li>
        <li class="figure"><span class="num">${size(docs.heldBytes)}</span>held</li>
        ${docs.usage != null
          ? html`<li class="figure"><span class="num">${size(docs.usage)}</span>used of ${size(docs.quota)} allowed</li>`
          : null}
      </ul>
      <p class="t-state">${kept}</p>
      <${KeepStorage} docs=${docs} />
      <p class="t-meta status-origin">
        This copy belongs to <span class="status-address">${location.origin}</span>. Opened from
        any other address, the app starts empty, so that address never changes.
      </p>
      ${docs.downloading ? html`<p class="t-meta">Downloading ${docs.downloading}…</p>` : null}
      ${docs.waitingForWifi
        ? html`<p class="t-meta">Documents wait for Wi-Fi. More can allow mobile data.</p>`
        : null}
      ${docs.error ? html`<p class="t-meta">${docs.error}</p>` : null}
      <p class="t-meta status-outbox">
        ${waiting
          ? `${waiting === 1 ? "One change" : `${waiting} changes`} made here ${waiting === 1 ? "is" : "are"} waiting to send. They go, in the order you made them, when the PC is reachable.`
          : "Nothing made on this phone is waiting to send."}
      </p>
    </div>
  </section>`;
}

export function Status() {
  const found = useLocal("/api/status");
  if (found.status === "loading") return html`<div class="screen"><${Skeleton} rows=${4} /></div>`;
  if (found.status === "absent") {
    return html`<div class="screen"><${ScreenHeader} title="Status" /><${NotSynced} reason=${found.reason} /></div>`;
  }
  const status = found.data;

  // Two different syncs, and this card is the OTHER one. The line at the top of
  // every screen ("Synced 15:29") is when this phone last copied from the PC;
  // this is when the PC last checked Google Classroom (`agent run`, sync_runs).
  // Before 2026-10-16 both said "synced", and they disagreed by an hour and a
  // half on the same screen.
  const sync = status.last_sync;
  const synced = sync
    ? sync.status === "error"
      ? `The PC's last check of Classroom, ${recently(sync.started_at)}, failed: ${sync.error || "no reason recorded"}.`
      : sync.status === "running"
      ? `The PC started checking Classroom ${recently(sync.started_at)} and has not finished.`
      : `The PC last checked Classroom ${recently(sync.finished_at || sync.started_at)}.`
    : "The PC has not checked Classroom yet.";

  return html`<div class="screen status">
    <${ScreenHeader} title="Status">
      Everything the app counts, in full, zeros included.
    </${ScreenHeader}>

    <${ThisPhone} />
    <${NotSent} title="Not sent" />

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
