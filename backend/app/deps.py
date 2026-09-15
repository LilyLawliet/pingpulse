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
    TokenDevice,
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


async def claim_seat(db: AsyncSession, token: AccessToken, device_id: str) -> None:
    """Let this machine use the token, or refuse it.

    A licence covers one person and their team, so each machine claims a seat.
    A machine that already holds one keeps it; a new machine gets one only if
    the licence has a seat spare. The refusal is explicit — 403 with a real
    explanation — because the alternative is a client quietly wondering why the
    app will not open.

    Requests with no device id are allowed through so that scripts, curl and
    the health checks keep working. Seats are about stopping a licence being
    forwarded around, not about blocking every unidentified caller.
    """
    if not device_id:
        return

    existing = await db.scalar(
        select(TokenDevice).where(
            TokenDevice.token == token.token,
            TokenDevice.device_id == device_id,
        )
    )
    if existing is not None:
        existing.last_seen_at = datetime.now(timezone.utc)
        return

    claimed = await db.scalar(
        select(func.count())
        .select_from(TokenDevice)
        .where(TokenDevice.token == token.token)
    ) or 0

    if claimed >= token.max_devices:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"This licence is already in use on {token.max_devices} device"
                f"{'s' if token.max_devices != 1 else ''}. It covers one team, "
                "not unlimited machines — contact us to add seats or release one."
            ),
        )

    try:
        # A savepoint rather than a bare flush. Two requests from the same new
        # machine race for this seat and both get past the check above, and
        # that is the ordinary case rather than an edge one: the dashboard
        # fires several calls in parallel the moment it loads, so every first
        # visit from a new device runs this race and one of them used to come
        # back as a 500.
        #
        # Rolling the whole session back instead would discard the caller's
        # transaction and expire the objects already loaded on it - including
        # the token being authenticated - so only this insert is undone.
        async with db.begin_nested():
            db.add(
                TokenDevice(
                    token=token.token,
                    device_id=device_id,
                    last_seen_at=datetime.now(timezone.utc),
                )
            )
    except IntegrityError:
        # The loser has nothing to fix: the seat it wanted now exists and
        # belongs to this machine, so the claim is satisfied, not failed.
        logger.debug("device %s claimed its seat on a parallel request", device_id[:8])


async def current_token(
    authorization: str | None = Header(default=None),
    x_pingpulse_device: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> AccessToken:
    """The validated token behind this request, and its seat."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("An access token is required")

    record = await resolve_token(db, authorization.split(" ", 1)[1])
    await claim_seat(db, record, (x_pingpulse_device or "").strip()[:64])
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
