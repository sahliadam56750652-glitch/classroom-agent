// Light, dark, or whatever the system says. DESIGN.md section 8.
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

const KEY = "margin.theme";
export const CHOICES = ["system", "light", "dark"];

// The page ground of each theme, for the browser's own chrome.
const GROUND = { light: "#f3efe7", dark: "#171512" };

export function storedTheme() {
  try {
    const found = localStorage.getItem(KEY);
    return CHOICES.includes(found) ? found : "system";
  } catch {
    return "system";
  }
}

function effective(choice) {
  if (choice !== "system") return choice;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
  const meta = document.querySelector('meta[name="theme-color"]:not([media])');
  if (meta) meta.setAttribute("content", GROUND[effective(choice)]);
}

export function setTheme(choice) {
  if (!CHOICES.includes(choice)) return;
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    // Not remembered, but still applied for this visit.
  }
  applyTheme(choice);
}
