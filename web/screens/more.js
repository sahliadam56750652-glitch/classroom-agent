// Everything the tab bar has no room for.
//
// Five tabs is the most a thumb can tell apart, and four of them are places I go
// to study. What is left is entering things and checking on the machine, so it
// lives here. On a wide screen the sidebar lists it all and this screen is never
// needed -- it still works there, because a pasted /more link should not be a
// dead end.

import { html } from "/html.js";
import { Icon } from "/icons.js";
import { linkProps } from "/router.js";
import { ScreenHeader } from "/ui.js";
import { useState } from "preact/hooks";
import { CHOICES, setTheme, storedTheme } from "/theme.js";
import { setDocumentsOnMobileData, useDocuments } from "/sync.js";

export const SECONDARY = [
  ["/add", "Add", "add", "A task from class, or a project."],
  ["/status", "Status", "status", "When it last synced, and every figure it counts."],
];

const THEME_WORDS = { system: "System", light: "Light", dark: "Dark", bright: "Bright" };

/** Light, dark, bright or the system's choice, for this device only. */
function ThemeChoice() {
  const [chosen, setChosen] = useState(storedTheme());
  return html`<section class="section" aria-labelledby="theme-title">
    <h2 class="section-title" id="theme-title">Appearance</h2>
    <div class="choice" role="group" aria-labelledby="theme-title">
      ${CHOICES.map(
        (choice) => html`<button
          key=${choice}
          type="button"
          aria-pressed=${chosen === choice ? "true" : "false"}
          onClick=${() => {
            setTheme(choice);
            setChosen(choice);
          }}
        >${THEME_WORDS[choice]}</button>`
      )}
    </div>
    <p class="t-meta">Remembered on this device. System follows the phone's own setting.</p>
  </section>`;
}

/** Whether documents may download over mobile data, for this device. */
function DocumentsChoice() {
  const docs = useDocuments();
  const allowed = docs.onMobileDataAllowed;
  return html`<section class="section" aria-labelledby="docs-title">
    <h2 class="section-title" id="docs-title">Documents</h2>
    <div class="choice" role="group" aria-labelledby="docs-title">
      <button type="button" aria-pressed=${allowed ? "false" : "true"} onClick=${() => setDocumentsOnMobileData(false)}>
        Wi-Fi only
      </button>
      <button type="button" aria-pressed=${allowed ? "true" : "false"} onClick=${() => setDocumentsOnMobileData(true)}>
        Mobile data too
      </button>
    </div>
    <p class="t-meta">
      This semester's documents are kept on this phone so they open with the PC off.
      The first download is large.
    </p>
  </section>`;
}

export function More({ onSignOut }) {
  return html`<div class="screen">
    <${ScreenHeader} title="More" />
    <${ThemeChoice} />
    <${DocumentsChoice} />
    <ul class="list">
      ${SECONDARY.map(
        ([to, label, icon, what]) => html`<li key=${to}>
          <a class="card more-link" ...${linkProps(to)}>
            <${Icon} name=${icon} class="more-icon" />
            <div class="card-body">
              <span class="t-lead">${label}</span>
              <span class="t-meta">${what}</span>
            </div>
            <${Icon} name="chevron" class="chevron" />
          </a>
        </li>`
      )}
    </ul>
    <div>
      <button class="button secondary-button" type="button" onClick=${onSignOut}>
        <${Icon} name="signout" />
        Sign out
      </button>
    </div>
  </div>`;
}
