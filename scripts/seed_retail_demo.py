"""Seed the retail tenant: a general e-commerce store.

One of the two business models this product supports. The other is
`seed_saas_demo.py`. Retail means physical goods — stock levels, delivery
windows, cash on delivery, order tracking, returns. SaaS means plans, seats
and booked calls. Running both against the same agent is the point: behaviour
comes from each organization's own rows, never from anything hard-coded.

The catalogue here is self-contained rather than scraped from a live shop, so
seeding is deterministic, offline, and not tied to any one brand's website.

    python scripts/seed_retail_demo.py

Nothing is written outside Drive D:.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import delete, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    ChannelConfig,
    KnowledgeDocument,
    Organization,
    OrganizationMember,
    User,
)
from app.services import retrieval  # noqa: E402

NAME = "Aurora Retail"
DOMAIN = "https://aurora-retail.example.com/"
# The Twilio sandbox number, so inbound routing picks this tenant by destination.
CHANNEL_NUMBER = "+14155238886"
CURRENCY = "AED"

# Product images live on this deployment's own /media mount. WhatsApp fetches
# attachments over the public tunnel, so this must be the externally reachable
# base URL, not localhost.
MEDIA_BASE = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

SALES_PROMPT = """You are the sales assistant for Aurora Retail, a general online store
selling electronics, home and everyday essentials across the UAE.

You are the shop. You answer questions yourself, right now, from the facts you are given.
You never say a colleague will follow up and you never promise a callback.

Quote exact prices in dirhams and exact stock levels. Cash on delivery and card are both
accepted. If someone asks to speak to a person or book a call, send the booking link you
are given rather than promising anything.

