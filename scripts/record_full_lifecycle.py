"""Record the Retail + SaaS lifecycle demo against the running stack, live.

Three scenes, one per behaviour the product is sold on:

  1. Retail  — a product question answered with a real price and a photo.
  2. SaaS    — a tier question answered with the real plan price, in dollars.
  3. Booking — "can we book a call?" answered with a calendar link and nothing
               else. This is the scene that regressed: it used to come back
               with a product FAQ.

Every message is sent one at a time, on camera, and each reply is whatever the
live pipeline generated at that moment — nothing is pre-generated. Provider
latency varies a lot on these keys, so this script does not try to hit a target
runtime; `edit_demo_video.py` re-times the finished capture instead.

Alongside the video it writes `scenes.json` (start/end second of each scene) so
the editor's captions land on real boundaries.

    python scripts/record_full_lifecycle.py

Everything is written under Drive D:.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time

import httpx
from playwright.async_api import async_playwright

BACKEND = "http://localhost:8000"
DASHBOARD = "http://localhost:3000"

# Kept on D:, alongside the rendered output.
OUT_DIR = pathlib.Path(r"D:\pingpulse\demo")
RAW_DIR = OUT_DIR / "raw"
SCENES_PATH = OUT_DIR / "scenes.json"

VIEWPORT = {"width": 1920, "height": 1080}

OPERATOR_EMAIL = os.environ.get("PINGPULSE_EMAIL", "demo@pingpulse.app")
OPERATOR_PASSWORD = os.environ.get("PINGPULSE_PASSWORD", "")

RETAIL_ORG = "Aurora Retail"
SAAS_ORG = "Lumen Analytics"
RETAIL_NUMBER = "whatsapp:+14155238886"
SAAS_NUMBER = "whatsapp:+14155238887"


class Timeline:
    """Records when each scene starts and ends, relative to the video."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.scenes: list[dict] = []
        self._open: dict | None = None

    def begin(self, key: str, title: str, caption: str) -> None:
        self.end()
        self._open = {
            "key": key,
            "title": title,
            "caption": caption,
            "start": round(time.monotonic() - self.started, 2),
        }
        print(f"  [{self._open['start']:5.1f}s] {title}")

    def end(self) -> None:
        if self._open:
            self._open["end"] = round(time.monotonic() - self.started, 2)
            self.scenes.append(self._open)
            self._open = None

    def save(self, duration: float) -> None:
        self.end()
        SCENES_PATH.write_text(
            json.dumps({"duration": duration, "scenes": self.scenes}, indent=2),
            encoding="utf-8",
        )


