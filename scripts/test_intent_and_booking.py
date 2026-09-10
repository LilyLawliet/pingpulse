"""Verify call-booking intent and reply conciseness against the live stack.

Two behaviours are being pinned down here, both of which were previously wrong:

  1. "I want to book a call with your team" must be read as `book_call` and
     answered with a booking link and a question about timing — NOT with a
     product FAQ or a store policy, which is what the agent used to do.
  2. "What's the pricing for the enterprise plan?" must come back with the
     real number, in one to three sentences, with no filler.

Runs against both tenants: Aurora Retail (e-commerce, AED) and Lumen
Analytics (B2B SaaS, USD).

    python scripts/test_intent_and_booking.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time

import httpx

BASE = "http://localhost:8000"
RETAIL_NUMBER = "whatsapp:+14155238886"
SAAS_NUMBER = "whatsapp:+14155238887"

OPERATOR_EMAIL = os.environ.get("PINGPULSE_EMAIL", "demo@pingpulse.app")
OPERATOR_PASSWORD = os.environ.get("PINGPULSE_PASSWORD", "")

# Anything that reads as "a human will deal with this later".
BANNED = (
    "get back to you", "our team will", "we'll contact you", "we will contact you",
    "representative will", "get in touch", "reach out to you",
)

# Filler that rule 11/12 of the sales policy exists to remove.
FILLER = (
    "great question", "happy to help", "thanks for reaching out",
    "i'd be happy", "i would be happy", "thank you for your interest",
)

AUTH: dict[str, str] = {}
results: list[bool] = []


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if passed else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    results.append(bool(passed))
    return bool(passed)


def sentence_count(text: str) -> int:
    """Content sentences.

    Two things are deliberately not counted. A URL contains dots, so a bare
    booking link would otherwise read as several sentences. And a leading
    salutation ("Hi Sara.") is a greeting, not a sentence of substance —
    counting it would fail a reply like "Hi Sara. We have X at 349 and Y at
    179. Photos attached. Which one?", which is 26 words and exactly the crisp
    answer the rule is asking for. The word cap below is what actually stops
    rambling; this counts how many points the reply makes.
    """
    body = re.sub(r"https?://\S+", "LINK", text or "").strip()
    body = re.sub(r"^(hi|hello|hey|salam|assalam[\w\-]*)\b[^.!?]{0,24}[.!,]\s*", "", body, flags=re.I)
    return len([s for s in re.split(r"[.!?]+", body) if s.strip()])


async def say(client, phone, text, to):
    form = {
        "MessageSid": f"SM_i_{abs(hash((phone, text))) % 10**10}",
        "From": f"whatsapp:{phone}",
        "To": to,
        "Body": text,
        "ProfileName": "Tester",
        "NumMedia": "0",
    }
    response = await client.post(f"{BASE}/api/v1/whatsapp/webhook", data=form)
    response.raise_for_status()
    await asyncio.sleep(0.3)


async def switch_to(client, name: str) -> None:
    orgs = (await client.get(f"{BASE}/api/v1/organizations", headers=AUTH)).json()
    target = next(m["organization"] for m in orgs if m["organization"]["name"] == name)
    await client.post(
        f"{BASE}/api/v1/organizations/switch", headers=AUTH,
        json={"organization_id": target["id"]},
    )


async def last_reply(client, phone) -> tuple[str, list[str]]:
    contacts = (await client.get(f"{BASE}/api/v1/crm/contacts", headers=AUTH)).json()
    contact = next((c for c in contacts if c["phone_number"] == phone), None)
    if not contact:
        return "", []
    messages = (
        await client.get(f"{BASE}/api/v1/crm/contacts/{contact['id']}/messages", headers=AUTH)
    ).json()
    agent = [m for m in messages if m["sender"] == "agent"]
    return (agent[-1]["content"], agent[-1].get("media_urls") or []) if agent else ("", [])


def assert_concise(reply: str, limit: int = 3) -> None:
    count = sentence_count(reply)
    check(f"concise ({count} sentences, max {limit})", count <= limit, reply[:70])
    # The real guard against rambling. A URL is one token however long it is.
    words = len(re.sub(r"https?://\S+", "LINK", reply or "").split())
    check(f"under 45 words ({words})", words <= 45, reply[:70])
    hit = next((f for f in FILLER if f in reply.lower()), None)
    check("no filler opener", hit is None, hit or "")
    hit = next((b for b in BANNED if b in reply.lower()), None)
    check("no human-handoff promise", hit is None, hit or "")


async def main() -> int:
    if not OPERATOR_PASSWORD:
        print("Set PINGPULSE_EMAIL and PINGPULSE_PASSWORD first.", file=sys.stderr)
        return 2

    run = int(time.time()) % 100000

    async with httpx.AsyncClient(timeout=240) as client:
        token = (
            await client.post(
                f"{BASE}/api/v1/auth/login",
                json={"email": OPERATOR_EMAIL, "password": OPERATOR_PASSWORD},
            )
        ).json()["access_token"]
        AUTH["Authorization"] = f"Bearer {token}"

        # ---------------------------------------------- 1. book_call, SaaS
        print("=== TEST 1 — 'I want to book a call with your team' (SaaS) ===")
        await switch_to(client, "Lumen Analytics")
        phone = f"+1415{run}11"
        await say(client, phone, "I want to book a call with your team", SAAS_NUMBER)
        reply, _ = await last_reply(client, phone)
        print(f"  agent: {reply[:220]}")
        check("sends a booking link", "http" in reply.lower(), reply[:60])
        check(
            "asks about timing",
            bool(re.search(r"\b(time|slot|when|suits|available|schedule)\b", reply, re.I)),
            reply[:60],
        )
        # The bug this test exists for: answering a booking request with the catalogue.
        check(
            "no plan prices in a booking reply",
            not re.search(r"(usd|\$)\s?\d", reply, re.I),
            reply[:60],
        )
        assert_concise(reply)

        # ---------------------------------------------- 2. book_call, retail
        print("\n=== TEST 2 — 'Can we schedule a demo?' (retail) ===")
        await switch_to(client, "Aurora Retail")
        phone = f"+9715{run}22"
        await say(client, phone, "Can we schedule a demo?", RETAIL_NUMBER)
        reply, media = await last_reply(client, phone)
        print(f"  agent: {reply[:220]}")
        check("sends a booking link", "http" in reply.lower(), reply[:60])
        check("no product photos attached to a booking reply", not media, str(len(media)))
        check(
            "no product prices in a booking reply",
            not re.search(r"aed\s?\d", reply, re.I),
            reply[:60],
        )
        assert_concise(reply)

        # ---------------------------------------------- 3. SaaS pricing
        print("\n=== TEST 3 — \"What's the pricing for the enterprise plan?\" ===")
        await switch_to(client, "Lumen Analytics")
        phone = f"+1415{run}33"
        await say(client, phone, "What's the pricing for the enterprise plan?", SAAS_NUMBER)
        reply, _ = await last_reply(client, phone)
        print(f"  agent: {reply[:220]}")
        check("quotes the real enterprise figure", "1,500" in reply or "1500" in reply, reply[:60])
        check("uses dollars not dirhams", "aed" not in reply.lower(), reply[:60])
        assert_concise(reply)

        # ---------------------------------------------- 4. retail product
        print("\n=== TEST 4 — retail product query with photo ===")
        phone = f"+9715{run}44"
        await switch_to(client, "Aurora Retail")
        await say(client, phone, "Do you have wireless earbuds? Send a photo", RETAIL_NUMBER)
        reply, media = await last_reply(client, phone)
        print(f"  agent: {reply[:220]}")
        print(f"  media: {len(media)}")
        check("quotes a real catalogue price", bool(re.search(r"(349|179)", reply)), reply[:60])
        check("attaches at least one photo", len(media) >= 1)
        assert_concise(reply)

        # ---------------------------------------------- 5. tenant isolation
        print("\n=== TEST 5 — tenants stay separate ===")
        phone = f"+1415{run}55"
        await switch_to(client, "Lumen Analytics")
        await say(client, phone, "Do you sell wireless earbuds?", SAAS_NUMBER)
        reply, _ = await last_reply(client, phone)
        print(f"  agent: {reply[:200]}")
        check(
            "SaaS tenant never quotes retail stock prices",
            not re.search(r"aed\s?\d", reply, re.I),
            reply[:60],
        )

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