Never invent a product, a price, a stock level or a delivery promise that is not in the
facts below."""

TONE = (
    "Warm, quick and practical. Talks like someone on the shop floor who knows the "
    "stock, gives a straight answer first, and does not pad it out."
)

PRODUCT_RULES = """CATALOGUE (prices in AED, VAT included)
- Aurora SoundPods Pro (wireless earbuds) — AED 349 — in stock (42)
- Aurora SoundPods Lite (wireless earbuds) — AED 179 — in stock (110)
- Nomad 20000mAh Power Bank — AED 129 — in stock (76)
- Halo Smart Desk Lamp — AED 219 — in stock (18)
- Drift Ceramic Coffee Set (4 cups) — AED 159 — in stock (33)
- Aurora Trail Backpack 28L — AED 269 — low stock (6)
PAYMENT
- Cash on delivery and card on delivery both accepted, no extra fee.
- Card online via Visa, Mastercard and Apple Pay.
DELIVERY
- Dubai and Sharjah: next day. Rest of the UAE: 2-3 working days.
- Free delivery over AED 200, otherwise AED 15.
RETURNS
- 14 days, unused and in original packaging, refund to the original payment method.
ORDER TRACKING
- Every order gets an AR- tracking number by SMS the moment it ships."""

# Product images are rendered by `make_catalogue_images.py` and served from the
# app's own /media mount rather than pulled from a stock-photo service. Two of
# those were tried first: a random one served a sunset for the earbuds, and a
# keyword one served a statue and a circuit board. Hosting them ourselves also
# means the demo does not depend on a third-party image host being up.
# Run `python scripts/make_catalogue_images.py` before seeding.
PRODUCTS = [
    {
        "title": "Aurora SoundPods Pro",
        "content": (
            "Aurora SoundPods Pro wireless earbuds, AED 349. Active noise cancellation, "
            "8 hours playback and 28 hours with the charging case, USB-C, IPX5 water "
            "resistant. Two-year warranty. In stock, 42 units."
        ),
        "media": f"{MEDIA_BASE}/media/catalogue/soundpods-pro.png",
        "attributes": {
            "price": "349", "currency": CURRENCY, "colour": "black",
            "category": "electronics", "stock": 42, "sku": "AR-SPP-001",
        },
    },
    {
        "title": "Aurora SoundPods Lite",
        "content": (
            "Aurora SoundPods Lite wireless earbuds, AED 179. 6 hours playback, 22 hours "
            "with the case, USB-C, IPX4. One-year warranty. In stock, 110 units."
        ),
        "media": f"{MEDIA_BASE}/media/catalogue/soundpods-lite.png",
        "attributes": {
            "price": "179", "currency": CURRENCY, "colour": "white",
            "category": "electronics", "stock": 110, "sku": "AR-SPL-002",
        },
    },
    {
        "title": "Nomad 20000mAh Power Bank",
        "content": (
            "Nomad 20000mAh power bank, AED 129. 65W USB-C fast charge, charges a laptop "
            "and a phone at once, airline safe. In stock, 76 units."
        ),
        "media": f"{MEDIA_BASE}/media/catalogue/nomad-powerbank.png",
        "attributes": {
            "price": "129", "currency": CURRENCY, "colour": "grey",
            "category": "electronics", "stock": 76, "sku": "AR-PWR-003",
        },
    },
    {
        "title": "Halo Smart Desk Lamp",
        "content": (
            "Halo smart desk lamp, AED 219. Adjustable warm-to-cool light, touch dimming, "
            "USB-C charging port in the base, app and voice control. In stock, 18 units."
        ),
        "media": f"{MEDIA_BASE}/media/catalogue/halo-desk-lamp.png",
        "attributes": {
            "price": "219", "currency": CURRENCY, "colour": "white",
            "category": "home", "stock": 18, "sku": "AR-LMP-004",
        },
    },
    {
        "title": "Drift Ceramic Coffee Set",
        "content": (
            "Drift ceramic coffee set, AED 159. Four 250ml cups and saucers, matte glaze, "
            "dishwasher and microwave safe. In stock, 33 units."
        ),
        "media": f"{MEDIA_BASE}/media/catalogue/drift-coffee-set.png",
        "attributes": {
            "price": "159", "currency": CURRENCY, "colour": "beige",
            "category": "home", "stock": 33, "sku": "AR-COF-005",
        },
    },
    {
        "title": "Aurora Trail Backpack 28L",
        "content": (
            "Aurora Trail backpack, 28 litres, AED 269. Padded 16-inch laptop sleeve, "
            "water-resistant shell, luggage pass-through. Low stock, 6 units left."
        ),
        "media": f"{MEDIA_BASE}/media/catalogue/trail-backpack.png",
        "attributes": {
            "price": "269", "currency": CURRENCY, "colour": "navy",
            "category": "accessories", "stock": 6, "sku": "AR-BAG-006",
        },
    },
]

DOCUMENTS = [
    (
        "Delivery and shipping",
        "Aurora Retail delivers next day to Dubai and Sharjah, and in 2 to 3 working days "
        "to the rest of the UAE. Delivery is free on orders over AED 200; below that it is "
        "AED 15. Orders placed before 4pm are dispatched the same day.",
    ),
    (
        "Payment options",
        "Aurora Retail accepts cash on delivery and card on delivery with no extra fee, as "
        "well as online payment by Visa, Mastercard and Apple Pay. Prices include VAT.",
    ),
    (
        "Order tracking",
        "Every Aurora Retail order gets a tracking number beginning with AR-, sent by SMS "
        "as soon as the parcel ships. Customers can quote that number here at any time to "
        "get the current status.",
    ),
    (
        "Returns and warranty",
        "Aurora Retail accepts returns within 14 days if the item is unused and in its "
        "original packaging, refunded to the original payment method within 5 working "
        "days. Electronics carry the manufacturer warranty: two years on SoundPods Pro, "
        "one year on SoundPods Lite.",
    ),
]


async def main() -> int:
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(Organization).where(Organization.name == NAME))
        ).scalar_one_or_none()

        organization = existing or Organization(name=NAME, sales_prompt=SALES_PROMPT)
        organization.sales_prompt = SALES_PROMPT
        organization.target_tone = TONE
        organization.product_rules = PRODUCT_RULES
        organization.default_currency = CURRENCY
        organization.default_language = "en"
        organization.primary_domain = DOMAIN
        if existing is None:
            session.add(organization)
        await session.flush()
        print(f"organization: {organization.name} ({organization.id})")

        # Every existing account can see it, so the dashboard can switch tenants.
        for user in (await session.execute(select(User))).scalars().all():
            member = (
                await session.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.user_id == user.id,
                        OrganizationMember.organization_id == organization.id,
                    )
                )
            ).scalar_one_or_none()
            if member is None:
                session.add(
                    OrganizationMember(
                        organization_id=organization.id, user_id=user.id, role="OWNER"
                    )
                )

        channel = (
            await session.execute(
                select(ChannelConfig).where(ChannelConfig.phone_number == CHANNEL_NUMBER)
            )
        ).scalar_one_or_none()
        if channel is None:
            session.add(
                ChannelConfig(
                    organization_id=organization.id,
                    channel="whatsapp",
                    provider="twilio",
                    phone_number=CHANNEL_NUMBER,
                )
            )
        else:
            channel.organization_id = organization.id
        await session.flush()
        print(f"channel: {CHANNEL_NUMBER}")

        await session.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == organization.id
            )
        )

        for title, content in DOCUMENTS:
            document = await retrieval.index_document(
                session, organization.id, title, content, source=DOMAIN
            )
            document.doc_type = "policy"

        for product in PRODUCTS:
            document = await retrieval.index_document(
                session, organization.id, product["title"], product["content"],
                source=DOMAIN,
            )
            document.doc_type = "product"
            document.media_urls = [product["media"]]
            document.attributes = product["attributes"]

        await session.flush()
        print(f"knowledge: {len(DOCUMENTS)} policies, {len(PRODUCTS)} products")

        await session.commit()
        print(f"\n{NAME} is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
