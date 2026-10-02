// Routing, in about forty lines.
//
// Nine screens is a switch on `location.pathname`, not a dependency. History
// routing rather than a hash, because a deep link has to survive being pasted
// and Caddy's `try_files {path} /index.html` already serves the app for any
// unknown path -- as does `agent serve`, which is what its ClientFiles mount is
// for.
//
// Every navigation is a real history entry, so the phone's back gesture works.
// That is the whole reason not to invent an in-app back button: the system one
// already exists and people already know it.

import { useEffect, useState } from "preact/hooks";

/** Where we are now, as a path plus its query. */
export function useRoute() {
  const [route, setRoute] = useState(() => read());

  useEffect(() => {
    const onPop = () => setRoute(read());
    addEventListener("popstate", onPop);
    // `navigate` fires this, so a programmatic move updates every subscriber
    // rather than only the one that called it.
    addEventListener("agent:navigate", onPop);
    return () => {
      removeEventListener("popstate", onPop);
      removeEventListener("agent:navigate", onPop);
    };
  }, []);

  return route;
}

function read() {
  return {
    path: location.pathname,
    query: new URLSearchParams(location.search),
  };
}

/** Go somewhere, adding a history entry. */
export function navigate(to, { replace = false } = {}) {
  const go = () => {
    if (replace) history.replaceState(null, "", to);
    else history.pushState(null, "", to);
    dispatchEvent(new Event("agent:navigate"));
  };
  // A short cross-fade between screens, where the browser has View Transitions
  // and I have not asked for less motion. Everywhere else, the move is instant
  // -- the transition is a courtesy, never a dependency. A replace is a
  // correction (signed out, signed in), not a move, so it never animates.
  //
  // And a transition must never be what stands between a tap and the screen.
  // Where the browser declines to run the update -- a hidden document, a
  // headless one -- the move happens anyway after 120ms, exactly once.
  if (
    !replace &&
    document.startViewTransition &&
    !matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    let moved = false;
    const once = () => {
      if (moved) return;
      moved = true;
      go();
    };
    try {
      document.startViewTransition(once);
    } catch {
      once();
    }
    setTimeout(once, 120);
  } else {
    go();
  }
}

/**
 * An anchor that routes internally but is still a real link.
 *
 * A real `href` matters: it is what makes long-press, open-in-new-tab and the
 * status bar preview work, and what lets the browser treat it as a link at all.
 * The click handler only intercepts the plain left-click case, so a modified
 * click still does what the person expects.
 */
export function linkProps(to) {
  return {
    href: to,
    onClick: (event) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      event.preventDefault();
      navigate(to);
    },
  };
}

/**
 * The first path segment after a prefix, or null.
 *
 * `match("/read/", "/read/abc")` is "abc". Decoded, because a Drive id is safe
 * but a course id from the timetable need not be.
 */
export function match(prefix, path) {
  if (!path.startsWith(prefix)) return null;
  const rest = path.slice(prefix.length).split("/")[0];
  return rest ? decodeURIComponent(rest) : null;
}
