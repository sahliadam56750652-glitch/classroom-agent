# Vendored dependencies

Committed, not fetched. There is no `package.json`, no lockfile and no build step:
what is in this directory is what the browser runs and what Caddy serves.

**Why vendored rather than from a CDN.** An offline-first client cannot depend on
a third-party origin being reachable — the whole promise in DESIGN.md §6 is that
what has been delivered reads with the radio off, and a `unpkg.com` import breaks
that on the first load after a cache eviction. It also keeps the move to the
Oracle box a directory copy, which is invariant 5's whole point, and means the box
needs no toolchain: it already has Python and now needs nothing else.

**Why no build step.** The alternative was Vite, which makes `web/dist` a build
artifact — and then either minified bundles get committed, which is noise in every
diff, or the ARM box grows a Node dependency to produce them. Neither is worth
having. The source being the served thing is also what lets a screen be fixed on
the box with an editor at 23:00.

## What is here

| file | version | bytes | purpose |
|---|---|---|---|
| `preact.module.js` | 10.24.3 | 11,429 | the renderer |
| `preact-hooks.module.js` | 10.24.3 | 3,729 | `useState`, `useEffect` |
| `htm.module.js` | 3.1.1 | 1,207 | JSX-shaped templates with no compiler |
| `pdf.min.mjs` | pdfjs-dist 4.10.38 | 352,645 | the reader |
| `pdf.worker.min.mjs` | pdfjs-dist 4.10.38 | 1,375,838 | PDF.js's worker |

16 KB for the framework. The 1.4 MB worker is the honest cost of rendering PDFs
in a browser, and it is loaded lazily — only the reader pulls it, so the default
screen never pays for it.

`preact-hooks.module.js` imports `"preact"` as a bare specifier, which a browser
cannot resolve on its own. The import map in `index.html` is what points it at the
file beside it. That import map is load-bearing; without it the app fails at
parse time with a module resolution error.

## Refreshing one

    python -c "import urllib.request; open('web/vendor/htm.module.js','wb').write(urllib.request.urlopen('https://unpkg.com/htm@3.1.1/dist/htm.module.js').read())"

Then update the version and the byte count in the table above, and re-run
`python -m pytest tests/test_web_assets.py` — it checks the table against what is
actually on disk, so a refresh that forgets the table is a failing test rather
than a document that has quietly stopped being true.

Pin exact versions. A range would reintroduce the thing vendoring exists to avoid.
