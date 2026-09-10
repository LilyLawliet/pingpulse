"""Record the PingPulse client demo.

Re-record with: python scripts/record_demo.py  (needs the stack running)

Drives the real product end to end: a browser watches the dashboard while real
Twilio-shaped webhooks arrive, so every reply in the video is genuinely
generated and dispatched by the running stack. Nothing is mocked.
"""

import asyncio
import os
import pathlib
import shutil
import subprocess
import sys

import httpx
from playwright.async_api import async_playwright

BACKEND = "http://localhost:8000"
DASHBOARD = "http://localhost:3000"
OUT_DIR = pathlib.Path(r"D:\pingpulse\demo")
FINAL_MP4 = OUT_DIR / "pingpulse-demo.mp4"

# Capture size. 1600x900 is 16:9 and encodes to yuv420p without odd dimensions.
VIEWPORT = {"width": 1600, "height": 900}


def find_ffmpeg() -> str | None:
    """PATH, then $FFMPEG, then a build unpacked under .tmp.

    $FFMPEG is accepted as either the executable or the directory holding it,
    and every candidate is checked for existence — a stale variable pointing at
    a directory that is no longer there must not win over a working binary.
    """
    candidates: list[pathlib.Path] = []

    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(pathlib.Path(on_path))

    configured = os.environ.get("FFMPEG")
    if configured:
        entry = pathlib.Path(configured)
        candidates += [entry, entry / "ffmpeg.exe", entry / "ffmpeg"]

    candidates.append(pathlib.Path(r"D:\pingpulse\tools\ffmpeg.exe"))
    candidates += sorted(pathlib.Path(r"D:\pingpulse\.tmp").glob("ffmpeg/**/bin/ffmpeg.exe"))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def encode(source: pathlib.Path, target: pathlib.Path) -> bool:
    """Re-encode the capture to a universally playable, high-clarity MP4."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ffmpeg not found — leaving the .webm capture in place", file=sys.stderr)
        return False

    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-i", str(source),
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            # Resample to a constant 30 fps so playback never stutters.
            "-vf", "fps=30",
            "-r", "30",
            "-movflags", "+faststart",
            "-an",
            str(target),
        ],
        check=True,
    )
    return True

# Three shoppers, so the pipeline visibly fills and stages advance on camera.
# Each script ends on a vague reference ("the black pair", "the tan one") that
# only resolves if the agent is actually reading the conversation — which is
# the point being demonstrated.
SHOPPERS = [
    {
        "phone": "+923004471290",
        "name": "Zainab",
        "script": [
            "Hi! Do you have white designer sneakers in size 39?",
            "How much are they?",
            "Lovely, can you book a pair for me to collect?",
        ],
    },
    {
        "phone": "+923218855012",
        "name": "Bilal",
        "script": [
            "Salam, do you deliver to Lahore?",
            "What's the price range for men's formal shoes?",
            "The tan one sounds good, size 43?",
        ],
    },
    {
        "phone": "+923337712004",
        "name": "Hina",
        "script": [
            "Are the heels real leather?",
            "I love the black pair, how much are they?",
            "Perfect, I want to buy them.",
        ],
    },
]


async def send(client: httpx.AsyncClient, phone: str, name: str, body: str) -> None:
    await client.post(
        f"{BACKEND}/api/v1/whatsapp/webhook",
        data={
            "MessageSid": f"SM_demo_{abs(hash((phone, body))) % 10**12}",
            "AccountSid": os.environ.get("TWILIO_ACCOUNT_SID", "ACdemo"),
            "From": f"whatsapp:{phone}",
            "To": "whatsapp:+14155238886",
            "Body": body,
            "ProfileName": name,
            "NumMedia": "0",
        },
    )


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--force-device-scale-factor=1"])
        context = await browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(OUT_DIR),
            record_video_size=VIEWPORT,
        )
        page = await context.new_page()
        await page.goto(DASHBOARD, wait_until="networkidle")
        await page.wait_for_timeout(2600)

        async with httpx.AsyncClient(timeout=120) as client:
            # Opening beat: let the viewer take in the console at rest.
            await page.wait_for_timeout(1800)

            # Round 1 — first contact from each shopper, staggered so the
            # conversation list fills one row at a time.
            for shopper in SHOPPERS:
                await send(client, shopper["phone"], shopper["name"], shopper["script"][0])
                await page.wait_for_timeout(2600)

            # Round 2 — follow-ups that push people down the funnel.
            for index in range(1, 3):
                for shopper in SHOPPERS:
                    if index < len(shopper["script"]):
                        await send(
                            client, shopper["phone"], shopper["name"], shopper["script"][index]
                        )
                        await page.wait_for_timeout(2800)

            # Close: walk the three conversations so each thread is readable.
            await page.wait_for_timeout(1200)
            rows = page.locator("section:has-text('Conversations') button")
            for i in range(min(3, await rows.count())):
                await rows.nth(i).click()
                await page.wait_for_timeout(2400)

            await page.wait_for_timeout(1500)

        await context.close()
        await browser.close()

    captures = sorted(OUT_DIR.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    if not captures:
        print("no capture produced", file=sys.stderr)
        return

    capture = captures[-1]
    if encode(capture, FINAL_MP4):
        for leftover in OUT_DIR.glob("*.webm"):
            leftover.unlink()
        size_mb = FINAL_MP4.stat().st_size / 1024 / 1024
        print(f"{FINAL_MP4}  ({size_mb:.2f} MB)")
    else:
        print(capture)


if __name__ == "__main__":
    asyncio.run(main())
