"""Outbound WhatsApp that survives the transport being briefly unavailable.

A reply is generated while the customer is waiting, but the thing that carries
it can be missing for a few seconds — most often because `wa-qr-service` is
restarting during a deploy and the paired session has not reconnected yet.
Before this module existed that reply was simply lost: the send failed, the row
was written with a null sid, and nothing ever tried again.

So a failed send is now triaged rather than discarded:

  * A *retryable* failure — the bridge is down, the session is still coming
    back, the network blipped, the provider returned a 5xx — parks the message
    on a Redis list and reports QUEUED.
  * A *permanent* failure — no credentials, a number the provider refuses —
    reports FAILED, because retrying it forever would only hide the problem.

`run_drainer` walks the parked messages every few seconds, and again the moment
a session reports AUTHENTICATED, so a deploy costs a customer a short delay
instead of a missing answer.

Two deliberate choices:

  * One list per channel, drained strictly from the head. A conversation with
    two replies waiting must deliver them in the order they were written, so a
    job that fails is left where it is and the drain stops there rather than
    skipping ahead.
  * The drainer runs inside the API process, not the Celery worker. It needs
    the websocket manager to tell the desktop app that a message it last saw
    as "queued" has now gone out, and that manager lives here. Redis is still
    the queue; only the consumer is local.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import select

from app.config import settings
from app.services import whatsapp

logger = logging.getLogger(__name__)

# The three states a stored message can be in. QUEUED is the one that did not
# exist before: neither delivered nor given up on.
SENT = "SENT"
QUEUED = "QUEUED"
FAILED = "FAILED"

CHANNELS_KEY = "pingpulse:outbox:channels"

# After this many failed attempts a message is given up on. At the drain
# interval below that is several minutes of trying, which comfortably covers a
# container restart without keeping a dead message alive forever.
MAX_ATTEMPTS = 20

# A sales reply that arrives an hour late is still useful. One that arrives the
# next morning, answering a question the customer has forgotten asking, is
# worse than none — so queued work expires rather than surprising anyone.
MAX_AGE_SECONDS = 3600

# Substrings meaning "the transport was not available", as opposed to "the
# provider looked at this message and refused it". Matched case-insensitively
# against the error the provider handed back.
RETRYABLE_MARKERS = (
    "qr-session-error",
    "session not connected",
    "session is starting",
    "connection",
    "connect",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "max retries",
    "read error",
    "server error",
    "502",
    "503",
    "504",
)


def queue_key(channel_id) -> str:
    return f"pingpulse:outbox:{channel_id}"


def lock_key(channel_id) -> str:
    return f"pingpulse:outbox:lock:{channel_id}"


@asynccontextmanager
async def _connection():
    """A short-lived Redis client.

    Deliberately not a module-level singleton: this is called both from the
    API's event loop and from Celery tasks that run `asyncio.run` per job, and
    a client bound to a loop that has since closed fails on its next use.
    Connecting to Redis on the container network costs well under a
    millisecond, so the simpler lifetime is worth more than the saving.
    """
    client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )
    try:
        yield client
    finally:
        closer = getattr(client, "aclose", None) or client.close
        try:
            await closer()
        except Exception:  # noqa: BLE001 - closing must not mask the real error
            pass


def is_retryable(reason: str | None) -> bool:
    """Whether this failure is worth queueing rather than reporting."""
    text = (reason or "").lower()
    if not text:
        return False
    # A missing configuration is permanent: queueing it would park messages
    # behind a problem only the operator can fix.
    if "not configured" in text or "not installed" in text:
        return False
    return any(marker in text for marker in RETRYABLE_MARKERS)


@dataclass(frozen=True)
class Delivery:
    """The outcome of one attempt to reach a customer."""

    status: str
    reference: str | None
    detail: str

    @property
    def sent(self) -> bool:
        return self.status == SENT

    @property
    def queued(self) -> bool:
        return self.status == QUEUED


async def enqueue(channel_id, job: dict) -> bool:
    """Park one message. False means Redis itself is unreachable."""
    try:
        async with _connection() as client:
            pipe = client.pipeline()
            pipe.rpush(queue_key(channel_id), json.dumps(job))
            pipe.sadd(CHANNELS_KEY, str(channel_id))
            await pipe.execute()
        return True
    except Exception as exc:  # noqa: BLE001 - a broken queue must not raise into a reply
        logger.error("could not queue outbound message: %s", exc)
        return False


async def deliver(
    channel,
    to_number: str,
    body: str,
    media_urls: list[str] | None = None,
    *,
    message_id=None,
    organization_id=None,
) -> Delivery:
    """Send one message, parking it for a retry if the transport is missing.

    `message_id` is the stored row this belongs to, so a later drain can flip
    it from queued to delivered. Pass it whenever there is a row; without one
    the message is still sent and retried, it just cannot be reconciled.
    """
    sent, reference = await whatsapp.send_message(channel, to_number, body, media_urls)
    if sent:
        return Delivery(SENT, reference, "delivered")

    channel_id = getattr(channel, "id", None)
    if channel_id is None or not is_retryable(reference):
        # Either there is nothing to retry through, or the provider has made a
        # decision that trying again will not change.
        return Delivery(FAILED, None, reference or "no channel configured")

    job = {
        "id": uuid.uuid4().hex,
        "message_id": str(message_id) if message_id else None,
        "organization_id": str(organization_id) if organization_id else None,
        "to": to_number,
        "body": body,
        "media_urls": list(media_urls or []),
        "attempts": 1,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "last_error": reference,
    }

    if await enqueue(channel_id, job):
        logger.info("queued a reply for %s: %s", to_number, reference)
        return Delivery(QUEUED, None, reference or "transport unavailable")

    return Delivery(FAILED, None, f"{reference} (and the retry queue is unreachable)")


def _expired(job: dict) -> bool:
    raw = job.get("enqueued_at")
    if not raw:
        return False
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds() > MAX_AGE_SECONDS


async def _settle(db, job: dict, status: str, reference: str | None) -> None:
    """Record what became of a queued message on its stored row."""
    from app.models import Message

    message_id = job.get("message_id")
    if not message_id:
        return
    try:
        message = await db.get(Message, uuid.UUID(message_id))
    except (ValueError, TypeError):
        return
    if message is None:
        return
    message.delivery_status = status
    if status == SENT:
        message.twilio_sid = reference


async def drain_channel(db, channel) -> dict:
    """Deliver everything parked for one channel, in order.

    Stops at the first job that still cannot go out: the messages behind it
    belong to the same conversations and must not overtake it. The lock keeps
    the periodic drain and an AUTHENTICATED-triggered drain from running over
    each other, which would corrupt the head-of-list update below.
    """
    from app.services.ws_manager import EVENT_SYNC, manager

    key = queue_key(channel.id)
    delivered = 0
    dropped = 0
    remaining = 0

    async with _connection() as client:
        if not await client.set(lock_key(channel.id), "1", nx=True, ex=120):
            return {"delivered": 0, "dropped": 0, "remaining": -1, "skipped": "locked"}

        try:
            while True:
                raw = await client.lindex(key, 0)
                if raw is None:
                    break

                try:
                    job = json.loads(raw)
                except ValueError:
                    await client.lpop(key)
                    continue

                if _expired(job) or job.get("attempts", 0) >= MAX_ATTEMPTS:
                    await client.lpop(key)
                    await _settle(db, job, FAILED, None)
                    dropped += 1
                    logger.warning(
                        "gave up on a queued reply to %s after %s attempt(s): %s",
                        job.get("to"),
                        job.get("attempts"),
                        job.get("last_error"),
                    )
                    continue

                ok, reference = await whatsapp.send_message(
                    channel, job["to"], job["body"], job.get("media_urls")
                )

                if ok:
                    await client.lpop(key)
                    await _settle(db, job, SENT, reference)
                    delivered += 1
                    continue

                # Still unreachable. Count the attempt and leave the job where
                # it is, so nothing behind it jumps the queue.
                job["attempts"] = job.get("attempts", 0) + 1
                job["last_error"] = reference
                await client.lset(key, 0, json.dumps(job))
                break

            remaining = await client.llen(key)
            if remaining == 0:
                await client.srem(CHANNELS_KEY, str(channel.id))
        finally:
            await client.delete(lock_key(channel.id))

    if delivered or dropped:
        await db.commit()
        # The desktop app is showing these as "queued"; tell it to look again.
        await manager.broadcast(
            EVENT_SYNC,
            {
                "contact_id": None,
                "organization_id": str(channel.organization_id),
                "outbox": {
                    "delivered": delivered,
                    "dropped": dropped,
                    "remaining": remaining,
                },
            },
        )
        logger.info(
            "outbox for channel %s: %d delivered, %d dropped, %d waiting",
            channel.id,
            delivered,
            dropped,
            remaining,
        )

    return {"delivered": delivered, "dropped": dropped, "remaining": remaining}


async def pending_channel_ids() -> list[str]:
    try:
        async with _connection() as client:
            return sorted(await client.smembers(CHANNELS_KEY))
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read the outbox index: %s", exc)
        return []


async def drain_pending() -> dict:
    """One pass over every channel with parked messages."""
    from app.database import SessionLocal
    from app.models import ChannelConfig

    channel_ids = await pending_channel_ids()
    if not channel_ids:
        return {"channels": 0, "delivered": 0}

    delivered = 0
    async with SessionLocal() as db:
        for raw_id in channel_ids:
            try:
                channel_uuid = uuid.UUID(raw_id)
            except (ValueError, TypeError):
                continue

            channel = (
                await db.execute(
                    select(ChannelConfig).where(ChannelConfig.id == channel_uuid)
                )
            ).scalar_one_or_none()

            if channel is None:
                # The channel was deleted while messages were parked; there is
                # no longer a number to send them from.
                async with _connection() as client:
                    await client.delete(queue_key(raw_id))
                    await client.srem(CHANNELS_KEY, raw_id)
                continue

            result = await drain_channel(db, channel)
            delivered += result.get("delivered", 0)

    return {"channels": len(channel_ids), "delivered": delivered}


async def drain_now(channel_id) -> None:
    """Drain one channel immediately, for when a session comes back up."""
    from app.database import SessionLocal
    from app.models import ChannelConfig

    async with SessionLocal() as db:
        channel = await db.get(ChannelConfig, channel_id)
        if channel is not None:
            await drain_channel(db, channel)


async def run_drainer(stop: asyncio.Event) -> None:
    """Background loop: retry parked messages until they go out or expire.

    Started by the API's lifespan. A failure here is logged and slept off — the
    loop must outlive a Redis restart, because the whole point of it is to be
    the thing still running when something else is not.
    """
    interval = max(5, settings.outbox_drain_seconds)
    logger.info("outbox drainer running every %ds", interval)

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return  # stop was set: shutting down
        except asyncio.TimeoutError:
            pass

        try:
            await drain_pending()
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            logger.error("outbox drain failed: %s", exc)
