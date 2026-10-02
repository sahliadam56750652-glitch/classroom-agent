"""Draw the app's icons from one geometry: web/icon.svg and the four PNGs.

    python tools/icons.py

The mark is DESIGN.md section 8's: a page with its margin rule, on fountain-pen
blue. Drawn with Pillow rather than rendered from the SVG, because there is no
SVG rasteriser in this project's dependencies and adding one for four files is
not worth it -- so the geometry lives here once and both outputs follow it.

The maskable versions keep the page inside the central 80% safe zone, because a
launcher may crop the icon to a circle.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parents[1] / "web"

BLUE = "#2a4f93"
PAPER = "#fbf9f5"
AMBER = "#c79a3a"
INK = "#2a4f93"

# On a 64-unit square: (x, y, w, h, r) of the page, the margin line's x, and the
# three text lines as (x1, x2, y).
PAGE = (17, 10, 30, 44, 5)
MARGIN_X = 25
LINES = ((29, 41, 22), (29, 41, 30), (29, 36, 38))


def svg() -> str:
    x, y, w, h, r = PAGE
    lines = "".join(
        f'<path d="M{a} {yy}H{b}" stroke="{INK}" stroke-width="3.2" stroke-linecap="round"/>'
        for a, b, yy in LINES
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="14" fill="{BLUE}"/>'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{PAPER}"/>'
        f'<path d="M{MARGIN_X} {y}V{y + h}" stroke="{AMBER}" stroke-width="2.4"/>'
        f"{lines}</svg>\n"
    )


def png(size: int, *, maskable: bool) -> Image.Image:
    scale = 4  # drawn large and reduced, for smooth edges
    big = size * scale
    unit = big / 64
    image = Image.new("RGB", (big, big), BLUE) if maskable else Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if not maskable:
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=14 * unit, fill=BLUE)
    # Maskable: shrink the drawing about the centre into the 80% safe zone.
    shrink = 0.78 if maskable else 1.0
    offset = (1 - shrink) * 32

    def at(value: float) -> float:
        return (offset + value * shrink) * unit

    x, y, w, h, r = PAGE
    draw.rounded_rectangle((at(x), at(y), at(x + w), at(y + h)), radius=r * unit * shrink, fill=PAPER)
    draw.line((at(MARGIN_X), at(y), at(MARGIN_X), at(y + h)), fill=AMBER, width=max(1, round(2.4 * unit * shrink)))
    stroke = 3.2 * unit * shrink
    for a, b, yy in LINES:
        draw.line((at(a), at(yy), at(b), at(yy)), fill=INK, width=max(1, round(stroke)))
        for cx in (at(a), at(b)):
            draw.ellipse((cx - stroke / 2, at(yy) - stroke / 2, cx + stroke / 2, at(yy) + stroke / 2), fill=INK)
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    (WEB / "icon.svg").write_text(svg(), encoding="utf-8")
    for size in (192, 512):
        png(size, maskable=False).save(WEB / f"icon-{size}.png")
        png(size, maskable=True).save(WEB / f"icon-{size}-maskable.png")
    print("wrote icon.svg and four PNGs")


if __name__ == "__main__":
    main()
