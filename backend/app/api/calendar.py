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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import Organization
from app.services import booking, busy_calendar, calendar_feed, invites, notifications, oplog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["calendar"])


def _external_scheme(request: Request) -> str:
    """The scheme the *client* used, not the one that reached this process.

    Caddy terminates TLS and forwards plain HTTP, so `request.url.scheme` is
    "http" in production and the subscription link came out as an http:// URL
    that redirects. A browser follows a 308 and never notices; a calendar
    client subscribing in the background may not, and the failure is a feed
    that silently stays empty rather than an error anybody sees.

    Upgrade only. A forwarded header is client-supplied, and honouring it to
    move *down* to http would let a request talk this into handing somebody a
    plaintext link.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    first = forwarded.split(",")[0].strip().lower()
    return "https" if first == "https" else request.url.scheme


def _urls(request: Request, token: str) -> dict:
    """The same feed, in the two forms a person might need.

    `webcal://` is what makes a phone offer to subscribe rather than download
    a file, and it is the one that should be tapped. The https form is there
    for pasting into a desktop client that will not accept the other.
    """
    scheme = _external_scheme(request)
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    url = f"{scheme}://{host}/api/v1/calendar/{token}.ics"
    return {
        "webcal": url.replace("https://", "webcal://").replace("http://", "webcal://"),
        "https": url,
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


# ------------------------------------------------------ the owner's calendar
class ConnectionIn(BaseModel):
    # None leaves a setting as it is; "" clears it.
    busy_calendar_url: str | None = Field(default=None, max_length=2000)
    meeting_link: str | None = Field(default=None, max_length=500)
    meeting_kind: str | None = None
    meeting_minutes: int | None = Field(default=None, ge=5, le=480)
    invite_owner: bool | None = None


def _connection(organization, check: dict | None = None) -> dict:
    appointments = (organization.agent_config or {}).get("appointments") or {}
    kind = booking.meeting_kind(organization)
    url = busy_calendar.url_of(organization)
    return {
        "busy_calendar_set": bool(url),
        # Only where it points. The address is a secret that shows the owner's
        # whole calendar, and it is not repeated back once saved.
        "busy_calendar_host": urlparse(busy_calendar.normalise(url)).hostname if url else None,
        "meeting_link": appointments.get("meeting_link") or "",
        "meeting_kind": kind,
        "meeting_minutes": booking.duration_minutes(organization, kind),
        "invite_owner": appointments.get(invites.CONFIG_KEY, True) is not False,
        "email_available": notifications.email_available(),
        "check": check,
    }


async def _check(organization) -> dict:
    """Read the owner's calendar now and say what was found."""
    if not busy_calendar.url_of(organization):
        return {"ok": False, "says": "No calendar address is set."}
    try:
        copy = await busy_calendar.read(organization, fresh=True)
    except busy_calendar.Unreadable as exc:
        return {"ok": False, "says": f"Couldn't read it: {exc}."}
    now = datetime.now(timezone.utc)
    soon = [b for b in copy.busy if now < b.ends_at and b.starts_at < now + timedelta(days=14)]
    return {
        "ok": True,
        "busy_next_14_days": len(soon),
        "says": f"Read it. {len(soon)} busy time{'s' if len(soon) != 1 else ''} in the next two "
        "weeks - the agent won't offer those.",
    }


@router.get("/calendar/connection")
async def get_connection(tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)):
    organization = await db.get(Organization, tenant.id)
    address = await notifications.address_for(db, organization)
    return {**_connection(organization), "invites_go_to": address or None}


@router.put("/calendar/connection")
async def save_connection(
    payload: ConnectionIn,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Save meeting settings and the owner's calendar address, reading it first."""
    tenant.require_role(WRITE_ROLES)
    organization = await db.get(Organization, tenant.id)
    config = dict(organization.agent_config or {})
    appointments = dict(config.get("appointments") or {})

    if payload.busy_calendar_url is not None:
        url = payload.busy_calendar_url.strip()
        if url:
            problem = busy_calendar.problem_with(url)
            if problem:
                raise HTTPException(status_code=422, detail=problem)
            appointments[busy_calendar.CONFIG_KEY] = busy_calendar.normalise(url)
        else:
            appointments.pop(busy_calendar.CONFIG_KEY, None)
        busy_calendar.forget(organization)
    if payload.meeting_link is not None:
        link = payload.meeting_link.strip()
        if link and not link.lower().startswith("https://"):
            raise HTTPException(status_code=422, detail="The meeting link must start with https://")
        if link:
            appointments["meeting_link"] = link
        else:
            appointments.pop("meeting_link", None)
    if payload.meeting_kind is not None:
        if payload.meeting_kind not in ("phone", "video"):
            raise HTTPException(status_code=422, detail="A meeting is a phone call or a video call.")
        appointments["meeting_kind"] = payload.meeting_kind
    if payload.meeting_minutes is not None:
        kind = appointments.get("meeting_kind") or booking.meeting_kind(organization)
        per_kind = dict(appointments.get("duration_by_kind") or {})
        per_kind[kind] = payload.meeting_minutes
        appointments["duration_by_kind"] = per_kind
    if payload.invite_owner is not None:
        appointments[invites.CONFIG_KEY] = payload.invite_owner

    trial = SimpleNamespace(
        id=organization.id,
        timezone=organization.timezone,
        agent_config={**config, "appointments": appointments},
    )
    check = None
    if payload.busy_calendar_url:
        # Read before saving. A link that doesn't work, saved, would stop every
        # booking - "unreadable" offers no times, by design.
        check = await _check(trial)
        if not check["ok"]:
            raise HTTPException(status_code=422, detail=check["says"])

    config["appointments"] = appointments
    organization.agent_config = config
    await db.flush()
    await oplog.record(
        db,
        tenant.id,
        "calendar.connection.save",
        user_id=getattr(tenant.user, "id", None),
        resource_type="organization",
        resource_id=tenant.id,
        # Never the calendar address itself.
        changes={
            "busy_calendar_set": bool(appointments.get(busy_calendar.CONFIG_KEY)),
            "meeting_link_set": bool(appointments.get("meeting_link")),
        },
    )
    address = await notifications.address_for(db, organization)
    return {**_connection(organization, check), "invites_go_to": address or None}


@router.post("/calendar/connection/check")
async def check_connection(tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)):
    organization = await db.get(Organization, tenant.id)
    return await _check(organization)
