"""Seed a second, deliberately different tenant: a B2B SaaS company.

Aurora Retail is the e-commerce tenant — stock, delivery, cash on delivery. This one sells
software to businesses: monthly plans, integrations, and calls booked rather
than parcels shipped. Running both proves the agent is driven by each
organization's own data rather than anything hard-coded for clothing.

    python scripts/seed_saas_demo.py
"""

from __future__ import annotations

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
    KnowledgeDocument,
    Organization,
    OrganizationMember,
    User,
)
from app.services import retrieval  # noqa: E402

NAME = "Lumen Analytics"
DOMAIN = "https://lumenanalytics.example.com/"
# A distinct number, so inbound routing picks the tenant by destination.
CHANNEL_NUMBER = "+14155238887"

SALES_PROMPT = """You are the sales assistant for Lumen Analytics, a B2B SaaS product that
gives operations teams live dashboards over their existing databases.

You are the sales team. You answer questions yourself, right now, from the facts you are
given. You never say a colleague will follow up and you never promise a callback.

Qualify properly: company size, what tools they already use, and what they are trying to
measure. Quote exact plan prices in US dollars. If they want to talk to a person, send the
booking link you are given rather than promising anything.

Never invent a feature, an integration or a price that is not in the facts below."""

TONE = (
    "Direct, knowledgeable and unhurried. Speaks to operators and founders as peers, "
    "avoids marketing language, and is comfortable saying a feature does not exist."
)

PRODUCT_RULES = """PLANS (per month, billed monthly; annual billing is 20% less)
- Starter: USD 49 — 3 dashboards, 5 users, daily refresh, email support
- Growth: USD 199 — 25 dashboards, 25 users, hourly refresh, Slack alerts, priority support
- Scale: USD 599 — unlimited dashboards, unlimited users, 5-minute refresh, SSO, audit log
- Enterprise: custom pricing, from USD 1,500 — on-premise option, SLA, dedicated engineer
GENERAL
- 14-day free trial on any plan, no card required.
- Cancel any time; billing stops at the end of the period.
- Currency is US dollars."""

DOCUMENTS = [
    (
        "Pricing and plans",
        "Lumen Analytics has four plans billed monthly: Starter at USD 49 per month for 3 "
        "dashboards and 5 users; Growth at USD 199 per month for 25 dashboards, 25 users, "
        "hourly refresh and Slack alerts; Scale at USD 599 per month for unlimited "
        "dashboards and users, 5-minute refresh, SSO and an audit log; and Enterprise from "
        "USD 1,500 per month with on-premise deployment, an SLA and a dedicated engineer. "
        "Annual billing saves 20 percent.",
    ),
    (
        "Free trial",
        "Every plan includes a 14-day free trial with no credit card required. The trial "
        "includes the full feature set of the plan being trialled. If no plan is chosen the "
        "account moves to read-only rather than deleting any data.",
    ),
    (
        "Integrations and data sources",
        "Lumen connects to PostgreSQL, MySQL, Microsoft SQL Server, Snowflake, BigQuery, "
        "Redshift and Google Sheets. Alerts can be delivered to Slack, Microsoft Teams and "
        "email. There is a REST API and webhooks on Growth and above. There is no native "
        "Salesforce or HubSpot connector yet.",
    ),
    (
        "Security and compliance",
        "Data is encrypted in transit and at rest. Lumen is SOC 2 Type II certified and "
        "GDPR compliant. Scale and Enterprise support SAML single sign-on and an audit log. "
        "Enterprise can be deployed on-premise or in a customer's own cloud account.",
    ),
    (
        "Onboarding and support",
        "Setup takes about 30 minutes: connect a data source, pick a template, invite the "
        "team. Starter includes email support with a one business day response. Growth adds "
        "priority support. Scale and Enterprise include a named contact. Migration help from "
        "another BI tool is included on Scale and above.",
    ),
    (
        "Contracts and billing",
        "Monthly plans can be cancelled at any time and billing stops at the end of the "
        "current period. Annual plans are invoiced up front and save 20 percent. Payment is "
        "by card or, on Enterprise, by bank transfer against an invoice with 30-day terms.",
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
        organization.default_currency = "USD"
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
        await session.flush()
        print(f"knowledge: {len(DOCUMENTS)} documents")

        await session.commit()
        print("\nLumen Analytics is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
