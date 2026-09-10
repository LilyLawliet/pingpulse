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


def new_token() -> str:
    return uuid.uuid4().hex


def schedule_followups(contact, delays: tuple[float, ...] | None = None) -> str | None:
    """Queue the nudges for one contact and return the token that owns them.

    The caller stores the token on the contact; when the customer replies, that
    token is cleared and every queued task for it becomes a no-op.
    """
    if not settings.followups_enabled:
        return None
    if getattr(contact, "sales_stage", None) not in FOLLOWUP_STAGES:
        return None

    hours = delays or (settings.followup_first_hours, settings.followup_second_hours)
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


async def _run_followup(contact_id: str, organization_id: str, token: str, attempt: int) -> str:
    """The actual work, awaited inside the worker's own event loop."""
    from app.models import ChannelConfig, CRMContact, Message
    from app.services import outbox

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
            if metadata.get("followup_token") != token:
                # The customer replied, or a newer reply re-armed the sequence.
                return "cancelled"

            if contact.sales_stage not in FOLLOWUP_STAGES:
                return f"stage moved to {contact.sales_stage}"

            body = FIRST_NUDGE if attempt == 1 else SECOND_NUDGE

            # A nudge must come from the same number the conversation is on.
            channel = (
                await session.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.organization_id == contact.organization_id,
                        ChannelConfig.is_active.is_(True),
                    )
                )
            ).scalars().first()

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
            )
            nudge.delivery_status = delivery.status
            nudge.twilio_sid = delivery.reference
            sent = delivery.sent
            metadata["last_followup_at"] = datetime.now(timezone.utc).isoformat()
            metadata["followup_attempts"] = attempt
            if attempt >= 2:
                # Two nudges is the limit; never become a nuisance.
                metadata.pop("followup_token", None)
            contact.contact_metadata = metadata

            await session.commit()
            if delivery.queued:
                return "queued for retry"
            return "sent" if sent else f"send failed: {delivery.detail}"
    finally:
        await engine.dispose()


@celery_app.task(name="pingpulse.schedule_customer_followup", bind=True, max_retries=2)
def schedule_customer_followup(
    self, contact_id: str, organization_id: str, token: str, attempt: int = 1
) -> str:
    """Send one follow-up, unless the customer has come back in the meantime."""
    try:
        outcome = asyncio.run(_run_followup(contact_id, organization_id, token, attempt))
        logger.info("follow-up %d for %s: %s", attempt, contact_id, outcome)
        return outcome
    except Exception as exc:  # noqa: BLE001
        logger.error("follow-up failed for %s: %s", contact_id, exc)
        raise self.retry(exc=exc, countdown=300)


@celery_app.task(name="pingpulse.ping")
def ping() -> str:
    """Liveness probe for the worker."""
    return "pong"
