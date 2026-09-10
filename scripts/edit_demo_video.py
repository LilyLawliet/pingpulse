"""Turn the raw lifecycle recording into a captioned 1080p demo.

Overlays are rendered as PNGs with Pillow and composited by ffmpeg, rather than
using ffmpeg's `drawtext`. Two reasons: drawtext needs fonts escaped into a
filter string, which breaks on Windows paths and on any punctuation in the
caption; and rendering the banner ourselves gives real control over type,
spacing and the accent rule.

Each scene gets a lower-third banner: an English title, a one-line English
caption underneath, and a step counter.

    python scripts/edit_demo_video.py

Reads D:\\pingpulse\\demo\\raw + scenes.json, and writes the finished MP4
beside them.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = pathlib.Path(r"D:\pingpulse\demo")
RAW_DIR = OUT_DIR / "raw"
WORK_DIR = OUT_DIR / "work"          # intermediate chunks, also on D:
SCENES_PATH = OUT_DIR / "scenes.json"
FINAL_PATH = OUT_DIR / "retail_saas_agent_lifecycle.mp4"

WIDTH, HEIGHT = 1920, 1080
BANNER_SECONDS = 5.5

# The raw capture's length depends on live LLM latency, which on the
# free-tier keys this runs against has been observed anywhere from 10s to 58s
# for the *same* message. Rather than guess wait times to land on a length,
# the raw capture is timed as long as it actually took and then re-timed here
# to this target — a deterministic step regardless of how the recording went.
TARGET_DURATION = 60.0
# Bounds on how much the footage will be sped up or slowed down to hit that
# target. Outside this range the material is too far from 60s for a uniform
# re-time to still look natural, so the factor is clamped and the actual
# result reported instead of silently forcing it.
MIN_RETIME_FACTOR, MAX_RETIME_FACTOR = 0.5, 2.0
# Below this much drift from the target, re-timing is skipped entirely —
# not worth the (mild) quality cost of an extra pass for a fraction of a second.
RETIME_TOLERANCE = 0.03

# The product's own palette, so the film and the app look like one thing.
INK = (233, 240, 244)
DIM = (143, 163, 176)
ACCENT = (47, 216, 168)
PANEL = (10, 16, 22)


def find_ffmpeg(name: str = "ffmpeg") -> str | None:
    """PATH, then $FFMPEG, then the copy vendored under tools/."""
    candidates: list[pathlib.Path] = []
    on_path = shutil.which(name)
    if on_path:
        candidates.append(pathlib.Path(on_path))

    configured = os.environ.get("FFMPEG")
    if configured:
        entry = pathlib.Path(configured)
        candidates += [entry, entry / f"{name}.exe", entry / name]

    candidates.append(pathlib.Path(r"D:\pingpulse\tools") / f"{name}.exe")
    candidates += sorted(pathlib.Path(r"D:\pingpulse\.tmp").glob(f"ffmpeg/**/bin/{name}.exe"))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def probe_duration(ffprobe: str, path: pathlib.Path) -> float | None:
    """Seconds of actual playable video in `path`, or None if ffprobe can't say.

    Reads the container's `format=duration`, not the video stream's own
    `stream=duration` — several webm captures here report "N/A" for the
    latter even though the container duration is perfectly well known.
    """
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    text = result.stdout.strip()
    try:
        return float(text)
    except ValueError:
        return None


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """A real typeface if the system has one, otherwise Pillow's default.

    Only read from, never written to — the font files happen to live under the
    Windows directory.
    """
    names = (
        ["segoeuib.ttf", "arialbd.ttf", "calibrib.ttf"]
        if bold
        else ["segoeui.ttf", "arial.ttf", "calibri.ttf"]
    )
    for name in names:
        path = pathlib.Path(r"C:\Windows\Fonts") / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def render_banner(index: int, total: int, title: str, caption: str, path: pathlib.Path) -> None:
    """A lower-third banner on a transparent canvas, ready to composite."""
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    title_font = load_font(46, bold=True)
    caption_font = load_font(30)
    step_font = load_font(22, bold=True)

    margin, bar_height = 90, 168
    top = HEIGHT - bar_height - 80

    # A translucent slab keeps the text readable over any part of the UI.
    draw.rounded_rectangle(
        [margin, top, WIDTH - margin, top + bar_height],
        radius=18,
        fill=PANEL + (232,),
    )
    # One accent rule on the leading edge, matching the dashboard's own.
    draw.rounded_rectangle(
        [margin, top, margin + 7, top + bar_height], radius=4, fill=ACCENT + (255,)
    )

    text_x = margin + 42
    draw.text(
        (text_x, top + 26),
        f"STEP {index} OF {total}",
        font=step_font,
        fill=ACCENT + (255,),
    )
    draw.text((text_x, top + 60), title, font=title_font, fill=INK + (255,))
    draw.text((text_x, top + 116), caption, font=caption_font, fill=DIM + (255,))

    canvas.save(path)


def build_filter(scenes: list[dict], speed_factor: float = 1.0) -> tuple[str, list[str]]:
    """A filter graph that scales to 1080p, re-times to the target length, and
    fades each banner in and out.

    `scenes` must already be in the OUTPUT timeline — i.e. `start`/`end` have
    had `speed_factor` applied — so the banner windows line up with where
    `setpts` actually puts each moment after re-timing.
    """
    inputs: list[str] = []
    retime = f",setpts=PTS*{speed_factor:.6f}" if speed_factor != 1.0 else ""
    steps = [f"[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
             f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black{retime},fps=30[base]"]

    current = "base"
    for position, scene in enumerate(scenes, start=1):
        start = scene["start"]
        end = min(scene["start"] + BANNER_SECONDS, scene["end"])
        # Loop the still for as long as the banner has to exist. A bare `-i
        # banner.png` is a single frame at t=0, so `fade=st=<start>` would find
        # nothing to fade at that moment and leave the overlay transparent for
        # the whole film — present in the filter graph, invisible on screen.
        inputs += ["-loop", "1", "-framerate", "30", "-t", f"{end:.2f}", "-i", scene["png"]]
        label = f"v{position}"
        # Half a second either side, so banners appear and leave gently.
        steps.append(
            f"[{position}:v]format=rgba,"
            f"fade=t=in:st={start:.2f}:d=0.4:alpha=1,"
            f"fade=t=out:st={max(start, end - 0.4):.2f}:d=0.4:alpha=1[o{position}]"
        )
        steps.append(
            f"[{current}][o{position}]overlay=0:0:"
            f"enable='between(t,{start:.2f},{end:.2f})'[{label}]"
        )
        current = label

    steps.append(f"[{current}]null[out]")
    return ";".join(steps), inputs


def main() -> int:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ffmpeg not found. Put it on PATH, in $FFMPEG, or at "
              r"D:\pingpulse\tools\ffmpeg.exe", file=sys.stderr)
        return 2

    if not SCENES_PATH.is_file():
        print(f"{SCENES_PATH} is missing — run record_full_lifecycle.py first.",
              file=sys.stderr)
        return 2

    captures = sorted(RAW_DIR.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    if not captures:
        print(f"no recording in {RAW_DIR} — run record_full_lifecycle.py first.",
              file=sys.stderr)
        return 2
    source = captures[-1]

    scenes = json.loads(SCENES_PATH.read_text(encoding="utf-8"))["scenes"]
    if not scenes:
        print("scenes.json has no scenes", file=sys.stderr)
        return 2

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for stale in WORK_DIR.glob("*.png"):
        stale.unlink()

    print(f"source     : {source.name} ({source.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"scenes     : {len(scenes)}")

    # ---- work out how much to speed up or slow down the raw capture -----
    ffprobe = find_ffmpeg("ffprobe")
    source_duration = probe_duration(ffprobe, source) if ffprobe else None
    speed_factor = 1.0
    if source_duration and source_duration > 0:
        raw_factor = TARGET_DURATION / source_duration
        clamped = max(MIN_RETIME_FACTOR, min(MAX_RETIME_FACTOR, raw_factor))
        if abs(clamped - 1.0) > RETIME_TOLERANCE:
            speed_factor = clamped
        note = "" if clamped == raw_factor else f" (clamped from {raw_factor:.3f})"
        expected = source_duration * speed_factor
        direction = "sped up" if speed_factor < 1.0 else "slowed down"
        print(f"captured   : {source_duration:.1f}s -> re-timing x{speed_factor:.3f}{note} "
              f"({direction}) -> ~{expected:.1f}s")
    else:
        print("captured   : duration unknown (ffprobe unavailable) — no re-timing applied",
              file=sys.stderr)

    for index, scene in enumerate(scenes, start=1):
        png = WORK_DIR / f"banner{index}.png"
        render_banner(index, len(scenes), scene["title"], scene["caption"], png)
        scene["png"] = str(png)
        # Scale into the OUTPUT timeline so the banner still lines up with
        # its scene after setpts has compressed or stretched the footage.
        scene["start"] *= speed_factor
        scene["end"] *= speed_factor
        print(f"  {index}. {scene['title']}  [{scene['start']:.1f}s]")

    graph, inputs = build_filter(scenes, speed_factor)

    command = [
        ffmpeg, "-y", "-v", "error", "-stats",
        "-i", str(source),
        *inputs,
        "-filter_complex", graph,
        "-map", "[out]",
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-movflags", "+faststart",
        "-an",
        str(FINAL_PATH),
    ]

    print("\nencoding 1080p / 30fps / libx264 -preset slow -crf 18 ...")
    result = subprocess.run(command)
    if result.returncode != 0:
        print("ffmpeg failed", file=sys.stderr)
        return result.returncode

    size_mb = FINAL_PATH.stat().st_size / 1024 / 1024
    final_duration = probe_duration(ffprobe, FINAL_PATH) if ffprobe else None
    duration_note = f", {final_duration:.1f}s" if final_duration else ""
    print(f"\nrendered: {FINAL_PATH}  ({size_mb:.2f} MB{duration_note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
