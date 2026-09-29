// Everything the tab bar has no room for.
//
// Five tabs is the most a thumb can tell apart, so four places live here. On a
// wide screen the sidebar lists all of them and this screen is never needed --
// it still works there, because a pasted /more link should not be a dead end.

import { html } from "/html.js";
import { Icon } from "/icons.js";
import { linkProps } from "/router.js";
import { ScreenHeader } from "/ui.js";

export const SECONDARY = [
  ["/deadlines", "Deadlines", "deadlines", "Everything with a due date, in date order."],
  ["/projects", "Projects", "projects", "Longer work, and its milestones."],
  ["/add", "Add", "add", "Something that never came through Classroom."],
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
