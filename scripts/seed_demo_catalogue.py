"""Give an existing organization enough product knowledge to hold a conversation.

A demo tenant with no catalogue is not a quiet demo — it is a broken-looking one.
The sales prompt tells the agent never to invent a price, a size or a stock
level, which is right, so with nothing indexed it correctly declines to answer
the first question anyone asks it. The client sees an agent that cannot talk.

This fills that gap for a tenant that already exists, and deliberately does
nothing else:

  * it does not create a channel — the client chooses Twilio or WhatsApp Web
    themselves, and whichever they connect claims their number;
  * it does not touch the sales prompt, which was set when they were onboarded
    and names their shop;
  * it does not grant anyone membership. `seed_retail_demo.py` makes every user
    an OWNER so the demo tenant can be switched into, which is fine for a
    scratch tenant on a laptop and is not fine on a server where another
    client's data lives.

Prices are written in the organization's own currency, because quoting a
customer in the wrong one is worse than quoting nothing.

    python scripts/seed_demo_catalogue.py --org "Retail Client"
    python scripts/seed_demo_catalogue.py --org "Retail Client" --show

Nothing is written outside Drive D:.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import delete, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import KnowledgeDocument, Organization  # noqa: E402
from app.services import retrieval  # noqa: E402

TONE = (
    "Warm, quick and practical. Talks like someone on the shop floor who knows the "
    "stock, gives a straight answer first, and does not pad it out."
)

# (title, price, stock, category, colour, sku, description)
PRODUCTS = [
    ("Aurora SoundPods Pro", 89, 42, "electronics", "black", "AR-SPP-001",
     "Wireless earbuds with active noise cancellation, 8 hours playback and 28 hours "
     "with the charging case, USB-C, IPX5 water resistant. Two-year warranty."),
    ("Aurora SoundPods Lite", 45, 110, "electronics", "white", "AR-SPL-002",
     "Wireless earbuds, 6 hours playback and 22 hours with the case, USB-C, IPX4. "
     "One-year warranty."),
    ("Nomad 20000mAh Power Bank", 35, 76, "electronics", "grey", "AR-PWR-003",
     "65W USB-C fast charge, charges a laptop and a phone at once, airline safe."),
    ("Halo Smart Desk Lamp", 59, 18, "home", "white", "AR-LMP-004",
     "Adjustable warm-to-cool light, touch dimming, USB-C charging port in the base, "
     "app and voice control."),
    ("Drift Ceramic Coffee Set", 42, 33, "home", "beige", "AR-COF-005",
     "Four 250ml cups and saucers, matte glaze, dishwasher and microwave safe."),
    ("Aurora Trail Backpack 28L", 69, 6, "accessories", "navy", "AR-BAG-006",
     "28 litres, padded 16-inch laptop sleeve, water-resistant shell, luggage "
     "pass-through. Low stock."),
]

POLICIES = [
    ("Delivery and shipping",
     "Orders placed before 4pm are dispatched the same day. Delivery within the city is "
     "next day; elsewhere in the country it is 2 to 3 working days. Delivery is free on "
     "orders over {c} 100, otherwise {c} 5."),
    ("Payment options",
     "Cash on delivery and card on delivery are both accepted with no extra fee, as well "
     "as online payment by Visa, Mastercard and Apple Pay. All prices include tax."),
    ("Order tracking",
     "Every order gets a tracking number beginning with AR-, sent by SMS as soon as the "
     "parcel ships. Customers can quote that number in this chat at any time to get the "
     "current status."),
    ("Returns and warranty",
     "Returns are accepted within 14 days if the item is unused and in its original "
     "packaging, refunded to the original payment method within 5 working days. "
     "Electronics carry the manufacturer warranty: two years on SoundPods Pro, one year "
     "on SoundPods Lite."),
]


def product_rules(currency: str) -> str:
    lines = [f"CATALOGUE (prices in {currency}, tax included)"]
    for title, price, stock, *_ in PRODUCTS:
        state = f"in stock ({stock})" if stock > 10 else f"low stock ({stock} left)"
        lines.append(f"- {title} — {currency} {price} — {state}")
    lines += [
        "PAYMENT",
        "- Cash on delivery and card on delivery both accepted, no extra fee.",
        "- Card online via Visa, Mastercard and Apple Pay.",
        "DELIVERY",
        "- Same-city: next day. Elsewhere: 2-3 working days.",
        f"- Free delivery over {currency} 100, otherwise {currency} 5.",
        "RETURNS",
        "- 14 days, unused and in original packaging, refund to the original method.",
        "ORDER TRACKING",
        "- Every order gets an AR- tracking number by SMS the moment it ships.",
    ]
    return "\n".join(lines)


async def find(session, name: str) -> Organization:
    organization = (
        await session.execute(select(Organization).where(Organization.name == name))
    ).scalar_one_or_none()
    if organization is None:
        raise SystemExit(
            f"  no organization named {name!r}. Onboard them first:\n"
            f"    python scripts/onboard_client.py --name {name!r} --preset retail"
        )
    return organization


async def show(name: str) -> int:
    async with SessionLocal() as session:
        organization = await find(session, name)
        documents = (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.organization_id == organization.id
                )
            )
        ).scalars().all()

        print(f"\n  {organization.name}  ({organization.default_currency})")
        print(f"    catalogue lines : {len(organization.product_rules or '')} chars")
        print(f"    indexed         : {len(documents)} document(s)")
        for document in sorted(documents, key=lambda d: (d.doc_type or "", d.title)):
            print(f"      {(document.doc_type or '?'):8} {document.title}")
    return 0


async def run(name: str) -> int:
    async with SessionLocal() as session:
        organization = await find(session, name)
        currency = organization.default_currency or "USD"

        organization.product_rules = product_rules(currency)
        if not (organization.target_tone or "").strip():
            organization.target_tone = TONE
        await session.flush()
        print(f"  organization : {organization.name} ({currency})")

        # Replaced wholesale rather than added to, so re-running does not leave
        # two versions of a price in the index for the agent to choose between.
        await session.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == organization.id
            )
        )

        for title, body in POLICIES:
            document = await retrieval.index_document(
                session, organization.id, title, body.format(c=currency), source="demo"
            )
            document.doc_type = "policy"

        for title, price, stock, category, colour, sku, description in PRODUCTS:
            state = f"In stock, {stock} units." if stock > 10 else f"Low stock, {stock} left."
            document = await retrieval.index_document(
                session,
                organization.id,
                title,
                f"{title}, {currency} {price}. {description} {state}",
                source="demo",
            )
            document.doc_type = "product"
            document.attributes = {
                "price": str(price), "currency": currency, "colour": colour,
                "category": category, "stock": stock, "sku": sku,
            }

        await session.commit()
        print(f"  knowledge    : {len(POLICIES)} policies, {len(PRODUCTS)} products")
        print(f"\n  {organization.name} can hold a conversation now.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", required=True, help="Existing organization name")
    parser.add_argument("--show", action="store_true", help="Show what is indexed")
    args = parser.parse_args()
    return asyncio.run(show(args.org) if args.show else run(args.org))


if __name__ == "__main__":
    sys.exit(main())
