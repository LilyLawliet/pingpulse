"""Request dependencies that enforce tenancy.

Nothing tenant-owned is reached without going through `current_org`, which
resolves the caller's active organization and proves membership. Handlers then
filter on that id — they never accept an organization id from the client.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Organization, OrganizationMember, User
from app.security import decode_access_token

WRITE_ROLES = ("OWNER", "ADMIN", "AGENT")
ADMIN_ROLES = ("OWNER", "ADMIN")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("Sign in to continue")

    user_id = decode_access_token(authorization.split(" ", 1)[1].strip())
    if not user_id:
        raise _unauthorized("Your session has expired. Sign in again.")

    try:
        user = await db.get(User, uuid.UUID(user_id))
    except ValueError:
        raise _unauthorized("Your session has expired. Sign in again.")

    if user is None or not user.is_active:
        raise _unauthorized("Your session has expired. Sign in again.")
    return user


async def membership_of(
    db: AsyncSession, user: User, organization_id: uuid.UUID
) -> OrganizationMember | None:
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


class Tenant:
    """The organization this request may touch, and the caller's role in it."""

    def __init__(self, organization: Organization, role: str, user: User):
        self.organization = organization
        self.id = organization.id
        self.role = role
        self.user = user

    def require_role(self, allowed: tuple[str, ...]) -> None:
        if self.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role does not allow that",
            )


async def current_org(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Resolve the active organization and prove the caller belongs to it.

    A membership row that has since been revoked fails here, so a stale
    `active_organization_id` cannot be used to keep reading a tenant's data.
    """
    if user.active_organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active organization. Create one or switch to one first.",
        )

    membership = await membership_of(db, user, user.active_organization_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are no longer a member of that organization",
        )

    organization = await db.get(Organization, user.active_organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    return Tenant(organization=organization, role=membership.role, user=user)
