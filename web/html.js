// htm bound to Preact, in one place.
//
// A module rather than two lines repeated in every screen: `htm.bind(h)` creates a
// template cache, and binding it per file would give each one its own -- which
// works, and quietly costs the caching that is the whole reason htm is fast enough
// to use without a compiler.
import { h } from "preact";
import htm from "htm";

export const html = htm.bind(h);
