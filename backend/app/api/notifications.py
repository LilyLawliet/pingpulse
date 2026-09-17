"""Subscribing a browser, and choosing what is worth an interruption.

The settings here are the ones that decide whether a person finds out about an
escalation tonight or tomorrow morning, so every write is audited alongside
the rest of the agent's configuration.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import NOTIFY_EVENTS, Notification, Organization, PushSubscription
from app.services import notifications, oplog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("/settings")
async def get_settings(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """What this shop is listening for, and what it could listen for.

    The catalogue travels with the settings rather than being duplicated in
    the browser, so adding an event server-side makes it appear on the
    settings page without shipping a new dashboard.
    """
    organization = await db.get(Organization, tenant.id)
    config = organization.notify_config or notifications.defaults()
    devices = await db.scalar(
        select(func.count(PushSubscription.id)).where(
            PushSubscription.organization_id == tenant.id
        )
    )

    # Counted here so the dashboard learns about a failed alert from a call it
    # was already making. A badge that needs its own request is a badge that
    # gets dropped the first time somebody trims a render.
    #
    # Counted in Python rather than in SQL, and that is the point: the first
    # version asked for rows with no sent_at, which missed the alert that
    # finished having reached nobody - so the badge said one while the list
    # underneath it showed two in red. Both now come from delivery_state, so
    # they cannot disagree.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    lately = (
        await db.execute(
            select(Notification)
            .where(
                Notification.organization_id == tenant.id,
                Notification.created_at > now - timedelta(days=7),
            )
            .order_by(Notification.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    stuck = sum(
        1
        for row in lately
        if notifications.delivery_state(row)["status"] == notifications.UNDELIVERED
    )

    return {
        "events": [
            {
                "key": key,
                "description": description,
                "default": on,
                "enabled": notifications.wants(organization, key),
            }
            for key, description, on in NOTIFY_EVENTS
        ],
        "email": notifications.email_for(organization),
        # What to put in the box for a shop that has not filled it in. The
        # account's own address is nearly always the right answer, and an
        # empty field is the difference between email alerts working on day
        # one and never being switched on at all.
        "suggested_email": notifications.usable_address(
            getattr(tenant.user, "email", None)
        ),
        "devices": devices or 0,
        # How many alerts in the past week ran out of chances without
        # reaching anybody. Zero nearly always; the point is the other case.
        "undelivered": stuck or 0,
        # What this deployment can actually do. A settings page offering email
        # on a server with no mail account configured is a promise it cannot
        # keep, so the browser is told which switches mean anything.
        "push_available": notifications.push_available(),
        "email_available": notifications.email_available(),
        "vapid_public_key": settings.vapid_public_key or None,
    }


@router.put("/settings")
async def save_settings(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(WRITE_ROLES)
    organization = await db.get(Organization, tenant.id)

    before = dict(organization.notify_config or {})
    organization.notify_config = notifications.clean_config(payload or {})
    await db.flush()

    await oplog.record(
        db,
        tenant.id,
        "notifications.settings",
        user_id=getattr(tenant.user, "id", None),
        resource_type="organization",
        resource_id=tenant.id,
        changes=oplog.changes_between(before, organization.notify_config),
    )
    await db.commit()
    return {"saved": True, "notify_config": organization.notify_config}


@router.post("/subscribe", status_code=201)
async def subscribe(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Register one browser for push.

    Resubscribing replaces the existing row rather than adding one. A browser
    reissues its subscription whenever the service worker updates, and without
    this every reload would add another endpoint until one escalation arrived
    a dozen times.
    """
    if not notifications.push_available():
        raise HTTPException(
            status_code=503,
            detail="Push is not set up on this server yet.",
        )

    endpoint = (payload.get("endpoint") or "").strip()
    keys = payload.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()

    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=422, detail="That subscription is incomplete.")
    if not endpoint.startswith("https://"):
        raise HTTPException(status_code=422, detail="A push endpoint must be https.")

    existing = (
        await db.execute(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Including one belonging to another tenant: the same browser signing
        # into a different business keeps one subscription, pointed at
        # whichever business it last agreed to be told about.
        existing.organization_id = tenant.id
        existing.user_id = getattr(tenant.user, "id", None)
        existing.p256dh = p256dh
        existing.auth = auth
        existing.label = (payload.get("label") or existing.label or "")[:120] or None
        existing.failures = 0
        await db.commit()
        return {"subscribed": True, "replaced": True}

    count = await db.scalar(
        select(func.count(PushSubscription.id)).where(
            PushSubscription.organization_id == tenant.id
        )
    )
    if (count or 0) >= notifications.MAX_SUBSCRIPTIONS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This business already has {count} devices registered. "
                "Remove one before adding another."
            ),
        )

    try:
        db.add(
            PushSubscription(
                organization_id=tenant.id,
                user_id=getattr(tenant.user, "id", None),
                endpoint=endpoint,
                p256dh=p256dh,
                auth=auth,
                label=(payload.get("label") or "")[:120] or None,
            )
        )
        await db.commit()
    except IntegrityError:
        # Two tabs of the same browser subscribing at once. Both are the same
        # device asking for the same thing, so the loser is not an error.
        await db.rollback()
        return {"subscribed": True, "replaced": True}

    return {"subscribed": True, "replaced": False}


