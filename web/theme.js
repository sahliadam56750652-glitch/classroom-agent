// Light, dark, bright, or whatever the system says. DESIGN.md section 8.
//
// Bright is a light theme of its own and the system never picks it: System
// means light or dark, whichever the phone says.
//
// Remembered per device in localStorage and nowhere else: which theme a screen
// uses is a fact about that screen, not about me, so it is not synced and the
// server never hears of it. Wrapped in try/catch because storage can be refused
// (a private window, cleared site data) and the app must still render -- in the
// system's theme.
//
// The choice is applied before the first paint by a few lines in index.html,
// which read the same key. This module is what the More screen calls to change
// it, and it keeps <meta name="theme-color"> in step so the phone's own chrome
// matches.

const KEY = "lectern.theme";
// The key from the week the app was called Margin, read once so a choice made
// then survives the rename, and cleared the next time a theme is chosen.
const OLD_KEY = "margin.theme";
export const CHOICES = ["system", "light", "dark", "bright"];

// The page ground of each theme, for the browser's own chrome.
const GROUND = { light: "#f3efe7", dark: "#171512", bright: "#f4f7fb" };

// What the page declares it is, per theme -- on <meta name="color-scheme"> and
// on :root. "only" keeps Chrome's "dark theme for sites" from darkening a page
// that chose to be light (index.html has the story). Same table as index.html.
const SCHEME = { system: "light dark", light: "only light", dark: "only dark", bright: "only light" };

const SYSTEM_DARK = matchMedia("(prefers-color-scheme: dark)");

export function storedTheme() {
  try {
    const found = localStorage.getItem(KEY) || localStorage.getItem(OLD_KEY);
    return CHOICES.includes(found) ? found : "system";
  } catch {
    return "system";
  }
}

function effective(choice) {
  if (choice !== "system") return choice;
  return SYSTEM_DARK.matches ? "dark" : "light";
}

export function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
  const scheme = SCHEME[choice] || SCHEME.system;
  root.style.colorScheme = scheme;
  const declared = document.querySelector('meta[name="color-scheme"]');
  if (declared) declared.setAttribute("content", scheme);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", GROUND[effective(choice)]);
}

// With System chosen, the phone switching between light and dark moves the
// status bar too -- the one thing the page cannot do by CSS alone.
SYSTEM_DARK.addEventListener("change", () => {
  if (storedTheme() === "system") applyTheme("system");
});

export function setTheme(choice) {
  if (!CHOICES.includes(choice)) return;
  try {
    localStorage.removeItem(OLD_KEY);
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    // Not remembered, but still applied for this visit.
  }
  applyTheme(choice);
  // Two controls can be on screen at once -- More's and the sidebar's -- and
  // each should show the theme the other just chose.
  dispatchEvent(new CustomEvent("lectern:theme", { detail: choice }));
}
