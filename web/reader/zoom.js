// The reader's zoom and page geometry. Pure functions, no DOM, so the maths a
// pinch or a restored position depends on can be tested on its own (2026-10-17).
//
// Units. PDF.js measures a page in points (1/72 inch) and renders it at a
// `scale`: CSS pixels per point. "100%" means what it means in Chrome's own PDF
// viewer -- the page at its printed size on a 96-dpi screen -- so a percentage
// is `scale / CSS_UNITS`.
//
// Everything that has to survive a change of zoom is stored as (page, fraction
// across, fraction down) and never as pixels: a read position, the point under
// the fingers in a pinch, the point under the cursor on Ctrl+wheel. Pixels are
// worked out from it against the layout of the moment, and the layout is a pure
// function of the page sizes, the scale and the frame.

export const CSS_UNITS = 96 / 72;

// The menu's steps, as percentages. The same spread Chrome offers.
export const STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4];
export const MIN_PERCENT = 0.25;
export const MAX_PERCENT = 4;

// "Auto" never blows a portrait page up past this, however wide the window --
// PDF.js's own MAX_AUTO_SCALE. On a phone it simply fits the width.
export const MAX_AUTO_PERCENT = 1.25;

// Android blanks a canvas past roughly 16 megapixels or a side past a few
// thousand pixels. Above these the page renders at a lower resolution, never
// blank.
export const MAX_CANVAS_PIXELS = 16_777_216;
export const MAX_CANVAS_SIDE = 8192;

export const MODES = ["auto", "width", "page"];

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

/** A percentage kept inside 25%..400%. */
export function clampPercent(percent) {
  return Number.isFinite(percent) ? clamp(percent, MIN_PERCENT, MAX_PERCENT) : 1;
}

/**
 * The scales that fit one page of `natural` size (points) into `frame`.
 * frame: { width, height, padTop, padSide, padBottom } in CSS pixels.
 */
export function fitScales(natural, frame) {
  const width = (frame.width - 2 * frame.padSide) / natural.width;
  const height = (frame.height - frame.padTop - frame.padBottom) / natural.height;
  return { width, page: Math.min(width, height), height };
}

/**
 * The CSS scale for a zoom: { mode: "auto" | "width" | "page" | "custom", percent }.
 *
 * Auto is PDF.js's "auto": a landscape page (a slide) fits whole; a portrait
 * page fits the width but no larger than MAX_AUTO_PERCENT -- so a phone gets
 * the width and a 1920-wide monitor gets a readable page, not a billboard.
 */
export function scaleFor(zoom, natural, frame) {
  const fit = fitScales(natural, frame);
  let scale;
  if (zoom.mode === "width") scale = fit.width;
  else if (zoom.mode === "page") scale = fit.page;
  else if (zoom.mode === "custom") scale = zoom.percent * CSS_UNITS;
  else {
    const landscape = natural.width > natural.height;
    scale = landscape ? fit.page : Math.min(MAX_AUTO_PERCENT * CSS_UNITS, fit.width);
  }
  return clamp(scale, MIN_PERCENT * CSS_UNITS, MAX_PERCENT * CSS_UNITS);
}

export function percentOf(scale) {
  return scale / CSS_UNITS;
}

/** The next step above or below a percentage, for + and −. */
export function stepUp(percent) {
  return STEPS.find((step) => step > percent + 0.005) ?? MAX_PERCENT;
}

export function stepDown(percent) {
  return [...STEPS].reverse().find((step) => step < percent - 0.005) ?? MIN_PERCENT;
}

/**
 * Where every page sits at `scale`. `sizes` is the natural size (points) of each
 * page, 1-based pages at index 0. Pages stack top to bottom, each centred in a
 * content area at least as wide as the frame; wider than the frame, the content
 * is that wide and pans sideways.
 */
export function layout(sizes, scale, frame) {
  const gap = frame.gap ?? 12;
  let widest = 0;
  for (const size of sizes) widest = Math.max(widest, size.width * scale);
  const width = Math.max(frame.width, widest + 2 * frame.padSide);
  const boxes = [];
  let y = frame.padTop;
  for (const size of sizes) {
    const w = size.width * scale;
    const h = size.height * scale;
    boxes.push({ x: (width - w) / 2, y, w, h });
    y += h + gap;
  }
  const height = (boxes.length ? y - gap : y) + frame.padBottom;
  return { boxes, width, height, gap, scale };
}