@router.post("/unsubscribe")
async def unsubscribe(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Stop pushing to one browser."""
    endpoint = (payload.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(status_code=422, detail="Which subscription?")

    row = (
        await db.execute(
            select(PushSubscription).where(
                PushSubscription.endpoint == endpoint,
                PushSubscription.organization_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    # Idempotent: a browser revoking a subscription we never had is in the
    # state it asked for.
    return {"unsubscribed": True}


@router.post("/test")
async def send_test(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Prove the whole chain, end to end, on demand.

    Sent inline rather than queued, and exempt from the cool-off and the event
    switches. Somebody pressing "send a test" wants to know within a second
    whether it works, and the answer has to be about this attempt rather than
    about one they made twenty minutes ago.
    """
    tenant.require_role(WRITE_ROLES)
    organization = await db.get(Organization, tenant.id)

    row = Notification(
        organization_id=tenant.id,
        event="escalation",
        title="Test alert",
        body=(
            f"This is a test from {organization.name}. If you are reading it on a "
            "device with the dashboard closed, alerts are working."
        ),
    )
    db.add(row)
    await db.flush()

    result = await notifications.deliver(db, organization, row)
    await db.commit()
    # "Sent: true" with a delivery of {"push": "no devices", "email": "no
    # address"} is a lie told in two parts, and it was the answer somebody
    # got for pressing the button that exists to tell them the truth.
    state = notifications.delivery_state(row)
    return {
        "sent": state["status"] == notifications.DELIVERED,
        "delivery": result,
        **state,
    }


@router.get("")
async def recent(
    days: int = Query(default=7, ge=1, le=90),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """What this shop has been told lately, and whether it got through.

    "I never got told" is the complaint this answers. Without a record there
    is no way to tell a notification that failed from one that arrived and
    was not noticed.
    """
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await db.execute(
            select(Notification)
            .where(
                Notification.organization_id == tenant.id,
                Notification.created_at >= since,
            )
            .order_by(Notification.created_at.desc())
            .limit(100)
        )
    ).scalars().all()

    return {
        "notifications": [
            {
                "id": str(row.id),
                "event": row.event,
                "title": row.title,
                "body": row.body,
                "contact_id": str(row.contact_id) if row.contact_id else None,
                # The raw map stays for whoever is reading logs; the words
                # beside it are what a person is shown. The browser is never
                # asked to interpret "failed: TimeoutError".
                "delivery": row.delivery or {},
                **notifications.delivery_state(row),
                "sent_at": row.sent_at,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }
