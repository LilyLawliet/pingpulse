"""Remove tenants that are no longer part of the supported set.

The product now supports exactly two business models — retail e-commerce
(`seed_retail_demo.py`) and B2B SaaS (`seed_saas_demo.py`). Anything else that
was seeded during development is removed here, along with everything hanging
off it: catalogue rows, policy documents, contacts, conversations and channel
bindings.

Deleting the organization cascades to its tenant-scoped tables, but the
channel binding is unbound first so the phone number can be reclaimed by the
replacement tenant rather than being deleted with the old one.

    python scripts/wipe_legacy_tenants.py

Nothing is written outside Drive D:.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import delete, func, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    ChannelConfig,
    CRMContact,
    KnowledgeDocument,
    Organization,
    User,
)

RETIRED = ("Nishat Linen",)


async def main() -> int:
    async with SessionLocal() as session:
        for name in RETIRED:
            organization = (
                await session.execute(select(Organization).where(Organization.name == name))
            ).scalar_one_or_none()
            if organization is None:
                print(f"{name}: not present, nothing to remove")
                continue

            documents = await session.scalar(
                select(func.count()).select_from(KnowledgeDocument).where(
                    KnowledgeDocument.organization_id == organization.id
                )
            )
            contacts = await session.scalar(
                select(func.count()).select_from(CRMContact).where(
                    CRMContact.organization_id == organization.id
                )
            )

            # Free the phone number before the cascade takes the binding with it.
            await session.execute(
                delete(ChannelConfig).where(
                    ChannelConfig.organization_id == organization.id
                )
            )

            # Any account pointing at this tenant needs somewhere else to land,
            # or it signs in to a dangling organization.
            for user in (await session.execute(select(User))).scalars().all():
                if user.active_organization_id == organization.id:
                    user.active_organization_id = None

            await session.delete(organization)
            await session.flush()
            print(f"{name}: removed ({documents} documents, {contacts} contacts)")

        await session.commit()

        remaining = (await session.execute(select(Organization))).scalars().all()
        print("\nremaining tenants:")
        for organization in remaining:
            print(f"  - {organization.name} ({organization.default_currency})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