/**
 * The page under a content point, and where on it, as fractions. A point in
 * the gap below a page counts as that page's bottom; above the first page, its
 * top. `fx` is not clamped, so a point beside a narrow page keeps its offset.
 */
export function locate(lay, x, y) {
  const { boxes } = lay;
  if (!boxes.length) return { page: 1, fx: 0.5, fy: 0 };
  let index = 0;
  while (index < boxes.length - 1 && y >= boxes[index + 1].y) index += 1;
  const box = boxes[index];
  return {
    page: index + 1,
    fx: (x - box.x) / box.w,
    fy: clamp((y - box.y) / box.h, 0, 1),
  };
}

/** The content point for (page, fx, fy) in a layout. */
export function pointOf(lay, page, fx, fy) {
  const box = lay.boxes[clamp(page, 1, lay.boxes.length) - 1];
  if (!box) return { x: 0, y: 0 };
  return { x: box.x + fx * box.w, y: box.y + fy * box.h };
}

/**
 * The scroll that keeps one spot under one screen point across a layout change.
 *
 * `before` is where the spot was on screen in the old layout, `after` where it
 * should be in the new one -- the same point for Ctrl+wheel, the moved midpoint
 * between the fingers for a pinch. Points are relative to the scroller's top
 * left. Returns { left, top }, never negative.
 */
export function anchorScroll(oldLay, newLay, scroll, before, after = before) {
  const spot = locate(oldLay, scroll.left + before.x, scroll.top + before.y);
  const target = pointOf(newLay, spot.page, spot.fx, spot.fy);
  return {
    left: Math.max(0, target.x - after.x),
    top: Math.max(0, target.y - after.y),
  };
}

/** The scale a pinch reaches: the start scale times how far the fingers spread. */
export function pinchPercent(startPercent, startDistance, distance) {
  if (!startDistance) return clampPercent(startPercent);
  return clampPercent(startPercent * (distance / startDistance));
}

/** The pages to keep rendered: those on screen, and `extra` either side. */
export function visibleRange(lay, scrollTop, viewHeight, extra = 1) {
  const { boxes } = lay;
  if (!boxes.length) return [1, 0];
  const top = scrollTop;
  const bottom = scrollTop + viewHeight;
  let first = boxes.findIndex((box) => box.y + box.h >= top);
  if (first < 0) first = boxes.length - 1;
  let last = first;
  while (last < boxes.length - 1 && boxes[last + 1].y <= bottom) last += 1;
  return [Math.max(1, first + 1 - extra), Math.min(boxes.length, last + 1 + extra)];
}

/**
 * The backing-store ratio for a canvas of css size w×h: the device's pixel
 * ratio, unless that would pass the caps -- then as much as fits. A lower
 * resolution looks soft; a blank page is what an oversized canvas gives.
 */
export function renderRatio(cssWidth, cssHeight, devicePixelRatio = 1) {
  const area = cssWidth * cssHeight;
  return Math.max(
    0.1,
    Math.min(
      devicePixelRatio,
      Math.sqrt(MAX_CANVAS_PIXELS / Math.max(1, area)),
      MAX_CANVAS_SIDE / Math.max(1, cssWidth),
      MAX_CANVAS_SIDE / Math.max(1, cssHeight)
    )
  );
}

/** The zoom stored for one document on this device, or Auto. */
export function storedZoom(driveId) {
  try {
    const found = JSON.parse(localStorage.getItem(`lectern.zoom.${driveId}`) || "null");
    if (found && (MODES.includes(found.mode) || (found.mode === "custom" && Number.isFinite(found.percent) && found.percent > 0))) {
      return found.mode === "custom" ? { mode: "custom", percent: clampPercent(found.percent) } : { mode: found.mode };
    }
  } catch {
    // Private mode, cleared storage: start on Auto.
  }
  return { mode: "auto" };
}

export function storeZoom(driveId, zoom) {
  try {
    localStorage.setItem(`lectern.zoom.${driveId}`, JSON.stringify(zoom));
  } catch {
    // Not remembered; the zoom still holds for this visit.
  }
}
