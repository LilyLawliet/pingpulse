"""Give the demo recordings their own operator account.

Recording the lifecycle needs a login the scripts can perform unattended. Using
a real person's account would mean either knowing their password or resetting
it, and it would put their personal email on screen in a client video. So this
provisions a separate account instead, an OWNER of every existing organization,
and leaves real accounts untouched.

    docker exec -i pingpulse-backend python - < scripts/provision_demo_operator.py

Idempotent: re-running resets the demo password and tops up memberships.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Organization, OrganizationMember, User
from app.security import hash_password

EMAIL = os.environ.get("DEMO_OPERATOR_EMAIL", "demo@pingpulse.app")
PASSWORD = os.environ.get("DEMO_OPERATOR_PASSWORD", "PingPulseDemo!2026")
FULL_NAME = "PingPulse Demo"
PREFERRED_ORG = "Aurora Retail"


async def main() -> None:
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == EMAIL))
        ).scalar_one_or_none()

        if user is None:
            user = User(email=EMAIL, full_name=FULL_NAME, password_hash=hash_password(PASSWORD))
            db.add(user)
            await db.flush()
            print(f"created {EMAIL}")
        else:
            user.password_hash = hash_password(PASSWORD)
            user.is_active = True
            print(f"reset password for {EMAIL}")

        organizations = (await db.execute(select(Organization))).scalars().all()
        existing = {
            m.organization_id
            for m in (
                await db.execute(
                    select(OrganizationMember).where(OrganizationMember.user_id == user.id)
                )
            ).scalars()
        }

        for organization in organizations:
            if organization.id not in existing:
                db.add(
                    OrganizationMember(
                        organization_id=organization.id, user_id=user.id, role="OWNER"
                    )
                )
                print(f"  + OWNER of {organization.name}")

        preferred = next((o for o in organizations if o.name == PREFERRED_ORG), None)
        user.active_organization_id = (preferred or organizations[0]).id if organizations else None

        await db.commit()
        print(f"active organization: {preferred.name if preferred else '(none)'}")


if __name__ == "__main__":
    asyncio.run(main())
