// The reader. DESIGN.md section 6 -- "the reader is the product; everything else
// is navigation."
//
// The whole design is about not getting in the way at 23:00:
//
//   PDF.js is pointed at the URL, not at bytes, so it issues Range requests and
//   a 92-page deck opens on page 1 without pulling the file. That is what slice 3
//   of the API was for, and if it ever stops working the only symptom is that the
//   reader "feels slow" -- so a test asserts the 206.
//
//   Pages render only while on screen (and one either side) and are dropped as
//   they leave. On 92 pages that is the difference between working and not.
//
//   The PDF renders exactly as the professor made it. No invert, no filter, no
//   "dark mode PDF": inverting a slide deck destroys diagrams, code screenshots
//   and photographed boards, which is precisely the content the vision OCR exists
//   to preserve.
//
//   Zoom works like Chrome's own PDF viewer (2026-10-17): Auto, Fit width, Fit
//   page and 25%..400%, from the header, Ctrl +/-/0, Ctrl+wheel and a trackpad
//   pinch at the cursor, a finger pinch between the fingers, and a double tap.
//   Only the PDF scales -- never the header, never the app. The geometry lives in
//   zoom.js, and every position that must survive a zoom -- where I stopped, the
//   spot under the fingers -- is a page and a fraction, never pixels.
//
//   Chrome goes on scroll. One element persists -- where I am -- because that is
//   the only thing section 6 says survives.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import { html } from "/html.js";
import { Offline, api, describe } from "/api.js";
import { linkProps, navigate } from "/router.js";
import { send } from "/outbox.js";
import { documentFromSnapshot } from "/sync.js";
import { Icon } from "/icons.js";
import { Problem } from "/ui.js";
import {
  STEPS,
  clampPercent,
  layout,
  locate,
  percentOf,
  pinchPercent,
  pointOf,
  renderRatio,
  scaleFor,
  stepDown,
  stepUp,
  storeZoom,
  storedZoom,
  visibleRange,
} from "/reader/zoom.js";

// How long after scrolling stops before the position is written. Long enough
// that a flick through five pages is one write, short enough that closing the
// tab a moment later still records where I got to.
const SETTLE_MS = 800;

// The header's height. Pages start below it, and "where I am" is the point
// just under it -- the line a person reads from after scrolling.
const HEADER = 56;
const FRAME = { padSide: 8, padBottom: 64, gap: 12 };

const MODE_WORDS = { auto: "Auto", width: "Fit width", page: "Fit page" };

let pdfjs = null;

/**
 * PDF.js, loaded the first time a document is opened and never before.
 *
 * 1.4 MB of worker. The default screen must not pay for it, which is the whole
 * reason this is a dynamic import rather than a module-level one.
 */
async function loadPdfjs() {
  if (pdfjs) return pdfjs;
  const module = await import("/vendor/pdf.min.mjs");
  module.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker.min.mjs";
  pdfjs = module;
  return pdfjs;
}

/**
 * One page, rendered at its box's size.
 *
 * At the device's real pixel density, so a dense equation is sharp -- unless
 * that would pass the canvas caps, in which case at whatever resolution fits:
 * Android blanks an oversized canvas, and a soft page beats a blank one.
 * The new canvas replaces the old one only once it has finished drawing, so a
 * change of zoom never flashes white.
 */
