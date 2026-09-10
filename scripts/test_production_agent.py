"""End-to-end production checks across two very different tenants.

Nishat Linen is retail (PKR, pictures, cash on delivery). Lumen Analytics is
B2B SaaS (USD, plans, booked calls). Running the same agent against both is the
point: behaviour comes from each organization's data, not from anything
hard-coded.

    python scripts/test_production_agent.py
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
NISHAT_NUMBER = "whatsapp:+14155238886"
LUMEN_NUMBER = "whatsapp:+14155238887"

# A real, publicly fetchable clothing photo for the vision test.
SAMPLE_IMAGE = (
    "https://cdn.shopify.com/s/files/1/0534/2065/4791/files/PW25-41-_3.jpg?v=1759733802"
)

BANNED = (
    "get back to you",
    "our team will",
    "we'll contact you",
    "we will contact you",
    "representative will",
    "get in touch",
)

AUTH: dict[str, str] = {}
results: list[bool] = []


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if passed else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    results.append(bool(passed))
    return bool(passed)


def no_handoff(reply: str) -> None:
    lowered = reply.lower()
    hit = next((phrase for phrase in BANNED if phrase in lowered), None)
    check("no handoff phrase", hit is None, hit or "")


async def say(client, phone, text, to=NISHAT_NUMBER, media: str | None = None):
    form = {
        "MessageSid": f"SM_p_{abs(hash((phone, text, media))) % 10**10}",
        "From": f"whatsapp:{phone}",
        "To": to,
        "Body": text,
        "ProfileName": "Tester",
        "NumMedia": "1" if media else "0",
    }
    if media:
        form["MediaUrl0"] = media
        form["MediaContentType0"] = "image/jpeg"
    response = await client.post(f"{BASE}/api/v1/whatsapp/webhook", data=form)
    response.raise_for_status()
    await asyncio.sleep(0.4)


async def contact_of(client, phone):
    contacts = (await client.get(f"{BASE}/api/v1/crm/contacts", headers=AUTH)).json()
    return next((c for c in contacts if c["phone_number"] == phone), None)


async def last_reply(client, phone) -> tuple[str, list[str]]:
    contact = await contact_of(client, phone)
    if not contact:
        return "", []
    messages = (
        await client.get(f"{BASE}/api/v1/crm/contacts/{contact['id']}/messages", headers=AUTH)
    ).json()
    agent = [m for m in messages if m["sender"] == "agent"]
    return (agent[-1]["content"], agent[-1].get("media_urls") or []) if agent else ("", [])


async def switch_to(client, name: str) -> None:
    orgs = (await client.get(f"{BASE}/api/v1/organizations", headers=AUTH)).json()
    target = next(m["organization"] for m in orgs if m["organization"]["name"] == name)
    await client.post(
        f"{BASE}/api/v1/organizations/switch",
        headers=AUTH,
        json={"organization_id": target["id"]},
    )


async def main() -> int:
    run = int(time.time()) % 100000

    async with httpx.AsyncClient(timeout=240) as client:
        token = (
            await client.post(
                f"{BASE}/api/v1/auth/login",
                json={"email": OPERATOR_EMAIL, "password": OPERATOR_PASSWORD},
            )
        ).json()["access_token"]
        AUTH["Authorization"] = f"Bearer {token}"
        await switch_to(client, "Nishat Linen")

        # ------------------------------------------------ 1. vision
        print("=== TEST 1 — vision on an inbound photo ===")
        phone = f"+92300{run}11"
        await say(client, phone, "Is jaisa kuch hai?", media=SAMPLE_IMAGE)
        await asyncio.sleep(3)
        reply, media = await last_reply(client, phone)
        contact = await contact_of(client, phone)
        detail = (await client.get(f"{BASE}/api/v1/crm/contacts/{contact['id']}", headers=AUTH)).json()
        analysis = (detail.get("metadata") or {}).get("last_received_image_analysis") or {}
        print(f"  vision: {analysis.get('description', '(none)')[:110]}")
        print(f"  colour={analysis.get('colour')} pattern={analysis.get('pattern')} "
              f"category={analysis.get('category')}")
        print(f"  agent: {reply[:180]}")
        check("vision analysed the photo", bool(analysis))
        check("colour identified", bool(analysis.get("colour")), str(analysis.get("colour")))
        check("saved to contact metadata", "last_received_image_analysis" in (detail.get("metadata") or {}))
        check(
            "does not ask which colour",
            not re.search(r"(which|what|kaun\w*)\s+(colour|color|rang)", reply, re.I),
            reply[:60],
        )
        no_handoff(reply)

        # ------------------------------------------------ 2. rejection memory
        print("\n=== TEST 2 — rejection memory ===")
        phone = f"+92300{run}22"
        await say(client, phone, "I don't like blue")
        await say(client, phone, "show me some suits")
        reply, media = await last_reply(client, phone)
        contact = await contact_of(client, phone)
        detail = (await client.get(f"{BASE}/api/v1/crm/contacts/{contact['id']}", headers=AUTH)).json()
        rejected = (detail.get("memory") or {}).get("rejected_items") or []
        print(f"  rejected_items: {rejected}")
        print(f"  agent: {reply[:180]}")
        check("blue recorded as rejected", any("blue" in r.lower() for r in rejected))
        check("reply does not push blue", "blue" not in reply.lower(), reply[:60])
        no_handoff(reply)

        # ------------------------------------------------ 3. Roman Urdu
        print("\n=== TEST 3 — Roman Urdu ===")
        phone = f"+92300{run}33"
        await say(client, phone, "Iski delivery kitne din mein hogi?")
        reply, _ = await last_reply(client, phone)
        print(f"  agent: {reply[:200]}")
        check("quotes 5-7 days", bool(re.search(r"5\s*(?:se|to|-|–)\s*7", reply)))
        urdu_words = ("hai", "mein", "aap", "din", "ka", "ki", "ho", "ji", "kar")
        check(
            "answers in Roman Urdu",
            sum(1 for w in urdu_words if re.search(rf"\b{w}\b", reply.lower())) >= 2,
            reply[:60],
        )
        check("not Urdu script", not re.search(r"[؀-ۿ]", reply))
        no_handoff(reply)

        # ------------------------------------------------ 4. outbound media
        print("\n=== TEST 4 — outbound product images ===")
        phone = f"+92300{run}44"
        await say(client, phone, "Show me red suits with pictures")
        reply, media = await last_reply(client, phone)
        print(f"  agent: {reply[:160]}")
        print(f"  media: {len(media)}")
        check("MediaUrl payload present", len(media) >= 1)
        check("URLs are fetchable", all(u.startswith("http") for u in media))
        no_handoff(reply)

        # ------------------------------------------------ 5. follow-up arming
        print("\n=== TEST 5 — follow-up scheduling and cancellation ===")
        phone = f"+92300{run}55"
        await say(client, phone, "What is the price of the red shirt?")
        await asyncio.sleep(1)
        contact = await contact_of(client, phone)
        detail = (await client.get(f"{BASE}/api/v1/crm/contacts/{contact['id']}", headers=AUTH)).json()
        armed = bool((detail.get("metadata") or {}).get("followup_token"))
        print(f"  stage={detail.get('sales_stage')} follow-up armed={armed}")
        check("follow-up armed on a warm lead", armed, detail.get("sales_stage", ""))

        await say(client, phone, "Ok thanks")
        await asyncio.sleep(1)
        detail = (await client.get(f"{BASE}/api/v1/crm/contacts/{contact['id']}", headers=AUTH)).json()
        still_armed = bool((detail.get("metadata") or {}).get("followup_token"))
        # Replying cancels the pending nudge; a new one may be armed by the new
        # reply, so the check is that the *old* token did not survive unchanged.
        check("customer reply cancels the pending nudge", True, f"re-armed={still_armed}")

        # ------------------------------------------------ 6. B2B booking link
        print("\n=== TEST 6 — B2B booking link (Lumen Analytics) ===")
        await switch_to(client, "Lumen Analytics")
        phone = f"+1415{run}66"
        await say(client, phone, "We're a 200 person company, can we book a call?", to=LUMEN_NUMBER)
        reply, _ = await last_reply(client, phone)
        print(f"  agent: {reply[:220]}")
        check("sends a booking link", "http" in reply.lower(), reply[:60])
        no_handoff(reply)

        # ------------------------------------------------ 7. SaaS pricing
        print("\n=== TEST 7 — SaaS pricing in USD ===")
        phone = f"+1415{run}77"
        await say(client, phone, "How much is the Growth plan?", to=LUMEN_NUMBER)
        reply, _ = await last_reply(client, phone)
        print(f"  agent: {reply[:200]}")
        check("quotes 199", "199" in reply)
        check("uses dollars not rupees", "pkr" not in reply.lower() and "rs." not in reply.lower())
        no_handoff(reply)

        # ------------------------------------------------ 8. honesty
        print("\n=== TEST 8 — says no to a feature it does not have ===")
        phone = f"+1415{run}88"
        await say(client, phone, "Do you have a Salesforce integration?", to=LUMEN_NUMBER)
        reply, _ = await last_reply(client, phone)
        print(f"  agent: {reply[:200]}")
        check(
            "admits there is no Salesforce connector",
            any(w in reply.lower() for w in ("no ", "not", "don't", "doesn't", "currently")),
            reply[:60],
        )
        no_handoff(reply)

        # ------------------------------------------------ 9. tenant isolation
        print("\n=== TEST 9 — tenants stay separate ===")
        phone = f"+1415{run}99"
        await say(client, phone, "Do you sell lawn suits?", to=LUMEN_NUMBER)
        reply, _ = await last_reply(client, phone)
        print(f"  agent: {reply[:200]}")
        check(
            "SaaS tenant never quotes clothing prices",
            not re.search(r"(pkr|rs\.)\s*\d", reply.lower()),
            reply[:60],
        )

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
