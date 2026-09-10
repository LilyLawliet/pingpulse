"""Reset the database to a single Nishat Linen tenant and ingest its knowledge.

What it does, in order:
  1. Deletes the test organizations, their contacts and their conversations.
  2. Creates (or updates) the Nishat Linen organization.
  3. Re-points every existing WhatsApp channel binding at it, so the numbers
     already configured with Twilio keep working untouched.
  4. Loads the operational policy facts and the live product catalogue into the
     knowledge base, tagged with the organization id.

    python scripts/seed_nishat_linen.py [--products 120] [--keep-contacts]

Safe to run repeatedly: knowledge is replaced, not duplicated.
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
from app.models import (  # noqa: E402
    ChannelConfig,
    CRMContact,
    KnowledgeDocument,
    Organization,
    OrganizationMember,
    User,
)
from app.services import ingestion, retrieval  # noqa: E402

NAME = "Nishat Linen"
DOMAIN = "https://nishatlinen.com/"

SALES_PROMPT = """You are the WhatsApp sales assistant for Nishat Linen, a Pakistani clothing brand.

You are the shop. You answer the customer yourself, right now, using the facts you are given.
You never tell a customer that a human will follow up, and you never say you will get back to
them later — if you do not know something, say what you do know and offer the nearest option.

Answer the customer's actual question first. Then, when it is useful, name a specific product
and its exact price in rupees, and offer to show pictures or take the order. Quote prices only
from the facts provided; never invent a price, a fabric, a size or a delivery promise.

Customers write in English, Urdu or Roman Urdu — reply in whichever they used."""

TONE = (
    "Warm, courteous and efficient, like a well-trained retail assistant in Lahore. "
    "Uses simple English, understands Roman Urdu, never pushy."
)

PRODUCT_RULES = """GENERAL
- Currency is Pakistani rupees (Rs. / PKR).
- Delivery: 5 to 7 working days nationwide across Pakistan; up to 7 working days during mega sale events.
- Payment: Cash on Delivery anywhere in Pakistan, Visa/Mastercard credit or debit card online, and Tabby (buy now, pay later in 4 interest-free installments).
- Orders above Rs. 15,000 require advance payment and cannot be Cash on Delivery.
- Returns and exchanges within 7 days of delivery, unused and unwashed with tags attached.
- Delivery charges are shown at checkout before the order is confirmed."""


async def wipe_test_data(session, keep_contacts: bool) -> dict[str, int]:
    """Remove the demo tenants. Channel bindings are deliberately left alone."""
    removed = {"organizations": 0, "contacts": 0, "users": 0}

    organizations = (await session.execute(select(Organization))).scalars().all()
    for organization in organizations:
        if organization.name == NAME:
            continue
        # Contacts, messages, llm_logs and knowledge cascade from the org.
        await session.delete(organization)
        removed["organizations"] += 1

    if not keep_contacts:
        result = await session.execute(delete(CRMContact))
        removed["contacts"] = result.rowcount or 0

    # Accounts with no membership left are demo leftovers.
    for user in (await session.execute(select(User))).scalars().all():
        memberships = (
            await session.execute(
                select(OrganizationMember).where(OrganizationMember.user_id == user.id)
            )
        ).scalars().all()
        if not memberships and user.email.endswith("@example.com"):
            await session.delete(user)
            removed["users"] += 1

    await session.flush()
    return removed


async def upsert_organization(session) -> Organization:
    existing = (
        await session.execute(select(Organization).where(Organization.name == NAME))
    ).scalar_one_or_none()

    organization = existing or Organization(name=NAME, sales_prompt=SALES_PROMPT)
    organization.sales_prompt = SALES_PROMPT
    organization.target_tone = TONE
    organization.product_rules = PRODUCT_RULES
    organization.default_currency = "PKR"
    # English by default; the prompt tells the agent to answer in Urdu or Roman
    # Urdu whenever the customer writes that way.
    organization.default_language = "en"
    organization.primary_domain = DOMAIN

    if existing is None:
        session.add(organization)
    await session.flush()
    return organization


async def rebind_channels(session, organization: Organization) -> list[str]:
    """Keep the same WhatsApp numbers, pointed at the new organization."""
    channels = (await session.execute(select(ChannelConfig))).scalars().all()
    for channel in channels:
        channel.organization_id = organization.id
        channel.is_active = True
    await session.flush()
    return [c.phone_number for c in channels]


async def reattach_owners(session, organization: Organization) -> int:
    """Every real account keeps access, so the dashboard still opens."""
    users = (await session.execute(select(User))).scalars().all()
    attached = 0
    for user in users:
        membership = (
            await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.user_id == user.id,
                    OrganizationMember.organization_id == organization.id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            session.add(
                OrganizationMember(
                    organization_id=organization.id, user_id=user.id, role="OWNER"
                )
            )
            attached += 1
        user.active_organization_id = organization.id
    await session.flush()
    return attached


async def load_knowledge(session, organization: Organization, max_products: int) -> dict[str, int]:
    """Replace this organization's knowledge base. Idempotent by design."""
    await session.execute(
        delete(KnowledgeDocument).where(
            KnowledgeDocument.organization_id == organization.id
        )
    )
    await session.flush()

    counts = {"policies": 0, "products": 0}

    for document in ingestion.nishat_policy_documents():
        stored = await retrieval.index_document(
            session,
            organization_id=organization.id,
            title=document["title"],
            content=document["content"],
            source=DOMAIN,
        )
        stored.doc_type = "policy"
        counts["policies"] += 1
    await session.flush()
    print(f"  policies indexed: {counts['policies']}")

    print(f"  fetching catalogue from {DOMAIN} ...")
    products = await ingestion.fetch_catalogue(DOMAIN, max_products=max_products)
    for index, product in enumerate(products, start=1):
        stored = await retrieval.index_document(
            session,
            organization_id=organization.id,
            title=product["title"],
            content=product["content"],
            source=product["source"],
        )
        stored.doc_type = "product"
        stored.media_urls = product["media_urls"]
        stored.attributes = product["attributes"]
        counts["products"] += 1
        if index % 25 == 0:
            await session.flush()
            print(f"    indexed {index}/{len(products)} products")

    await session.flush()
    return counts


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--products", type=int, default=120)
    parser.add_argument("--keep-contacts", action="store_true")
    args = parser.parse_args()

    async with SessionLocal() as session:
        print("=== cleaning up test data ===")
        removed = await wipe_test_data(session, args.keep_contacts)
        print(
            f"  removed {removed['organizations']} organization(s), "
            f"{removed['contacts']} contact(s), {removed['users']} orphan user(s)"
        )

        print("\n=== Nishat Linen organization ===")
        organization = await upsert_organization(session)
        print(f"  id: {organization.id}")
        print(f"  currency: {organization.default_currency}  domain: {organization.primary_domain}")

        numbers = await rebind_channels(session, organization)
        print(f"  channels kept: {numbers or '(none configured)'}")

        attached = await reattach_owners(session, organization)
        print(f"  owners attached: {attached}")

        print("\n=== knowledge base ===")
        counts = await load_knowledge(session, organization, args.products)
        print(f"  policies: {counts['policies']}  products: {counts['products']}")

        await session.commit()
        print(f"\nDone. Organization {organization.id} is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