function Page({ pdf, number, box, scale, onSize }) {
  const holder = useRef(null);

  useEffect(() => {
    let cancelled = false;
    let task = null;

    (async () => {
      const page = await pdf.getPage(number);
      if (cancelled) return;
      const natural = page.getViewport({ scale: 1 });
      onSize(number, { width: natural.width, height: natural.height });
      const viewport = page.getViewport({ scale });
      const ratio = renderRatio(viewport.width, viewport.height, devicePixelRatio || 1);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.floor(viewport.width * ratio));
      canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
      task = page.render({
        canvasContext: canvas.getContext("2d"),
        viewport,
        transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
      });
      try {
        await task.promise;
      } catch (err) {
        // A cancelled render is the normal case when scrolling fast or zooming,
        // not a failure worth reporting.
        if (err && err.name !== "RenderingCancelledException") throw err;
        return;
      }
      if (cancelled || !holder.current) return;
      canvas.dataset.ratio = String(ratio);
      holder.current.replaceChildren(canvas);
    })();

    return () => {
      cancelled = true;
      if (task) task.cancel();
    };
  }, [pdf, number, scale]);

  return html`<div
    class="page"
    ref=${holder}
    data-page=${number}
    style=${{ left: `${box.x}px`, top: `${box.y}px`, width: `${box.w}px`, height: `${box.h}px` }}
  ></div>`;
}

/** − , the zoom menu, + -- in the header, calm, the modes and the steps. */
function ZoomBar({ zoom, percent, onZoom }) {
  // Steps are "step:1.25" -- never a bare letter, which a mode name could share.
  const value = zoom.mode === "custom" ? `step:${zoom.percent}` : zoom.mode;
  const shown = `${Math.round(percent * 100)}%`;
  const custom = zoom.mode === "custom" && !STEPS.includes(zoom.percent);
  return html`<div class="zoom-bar" role="group" aria-label="Zoom">
    <button
      class="button quiet-button zoom-step"
      type="button"
      aria-label="Zoom out"
      onClick=${() => onZoom({ mode: "custom", percent: stepDown(percent) })}
    >−</button>
    <select
      class="zoom-menu"
      aria-label=${`Zoom, now ${shown}`}
      value=${value}
      onChange=${(event) => {
        const chosen = event.target.value;
        onZoom(chosen.startsWith("step:") ? { mode: "custom", percent: Number(chosen.slice(5)) } : { mode: chosen });
      }}
    >
      ${Object.entries(MODE_WORDS).map(
        ([mode, words]) => html`<option key=${mode} value=${mode}>
          ${zoom.mode === mode ? `${words} (${shown})` : words}
        </option>`
      )}
      ${custom ? html`<option value=${value}>${shown}</option>` : null}
      ${STEPS.map((step) => html`<option key=${step} value=${`step:${step}`}>${Math.round(step * 100)}%</option>`)}
    </select>
    <button
      class="button quiet-button zoom-step"
      type="button"
      aria-label="Zoom in"
      onClick=${() => onZoom({ mode: "custom", percent: stepUp(percent) })}
    >+</button>
  </div>`;
}

