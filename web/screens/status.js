// Every figure the coverage number is built from.
//
// This screen exists because of one line in DESIGN.md section 7: "Never a state
// the app counts but will not show me. Anything feeding the Phase 4 coverage
// figure must be reachable in the UI. A number I cannot audit is a number I will
// not trust, and an untrusted coverage figure is a project that has failed."
//
// So it is deliberately plain and deliberately complete. No summary, no health
// score, no green ticks -- those would be a judgement about the numbers, and the
// point is to show the numbers.

import { useEffect, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, describe } from "/api.js";
import { localTime } from "/format.js";

function Counts({ title, counts, note }) {
  const entries = Object.entries(counts || {});
  if (!entries.length) {
    return html`<section>
      <h2>${title}</h2>
      <p class="quiet">none</p>
    </section>`;
  }
  return html`
    <section>
      <h2>${title}</h2>
      <ul class="facts">
        ${entries.map(
          ([name, count]) =>
            html`<li key=${name}><span>${name}</span><b>${count}</b></li>`
        )}
      </ul>
      ${note ? html`<p class="faint">${note}</p>` : null}
    </section>
  `;
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

  if (problem) {
    return html`<main class="pad"><p class="problem">${problem}</p></main>`;
  }
  if (!status) return html`<main class="pad"><p class="faint">…</p></main>`;

  const sync = status.last_sync;

  return html`
    <main class="pad">
      <h1>What is counted</h1>
      <p class="quiet">
        Every figure the coverage number is built from. A number I cannot audit
        is a number I will not trust.
      </p>

      <${Counts} title="Study items" counts=${status.study_items} />
      <${Counts}
        title="Extractions"
        counts=${status.extractions}
        note="'missing' is a Drive file that is gone. Permanent, not a retry."
      />
      <${Counts}
        title="OCR pages"
        counts=${status.ocr_pages}
        note=${
          "Pending drains at the configured run limit, twice a day. An item " +
          "can be delivered and still not be quizzable."
        }
      />

      <section>
        <h2>Events</h2>
        <ul class="facts">
          <li><span>reported</span><b>${status.events_total}</b></li>
          <li><span>waiting to be sent</span><b>${status.events_pending}</b></li>
        </ul>
      </section>

      ${status.ocr_errors && status.ocr_errors.length
        ? html`<section>
            <h2>OCR errors</h2>
            <ul class="facts">
              ${status.ocr_errors.map(
                (row) =>
                  html`<li key=${row.error}>
                    <span>${row.error}</span><b>${row.pages}</b>
                  </li>`
              )}
            </ul>
          </section>`
        : null}

      <section>
        <h2>This install</h2>
        <ul class="facts">
          <li><span>schema version</span><b>${status.schema_version}</b></li>
          <li><span>signed-in devices</span><b>${status.sessions_live}</b></li>
          <li>
            <span>last sync</span
            ><b>${sync ? localTime(sync.started_at) : "never"}</b>
          </li>
        </ul>
      </section>
    </main>
  `;
}
