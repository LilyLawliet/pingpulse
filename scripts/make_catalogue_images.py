"""Render the retail catalogue's product images locally.

Free stock-photo services were tried first and are not fit for this: a random
image service served a sunset for the earbuds, and a keyword one served a
statue and a circuit board, because Flickr tags are not a product taxonomy.
Rendering the cards here instead makes the catalogue deterministic, on-brand,
and — the part that matters for a client demo — independent of whether some
third-party image host happens to be reachable at the time.

These are clearly rendered cards, not photographs. A real deployment points
`media` at the shop's own product photography; this exists so the demo has
something honest and consistent to show.

The cards are written into the app's own media directory, which is bind-mounted
from Drive D: and served at /media, so WhatsApp and the dashboard both fetch
them from the same place as any other attachment.

    python scripts/make_catalogue_images.py

Writes to D:\\pingpulse\\docker\\media\\catalogue.
"""

from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = pathlib.Path(r"D:\pingpulse\docker\media\catalogue")

WIDTH, HEIGHT = 900, 1100

# The dashboard's palette, so an attachment looks like it belongs to the brand.
INK = (233, 240, 244)
DIM = (143, 163, 176)
ACCENT = (47, 216, 168)
BASE = (11, 17, 24)
PANEL = (17, 25, 34)

CARDS = [
    ("soundpods-pro", "Aurora\nSoundPods Pro", "Wireless earbuds", "AED 349", "earbuds"),
    ("soundpods-lite", "Aurora\nSoundPods Lite", "Wireless earbuds", "AED 179", "earbuds"),
    ("nomad-powerbank", "Nomad\n20000mAh", "Power bank", "AED 129", "powerbank"),
    ("halo-desk-lamp", "Halo\nSmart Desk Lamp", "Home lighting", "AED 219", "lamp"),
    ("drift-coffee-set", "Drift\nCeramic Coffee Set", "Kitchen", "AED 159", "cup"),
    ("trail-backpack", "Aurora\nTrail Backpack 28L", "Accessories", "AED 269", "bag"),
]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ["segoeuib.ttf", "arialbd.ttf"] if bold else ["segoeui.ttf", "arial.ttf"]
    for name in names:
        path = pathlib.Path(r"C:\Windows\Fonts") / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def draw_glyph(draw: ImageDraw.ImageDraw, kind: str, box: tuple[int, int, int, int]) -> None:
    """A simple line drawing of the product, so the cards are distinguishable."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    w = x1 - x0
    line = 9

    if kind == "earbuds":
        for side in (-1, 1):
            ox = cx + side * w // 5
            draw.ellipse([ox - 62, cy - 96, ox + 62, cy + 28], outline=ACCENT, width=line)
            draw.rounded_rectangle(
                [ox - 20, cy + 10, ox + 20, cy + 132], radius=18, outline=ACCENT, width=line
            )
    elif kind == "powerbank":
        draw.rounded_rectangle(
            [cx - 108, cy - 150, cx + 108, cy + 150], radius=28, outline=ACCENT, width=line
        )
        draw.polygon(
            [(cx + 18, cy - 74), (cx - 34, cy + 14), (cx + 4, cy + 14),
             (cx - 18, cy + 82), (cx + 36, cy - 8), (cx - 2, cy - 8)],
            outline=ACCENT,
        )
    elif kind == "lamp":
        draw.line([(cx, cy + 150), (cx, cy - 40)], fill=ACCENT, width=line)
        draw.line([(cx, cy - 40), (cx + 96, cy - 116)], fill=ACCENT, width=line)
        draw.polygon(
            [(cx + 52, cy - 172), (cx + 148, cy - 108), (cx + 96, cy - 60)],
            outline=ACCENT,
        )
        draw.line([(cx - 88, cy + 150), (cx + 88, cy + 150)], fill=ACCENT, width=line)
    elif kind == "cup":
        draw.rounded_rectangle(
            [cx - 96, cy - 78, cx + 60, cy + 104], radius=16, outline=ACCENT, width=line
        )
        draw.arc([cx + 40, cy - 40, cx + 148, cy + 60], 280, 80, fill=ACCENT, width=line)
        draw.line([(cx - 130, cy + 148), (cx + 130, cy + 148)], fill=ACCENT, width=line)
    else:  # bag
        draw.rounded_rectangle(
            [cx - 108, cy - 66, cx + 108, cy + 156], radius=30, outline=ACCENT, width=line
        )
        draw.arc([cx - 62, cy - 158, cx + 62, cy - 6], 180, 360, fill=ACCENT, width=line)
        draw.line([(cx - 108, cy + 44), (cx + 108, cy + 44)], fill=ACCENT, width=line)


def render(slug: str, title: str, category: str, price: str, glyph: str) -> pathlib.Path:
    card = Image.new("RGB", (WIDTH, HEIGHT), BASE)
    draw = ImageDraw.Draw(card)

    margin = 46
    draw.rounded_rectangle(
        [margin, margin, WIDTH - margin, HEIGHT - margin], radius=34, fill=PANEL
    )
    draw_glyph(draw, glyph, (margin, 210, WIDTH - margin, 620))

    draw.text((92, 118), category.upper(), font=font(30, bold=True), fill=ACCENT)
    draw.multiline_text((92, 720), title, font=font(62, bold=True), fill=INK, spacing=14)
    draw.text((92, 900), price, font=font(72, bold=True), fill=ACCENT)
    draw.text((92, 992), "Aurora Retail", font=font(30), fill=DIM)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{slug}.png"
    card.save(path, optimize=True)
    return path


def main() -> int:
    for slug, title, category, price, glyph in CARDS:
        path = render(slug, title, category, price, glyph)
        print(f"  {path.name}  ({path.stat().st_size // 1024} KB)")
    print(f"\n{len(CARDS)} cards written to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
