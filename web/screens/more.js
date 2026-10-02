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

export const SECONDARY = [
  ["/add", "Add", "add", "An upload, a task, a session held, or a project."],
  ["/status", "Status", "status", "When it last synced, and every figure it counts."],
];

export function More({ onSignOut }) {
  return html`<div class="screen">
    <${ScreenHeader} title="More" />
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
