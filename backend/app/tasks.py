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

from app.services import agent_config

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

# Not a refusal so much as a postponement: the caller re-queues for the hour
# the shop is willing to send at. Dropping it would lose a lead to a clock.
QUIET = "quiet hours"

# Somebody with a site visit on Thursday does not need "still thinking about
# it?" on Wednesday. Worse than noise - it tells them the business has lost
# track of them.
ALREADY_BOOKED = "already booked"


def refuse_followup(
    contact,
    token: str,
    attempt: int,
    manual: bool,
    organization=None,
    appointment=None,
) -> str | None:
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

    # A confirmed appointment answers the question the nudge was going to ask.
    # An operator who presses follow-up anyway has looked at the conversation
    # and decided, so this gate is for the automatic sequence only.
    if not manual and appointment is not None:
        return ALREADY_BOOKED

    # Last, because it is the only one the caller can act on rather than
    # simply obey: everything above means never, and this one means not yet.
    if organization is not None and agent_config.in_quiet_hours(organization):
        return QUIET

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

            from app.models import Organization
            from app.services import booking

            organization = await session.get(Organization, contact.organization_id)
            appointment = await booking.upcoming_for(session, contact.id)

            refusal = refuse_followup(
                contact,
                token,
                attempt,
                manual,
                organization=organization,
                appointment=appointment,
            )
            if refusal == QUIET:
                # Deferred, not dropped. Sent when the shop is willing to send.
                #
                # A broker that cannot take the re-queue loses this one nudge,
                # which is the same trade the rest of the queueing here makes:
                # never raise into a path whose failure is invisible to the
                # customer and whose success was only ever a convenience.
                when = agent_config.next_sendable_time(organization)
                try:
                    schedule_customer_followup.apply_async(
                        args=[
                            str(contact.id),
                            str(contact.organization_id),
                            token,
                            attempt,
                        ],
                        eta=when,
                    )
                    logger.info(
                        "follow-up %d for %s held until %s (quiet hours)",
                        attempt,
                        contact.id,
                        when.isoformat(timespec="minutes"),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "could not hold follow-up %d for %s: %s", attempt, contact.id, exc
                    )
                return QUIET
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


class NotYetVisible(Exception):
    """The row was handed over before the transaction that wrote it committed.

    Every caller flushes the notification, hands the id to the worker, and
    commits afterwards - often with a websocket broadcast in between. The
    worker reads through its own connection, so for that window the row does
    not exist yet, and a worker on the same host as the broker gets there in
    single-digit milliseconds. Treating that as "gone" loses the alert
    permanently and silently, because returning a string is a success.
    """


async def _run_notification(notification_id: str) -> str:
    """Deliver one recorded notification, inside the worker's own loop."""
    from app.models import Notification, Organization
    from app.services import notifications

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            row = await session.get(Notification, uuid.UUID(notification_id))
            if row is None:
                # Not "gone" - almost always "not committed yet". Worth a few
                # quick goes before believing it was really rolled back.
                raise NotYetVisible(notification_id)
            # Already delivered. A retry after a partial failure would send a
            # second copy of something the person has already been buzzed for.
            if row.sent_at is not None:
                return "already sent"

            organization = await session.get(Organization, row.organization_id)
            if organization is None:
                return "organization gone"

            result = await notifications.deliver(session, organization, row)
            await session.commit()

            # Committed first, so the record of what failed survives whatever
            # the retry does. The caller turns this into a Celery retry.
            failed = [
                name for name, outcome in result.items()
                if outcome and outcome.startswith("failed")
            ]
            if failed and row.sent_at is None:
                raise RuntimeError(
                    "could not deliver on: " + ", ".join(sorted(failed))
                )
            return f"push={result['push']} email={result['email']}"
    finally:
        await engine.dispose()


# Two different reasons to come back, and they want different patience. A
# mail server that timed out needs minutes; a transaction that has not landed
# yet needs a moment, and waiting two minutes for it would hold an escalation
# back long after it was readable.
NOT_VISIBLE_RETRIES = 4
NOT_VISIBLE_COUNTDOWN = 5


@celery_app.task(name="pingpulse.deliver_notification", bind=True, max_retries=2)
def deliver_notification(self, notification_id: str) -> str:
    """Send one alert to push and email.

    Out of band on purpose. A push round-trip to a sleeping phone can take the
    full timeout, and the alternative is a customer waiting on their reply
    while somebody's handset is woken up.
    """
    try:
        outcome = asyncio.run(_run_notification(notification_id))
        logger.info("notification %s: %s", notification_id, outcome)
        return outcome
    except NotYetVisible as exc:
        logger.info("notification %s is not committed yet; looking again", notification_id)
        raise self.retry(
            exc=exc,
            countdown=NOT_VISIBLE_COUNTDOWN,
            max_retries=NOT_VISIBLE_RETRIES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("notification %s failed: %s", notification_id, exc)
        raise self.retry(exc=exc, countdown=120)


def queue_notification(notification_id) -> bool:
    """Hand a recorded notification to the worker.

    Returns whether it was queued. A broker that is down leaves the row
    written and undelivered rather than raising into the reply path - the
    same trade the follow-up scheduler makes, for the same reason.
    """
    try:
        deliver_notification.delay(str(notification_id))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not queue notification %s: %s", notification_id, exc)
        return False


@celery_app.task(name="pingpulse.ping")
def ping() -> str:
    """Liveness probe for the worker."""
    return "pong"
