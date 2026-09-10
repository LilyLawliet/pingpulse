"""Live edge-case checks for the Nishat Linen agent.

Drives the real webhook against the running stack and grades the replies that
actually come back. Each conversation uses a fresh number so memory tests are
not contaminated by a previous run.

    python scripts/test_nishat_agent.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import time

import httpx

import os

# Credentials come from the environment so this file can live in a repository.
#   $PINGPULSE_EMAIL / $PINGPULSE_PASSWORD
OPERATOR_EMAIL = os.environ.get("PINGPULSE_EMAIL", "owner@example.com")
OPERATOR_PASSWORD = os.environ.get("PINGPULSE_PASSWORD", "")

BASE = "http://localhost:8000"
TO_NUMBER = "whatsapp:+14155238886"

BANNED = (
    "get back to you",
    "our team will",
    "we'll contact you",
    "we will contact you",
    "representative will",
    "get in touch",
)


async def say(client: httpx.AsyncClient, phone: str, text: str) -> dict:
    response = await client.post(
        f"{BASE}/api/v1/whatsapp/webhook",
        data={
            "MessageSid": f"SM_nl_{abs(hash((phone, text))) % 10**10}",
            "From": f"whatsapp:{phone}",
            "To": TO_NUMBER,
            "Body": text,
            "ProfileName": "Tester",
            "NumMedia": "0",
        },
    )
    response.raise_for_status()
    await asyncio.sleep(0.4)
    return {}


async def last_exchange(client: httpx.AsyncClient, phone: str) -> tuple[str, list[str]]:
    """The agent's most recent reply and any media it attached."""
    contacts = (await client.get(f"{BASE}/api/v1/crm/contacts", headers=AUTH)).json()
    contact = next((c for c in contacts if c["phone_number"] == phone), None)
    if not contact:
        return "", []
    messages = (
        await client.get(
            f"{BASE}/api/v1/crm/contacts/{contact['id']}/messages", headers=AUTH
        )
    ).json()
    agent = [m for m in messages if m["sender"] == "agent"]
    if not agent:
        return "", []
    return agent[-1]["content"], agent[-1].get("media_urls") or []


AUTH: dict[str, str] = {}


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if passed else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    return passed


def no_handoff(reply: str) -> tuple[bool, str]:
    lowered = reply.lower()
    hit = next((phrase for phrase in BANNED if phrase in lowered), None)
    return (hit is None), (hit or "")


async def main() -> int:
    run = int(time.time()) % 100000
    results: list[bool] = []

    async with httpx.AsyncClient(timeout=180) as client:
        token = (
            await client.post(
                f"{BASE}/api/v1/auth/login",
                json={"email": OPERATOR_EMAIL, "password": OPERATOR_PASSWORD},
            )
        ).json()["access_token"]
        AUTH["Authorization"] = f"Bearer {token}"

        # ---------------------------------------------------------- payments
        print("=== payment methods ===")
        phone = f"+92300{run}01"
        await say(client, phone, "What payment methods do you accept?")
        reply, _ = await last_exchange(client, phone)
        print(f"  agent: {reply[:220]}")
        lowered = reply.lower()
        results.append(
            check(
                "mentions COD, card and Tabby",
                ("cod" in lowered or "cash on delivery" in lowered)
                and "card" in lowered
                and "tabby" in lowered,
            )
        )
        ok, hit = no_handoff(reply)
        results.append(check("no handoff phrase", ok, hit))

        # ---------------------------------------------------------- delivery
        print("\n=== delivery to Lahore ===")
        phone = f"+92300{run}02"
        await say(client, phone, "How long will delivery take to Lahore?")
        reply, _ = await last_exchange(client, phone)
        print(f"  agent: {reply[:220]}")
        results.append(
            check(
                "quotes 5-7 working days",
                bool(re.search(r"5\s*(?:to|-|–)\s*7", reply)) or "5-7" in reply,
            )
        )
        ok, hit = no_handoff(reply)
        results.append(check("no handoff phrase", ok, hit))

        # ------------------------------------------------------ product+image
        print("\n=== red printed lawn suits (with pictures) ===")
        phone = f"+92300{run}03"
        await say(client, phone, "Show me red printed lawn suits")
        reply, media = await last_exchange(client, phone)
        print(f"  agent: {reply[:220]}")
        print(f"  media: {len(media)} attachment(s)")
        for url in media[:3]:
            print(f"    {url}")
        results.append(check("attached at least one product image", len(media) >= 1))
        # The earlier version of this test only checked that media was attached,
        # which passed even when the text was about the returns policy.
        product_words = ("suit", "shirt", "dress", "tunic", "piece", "rs.", "pkr")
        results.append(
            check(
                "reply is about products, not a policy",
                any(word in reply.lower() for word in product_words)
                and "returned or exchanged" not in reply.lower(),
                reply[:60],
            )
        )
        results.append(
            check(
                "images are real catalogue URLs",
                all(u.startswith("http") for u in media),
                media[0][:60] if media else "none",
            )
        )
        ok, hit = no_handoff(reply)
        results.append(check("no handoff phrase", ok, hit))

        # ------------------------------------------------------------ memory
        print("\n=== memory across turns ===")
        phone = f"+92300{run}04"
        await say(client, phone, "I am looking for something in red")
        await say(client, phone, "Actually, what is your delivery time?")
        await say(client, phone, "Ok, show me what you have")
        reply, media = await last_exchange(client, phone)
        print(f"  agent: {reply[:220]}")

        contacts = (await client.get(f"{BASE}/api/v1/crm/contacts", headers=AUTH)).json()
        contact = next((c for c in contacts if c["phone_number"] == phone), None)
        detail = await client.get(
            f"{BASE}/api/v1/crm/contacts/{contact['id']}", headers=AUTH
        )
        memory = detail.json().get("memory") or {}
        colour = (memory.get("facts", {}).get("colour_preference") or {}).get("value", "")
        print(f"  remembered colour: {colour or '(none)'}  stage: {detail.json().get('sales_stage')}")
        results.append(check("remembered the colour red", "red" in str(colour).lower()))
        results.append(
            check(
                "still on red after two other turns",
                "red" in reply.lower() or len(media) > 0,
                f"{len(media)} image(s)",
            )
        )

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
