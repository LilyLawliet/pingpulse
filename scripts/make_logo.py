"""Turn one brand image into every logo the product needs, on Drive D:.

Drop the artwork at assets/logo-source.png and run this. It writes:

  * frontend/public/logo.svg-less PNGs at the sizes the dashboard uses;
  * frontend/public/favicon.png and favicon.ico for the browser tab;
  * src-tauri/icons/* for the Windows installer, the .ico and the macOS .icns.

Two things it does to the source, both worth knowing about.

It lifts the artwork off its background. The mark supplied is brushed silver on
a light grey gradient, which is right for a letterhead and wrong for a black
console and for an app icon — pasted as-is it is a pale rectangle floating on
the dark. The background is flooded from the corners with a tolerance, so a
smooth backdrop lifts cleanly and a busy one does not; `--keep-background` skips
it when the source already has an alpha channel.

And it can invert the mark to near-white. Silver on black keeps its contrast, so
by default it does not — but a very dark source would disappear, and `--invert`
is there for that.

    python scripts/make_logo.py
    python scripts/make_logo.py --source path/to/art.png --keep-background

Nothing is written outside Drive D:.
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path

try:
    from PIL import Image, ImageChops
except ImportError:  # noqa: BLE001
    raise SystemExit("  Pillow is needed: .venv/Scripts/python.exe -m pip install pillow")

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "assets" / "logo-source.png"

WEB = REPO / "frontend" / "public"
TAURI = REPO / "src-tauri" / "icons"

# What the dashboard loads. 512 covers a retina header and the sign-in mark.
WEB_SIZES = {"logo-512.png": 512, "logo-256.png": 256, "logo-128.png": 128, "favicon.png": 64}

# Tauri's set. The names are fixed — tauri.conf.json refers to them by path.
TAURI_SIZES = {
    "32x32.png": 32, "64x64.png": 64, "128x128.png": 128, "128x128@2x.png": 256,
    "Square30x30Logo.png": 30, "Square44x44Logo.png": 44, "Square71x71Logo.png": 71,
    "Square89x89Logo.png": 89, "Square107x107Logo.png": 107, "Square142x142Logo.png": 142,
    "Square150x150Logo.png": 150, "Square284x284Logo.png": 284, "Square310x310Logo.png": 310,
    "StoreLogo.png": 50,
}
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def lift_background(image: Image.Image, tolerance: int = 38) -> Image.Image:
    """Make the backdrop transparent by flooding inward from the edges.

    A flood rather than a colour key: the backdrop here is a gradient, so every
    pixel of it is a slightly different grey and keying one value would leave a
    halo. Flooding follows the gradient as long as each step is close to its
    neighbour, and stops at the mark's edge where the step is large.

    It deliberately does not cross into the mark, so an enclosed counter — the
    hole in a D — stays filled. That is the correct result for a logo whose
    counters are part of the artwork rather than windows through it.
    """
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()

    def close(a, b) -> bool:
        return (
            abs(a[0] - b[0]) <= tolerance
            and abs(a[1] - b[1]) <= tolerance
            and abs(a[2] - b[2]) <= tolerance
        )

    seen = bytearray(width * height)
    queue = deque()
    for x in range(width):
        for y in (0, height - 1):
            queue.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            queue.append((x, y))

    while queue:
        x, y = queue.popleft()
        if not (0 <= x < width and 0 <= y < height):
            continue
        index = y * width + x
        if seen[index]:
            continue
        here = pixels[x, y]
        if here[3] == 0:
            seen[index] = 1
            continue
        # Compare against the neighbour we came from via the corner sample: a
        # gradient walks in small steps, an edge does not.
        if not close(here, pixels[0, 0]) and not close(here, pixels[width - 1, 0]):
            continue
        seen[index] = 1
        pixels[x, y] = (here[0], here[1], here[2], 0)
        queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    return image


def trim(image: Image.Image) -> Image.Image:
    """Crop to the mark, so every generated size is filled rather than padded."""
    alpha = image.getchannel("A")
    box = alpha.getbbox()
    return image.crop(box) if box else image


def square(image: Image.Image, pad: float = 0.08) -> Image.Image:
    """Centre the mark on a transparent square with a little air around it."""
    side = int(max(image.size) * (1 + pad * 2))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(
        image, ((side - image.width) // 2, (side - image.height) // 2), image
    )
    return canvas


def invert(image: Image.Image) -> Image.Image:
    """Flip the mark's luminance, keeping its alpha."""
    rgb = Image.merge("RGB", image.split()[:3])
    return Image.merge("RGBA", (*ImageChops.invert(rgb).split(), image.getchannel("A")))


def write(image: Image.Image, path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.resize((size, size), Image.LANCZOS).save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--keep-background", action="store_true",
                        help="the source already has transparency")
    parser.add_argument("--invert", action="store_true",
                        help="flip the mark to light, for a dark source")
    parser.add_argument("--tolerance", type=int, default=38)
    args = parser.parse_args()

    if not args.source.is_file():
        raise SystemExit(
            f"  no artwork at {args.source}\n"
            f"  save the logo there (PNG) and run this again"
        )

    mark = Image.open(args.source).convert("RGBA")
    print(f"  source        {args.source.name}  {mark.width}x{mark.height}")

    if not args.keep_background:
        mark = lift_background(mark, args.tolerance)
        covered = sum(1 for p in mark.getdata() if p[3] > 0) / (mark.width * mark.height)
        print(f"  background    lifted, {covered:.0%} of the frame is mark")
        if covered > 0.92:
            print("  !! almost nothing was lifted — is the backdrop smooth?"
                  " try --tolerance, or --keep-background if it is already clear")
    if args.invert:
        mark = invert(mark)

    mark = square(trim(mark))

    for name, size in WEB_SIZES.items():
        write(mark, WEB / name, size)
    print(f"  dashboard     {len(WEB_SIZES)} file(s) in frontend/public")

    for name, size in TAURI_SIZES.items():
        write(mark, TAURI / name, size)
    mark.save(TAURI / "icon.png")
    print(f"  desktop       {len(TAURI_SIZES)} file(s) in src-tauri/icons")

    mark.save(TAURI / "icon.ico", sizes=[(s, s) for s in ICO_SIZES])
    (WEB / "favicon.ico").write_bytes((TAURI / "icon.ico").read_bytes())
    print("  windows       icon.ico, favicon.ico")

    try:
        mark.save(TAURI / "icon.icns")
        print("  macos         icon.icns")
    except Exception as exc:  # noqa: BLE001 - Pillow may lack icns write support
        print(f"  !! icon.icns not written ({exc}); the existing one is unchanged")

    print("\n  Rebuild the dashboard to pick these up: cd frontend && npm run build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
