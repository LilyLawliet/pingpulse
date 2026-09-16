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

# Gmail wants a TCP connect, a STARTTLS negotiation and an AUTH round trip
# before it will take a message, and from a cold container that measured over
# fifteen seconds - which is how the first real alert failed while the inline
# test from a warm process had just succeeded. Nobody is waiting on this: it
# runs in a worker, so a generous timeout costs nothing and a tight one costs
# the alert.
EMAIL_TIMEOUT = 45


# ------------------------------------------------------------------ settings
def default_for(event: str) -> bool:
    """Whether this event is loud enough to be on before anybody asks."""
    for key, _, on in NOTIFY_EVENTS:
        if key == event:
            return on
    return False


def defaults() -> dict:
    """What a shop that has never opened the settings page gets."""
    return {"events": {key: on for key, _, on in NOTIFY_EVENTS}, "email": ""}


def wants(organization, event: str) -> bool:
    """Is this organization listening for this kind of thing?

    Stored as a choice per event rather than as a list of the ones that are
    on, and the difference matters the first time an event is added. With a
    list, a shop that had ever visited the settings page would silently never
    receive the new one - they did not turn it off, it simply was not there to
    be turned on - and the first such event added was the disconnect alert,
    which is the single most important thing this can tell anybody.

    So an event nobody has expressed an opinion about falls back to its own
    default, and only an explicit false is silence.
    """
    config = getattr(organization, "notify_config", None) or {}
    chosen = config.get("events")

    if isinstance(chosen, dict):
        value = chosen.get(event)
        return default_for(event) if value is None else bool(value)
    # The shape this used to be stored in, read literally. Guessing that a
    # list predates an event and therefore cannot have meant to exclude it
    # would override somebody who switched everything off on purpose, and an
    # empty list is about as clear as an instruction gets.
    if isinstance(chosen, list):
        return event in chosen
    return default_for(event)


def email_for(organization) -> str:
    config = getattr(organization, "notify_config", None) or {}
    address = (config.get("email") or "").strip()
    return address if "@" in address else ""


def clean_config(raw: dict) -> dict:
    """What a settings form is allowed to store.

    Always written as a full map, so every event the shop has actually been
    shown carries an explicit answer and anything added later is recognisably
    new rather than indistinguishable from a refusal.
    """
    events = raw.get("events")
    if isinstance(events, dict):
        chosen = {key: bool(events[key]) for key in NOTIFY_KEYS if key in events}
    elif isinstance(events, list):
        chosen = {key: key in events for key in NOTIFY_KEYS}
    else:
        chosen = {key: default_for(key) for key in NOTIFY_KEYS}

    email = (raw.get("email") or "").strip()[:320]
    return {"events": chosen, "email": email if "@" in email else ""}


# Addresses that exist to satisfy a NOT NULL, not to receive mail. Accounts
# created from an access token get one of these, and offering it as a
# suggestion would have somebody save a dead address and then wonder why no
# alert ever arrived.
PLACEHOLDER_DOMAINS = (".local", ".invalid", ".test", "example.com")


def usable_address(address: str | None) -> str:
    """An address worth suggesting, or empty."""
    candidate = (address or "").strip()
    if "@" not in candidate:
        return ""
    domain = candidate.rsplit("@", 1)[-1].lower()
    if any(domain == bad or domain.endswith(bad) for bad in PLACEHOLDER_DOMAINS):
        return ""
    return candidate


def push_available() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def email_available() -> bool:
    """Can this deployment actually put an email on the wire?

    A username with no password is the shape a half-finished setup takes -
    Gmail's host and address filled in, the app password still to come. Left
    unchecked, the settings page would offer a working email channel and every
    alert would fail at send time, which is the one thing this module is
    supposed to never do.
    """
    if not (settings.smtp_host and settings.smtp_from):
        return False
    if settings.smtp_user and not settings.smtp_password:
        return False
    return True


# -------------------------------------------------------------------- record
# How long an alert might still be on its way: two Celery retries two minutes
# apart, plus the attempt itself. Younger than this and an undelivered row is
# in flight; older and it has run out of chances.
DELIVERY_HORIZON_MINUTES = 5


