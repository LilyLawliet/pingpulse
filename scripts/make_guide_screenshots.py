"""Capture the screenshots used in the user guide.

Runs against a **local** stack seeded with the two synthetic demo tenants, and
never against production. That is not a convenience - the production database
holds real customers' names, phone numbers and conversations, and a guide is a
document that gets forwarded. There is no version of this worth shipping that
has a real person's WhatsApp thread in it.

    docker compose up -d                     # backend on :8010, dashboard on :3010
    python scripts/seed_retail_demo.py
    python scripts/make_guide_screenshots.py

Writes only to D:\\pingpulse\\docs\\images.
"""

from __future__ import annotations

import asyncio
import os
import re
import pathlib
import sys

from playwright.async_api import async_playwright

DASHBOARD = os.environ.get("GUIDE_DASHBOARD", "http://localhost:3010")
OUT_DIR = pathlib.Path(r"D:\pingpulse\docs\images\guide")
VIEWPORT = {"width": 1680, "height": 1050}

# Pinned rather than generated per run. The dashboard mints a device id on
# first load and every distinct one claims a licence seat, so a script that
# starts from a clean profile each time burns a seat per run and starts
# answering 403 around the ninth.
DEVICE_ID = "guide-screenshots-fixed-device"


async def settle(page, ms: int = 900) -> None:
    """Let the network go quiet and animations finish before shooting."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001 - a busy socket must not stop the run
        pass
    await page.wait_for_timeout(ms)


async def shoot(page, name: str) -> bool:
    await settle(page)
    path = OUT_DIR / f"{name}.png"
    await page.screenshot(path=str(path))
    size = path.stat().st_size
    print(f"  {name:<28} {size // 1024:>4} KB")
    return size > 0


async def click_text(page, text: str, timeout: int = 4000) -> bool:
    """Click the thing that says this, reporting rather than raising.

    Tries the button role first. The navigation is a row of buttons and the
    same words also appear in body copy, so matching on text alone picks a
    paragraph and then times out waiting for it to be clickable.
    """
    # Exact first. "Alerts" is a setup step and "Alerts off" is a chip in the
    # header, so a loose match picks the chip and shoots the wrong screen.
    for attempt in (
        page.get_by_role("button", name=text, exact=True),
        page.get_by_role("button", name=text, exact=False),
        page.get_by_text(text, exact=False),
    ):
        try:
            await attempt.first.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            continue
    print(f"  !! could not find {text!r}")
    return False


async def click_step(page, label: str) -> bool:
    """Click a setup step by name, without hitting the header behind it.

    The workspace header stays mounted under the setup page, and one of its
    chips is "Alerts off" - so matching "Alerts" loosely toggles alerts and
    shoots the wrong screen. A step's accessible name is its number and its
    label, optionally followed by "optional", so anchoring at the end tells
    the two apart.
    """
    pattern = re.compile(
        rf"(^|\s){re.escape(label)}(\s*optional)?\s*$", re.IGNORECASE
    )
    try:
        await page.get_by_role("button", name=pattern).first.click(timeout=4000)
        return True
    except Exception:  # noqa: BLE001
        print(f"  !! could not find the {label!r} step")
        return False


async def dismiss(page) -> None:
    """Close whatever overlay is open, however it prefers to be closed."""
    for closer in (
        page.get_by_role("button", name="Close"),
        page.locator("[aria-label^='Close']"),
    ):
        try:
            if await closer.count():
                await closer.first.click(timeout=2000)
                await settle(page, 600)
                return
        except Exception:  # noqa: BLE001
            pass
    await page.keyboard.press("Escape")
    await settle(page, 600)


async def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GUIDE_TOKEN", "")
    if not token:
        print("usage: python scripts/make_guide_screenshots.py <access-token>")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    taken: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        # Scale 1, not 2. At 1680px wide these land in an A4 text column at
        # roughly 240dpi, which is past what print resolves - and doubling it
        # made the guide fifteen times the size of every other client document
        # for no visible gain.
        context = await browser.new_context(
            viewport=VIEWPORT, device_scale_factor=1
        )
        # Set before the app's first script runs, so it never mints its own.
        await context.add_init_script(
            f"try {{ localStorage.setItem('pingpulse.device', '{DEVICE_ID}'); }} catch (e) {{}}"
        )
        page = await context.new_page()

        print("sign in")
        await page.goto(DASHBOARD, wait_until="domcontentloaded")
        await settle(page)
        if await page.locator("input[placeholder^='pp_live']").count():
            await shoot(page, "01-sign-in")
            taken.append("01-sign-in")
            await page.fill("input[placeholder^='pp_live']", token)
            await page.keyboard.press("Enter")
            await settle(page, 2500)

        print("workspace")
        for name in ("02-workspace",):
            if await shoot(page, name):
                taken.append(name)

        # The setup page, step by step - the spine of the guide.
        print("setup")
        if await click_text(page, "Setup"):
            await settle(page, 1200)
            if await shoot(page, "03-setup-business"):
                taken.append("03-setup-business")
            for index, (label, name) in enumerate(
                [
                    ("Connect WhatsApp", "04-setup-whatsapp"),
                    ("Prices and knowledge", "05-setup-knowledge"),
                    ("Hours and booking", "06-setup-hours"),
                    ("Alerts", "07-setup-alerts"),
                    ("Your board", "08-setup-board"),
                    ("Learning", "09-setup-learning"),
                    ("Problems", "10-setup-problems"),
                ]
            ):
                if await click_step(page, label):
                    await settle(page, 800)
                    if await shoot(page, name):
                        taken.append(name)
            await dismiss(page)

        print("a real conversation")
        try:
            row = page.locator("button, li, div[role='button']").filter(
                has_text="Maryam"
            )
            if await row.count():
                await row.first.click(timeout=4000)
                await settle(page, 1500)
                if await shoot(page, "11-conversation"):
                    taken.append("11-conversation")
        except Exception:  # noqa: BLE001
            print("  !! could not open a conversation")

        print("the rest")
        for label, name in [
            ("Board", "12-board"),
            ("Analytics", "13-analytics"),
            ("Try it", "14-try-it"),
            ("What's new", "15-whats-new"),
        ]:
            if await click_text(page, label):
                await settle(page, 1800)
                if await shoot(page, name):
                    taken.append(name)
                await dismiss(page)

        await browser.close()

    print(f"\n{len(taken)} screenshot(s) in {OUT_DIR}")
    return 0 if taken else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
