"""Telling somebody, when nobody is looking at the dashboard.

The agent has always run headless - the reply path never asks whether a tab is
open - and that is exactly the problem this solves. A customer demands a
manager at nine at night, the agent correctly hands the conversation over and
switches itself off, and the conversation then sits there until somebody
happens to open the dashboard the next morning. The websocket already carries
that event; it carries it to nobody.

So the same events now reach a browser that is closed, and a mailbox.

Four things are worth knowing before changing any of this.

*Nothing here may raise.* Every entry point swallows its own failures. These
are called from the middle of answering a customer, and a push service having
a bad afternoon must never be the reason a reply does not go out.

*Both channels are optional and independent.* No VAPID keys means push is off;
no SMTP host means email is off; neither configured means notifications are
recorded and not sent. A deployment missing them is a deployment with fewer
channels, not a broken settings page.

*The same situation is one notification.* A customer sending four angry
messages in a row is one thing happening, and four buzzes is how a person
learns to ignore the buzz. Repeats for the same contact and the same event
inside the cool-off window are dropped, and the check is a query rather than
worker memory, so it holds across processes and restarts.

*Quiet by default.* Only the events somebody would genuinely want their
evening interrupted for start switched on. A channel that cries wolf gets
turned off within a week, and then the escalation that mattered is missed too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.models import (
    NOTIFY_EVENTS,
    NOTIFY_KEYS,
    Notification,
    PushSubscription,
)

logger = logging.getLogger(__name__)

# How many devices one organization may register. Not a licence limit - a
# runaway-subscription guard, because a browser that resubscribes on every
# reload would otherwise fan one alert out to hundreds of dead endpoints.
MAX_SUBSCRIPTIONS = 40

# Consecutive failures before a subscription is assumed dead. Push services
# report a hard "gone" for a revoked subscription, which is deleted on the
# spot; this catches the slower kind, where a device quietly stops accepting.
MAX_FAILURES = 5

PUSH_TIMEOUT = 8
EMAIL_TIMEOUT = 15


# ------------------------------------------------------------------ settings
def defaults() -> dict:
    """What a shop that has never opened the settings page gets."""
    return {"events": [key for key, _, on in NOTIFY_EVENTS if on], "email": ""}


def wants(organization, event: str) -> bool:
    """Is this organization listening for this kind of thing?

    A missing config is the defaults rather than silence - a client who never
    found the settings page should still be told when somebody asks for a
    human. An explicitly empty list, though, is a decision: it means every
    alert was turned off on purpose, and it is honoured.
    """
    config = getattr(organization, "notify_config", None) or {}
    chosen = config.get("events")
    if not isinstance(chosen, list):
        chosen = defaults()["events"]
    return event in chosen


def email_for(organization) -> str:
    config = getattr(organization, "notify_config", None) or {}
    address = (config.get("email") or "").strip()
    return address if "@" in address else ""


def clean_config(raw: dict) -> dict:
    """What a settings form is allowed to store."""
    events = raw.get("events")
    if not isinstance(events, list):
        events = defaults()["events"]
    email = (raw.get("email") or "").strip()[:320]
    return {
        "events": [key for key in NOTIFY_KEYS if key in events],
        "email": email if "@" in email else "",
    }


def push_available() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def email_available() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


# -------------------------------------------------------------------- record
async def _recently_told(db, organization_id, event: str, contact_id) -> bool:
    """Have we already raised this, about this person, just now?"""
    if settings.notify_cooloff_minutes <= 0:
        return False
    since = datetime.now(timezone.utc) - timedelta(minutes=settings.notify_cooloff_minutes)
    query = select(Notification.id).where(
        Notification.organization_id == organization_id,
        Notification.event == event,
        Notification.created_at >= since,
    )
    # An event with no contact - a delivery failure on the channel itself -
    # collapses per organization instead.
    query = query.where(
        Notification.contact_id == contact_id
        if contact_id is not None
        else Notification.contact_id.is_(None)
    )
    return (await db.scalar(query.limit(1))) is not None


async def raise_alert(
    db,
    organization,
    event: str,
    title: str,
    body: str,
    *,
    contact_id=None,
) -> Notification | None:
    """Record something worth being told about. Never raises.

    Returns the row when one was written, and None when the shop is not
    listening for this or has already been told. Writing it is all this does -
    delivery is somebody else's problem, on purpose, because this is called
    from the middle of answering a customer.
    """
    try:
        if event not in NOTIFY_KEYS or organization is None:
            return None
        if not wants(organization, event):
            return None
        if await _recently_told(db, organization.id, event, contact_id):
            logger.debug("%s for %s is still inside its cool-off", event, contact_id)
            return None

        row = Notification(
            organization_id=organization.id,
            contact_id=contact_id,
            event=event,
            title=title[:160],
            body=body[:2000],
        )
        db.add(row)
        await db.flush()
        return row
    except Exception as exc:  # noqa: BLE001 - never at the cost of a reply
        logger.warning("could not record a %s notification: %s", event, exc)
        return None


# ------------------------------------------------------------------ delivery
def _payload(notification: Notification) -> str:
    return json.dumps(
        {
            "title": notification.title,
            "body": notification.body,
            "event": notification.event,
            "url": settings.dashboard_url,
            "tag": f"{notification.event}:{notification.contact_id or 'org'}",
        }
    )


async def _send_push(db, notification: Notification) -> str:
    """Push to every device this organization has registered.

    Runs in a thread because pywebpush is synchronous and each endpoint is a
    separate HTTPS round-trip to a different push service; three sleeping
    devices would otherwise hold the event loop for the length of three
    timeouts.
    """
    if not push_available():
        return "not configured"

    devices = (
        await db.execute(
            select(PushSubscription).where(
                PushSubscription.organization_id == notification.organization_id
            )
        )
    ).scalars().all()
    if not devices:
        return "no devices"

    from pywebpush import WebPushException, webpush

    payload = _payload(notification)
    claims = {"sub": settings.vapid_subject}
    sent = 0
    dead: list[PushSubscription] = []

    def deliver(device: PushSubscription) -> tuple[bool, bool]:
        """(delivered, gone) for one device."""
        try:
            webpush(
                subscription_info={
                    "endpoint": device.endpoint,
                    "keys": {"p256dh": device.p256dh, "auth": device.auth},
                },
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims=dict(claims),
                timeout=PUSH_TIMEOUT,
            )
            return True, False
        except WebPushException as exc:
            # 404 and 410 are the push service saying this subscription no
            # longer exists. Anything else might be temporary.
            status = getattr(exc.response, "status_code", None)
            return False, status in (404, 410)
        except Exception:  # noqa: BLE001
            return False, False

    results = await asyncio.gather(
        *(asyncio.to_thread(deliver, device) for device in devices),
        return_exceptions=True,
    )

    for device, result in zip(devices, results):
        if isinstance(result, BaseException):
            delivered, gone = False, False
        else:
            delivered, gone = result
        if delivered:
            sent += 1
            device.failures = 0
            device.last_sent_at = datetime.now(timezone.utc)
        elif gone:
            dead.append(device)
        else:
            device.failures = (device.failures or 0) + 1
            if device.failures >= MAX_FAILURES:
                dead.append(device)

    for device in dead:
        await db.delete(device)
    if dead:
        logger.info("dropped %d dead push subscription(s)", len(dead))

    return f"{sent} of {len(devices)}"


async def _send_email(organization, notification: Notification) -> str:
    """One plain message to the address the shop gave us."""
    if not email_available():
        return "not configured"
    address = email_for(organization)
    if not address:
        return "no address"

    from email.message import EmailMessage

    import aiosmtplib

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = address
    message["Subject"] = f"PingPulse: {notification.title}"
    message.set_content(
        f"{notification.body}\n\n"
        f"Open the dashboard: {settings.dashboard_url}\n\n"
        "You are getting this because alerts are switched on for "
        f"{organization.name}. Turn them off in your business settings."
    )

    try:
        await asyncio.wait_for(
            aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password or None,
                start_tls=settings.smtp_starttls,
            ),
            timeout=EMAIL_TIMEOUT,
        )
        return "sent"
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not email %s: %s", address, exc)
        return f"failed: {type(exc).__name__}"


async def deliver(db, organization, notification: Notification) -> dict:
    """Actually send one recorded notification. Never raises.

    Both channels are attempted even if the first fails, because they exist
    for each other: email is what catches the person whose browser is shut,
    and push is what catches the person whose email is buried.
    """
    result = {"push": "skipped", "email": "skipped"}
    try:
        result["push"] = await _send_push(db, notification)
    except Exception as exc:  # noqa: BLE001
        logger.warning("push delivery failed: %s", exc)
        result["push"] = f"failed: {type(exc).__name__}"
    try:
        result["email"] = await _send_email(organization, notification)
    except Exception as exc:  # noqa: BLE001
        logger.warning("email delivery failed: %s", exc)
        result["email"] = f"failed: {type(exc).__name__}"

    try:
        notification.delivery = result
        notification.sent_at = datetime.now(timezone.utc)
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record delivery of %s: %s", notification.id, exc)
    return result


async def raise_and_send(
    db, organization, event: str, title: str, body: str, *, contact_id=None
) -> Notification | None:
    """Record, then hand to the worker. Never raises.

    Queued rather than sent inline: a push round-trip to a sleeping phone can
    take the full timeout, and this is called while a customer is waiting for
    their reply. If the broker is unreachable the row stays written and
    undelivered, which is recoverable - unlike a reply that arrived eight
    seconds late because a notification was in front of it.
    """
    row = await raise_alert(db, organization, event, title, body, contact_id=contact_id)
    if row is None:
        return None

    try:
        from app.tasks import queue_notification

        queue_notification(row.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not queue notification %s: %s", row.id, exc)
    return row
