"""Request dependencies that enforce authentication and tenancy.

Two rules hold everything together:

  * Every request carries an access token, and that token is checked against
    the `access_tokens` table on every call. Revoking a token takes effect
    immediately rather than whenever it would have expired.
  * Nothing tenant-owned is reached without going through `current_org`, which
    resolves the caller's active organization and proves membership. Handlers
    filter on that id — they never accept an organization id from the client.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

from app.models import (
    AccessToken,
    Organization,
    OrganizationMember,
    User,
)

logger = logging.getLogger(__name__)

WRITE_ROLES = ("OWNER", "ADMIN", "AGENT")
ADMIN_ROLES = ("OWNER", "ADMIN")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def resolve_token(db: AsyncSession, raw: str | None) -> AccessToken:
    """Look a raw token up and prove it is currently usable.

    Shared by the HTTP dependency and the websocket handshake so both apply
    exactly the same three checks: it exists, it has not been revoked, and it
    has not expired.

    The failure messages are deliberately identical. Telling a caller that a
    token exists but has expired confirms the token is real, which helps anyone
    guessing at them.
    """
    token = (raw or "").strip()
    if not token:
        raise _unauthorized("An access token is required")

    record = await db.get(AccessToken, token)
    if record is None or not record.is_active:
        raise _unauthorized("That access token is not valid")

    expires_at = record.expires_at
    # SQLite hands back naive datetimes; compare in UTC either way.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise _unauthorized("That access token has expired")

    return record


async def current_token(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> AccessToken:
    """The validated token behind this request.

    A licence is the token and the date it runs out. It used to also be a
    count of machines, which cost a client an afternoon every time they opened
    the dashboard somewhere new - and cost us the truth on screen, because a
    refused machine looked exactly like a business that had not been set up.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("An access token is required")

    record = await resolve_token(db, authorization.split(" ", 1)[1])
    # Cheap operational signal: shows whether an issued token is in use.
    record.last_used_at = datetime.now(timezone.utc)
    return record


async def current_user(
    token: AccessToken = Depends(current_token),
    db: AsyncSession = Depends(get_db),
) -> User:
    """The identity the token acts as.

    The token carries the credential; the user it points at carries the
    organization membership that every tenant-scoped query is filtered by.
    """
    if token.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This token is not linked to an account. Re-issue it with an owner.",
        )

    user = await db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise _unauthorized("That access token is not valid")
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
    x_organization_id: str | None = Header(default=None),
) -> Tenant:
    """Resolve the organization this request is for and prove the caller belongs to it.

    A membership row that has since been revoked fails here, so a stale
    `active_organization_id` cannot be used to keep reading a tenant's data.

    The dashboard names the business on every request (X-Organization-Id).
    The account's active business is kept on the server for the whole
    account, so with two tabs open on two businesses, a switch in one used to
    make the other read - and save into - the business it was not showing.
    Without the header, the account's active business is used as before.
    """
    wanted = user.active_organization_id
    if x_organization_id:
        try:
            wanted = uuid.UUID(x_organization_id.strip())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="That is not a business id"
            ) from None

    if wanted is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active organization. Create one or switch to one first.",
        )

    membership = await membership_of(db, user, wanted)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are no longer a member of that organization",
        )

    organization = await db.get(Organization, wanted)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    return Tenant(organization=organization, role=membership.role, user=user)