export function Reader({ driveId }) {
  const [doc, setDoc] = useState(null);
  const [pdf, setPdf] = useState(null);
  const [current, setCurrent] = useState(1);
  const [problem, setProblem] = useState("");
  const [note, setNote] = useState("");
  const [chromeShown, setChromeShown] = useState(true);
  const [zoom, setZoom] = useState(() => storedZoom(driveId));
  const [sizes, setSizes] = useState(null);
  const [frame, setFrame] = useState(null);
  const [range, setRange] = useState([1, 2]);
  const [restoreTick, setRestoreTick] = useState(0);

  const scroller = useRef(null);
  const content = useRef(null);
  // `?page=N` -- from a quiz result's "Open page N" -- wins over the stored
  // position: I asked for that page by name, and where I last stopped is a
  // different question. 1-based, as the question gave it.
  const askedPage = Number(new URLSearchParams(location.search).get("page")) || 0;
  const restored = useRef(askedPage ? { page: askedPage, fraction: 0 } : null);
  const lastWritten = useRef(null);
  // The layout the DOM shows right now, and a spot to keep under a screen point
  // through the next layout change (a zoom at the cursor, a pinch).
  const shownLayout = useRef(null);
  const pending = useRef(null);
  const percentRef = useRef(1);
  const pointer = useRef(null);
  // The header's real height: taller than HEADER under a phone's notch.
  const headerHeight = useRef(HEADER);

  // Metadata and the stored position, in parallel with the PDF itself. Asked of
  // the PC; when it does not answer, the document's details come from the
  // phone's snapshot and reading starts where it can -- the position is said to
  // be unknown rather than silently guessed.
  useEffect(() => {
    let live = true;
    Promise.allSettled([
      api.get(`/api/documents/${encodeURIComponent(driveId)}`),
      api.get(`/api/documents/${encodeURIComponent(driveId)}/position`),
    ]).then(async ([found, position]) => {
      if (!live) return;
      if (found.status === "fulfilled") {
        setDoc(found.value);
      } else {
        const kept = await documentFromSnapshot(driveId);
        if (!live) return;
        if (kept) {
          setDoc(kept);
        } else {
          const err = found.reason;
          setProblem(
            err instanceof Offline
              ? "This document is not on the phone yet. It arrives with the next sync, while the PC is on."
              : describe(err)
          );
          return;
        }
      }
      if (position.status === "fulfilled") {
        // Said out loud, both when the page moved and when it is gone. A silent
        // fall back to page one would show me different material and look
        // exactly like it worked.
        if (position.value.note) setNote(position.value.note);
        if (position.value.page && !askedPage) {
          // A page and how far down it. A position from before fractions has
          // none, and that is the top of its page -- where it always went.
          restored.current = { page: position.value.page, fraction: position.value.fraction || 0 };
          setRestoreTick((n) => n + 1);
        }
      } else if (position.reason instanceof Offline) {
        setNote("The PC is not reachable, so where you stopped is not known here. This opens at the start.");
      }
    });
    return () => {
      live = false;
    };
  }, [driveId]);

  // The document itself, from the URL its details name: the file when it is a
  // PDF, the PDF the PC made of it when it is PowerPoint or Word (Phase 5e).
  // PDF.js fetches ranges from that URL as pages are asked for; nothing here
  // reads bytes. Waits for the details, because they say which URL -- and an
  // image or an unreadable file needs no PDF.js at all.
  const pdfUrl = doc && doc.view === "pdf" ? doc.pdf_url : null;
  useEffect(() => {
    if (!pdfUrl) return undefined;
    let live = true;
    let task = null;
    loadPdfjs()
      .then((lib) => {
        if (!live) return;
        task = lib.getDocument({
          url: pdfUrl,
          // The session cookie has to ride along, or every range request is a
          // 401 and the document never opens.
          withCredentials: true,
          rangeChunkSize: 65536,
          // Without this PDF.js streams the WHOLE file in the background after
          // opening, which is exactly the thing the range support exists to
          // avoid -- the first page would arrive fast and the other 91 would
          // arrive anyway, over mobile data, unasked. Range requests without
          // this flag are an optimisation of latency only, not of bytes.
          disableAutoFetch: true,
          disableStream: true,
        });
        return task.promise;
      })
      .then(async (opened) => {
        if (!live || !opened) return;
        // Every page is assumed the size of the first until it has been seen:
        // asking all 92 for their size up front is 92 range requests. A page
        // that turns out different corrects the layout when it renders, and
        // the spot I am reading stays put (see the layout effect below).
        const first = (await opened.getPage(1)).getViewport({ scale: 1 });
        if (!live) return;
        setSizes(Array.from({ length: opened.numPages }, () => ({ width: first.width, height: first.height })));
        setPdf(opened);
      })
      .catch((err) =>
        live &&
        setProblem(
          err instanceof Offline || (err && err.name === "UnexpectedResponseException")
            ? "This document is not on the phone yet. It arrives with the next sync, while the PC is on."
            : describe(err)
        )
      );
    return () => {
      live = false;
      if (task) task.destroy();
    };
  }, [pdfUrl]);

  // The frame: the scroller's size, followed through a resize or a rotation.
  useEffect(() => {
    const node = scroller.current;
    if (!node) return undefined;
    const measure = () => {
      const bar = node.parentElement && node.parentElement.querySelector(".reader-bar");
      headerHeight.current = (bar && bar.offsetHeight) || HEADER;
      setFrame({
        ...FRAME,
        padTop: headerHeight.current + FRAME.gap,
        width: node.clientWidth,
        height: node.clientHeight,
      });
    };
    measure();
    const watcher = new ResizeObserver(measure);
    watcher.observe(node);
    return () => watcher.disconnect();
  }, [pdf]);

  const scale = sizes && frame ? scaleFor(zoom, sizes[0], frame) : null;
  const lay = useMemo(() => (scale && sizes && frame ? layout(sizes, scale, frame) : null), [sizes, scale, frame]);
  if (scale) percentRef.current = percentOf(scale);

  function measureView() {
    const node = scroller.current;
    const shown = shownLayout.current;
    if (!node || !shown) return;
    setRange(visibleRange(shown, node.scrollTop, node.clientHeight, 1));
    setCurrent(locate(shown, node.scrollLeft + node.clientWidth / 2, node.scrollTop + node.clientHeight / 2).page);
  }

  // Every layout change keeps a spot on screen. A zoom keeps the spot under the
  // cursor or the fingers; opening keeps the stored position at the top; and
  // anything else -- a resize, a page that turned out a different size -- keeps
  // the line under the header where it was.
  useLayoutEffect(() => {
    const node = scroller.current;
    if (!node || !lay) return;
    const old = shownLayout.current;
    let target = null;
    if (pending.current) {
      const { spot, screen } = pending.current;
      pending.current = null;
      const at = pointOf(lay, spot.page, spot.fx, spot.fy);
      target = { left: at.x - screen.x, top: at.y - screen.y };
    } else if (restored.current && pdf) {
      const { page, fraction } = restored.current;
      restored.current = null;
      const at = pointOf(lay, page, 0.5, fraction);
      target = { left: (lay.width - node.clientWidth) / 2, top: at.y - headerHeight.current };
    } else if (old && old !== lay) {
      const spot = locate(old, node.scrollLeft + node.clientWidth / 2, node.scrollTop + headerHeight.current);
      const at = pointOf(lay, spot.page, spot.fx, spot.fy);
      target = { left: at.x - node.clientWidth / 2, top: at.y - headerHeight.current };
    }
    shownLayout.current = lay;
    if (content.current) content.current.style.transform = "";
    if (target) {
      node.scrollLeft = Math.max(0, target.left);
      node.scrollTop = Math.max(0, target.top);
    }
    measureView();
  }, [lay, restoreTick]);

  /** Change the zoom, keeping the spot under `screen` (scroller-relative) under it. */
  function zoomTo(next, screen) {
    const node = scroller.current;
    const shown = shownLayout.current;
    if (node && shown && next.mode === "page") {
      // Fit page shows a whole page: the one I am on, its top just under the
      // header -- not wherever a centred anchor would leave it.
      const page = locate(shown, node.scrollLeft + node.clientWidth / 2, node.scrollTop + node.clientHeight / 2).page;
      pending.current = {
        spot: { page, fx: 0.5, fy: 0 },
        screen: { x: node.clientWidth / 2, y: headerHeight.current + FRAME.gap },
      };
    } else if (node && shown) {
      const point = screen || { x: node.clientWidth / 2, y: node.clientHeight / 2 };
      pending.current = {
        spot: locate(shown, node.scrollLeft + point.x, node.scrollTop + point.y),
        screen: point,
      };
    }
    const clean =
      next.mode === "custom"
        ? Number.isFinite(next.percent) ? { mode: "custom", percent: clampPercent(next.percent) } : { mode: "auto" }
        : { mode: next.mode };
    setZoom(clean);
    storeZoom(driveId, clean);
  }

  const zoomRef = useRef(zoomTo);
  zoomRef.current = zoomTo;

  // A page reports its real size once seen; only a change re-lays the pages.
  const onSize = useMemo(
    () => (number, size) =>
      setSizes((known) => {
        if (!known) return known;
        const was = known[number - 1];
        if (was && Math.abs(was.width - size.width) < 0.5 && Math.abs(was.height - size.height) < 0.5) return known;
        const copy = known.slice();
        copy[number - 1] = size;
        return copy;
      }),
    []
  );

  // Scrolling: which pages to keep rendered, where I am, the chrome, and the
  // position, written once the scrolling settles.
  useEffect(() => {
    const node = scroller.current;
    if (!node) return undefined;
    let frameId = 0;
    let idle = null;
    let settle = null;
    const onScroll = () => {
      if (!frameId) frameId = requestAnimationFrame(() => { frameId = 0; measureView(); });
      setChromeShown(false);
      clearTimeout(idle);
      idle = setTimeout(() => setChromeShown(true), 400);
      clearTimeout(settle);
      settle = setTimeout(() => saveRef.current(), SETTLE_MS);
    };
    node.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      node.removeEventListener("scroll", onScroll);
      cancelAnimationFrame(frameId);
      clearTimeout(idle);
      clearTimeout(settle);
    };
  }, [pdf]);

  // Where I stopped: the page and how far down it, at the line under the header.
  // Only the page INDEX and the fraction are sent; the server derives the
  // content hash, because a browser cannot (`sections.anchor` hashes PyMuPDF's
  // text, and PDF.js's differs).
  function savePosition() {
    const node = scroller.current;
    const shown = shownLayout.current;
    if (!node || !shown || !pdf) return;
    // A file uploaded to a project is not a Drive document and has no place to
    // keep: a position for it would only ever be refused.
    if (driveId.startsWith("project-file-")) return;
    const spot = locate(shown, node.scrollLeft + node.clientWidth / 2, node.scrollTop + headerHeight.current);
    const fraction = Math.round(spot.fy * 1000) / 1000;
    const key = `${spot.page}:${Math.round(fraction * 50)}`;
    if (lastWritten.current === key) return;
    lastWritten.current = key;
    // Through the outbox, so with the PC off it waits on the phone and goes
    // when the PC is back -- one per document, the latest. It never interrupts
    // reading: a refusal means the PC no longer has this document.
    send({
      kind: "position",
      method: "PUT",
      path: `/api/documents/${encodeURIComponent(driveId)}/position`,
      body: { page_index: spot.page - 1, fraction },
      target: `doc:${driveId}`,
      label: doc ? doc.title : "",
    }).catch(() => {});
  }
  const saveRef = useRef(savePosition);
  saveRef.current = savePosition;

  // Desktop: Ctrl + / Ctrl − / Ctrl 0, and Ctrl+wheel -- which is also how a
  // trackpad pinch arrives -- zoom the PDF, not the app, at the cursor.
  useEffect(() => {
    const node = scroller.current;
    if (!node || !pdf) return undefined;
    let factor = 1;
    let at = null;
    let frameId = 0;
    const relative = (event) => {
      const rect = node.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    };
    const onWheel = (event) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 0.05 : 0.0025;
      factor *= Math.exp(-event.deltaY * unit);
      at = relative(event);
      if (!frameId) {
        frameId = requestAnimationFrame(() => {
          frameId = 0;
          zoomRef.current({ mode: "custom", percent: percentRef.current * factor }, at);
          factor = 1;
        });
      }
    };
    const onMove = (event) => { pointer.current = relative(event); };
    const onLeave = () => { pointer.current = null; };
    const onKey = (event) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      const percent = percentRef.current;
      let next = null;
      if (event.key === "+" || event.key === "=") next = { mode: "custom", percent: stepUp(percent) };
      else if (event.key === "-" || event.key === "_") next = { mode: "custom", percent: stepDown(percent) };
      else if (event.key === "0") next = { mode: "auto" };
      if (!next) return;
      event.preventDefault();
      zoomRef.current(next, pointer.current);
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    node.addEventListener("mousemove", onMove, { passive: true });
    node.addEventListener("mouseleave", onLeave);
    addEventListener("keydown", onKey);
    return () => {
      node.removeEventListener("wheel", onWheel);
      node.removeEventListener("mousemove", onMove);
      node.removeEventListener("mouseleave", onLeave);
      removeEventListener("keydown", onKey);
      cancelAnimationFrame(frameId);
    };
  }, [pdf]);

  // Phone: a pinch zooms the PDF between the fingers. During the gesture the
  // rendered pages are only scaled by a CSS transform -- cheap, smooth -- and
  // when the fingers lift they are laid out and rendered sharp at the new
  // zoom, the spot that was between the fingers staying there. A double tap
  // zooms in at the tap, or back to Auto when already zoomed in. The browser's
  // own pinch is off on the pages (touch-action in reader.css), so the header
  // and the app never scale.
  useEffect(() => {
    const node = scroller.current;
    if (!node || !pdf) return undefined;
    let pinch = null;
    let tap = null;
    let lastTap = null;
    const local = (touch) => {
      const rect = node.getBoundingClientRect();
      return { x: touch.clientX - rect.left, y: touch.clientY - rect.top };
    };
    const spread = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    const middle = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });

    const onStart = (event) => {
      if (event.touches.length === 2) {
        const a = local(event.touches[0]);
        const b = local(event.touches[1]);
        pinch = {
          distance: spread(a, b),
          mid: middle(a, b),
          now: middle(a, b),
          percent: percentRef.current,
          reached: percentRef.current,
          scroll: { left: node.scrollLeft, top: node.scrollTop },
        };
        tap = null;
      } else if (event.touches.length === 1) {
        tap = { at: local(event.touches[0]), moved: false };
      }
    };
    const onMove = (event) => {
      if (pinch && event.touches.length === 2) {
        event.preventDefault();
        const a = local(event.touches[0]);
        const b = local(event.touches[1]);
        pinch.now = middle(a, b);
        pinch.reached = pinchPercent(pinch.percent, pinch.distance, spread(a, b));
        const s = pinch.reached / pinch.percent;
        const origin = { x: pinch.scroll.left + pinch.mid.x, y: pinch.scroll.top + pinch.mid.y };
        if (content.current) {
          content.current.style.transformOrigin = `${origin.x}px ${origin.y}px`;
          content.current.style.transform =
            `translate(${pinch.now.x - pinch.mid.x}px, ${pinch.now.y - pinch.mid.y}px) scale(${s})`;
        }
      } else if (tap && event.touches.length === 1) {
        const at = local(event.touches[0]);
        if (spread(at, tap.at) > 10) tap.moved = true;
      }
    };
    const onEnd = (event) => {
      if (pinch && event.touches.length < 2) {
        const done = pinch;
        pinch = null;
        const shown = shownLayout.current;
        if (shown) {
          pending.current = {
            spot: locate(shown, done.scroll.left + done.mid.x, done.scroll.top + done.mid.y),
            screen: done.now,
          };
        }
        const next = { mode: "custom", percent: done.reached };
        setZoom(next);
        storeZoom(driveId, next);
        lastTap = null;
        return;
      }
      if (tap && !tap.moved && event.touches.length === 0) {
        const now = Date.now();
        if (lastTap && now - lastTap.time < 320 && spread(lastTap.at, tap.at) < 30) {
          event.preventDefault();
          lastTap = null;
          const auto = shownLayout.current && sizes && frame
            ? percentOf(scaleFor({ mode: "auto" }, sizes[0], frame))
            : percentRef.current;
          const zoomedIn = percentRef.current > auto * 1.05;
          zoomRef.current(zoomedIn ? { mode: "auto" } : { mode: "custom", percent: auto * 2 }, tap.at);
        } else {
          lastTap = { time: now, at: tap.at };
        }
      }
      tap = null;
    };
    node.addEventListener("touchstart", onStart, { passive: true });
    node.addEventListener("touchmove", onMove, { passive: false });
    node.addEventListener("touchend", onEnd, { passive: false });
    node.addEventListener("touchcancel", onEnd, { passive: false });
    return () => {
      node.removeEventListener("touchstart", onStart);
      node.removeEventListener("touchmove", onMove);
      node.removeEventListener("touchend", onEnd);
      node.removeEventListener("touchcancel", onEnd);
    };
  }, [pdf, sizes, frame]);

  if (problem) {
    return html`<main class="reader-problem">
      <${Problem}>${problem}</${Problem}>
      <p><a class="button secondary-button" ...${linkProps("/")}><${Icon} name="back" /> Back</a></p>
    </main>`;
  }

  // A file the reader cannot show says why, rather than a blank page.
  if (doc && !doc.readable) {
    return html`<main class="reader-problem">
      <${Problem}>${doc.unreadable_because || "This file cannot be shown in the reader."}</${Problem}>
      ${doc.url
        ? html`<p><a class="button secondary-button" href=${doc.url} target="_blank" rel="noopener">
            <${Icon} name="external" /> Open it in Drive
          </a></p>`
        : null}
      <p><a class="button quiet-button" ...${linkProps("/")}><${Icon} name="back" /> Back</a></p>
    </main>`;
  }

  const total = pdf ? pdf.numPages : doc ? doc.pages : 0;
  const [first, last] = range;

  return html`
    <div class="reader">
      <header class=${`reader-bar ${chromeShown ? "" : "gone"}`}>
        <a
          class="button quiet-button reader-back"
          aria-label="Back"
          ...${linkProps("/")}
          onClick=${(event) => {
            // Back is the system's back when there is somewhere to go: the
            // reader is opened from Today, a subject, the library or a quiz, and
            // each expects to be returned to, not to Today.
            event.preventDefault();
            if (history.length > 1) history.back();
            else navigate("/");
          }}
        ><${Icon} name="back" /></a>
        <span class="reader-title">${doc ? doc.title : ""}</span>
        ${pdf && scale
          ? html`<${ZoomBar} zoom=${zoom} percent=${percentOf(scale)} onZoom=${(next) => zoomTo(next, null)} />`
          : null}
      </header>

      ${note && chromeShown
        ? html`<p class="notice reader-note" role="status">${note}</p>`
        : null}

      <div class="pages" ref=${scroller}>
        ${doc && doc.view === "image"
          ? html`<div class="image-page"><img class="reader-image" src=${doc.pdf_url} alt=${doc.title} /></div>`
          : pdf && lay
          ? html`<div
              class="pages-content"
              ref=${content}
              style=${{ width: `${lay.width}px`, height: `${lay.height}px` }}
            >
              ${lay.boxes.map((box, index) => {
                const n = index + 1;
                return n >= first && n <= last
                  ? html`<${Page} key=${n} pdf=${pdf} number=${n} box=${box} scale=${scale} onSize=${onSize} />`
                  : // Off screen: an empty sheet of exactly the page's size, so
                    // the geometry -- and every position measured against it --
                    // is the same whether a page is rendered or not.
                    html`<div
                      class="page spacer"
                      key=${n}
                      data-page=${n}
                      style=${{ left: `${box.x}px`, top: `${box.y}px`, width: `${box.w}px`, height: `${box.h}px` }}
                    ></div>`;
              })}
            </div>`
          : html`<div class="page-loading" aria-busy="true"></div>`}
      </div>

      <!-- The one element that survives the chrome going. -->
      <div class="position">${total ? `${current} / ${total}` : ""}</div>
    </div>
  `;
}