async def _recently_told(db, organization_id, event: str, contact_id) -> bool:
    """Have we already *told* them this, about this person, just now?

    Told, not raised. The cool-off exists so four angry messages are one buzz,
    and a row that never reached anybody was not a buzz. Counting it silenced
    the next occurrence too, so an escalation whose email timed out took the
    following escalation down with it - the failure spreading rather than
    being contained.

    A row that is undelivered but still young is left counting, because it may
    yet arrive and two buzzes is exactly what this is here to prevent.
    """
    from sqlalchemy import or_

    if settings.notify_cooloff_minutes <= 0:
        return False
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=settings.notify_cooloff_minutes)
    query = select(Notification.id).where(
        Notification.organization_id == organization_id,
        Notification.event == event,
        Notification.created_at >= since,
        or_(
            Notification.sent_at.is_not(None),
            Notification.created_at >= now - timedelta(minutes=DELIVERY_HORIZON_MINUTES),
        ),
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


def _failed(outcome: str | None) -> bool:
    """Did this channel fail in a way worth trying again?

    "not configured" and "no devices" are settled answers, not failures. Only
    something that broke gets another go.
    """
    return bool(outcome) and outcome.startswith("failed")


async def deliver(db, organization, notification: Notification) -> dict:
    """Actually send one recorded notification. Never raises.

    Both channels are attempted even if the first fails, because they exist
    for each other: email catches the person whose browser is shut, push
    catches the person whose email is buried.

    A channel that already succeeded on an earlier attempt is skipped. Without
    that, one transient email failure alongside a delivered push would mean
    the retry buzzed everybody a second time about something they had already
    been told.
    """
    previous = notification.delivery or {}
    # Both keys always present, whatever shape the stored map turned out to
    # be. A row missing one raised a KeyError on the way out, which the task
    # could only read as a delivery failure - so it retried, hit the same
    # KeyError, and gave up without ever having tried to send anything.
    result = {"push": "skipped", "email": "skipped", **previous}

    if not previous or _failed(previous.get("push")):
        try:
            result["push"] = await _send_push(db, notification)
        except Exception as exc:  # noqa: BLE001
            logger.warning("push delivery failed: %s", exc)
            result["push"] = f"failed: {type(exc).__name__}"

    if not previous or _failed(previous.get("email")):
        try:
            result["email"] = await _send_email(organization, notification)
        except Exception as exc:  # noqa: BLE001
            logger.warning("email delivery failed: %s", exc)
            result["email"] = f"failed: {type(exc).__name__}"

    # Only finished when nothing is still failing. Stamping sent_at regardless
    # made every transient failure permanent: the retry would read "already
    # sent" and return without trying, so an alert lost to one slow handshake
    # was lost for good.
    unfinished = any(_failed(value) for value in result.values())
    try:
        notification.delivery = result
        if not unfinished:
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

    await _hand_to_worker(row.id)
    return row


async def _hand_to_worker(notification_id) -> None:
    """Enqueue without blocking the event loop.

    Celery's `.delay()` is synchronous socket work. Called straight from an
    async handler it stops the whole process while the broker is contacted -
    and this runs inside the request a customer is waiting on, so a slow Redis
    would show up as every reply getting slower, which is a far stranger
    symptom to diagnose than a missing notification.
    """
    try:
        from app.tasks import queue_notification

        await asyncio.to_thread(queue_notification, notification_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not queue notification %s: %s", notification_id, exc)


# ------------------------------------------------------- the silent failure
# Every other event in this module is triggered by an inbound message. That
# leaves one hole, and it is the worst one: a number that has been logged out
# receives nothing, so there is no message to trigger anything and the symptom
# is silence. A shop can go days believing its agent is working.
#
# So this is the one thing that has to be gone looking for rather than
# reacted to.
WHATSAPP_DOWN = "whatsapp_down"

# While it stays broken, say so once a day rather than every time the loop
# runs. The first alert is the useful one; the point of the rest is only to
# stop it being forgotten.
DOWN_REPEAT_HOURS = 24

# How often the watcher looks. Slow on purpose - this is a condition that
# lasts hours, and a tighter loop would only find it a few minutes sooner
# while asking the bridge for its health all day.
WATCH_INTERVAL_SECONDS = 300


async def connection_problem(db, organization) -> str | None:
    """Why this organization cannot send or receive right now, or None."""
    from app.services import whatsapp

    channel = await whatsapp.active_channel(db, organization.id)
    if channel is None:
        return "no WhatsApp number is connected"
    if channel.whatsapp_provider == "QR_SESSION":
        status = (channel.session_status or "").upper()
        if status != "AUTHENTICATED":
            return f"the paired session is {status.lower() or 'not paired'}"
    return None


async def _told_recently(db, organization_id) -> bool:
    """As with the cool-off: told, not raised.

    A daily alert whose email failed would otherwise buy the outage another
    full day of silence, which is the opposite of what the repeat is for.
    """
    from sqlalchemy import or_

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=DOWN_REPEAT_HOURS)
    found = await db.scalar(
        select(Notification.id)
        .where(
            Notification.organization_id == organization_id,
            Notification.event == WHATSAPP_DOWN,
            Notification.created_at >= since,
            or_(
                Notification.sent_at.is_not(None),
                Notification.created_at
                >= now - timedelta(minutes=DELIVERY_HORIZON_MINUTES),
            ),
        )
        .limit(1)
    )
    return found is not None


async def watch_connections(db) -> int:
    """Alert every shop whose number has stopped working. Never raises.

    Only shops that have used WhatsApp before are checked. A tenant created an
    hour ago and not yet set up is not broken, and telling them their number
    has stopped working would be both wrong and the first thing they ever
    heard from us.
    """
    from sqlalchemy import func, or_

    from app.models import ChannelConfig, Message, Organization

    told = 0
    try:
        ever_used = (
            select(Organization)
            .where(
                or_(
                    Organization.id.in_(select(ChannelConfig.organization_id)),
                    Organization.id.in_(select(Message.organization_id)),
                )
            )
        )
        organizations = (await db.execute(ever_used)).scalars().all()

        for organization in organizations:
            try:
                problem = await connection_problem(db, organization)
                if problem is None:
                    continue
                if not wants(organization, WHATSAPP_DOWN):
                    continue
                if await _told_recently(db, organization.id):
                    continue

                last = await db.scalar(
                    select(func.max(Message.created_at)).where(
                        Message.organization_id == organization.id
                    )
                )
                # Naive from SQLite, aware from PostgreSQL; both mean UTC.
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                when = f" The last message was {last.strftime('%d %b')}." if last else ""

                row = await raise_alert(
                    db,
                    organization,
                    WHATSAPP_DOWN,
                    "Your WhatsApp is not connected",
                    f"Nothing can be sent or received for {organization.name}: "
                    f"{problem}.{when} Open the dashboard and pair the number again.",
                )
                if row is not None:
                    # Committed before the worker is told, so the row is
                    # readable the instant the task is picked up. Here the
                    # window would otherwise stay open across every remaining
                    # tenant in the loop, which is as wide as it gets.
                    await db.commit()
                    told += 1
                    await _hand_to_worker(row.id)
            except Exception as exc:  # noqa: BLE001 - one bad tenant, not all of them
                logger.warning("connection check failed for %s: %s", organization.id, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("the connection watcher could not run: %s", exc)
    return told


async def run_connection_watch(stop) -> None:
    """The loop, started alongside the outbox drainer.

    In the app process rather than a Celery beat container, because there is
    no beat container and adding one to ship a five-minute poll would be a lot
    of moving parts for a question that is one query wide.
    """
    import asyncio

    from app.database import SessionLocal

    logger.info("connection watch started, looking every %ds", WATCH_INTERVAL_SECONDS)

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=WATCH_INTERVAL_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            async with SessionLocal() as session:
                told = await watch_connections(session)
                if told:
                    logger.warning("told %d organization(s) their WhatsApp is down", told)
                else:
                    # One line every five minutes, on purpose. This loop is the
                    # only thing that notices a number has stopped working, and
                    # a loop that has quietly died looks exactly like a loop
                    # with nothing to report. Now it is possible to tell.
                    logger.info("connection watch: nothing to report")
        except Exception as exc:  # noqa: BLE001
            logger.warning("connection watch tick failed: %s", exc)
