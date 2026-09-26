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

## Traps this client has already hit

Four of these cost real time in slice 1, and none of them is visible without
rendering the page. They are listed here because a no-build client has no
compiler to catch any of them, and `tests/test_web_render.py` exists because of
them.

**A comment inside an `html` template cannot contain `${`.** It is not a comment
to JavaScript -- the template literal sees live interpolation syntax, and `${}`
with nothing in it is a SyntaxError that takes the module down and leaves a blank
page. Put the explanation above the function instead.

**htm drops the whitespace between adjacent `${}` on separate lines.** It
produced "12 pagesare not transcribed" and "Sep 2908:30". Build a sentence as one
string rather than as several interpolations.

**`attr="false"` is TRUE.** Preact sets a boolean attribute from truthiness and
the string `"false"` is truthy, so `spellcheck="false"` turns spellcheck on. Use
`spellcheck=${false}`.

**`render()` appends.** Preact renders INTO a container rather than replacing its
contents, so anything already in `#app` stays on screen above the app. The
container is cleared first.

And one that is not htm's fault: **a separator styled with CSS margins has no
space in the text**. `<span class="sep">·</span>` looked correct and read as
"92 pagesan evening" to anything consuming `innerText`, including a screen
reader. The spaces are in the markup now.

## Verifying the reader

`tests/test_web_render.py` drives the screens with headless Chrome and
`--virtual-time-budget`, which is what makes it take four seconds. **That flag
cannot be used on the reader.** It fast-forwards timers but not the network, and
it starves a Web Worker: PDF.js loads, `getDocument` creates its task, and the
promise then never settles and never requests a byte. Nothing is broken; the
harness has simply run out of virtual time before the worker got any real time.

So the reader is checked by looking, and the quickest honest way to do that is to
let the page report through the access log:

    agent serve --config <config> --port 8231
    # then open /read/<drive_id> in a real browser and watch the log

A working reader produces exactly this, and the last line is the one worth
waiting for -- it means the settle timer fired and the position was saved:

    GET  /read/<id>                     200
    GET  /api/documents/<id>            200
    GET  /api/documents/<id>/position   200
    GET  /api/documents/<id>/file       206 or 304
    PUT  /api/documents/<id>/position   200

A `200` rather than a `206` or `304` on the file means ranges have stopped
working and the whole document is being pulled -- which looks like nothing at
all except a reader that feels slow.
