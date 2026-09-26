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
//   Pages render as they come into view and are released as they leave. On 92
//   pages that is the difference between working and not.
//
//   The PDF renders exactly as the professor made it. No invert, no filter, no
//   "dark mode PDF": inverting a slide deck destroys diagrams, code screenshots
//   and photographed boards, which is precisely the content the vision OCR exists
//   to preserve.
//
//   Chrome goes on scroll. One element persists -- where I am -- because that is
//   the only thing section 6 says survives.

import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "/html.js";
import { api, Offline } from "/api.js";
import { linkProps } from "/router.js";

// Rendered ahead of and behind the visible page. Two is enough that a scroll
// never waits, and small enough that 92 canvases never exist at once.
const NEARBY = 2;

// How long after scrolling stops before the position is written. Long enough
// that a flick through five pages is one write, short enough that closing the
// tab a moment later still records where I got to.
const SETTLE_MS = 800;

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

function Page({ pdf, number, scale, onVisible }) {
  const holder = useRef(null);
  const [rendered, setRendered] = useState(false);

  useEffect(() => {
    const node = holder.current;
    if (!node) return;
    const watcher = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) onVisible(number);
        }
      },
      // A page counts as "where I am" once its middle is in view, which matches
      // what a person would say they are reading.
      { rootMargin: "-45% 0px -45% 0px" }
    );
    watcher.observe(node);
    return () => watcher.disconnect();
  }, [number, onVisible]);

  useEffect(() => {
    let cancelled = false;
    let task = null;

    (async () => {
      const page = await pdf.getPage(number);
      if (cancelled) return;
      const viewport = page.getViewport({ scale });
      const canvas = holder.current?.querySelector("canvas");
      if (!canvas) return;

      // Rendered at the device's real pixel density, then sized back down in
      // CSS. Without this a retina phone renders a blurred page, which on a
      // slide of dense equations is the difference between readable and not.
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;

      task = page.render({
        canvasContext: canvas.getContext("2d"),
        viewport,
        transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
      });
      try {
        await task.promise;
        if (!cancelled) setRendered(true);
      } catch (err) {
        // A cancelled render is the normal case when scrolling fast, not a
        // failure worth reporting.
        if (err && err.name !== "RenderingCancelledException") throw err;
      }
    })();

    return () => {
      cancelled = true;
      if (task) task.cancel();
    };
  }, [pdf, number, scale]);

  return html`
    <div class="page" ref=${holder} data-page=${number}>
      <canvas class=${rendered ? "" : "blank"}></canvas>
    </div>
  `;
}