async def post(client: httpx.AsyncClient, url: str, *, attempts: int = 3, **kwargs):
    """POST, retrying a dropped connection.

    A scene can leave the pool idle for tens of seconds while the browser does
    its work, which is long enough for a keep-alive connection to be reset at
    the other end. httpx surfaces that as a ReadError on the *next* request,
    which killed a whole recording mid-take. Retrying re-dials.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = await client.post(url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.TransportError as exc:
            if attempt == attempts:
                raise
            print(f"    (retry {attempt}: {type(exc).__name__})")
            await asyncio.sleep(1.0)


async def send(client: httpx.AsyncClient, phone: str, to: str, text: str) -> None:
    """One inbound WhatsApp message, exactly as Twilio would deliver it.

    Blocks until the pipeline has dispatched the reply — the webhook only
    answers after commit — so the reply is on screen when this returns.
    """
    form = {
        "MessageSid": f"SM_life_{abs(hash((phone, text))) % 10**10}",
        "From": f"whatsapp:{phone}",
        "To": to,
        "Body": text,
        "ProfileName": "Sara",
        "NumMedia": "0",
    }
    started = time.monotonic()
    await post(client, f"{BACKEND}/api/v1/whatsapp/webhook", data=form)
    print(f"    ({time.monotonic() - started:4.1f}s) {text[:62]!r}")


async def open_latest_conversation(page) -> None:
    rows = page.locator("section:has-text('Conversations') button")
    if await rows.count():
        await rows.nth(0).click()


async def switch_to(api: httpx.AsyncClient, auth: dict, orgs: list[dict], name: str) -> dict:
    target = next(m["organization"] for m in orgs if m["organization"]["name"] == name)
    await post(
        api, f"{BACKEND}/api/v1/organizations/switch", headers=auth,
        json={"organization_id": target["id"]},
    )
    return target


async def show_tenant(page, name: str) -> None:
    """Move the dashboard itself onto a tenant, so the switch is visible."""
    switcher = page.locator("button, [role='button']").filter(has_text=RETAIL_ORG)
    if await switcher.count():
        await switcher.first.click()
        await page.wait_for_timeout(400)
        option = page.locator(f"text={name}")
        if await option.count():
            await option.first.click()
            return
    await page.reload(wait_until="networkidle")


async def main() -> int:
    if not OPERATOR_PASSWORD:
        print("Set PINGPULSE_EMAIL and PINGPULSE_PASSWORD first.", file=sys.stderr)
        return 2

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for stale in RAW_DIR.glob("*.webm"):
        stale.unlink()

    run = int(time.time()) % 100000
    retail_customer = f"+9715{run}01"
    saas_customer = f"+1415{run}02"

    async with httpx.AsyncClient(timeout=240) as api:
        token = (
            await api.post(
                f"{BACKEND}/api/v1/auth/login",
                json={"email": OPERATOR_EMAIL, "password": OPERATOR_PASSWORD},
            )
        ).json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        orgs = (await api.get(f"{BACKEND}/api/v1/organizations", headers=auth)).json()

        # Clear both tenants so each lead is seen arriving fresh.
        for membership in orgs:
            await api.post(
                f"{BACKEND}/api/v1/organizations/switch", headers=auth,
                json={"organization_id": membership["organization"]["id"]},
            )
            for contact in (
                await api.get(f"{BACKEND}/api/v1/crm/contacts", headers=auth)
            ).json():
                await api.delete(
                    f"{BACKEND}/api/v1/crm/contacts/{contact['id']}", headers=auth
                )

        await switch_to(api, auth, orgs, RETAIL_ORG)

        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--force-device-scale-factor=1"])
            context = await browser.new_context(
                viewport=VIEWPORT,
                record_video_dir=str(RAW_DIR),
                record_video_size=VIEWPORT,
            )
            page = await context.new_page()
            timeline = Timeline()

            # Each scene is marked as beginning once its reply is actually on
            # screen, not when the request goes out. `send` blocks until the
            # pipeline has dispatched, so marking it earlier would put the
            # caption over a still-empty console for its whole five seconds.

            # ---------------------------------------- 1. retail
            await page.goto(DASHBOARD, wait_until="networkidle")
            await page.wait_for_timeout(400)
            await page.fill('input[type="email"]', OPERATOR_EMAIL)
            await page.fill('input[type="password"]', OPERATOR_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(2000)

            await send(
                api, retail_customer, RETAIL_NUMBER,
                "Do you have wireless earbuds? Send a photo and the price.",
            )
            await open_latest_conversation(page)
            timeline.begin(
                "retail",
                "Retail — Product Query with Photo",
                "Real stock, real price, and the picture attached",
            )
            await page.wait_for_timeout(5200)
            await send(
                api, retail_customer, RETAIL_NUMBER,
                "Is cash on delivery available in Dubai?",
            )
            await page.wait_for_timeout(5200)

            # ---------------------------------------- 2. SaaS
            await switch_to(api, auth, orgs, SAAS_ORG)
            await show_tenant(page, SAAS_ORG)
            await page.wait_for_timeout(1000)
            await send(
                api, saas_customer, SAAS_NUMBER,
                "We're a 200 person company. What's the pricing for the enterprise plan?",
            )
            await open_latest_conversation(page)
            timeline.begin(
                "saas",
                "B2B SaaS — Tier Pricing, Answered Directly",
                "Same agent, different tenant, its own plans and currency",
            )
            await page.wait_for_timeout(6000)

            # ---------------------------------------- 3. booking
            await send(
                api, saas_customer, SAAS_NUMBER,
                "Can we book a call to discuss SaaS enterprise pricing?",
            )
            timeline.begin(
                "booking",
                "Call Booking Detected Instantly",
                "Calendar link straight away — no FAQs, no filler",
            )
            await page.wait_for_timeout(7000)

            timeline.end()
            duration = round(time.monotonic() - timeline.started, 2)

            video = page.video
            await context.close()
            await browser.close()

        raw_path = pathlib.Path(await video.path()) if video else None

    timeline.save(duration)

    # Self-verify: the booking reply must carry a link and no price.
    async with httpx.AsyncClient(timeout=60) as api:
        token = (
            await api.post(
                f"{BACKEND}/api/v1/auth/login",
                json={"email": OPERATOR_EMAIL, "password": OPERATOR_PASSWORD},
            )
        ).json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        orgs = (await api.get(f"{BACKEND}/api/v1/organizations", headers=auth)).json()

        for org_name, phone in ((RETAIL_ORG, retail_customer), (SAAS_ORG, saas_customer)):
            await switch_to(api, auth, orgs, org_name)
            contacts = (await api.get(f"{BACKEND}/api/v1/crm/contacts", headers=auth)).json()
            lead = next((c for c in contacts if c["phone_number"] == phone), None)
            if not lead:
                continue
            messages = (
                await api.get(
                    f"{BACKEND}/api/v1/crm/contacts/{lead['id']}/messages", headers=auth
                )
            ).json()
            replies = [m for m in messages if m["sender"] == "agent"]
            last = replies[-1]["content"] if replies else ""
            print(f"\n  [{org_name}] stage={lead.get('sales_stage')}/{lead.get('pipeline_stage')}")
            print(f"    last reply: {last[:120]}")
            if org_name == SAAS_ORG:
                print(f"    booking link present: {'http' in last.lower()}")

    print(f"\n  raw recording   : {raw_path}")
    print(f"  scene timings   : {SCENES_PATH}")
    print(f"  duration        : {duration:.1f}s across {len(timeline.scenes)} scenes")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
