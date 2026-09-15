"""Celery tasks: delayed follow-ups for conversations that went quiet.

A lead who asked about price and then stopped replying is worth one nudge, not
silence. Follow-ups are scheduled after a reply when the conversation is warm
(QUALIFIED / PRESENTATION / NEGOTIATION) and cancelled the moment the customer
writes back.

Cancellation is by *token*, not by revoking a Celery id: revoking an ETA task
is unreliable across brokers, so each contact holds the token of its pending
follow-up and the task simply exits if the token no longer matches. That makes
"customer replied" a single database write rather than a broker operation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from celery import Celery
from sqlalchemy import select

from app.config import settings
from app.services import consent

logger = logging.getLogger(__name__)

celery_app = Celery("pingpulse", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_max_tasks_per_child=200,
    broker_connection_retry_on_startup=True,
    # Queuing happens inside the request the customer is waiting on, so a
    # broker outage must fail fast instead of retrying for a minute.
    broker_transport_options={
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        "retry_policy": {"timeout": 3.0},
    },
    broker_connection_timeout=2,
)

FOLLOWUP_STAGES = ("QUALIFIED", "PRESENTATION", "NEGOTIATION")

FIRST_NUDGE = (
    "Just checking in — would you like me to hold anything for you, or show you "
    "a few more options?"
)
SECOND_NUDGE = (
    "Still here whenever you're ready. Tell me the colour or budget you have in "
    "mind and I'll pull up what we have."
)
# The last one anybody gets. It says so, and it offers the way out, because a
# third unanswered message is the point where persistence starts reading as
# pestering and the honest thing is to stop and say we have stopped.
THIRD_NUDGE = (
    "I'll leave it there so I'm not filling up your phone — just message me any "
    "time and I'll pick it straight back up. Reply STOP if you'd rather not hear "
    "from us."
)
NUDGES = (FIRST_NUDGE, SECOND_NUDGE, THIRD_NUDGE)

# Three, and the cap is here rather than in configuration because it is a
# promise to the customer and not a dial. A fourth automated message to
# somebody who has answered none of the first three is not a follow-up
# strategy, it is the reason numbers get reported.
MAX_FOLLOWUPS = 3


def new_token() -> str:
    return uuid.uuid4().hex


def schedule_one(contact, minutes: float, message: str | None = None) -> str | None:
    """Queue a single follow-up an operator asked for, and return its token.

    Separate from `schedule_followups` because the two are asked for by
    different people for different reasons. The automatic sequence is a policy
    — only warm conversations, at fixed hours, at most twice. This is someone
    looking at a conversation and deciding it needs a nudge, so it takes the
    delay and the wording they chose and does not second-guess the stage.

    It shares the automatic sequence's cancellation: the token is stored on the
    contact, and a customer writing back clears it, so a nudge is never sent to
    someone who has already replied.
    """
    token = new_token()
    try:
        schedule_customer_followup.apply_async(
            kwargs={
                "contact_id": str(contact.id),
                "organization_id": str(contact.organization_id),
                "token": token,
                "attempt": 1,
                "body": message or None,
                "manual": True,
            },
            countdown=max(1, int(minutes * 60)),
            retry=False,
        )
    except Exception as exc:  # noqa: BLE001 - a broker outage is reported, not raised
        logger.warning("could not queue the follow-up: %s", exc)
        return None

    logger.info(
        "queued a manual follow-up for contact %s in %s minute(s) (token %s)",
        contact.id,
        minutes,
        token[:8],
    )
    return token


def schedule_followups(contact, delays: tuple[float, ...] | None = None) -> str | None:
    """Queue the nudges for one contact and return the token that owns them.

    The caller stores the token on the contact; when the customer replies, that
    token is cleared and every queued task for it becomes a no-op.
    """
    if not settings.followups_enabled:
        return None
    if getattr(contact, "sales_stage", None) not in FOLLOWUP_STAGES:
        return None

    # Nothing is queued for somebody who has asked us to stop. The running
    # task checks again when it fires, because an opt-out usually lands in the
    # hours between these two moments.
    if getattr(contact, "opt_out", False):
        return None

    hours = delays or (
        settings.followup_first_hours,
        settings.followup_second_hours,
        settings.followup_third_hours,
    )
    hours = hours[:MAX_FOLLOWUPS]
    token = new_token()

    for index, delay_hours in enumerate(hours):
        try:
            schedule_customer_followup.apply_async(
                kwargs={
                    "contact_id": str(contact.id),
                    "organization_id": str(contact.organization_id),
                    "token": token,
                    "attempt": index + 1,
                },
                countdown=int(delay_hours * 3600),
                retry=False,
            )
        except Exception as exc:  # noqa: BLE001 - a broker outage must not break the reply
            logger.warning("could not queue follow-up: %s", exc)
            return None

    logger.info(
        "queued %d follow-up(s) for contact %s (token %s)", len(hours), contact.id, token[:8]
    )
    return token


async def _announce(contact, nudge, body: str) -> None:
    """Put a sent follow-up on the operator's screen. Never raises.

    Two events, matching what a live reply emits, because the dashboard uses
    them for different things: `outbound_message` puts the bubble in the open
    thread straight away, and `sync` refreshes the contact list and the stats
    that now count it. A follow-up that reached the customer must not be lost
    because a socket was busy, so failure here is logged and swallowed.
    """
    from app.services import ws_manager
    from app.services.ws_manager import manager

    try:
        await manager.broadcast(
            ws_manager.EVENT_OUTBOUND,
            {
                "contact_id": str(contact.id),
                "organization_id": str(contact.organization_id),
                "message_id": str(nudge.id),
                "content": body,
                "twilio_sid": nudge.twilio_sid,
                "delivery_status": nudge.delivery_status,
                "media_urls": [],
                "followup": True,
            },
        )
        await manager.broadcast(
            ws_manager.EVENT_SYNC,
            {
                "contact_id": str(contact.id),
                "organization_id": str(contact.organization_id),
            },
        )
    except Exception as exc:  # noqa: BLE001 - the message is already delivered
        logger.warning("could not announce the follow-up for %s: %s", contact.id, exc)


CANCELLED = "cancelled"
OPTED_OUT = "opted out"


def refuse_followup(contact, token: str, attempt: int, manual: bool) -> str | None:
    """Why this queued nudge must not go out, or None if it may.

    Separated from the task that sends it because the task owns a database
    engine of its own — a worker has no FastAPI lifespan — and a rule this
    important should be testable without one.

    The order matters. A cancelled sequence is checked first because it is the
    common case, and the opt-out before the stage gate because somebody who
    said STOP gets nothing regardless of how warm the lead looked.
    """
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    if metadata.get("followup_token") != token:
        # The customer replied, or a newer reply re-armed the sequence.
        return CANCELLED

    # Including a nudge that was already queued when they said it. These sit in
    # the broker for hours and the opt-out almost always arrives inside that
    # window, so queue-time checking alone sends to somebody who asked us to
    # stop.
    if not consent.may_send(contact):
        return OPTED_OUT

    # The stage gate belongs to the automatic sequence: it is what stops the
    # agent nudging a conversation that was never warm. An operator asking for
    # a follow-up has already made that judgement.
    if not manual and getattr(contact, "sales_stage", None) not in FOLLOWUP_STAGES:
        return f"stage moved to {getattr(contact, 'sales_stage', None)}"

    # A stale task carrying an attempt beyond the cap must not index past the
    # end of NUDGES in the worker.
    if not manual and attempt > MAX_FOLLOWUPS:
        return f"past the {MAX_FOLLOWUPS}-nudge cap"

    return None


async def _run_followup(
    contact_id: str,
    organization_id: str,
    token: str,
    attempt: int,
    body_override: str | None = None,
    manual: bool = False,
) -> str:
    """The actual work, awaited inside the worker's own event loop."""
    from app.models import CRMContact, Message
    from app.services import outbox, whatsapp

    # A worker process has no FastAPI lifespan, so it owns its engine.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            result = await session.execute(
                select(CRMContact).where(
                    CRMContact.id == uuid.UUID(contact_id),
                    CRMContact.organization_id == uuid.UUID(organization_id),
                )
            )
            contact = result.scalar_one_or_none()
            if contact is None:
                return "contact gone"

            metadata = dict(contact.contact_metadata or {})
            refusal = refuse_followup(contact, token, attempt, manual)
            if refusal is not None:
                if refusal == OPTED_OUT:
                    # Drop the token too, so the rest of the queued sequence
                    # becomes a no-op instead of each task rediscovering this.
                    metadata.pop("followup_token", None)
                    contact.contact_metadata = metadata
                    await session.commit()
                return refusal

            body = body_override or NUDGES[min(attempt, MAX_FOLLOWUPS) - 1]

            # A nudge must come from the same number the conversation is on.
            channel = await whatsapp.active_channel(session, contact.organization_id)

            # Routed by the channel's provider and parked for a retry if the
            # transport is down, exactly as a live reply is. Calling Twilio
            # directly here silently sent nothing for a tenant paired over
            # WhatsApp Web.
            nudge = Message(
                organization_id=contact.organization_id,
                contact_id=contact.id,
                sender="agent",
                content=body,
                media_urls=[],
                delivery_status=outbox.QUEUED,
            )
            session.add(nudge)
            await session.flush()

            delivery = await outbox.deliver(
                channel,
                contact.phone_number,
                body,
                message_id=nudge.id,
                organization_id=contact.organization_id,
                to_jid=(contact.contact_metadata or {}).get("wa_jid"),
            )
            nudge.delivery_status = delivery.status
            nudge.twilio_sid = delivery.reference
            sent = delivery.sent
            metadata["last_followup_at"] = datetime.now(timezone.utc).isoformat()
            metadata["followup_attempts"] = attempt
            if manual or attempt >= 2:
                # One requested nudge is one nudge. For the automatic sequence,
                # two is the limit; never become a nuisance.
                metadata.pop("followup_token", None)
                metadata.pop("followup_due_at", None)
                metadata.pop("followup_message", None)
            contact.contact_metadata = metadata

            await session.commit()

            # Tell the dashboards. This runs in the Celery worker, which holds
            # no sockets of its own, so it reaches the operator's screen over
            # the Redis bridge in ws_manager. Without it the nudge arrived on
            # the customer's phone and the conversation on screen showed
            # nothing — the one part of a follow-up an operator can actually
            # check is that it went out.
            await _announce(contact, nudge, body)

            if delivery.queued:
                return "queued for retry"
            return "sent" if sent else f"send failed: {delivery.detail}"
    finally:
        await engine.dispose()


@celery_app.task(name="pingpulse.schedule_customer_followup", bind=True, max_retries=2)
def schedule_customer_followup(
    self,
    contact_id: str,
    organization_id: str,
    token: str,
    attempt: int = 1,
    body: str | None = None,
    manual: bool = False,
) -> str:
    """Send one follow-up, unless the customer has come back in the meantime."""
    try:
        outcome = asyncio.run(
            _run_followup(contact_id, organization_id, token, attempt, body, manual)
        )
        logger.info("follow-up %d for %s: %s", attempt, contact_id, outcome)
        return outcome
    except Exception as exc:  # noqa: BLE001
        logger.error("follow-up failed for %s: %s", contact_id, exc)
        raise self.retry(exc=exc, countdown=300)


@celery_app.task(name="pingpulse.ping")
def ping() -> str:
    """Liveness probe for the worker."""
    return "pong"