export function Reader({ driveId }) {
  const [doc, setDoc] = useState(null);
  const [pdf, setPdf] = useState(null);
  const [current, setCurrent] = useState(1);
  const [problem, setProblem] = useState("");
  const [note, setNote] = useState("");
  const [chromeShown, setChromeShown] = useState(true);
  const [scale, setScale] = useState(1);

  const scroller = useRef(null);
  const restored = useRef(false);
  const lastWritten = useRef(null);

  // Metadata and the stored position, in parallel with the PDF itself.
  useEffect(() => {
    let live = true;
    Promise.all([
      api.get(`/api/documents/${encodeURIComponent(driveId)}`),
      api.get(`/api/documents/${encodeURIComponent(driveId)}/position`),
    ])
      .then(([found, position]) => {
        if (!live) return;
        setDoc(found);
        // Said out loud, both when the page moved and when it is gone. A silent
        // fall back to page one would show me different material and look
        // exactly like it worked.
        if (position.note) setNote(position.note);
        if (position.page) restored.current = position.page;
      })
      .catch((err) => {
        if (!live) return;
        setProblem(
          err instanceof Offline
            ? "No connection, and this document is not kept offline yet."
            : err.detail || err.message
        );
      });
    return () => {
      live = false;
    };
  }, [driveId]);

  // The document itself. PDF.js fetches ranges from this URL as pages are asked
  // for; nothing here reads bytes.
  useEffect(() => {
    let live = true;
    let task = null;
    loadPdfjs()
      .then((lib) => {
        if (!live) return;
        task = lib.getDocument({
          url: `/api/documents/${encodeURIComponent(driveId)}/file`,
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
      .then((opened) => live && opened && setPdf(opened))
      .catch((err) => live && setProblem(err.message || String(err)));
    return () => {
      live = false;
      if (task) task.destroy();
    };
  }, [driveId]);

  // Fit the page width to the viewport, and follow a rotation.
  useEffect(() => {
    if (!pdf) return;
    let live = true;
    const fit = async () => {
      const page = await pdf.getPage(1);
      if (!live) return;
      const natural = page.getViewport({ scale: 1 });
      const available = (scroller.current?.clientWidth || innerWidth) - 16;
      setScale(Math.max(0.2, available / natural.width));
    };
    fit();
    addEventListener("resize", fit);
    return () => {
      live = false;
      removeEventListener("resize", fit);
    };
  }, [pdf]);

  // Jump to the stored page once BOTH the document and the position have
  // arrived.
  //
  // `doc` is in the deps and that is the whole point: the two fetches race, and
  // with only `[pdf]` here a PDF that opened before the position came back would
  // never jump -- the effect would have run while `restored` was still empty and
  // never run again. Silently starting at page one is exactly the failure the
  // anchor exists to prevent, arriving by a different route.
  useEffect(() => {
    if (!pdf || !restored.current) return;
    const target = restored.current;
    restored.current = null;
    requestAnimationFrame(() => {
      scroller.current
        ?.querySelector(`[data-page="${target}"]`)
        ?.scrollIntoView({ block: "start" });
    });
  }, [pdf, doc]);

  // Chrome hides while scrolling and returns when it stops. One element
  // persists either way: where I am.
  useEffect(() => {
    const node = scroller.current;
    if (!node) return;
    let idle = null;
    const onScroll = () => {
      setChromeShown(false);
      clearTimeout(idle);
      idle = setTimeout(() => setChromeShown(true), 400);
    };
    node.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      node.removeEventListener("scroll", onScroll);
      clearTimeout(idle);
    };
  }, [pdf]);

  // Where I stopped, written after the scrolling settles.
  //
  // Only the page INDEX is sent. The server derives the content hash, because a
  // browser cannot: `sections.anchor` hashes PyMuPDF's extraction text and
  // PDF.js produces materially different text for the same page.
  useEffect(() => {
    if (!pdf || !current) return;
    const timer = setTimeout(() => {
      if (lastWritten.current === current) return;
      lastWritten.current = current;
      api
        .put(`/api/documents/${encodeURIComponent(driveId)}/position`, {
          page_index: current - 1,
        })
        .catch(() => {
          // A position that failed to save is not worth interrupting reading
          // for. It is re-sent on the next settle, and slice 3 queues it.
          lastWritten.current = null;
        });
    }, SETTLE_MS);
    return () => clearTimeout(timer);
  }, [driveId, current, pdf]);

  if (problem) {
    return html`<main class="pad">
      <p class="problem">${problem}</p>
      <p><a class="plain-link" ...${linkProps("/")}>Back</a></p>
    </main>`;
  }

  const total = pdf ? pdf.numPages : doc ? doc.pages : 0;
  const nearby = [];
  for (let n = Math.max(1, current - NEARBY); n <= Math.min(total, current + NEARBY); n++) {
    nearby.push(n);
  }

  return html`
    <div class="reader">
      <header class=${`reader-bar ${chromeShown ? "" : "gone"}`}>
        <a class="plain-link" ...${linkProps("/")}>Back</a>
        <span class="reader-title">${doc ? doc.title : ""}</span>
      </header>

      ${note && chromeShown
        ? html`<p class="reader-note">${note}</p>`
        : null}

      <div class="pages" ref=${scroller}>
        ${pdf
          ? Array.from({ length: total }, (_, index) => index + 1).map((n) =>
              nearby.includes(n)
                ? html`<${Page}
                    key=${n}
                    pdf=${pdf}
                    number=${n}
                    scale=${scale}
                    onVisible=${setCurrent}
                  />`
                : // A spacer of the same height, so the scrollbar does not jump
                  // as pages mount and unmount.
                  html`<div class="page spacer" data-page=${n} key=${n}></div>`
            )
          : html`<p class="pad faint">…</p>`}
      </div>

      <!-- The one element that survives the chrome going. -->
      <div class="position">${total ? `${current} / ${total}` : ""}</div>
    </div>
  `;
}
