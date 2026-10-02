"""Draw the app's icons from one geometry: web/icon.svg and the four PNGs.

    python tools/icons.py

The mark is DESIGN.md section 8's: a lectern -- a sloped reading desk with a
lip that holds the page, on a post and a foot -- in paper on fountain-pen blue,
the lip in highlighter amber. Drawn with Pillow rather than rendered from the
SVG, because there is no SVG rasteriser in this project's dependencies and
adding one for four files is not worth it -- so the geometry lives here once
and both outputs follow it. web/icons.js draws the same shapes for the in-app
mark, in the theme's own colours.

The maskable versions keep the drawing inside the central 80% safe zone,
because a launcher may crop the icon to a circle.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parents[1] / "web"

BLUE = "#2a4f93"
PAPER = "#fbf9f5"
AMBER = "#d6a64a"

# On a 64-unit square. The desk is a sloped quadrilateral; the lip runs along
# its lower edge; two lines of "text" lie on it at the same slope.
DESK = ((12, 26), (48, 15), (52, 23), (16, 34))
LIP = ((15, 35), (53, 23.5))
LINES = (((20, 26.5), (40, 20.5)), ((22, 30), (36, 25.8)))
POST = (29, 33, 35, 47)
FOOT = (19, 46, 45, 51, 2.5)


def svg() -> str:
    desk = " ".join(f"{x},{y}" for x, y in DESK)
    (lx1, ly1), (lx2, ly2) = LIP
    lines = "".join(
        f'<path d="M{a[0]} {a[1]}L{b[0]} {b[1]}" stroke="{BLUE}" stroke-width="2.6" stroke-linecap="round"/>'
        for a, b in LINES
    )
    px1, py1, px2, py2 = POST
    fx1, fy1, fx2, fy2, fr = FOOT
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="14" fill="{BLUE}"/>'
        f'<rect x="{px1}" y="{py1}" width="{px2 - px1}" height="{py2 - py1}" fill="{PAPER}"/>'
        f'<rect x="{fx1}" y="{fy1}" width="{fx2 - fx1}" height="{fy2 - fy1}" rx="{fr}" fill="{PAPER}"/>'
        f'<polygon points="{desk}" fill="{PAPER}" stroke="{PAPER}" stroke-width="2" stroke-linejoin="round"/>'
        f'<path d="M{lx1} {ly1}L{lx2} {ly2}" stroke="{AMBER}" stroke-width="3.4" stroke-linecap="round"/>'
        f"{lines}</svg>\n"
    )


def png(size: int, *, maskable: bool) -> Image.Image:
    scale = 4  # drawn large and reduced, for smooth edges
    big = size * scale
    unit = big / 64
    if maskable:
        image = Image.new("RGB", (big, big), BLUE)
    else:
        image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if not maskable:
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=14 * unit, fill=BLUE)
    shrink = 0.8 if maskable else 1.0
    offset = (1 - shrink) * 32

    def at(x: float, y: float) -> tuple[float, float]:
        return ((offset + x * shrink) * unit, (offset + y * shrink) * unit)

    def round_line(a, b, width, colour):
        draw.line((*at(*a), *at(*b)), fill=colour, width=max(1, round(width * unit * shrink)))
        radius = width * unit * shrink / 2
        for point in (a, b):
            cx, cy = at(*point)
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=colour)

    px1, py1, px2, py2 = POST
    draw.rectangle((*at(px1, py1), *at(px2, py2)), fill=PAPER)
    fx1, fy1, fx2, fy2, fr = FOOT
    draw.rounded_rectangle((*at(fx1, fy1), *at(fx2, fy2)), radius=fr * unit * shrink, fill=PAPER)
    draw.polygon([at(x, y) for x, y in DESK], fill=PAPER)
    # Soften the desk's corners the way the SVG's round join does.
    for a, b in zip(DESK, DESK[1:] + DESK[:1]):
        round_line(a, b, 2, PAPER)
    round_line(*LIP, 3.4, AMBER)
    for a, b in LINES:
        round_line(a, b, 2.6, BLUE)
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    (WEB / "icon.svg").write_text(svg(), encoding="utf-8")
    for size in (192, 512):
        png(size, maskable=False).save(WEB / f"icon-{size}.png")
        png(size, maskable=True).save(WEB / f"icon-{size}-maskable.png")
    print("wrote icon.svg and four PNGs")


if __name__ == "__main__":
    main()
