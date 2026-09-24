"""Subscribing to the diary from a phone.

Two endpoints with deliberately different front doors.

`/calendar/{token}.ics` is **public and unauthenticated**, because a
subscribing calendar client cannot authenticate. iOS Calendar and Google
Calendar fetch a URL every few hours for years and have no way to be prompted
for anything, so the secret has to be in the URL. That is the standard shape
for a published calendar and it is why the token is long, per organization and
rotatable in one press.

Everything else is behind the usual tenant auth.

Nothing here can change an appointment. The feed is read-only by construction,
which keeps the database the one place allowed to say a booking exists - a
calendar the business could edit would be a second answer to "am I booked?",
and having two was the fault the appointments table was built to end.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import Organization
from app.services import calendar_feed, oplog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["calendar"])


def _urls(request: Request, token: str) -> dict:
    """The same feed, in the three forms a person might need.

    `webcal://` is what makes a phone offer to subscribe rather than download
    a file, and it is the one that should be tapped. The https form is there
    for pasting into a desktop client that will not accept the other.
    """
    base = str(request.base_url).rstrip("/")
    https = f"{base}/api/v1/calendar/{token}.ics"
    return {
        "webcal": https.replace("https://", "webcal://").replace("http://", "webcal://"),
        "https": https,
    }


@router.get("/calendar/{token}.ics")
async def calendar_ics(token: str, db: AsyncSession = Depends(get_db)):
    """One organization's appointments, as a subscribable calendar.

    Unauthenticated by necessity and by design; see the module docstring. An
    unknown token is a flat 404 with no detail, because distinguishing "no
    such feed" from "wrong secret" would turn this into somewhere to guess.
    """
    if not token or len(token) < 16:
        raise HTTPException(status_code=404, detail="No such calendar.")

    organization = await db.scalar(
        select(Organization).where(Organization.calendar_token == token)
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="No such calendar.")

    rows = await calendar_feed.appointments_for(db, organization.id)
    body = calendar_feed.render(organization.name, rows)

    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            # Named so a download lands as something recognisable, though a
            # subscribing client never looks at this.
            "Content-Disposition": 'inline; filename="pingpulse.ics"',
            # A subscription must never be served from a cache: the whole
            # value of it is that today's diary is today's.
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/calendar/subscription")
async def get_subscription(
    request: Request,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """The current subscription link, if one has been made."""
    organization = await db.get(Organization, tenant.id)
    if not organization.calendar_token:
        return {"active": False, "urls": None}
    return {"active": True, "urls": _urls(request, organization.calendar_token)}


@router.post("/calendar/subscription")
async def create_subscription(
    request: Request,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Make a subscription link, or replace the one that exists.

    Replacing is how a link that has been forwarded is taken back: the old URL
    stops resolving the moment this returns, and every phone already
    subscribed to it goes quiet. That is said plainly in the response so the
    page can warn before anybody presses it a second time.
    """
    tenant.require_role(WRITE_ROLES)

    organization = await db.get(Organization, tenant.id)
    replaced = bool(organization.calendar_token)
    organization.calendar_token = calendar_feed.new_token()
    await db.flush()

    await oplog.record(
        db,
        tenant.id,
        "calendar.subscription.rotate" if replaced else "calendar.subscription.create",
        user_id=getattr(tenant.user, "id", None),
        resource_type="organization",
        resource_id=tenant.id,
        # Deliberately without the token. An audit log is read by more people
        # than the link is meant for.
        changes={"replaced": replaced},
    )

    return {
        "active": True,
        "replaced": replaced,
        "urls": _urls(request, organization.calendar_token),
    }


@router.delete("/calendar/subscription")
async def delete_subscription(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Stop the feed. Every subscribed phone stops updating."""
    tenant.require_role(WRITE_ROLES)

    organization = await db.get(Organization, tenant.id)
    if not organization.calendar_token:
        return {"active": False}

    organization.calendar_token = None
    await db.flush()
    await oplog.record(
        db,
        tenant.id,
        "calendar.subscription.delete",
        user_id=getattr(tenant.user, "id", None),
        resource_type="organization",
        resource_id=tenant.id,
    )
    return {"active": False}
