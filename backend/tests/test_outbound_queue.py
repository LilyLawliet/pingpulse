"""The outbound retry queue.

The bug these exist for: a reply was generated, the transport happened to be
missing for a few seconds — almost always `wa-qr-service` restarting during a
deploy — and the reply was simply lost. Nothing retried it, and the dashboard
showed it as though it had gone out.

So the rules being protected here are:

  * A failure that will pass on its own is queued, not discarded.
  * A failure that will never pass is reported, not queued forever behind a
    problem only the operator can fix.
  * Queued messages leave in the order they were written. A conversation whose
    second reply overtakes its first reads as nonsense.
  * Nothing is ever presented as delivered until it actually is.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.models import ChannelConfig, CRMContact, Message, Organization
from app.services import outbox


# --------------------------------------------------------------- fake redis
class FakeRedis:
    """Just enough Redis to drive a drain, in memory.

    Only the operations the outbox uses, with the same semantics: lists are
    read from the head and written back in place, and `set(nx=...)` is the
    lock.
    """

    def __init__(self, store: dict):
        self.store = store

    # --- lists
    async def rpush(self, key, value):
        self.store.setdefault(key, []).append(value)

    async def lindex(self, key, index):
        items = self.store.get(key) or []
        try:
            return items[index]
        except IndexError:
            return None

    async def lpop(self, key):
        items = self.store.get(key) or []
        return items.pop(0) if items else None

    async def lset(self, key, index, value):
        self.store[key][index] = value

    async def llen(self, key):
        return len(self.store.get(key) or [])

    # --- sets
    async def sadd(self, key, value):
        self.store.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.store.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.store.get(key, set()))

    # --- keys
    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis):
        self.redis = redis
        self.queued = []

    def rpush(self, key, value):
        self.queued.append(("rpush", key, value))

    def sadd(self, key, value):
        self.queued.append(("sadd", key, value))

    async def execute(self):
        for name, key, value in self.queued:
            await getattr(self.redis, name)(key, value)
        self.queued = []


@pytest.fixture
def fake_redis(monkeypatch):
    store: dict = {}

    @asynccontextmanager
    async def connection():
        yield FakeRedis(store)

    monkeypatch.setattr(outbox, "_connection", connection)
    return store


# ------------------------------------------------------------ classification
def test_a_missing_transport_is_worth_retrying():
    assert outbox.is_retryable("qr-session-refused: session not connected")
    assert outbox.is_retryable("qr-session-error: Connection refused")
    assert outbox.is_retryable("HTTPError: 503 Service Unavailable")
    assert outbox.is_retryable("read timed out")


def test_a_decision_the_provider_has_already_made_is_not():
    """Queueing these would hide a configuration problem behind a retry loop."""
    assert not outbox.is_retryable("Twilio is not configured for this organization")
    assert not outbox.is_retryable("twilio package not installed")
    assert not outbox.is_retryable("Unable to create record: 'To' number is not valid")
    assert not outbox.is_retryable("")
    assert not outbox.is_retryable(None)


# ------------------------------------------------------------------ queueing
@pytest.mark.asyncio
async def test_a_retryable_failure_is_parked_rather_than_lost(fake_redis, monkeypatch):
    channel = ChannelConfig(
        organization_id=None,
        channel="whatsapp",
        provider="twilio",
        phone_number="+14155238886",
        whatsapp_provider="QR_SESSION",
    )
    channel.id = "chan-1"

    async def dead(*_args, **_kwargs):
        return False, "qr-session-refused: session not connected"

    monkeypatch.setattr(outbox.whatsapp, "send_message", dead)

    delivery = await outbox.deliver(
        channel, "+971500001111", "we have it in blue", message_id="msg-1"
    )

    assert delivery.status == outbox.QUEUED
    assert delivery.reference is None, "nothing was delivered, so there is no reference"

    parked = fake_redis[outbox.queue_key("chan-1")]
    assert len(parked) == 1
    assert json.loads(parked[0])["body"] == "we have it in blue"
    assert "chan-1" in fake_redis[outbox.CHANNELS_KEY]


@pytest.mark.asyncio
async def test_a_permanent_failure_is_not_parked(fake_redis, monkeypatch):
    channel = ChannelConfig(
        organization_id=None,
        channel="whatsapp",
        provider="twilio",
        phone_number="+14155238886",
    )
    channel.id = "chan-2"

    async def refused(*_args, **_kwargs):
        return False, "Twilio is not configured for this organization"

    monkeypatch.setattr(outbox.whatsapp, "send_message", refused)

    delivery = await outbox.deliver(channel, "+971500001111", "hello", message_id="m")

    assert delivery.status == outbox.FAILED
    assert outbox.queue_key("chan-2") not in fake_redis


@pytest.mark.asyncio
async def test_a_send_that_works_is_never_queued(fake_redis, monkeypatch):
    channel = ChannelConfig(
        organization_id=None, channel="whatsapp", provider="twilio", phone_number="+1"
    )
    channel.id = "chan-3"

    async def fine(*_args, **_kwargs):
        return True, "SM123"

    monkeypatch.setattr(outbox.whatsapp, "send_message", fine)

    delivery = await outbox.deliver(channel, "+971500001111", "hi", message_id="m")

    assert delivery.status == outbox.SENT
    assert delivery.reference == "SM123"
    assert fake_redis == {}


# -------------------------------------------------------------------- drain
async def _org_with_channel(db_session, name: str) -> ChannelConfig:
    organization = Organization(name=name, sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()

    channel = ChannelConfig(
        organization_id=organization.id,
        channel="whatsapp",
        provider="twilio",
        phone_number="+14155559999",
        whatsapp_provider="QR_SESSION",
    )
    db_session.add(channel)
    await db_session.flush()
    return channel


_next_number = iter(range(1000, 9999))


async def _queued_message(db_session, channel, content: str) -> Message:
    # A distinct number per message: a contact is unique per organization, and
    # the ordering test needs two messages under the same channel.
    contact = CRMContact(
        organization_id=channel.organization_id,
        phone_number=f"+9715000{next(_next_number)}",
        pipeline_stage="LEAD",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()

    message = Message(
        organization_id=channel.organization_id,
        contact_id=contact.id,
        sender="agent",
        content=content,
        media_urls=[],
        delivery_status=outbox.QUEUED,
    )
    db_session.add(message)
    await db_session.flush()
    return message


@pytest.mark.asyncio
async def test_the_session_coming_back_delivers_what_was_parked(
    db_session, fake_redis, monkeypatch
):
    """The whole point: a deploy costs a delay, not a lost answer."""
    channel = await _org_with_channel(db_session, "Restarted Co")
    message = await _queued_message(db_session, channel, "still here?")

    async def dead(*_args, **_kwargs):
        return False, "qr-session-refused: session not connected"

    monkeypatch.setattr(outbox.whatsapp, "send_message", dead)
    await outbox.deliver(
        channel, "+971500003333", "still here?", message_id=message.id
    )
    assert message.delivery_status == outbox.QUEUED

    # The bridge finishes restoring its session.
    async def alive(*_args, **_kwargs):
        return True, "WA-9"

    monkeypatch.setattr(outbox.whatsapp, "send_message", alive)
    result = await outbox.drain_channel(db_session, channel)

    assert result["delivered"] == 1
    assert result["remaining"] == 0
    assert message.delivery_status == outbox.SENT
    assert message.twilio_sid == "WA-9"
    # The channel is off the index once it has nothing waiting.
    assert "chan" not in str(fake_redis.get(outbox.CHANNELS_KEY, set()))


@pytest.mark.asyncio
async def test_a_drain_stops_at_the_first_message_it_cannot_send(
    db_session, fake_redis, monkeypatch
):
    """Order is the point. A later reply must not overtake an earlier one."""
    channel = await _org_with_channel(db_session, "Ordered Co")
    first = await _queued_message(db_session, channel, "first")
    second = await _queued_message(db_session, channel, "second")

    async def dead(*_args, **_kwargs):
        return False, "qr-session-refused: session not connected"

    monkeypatch.setattr(outbox.whatsapp, "send_message", dead)
    await outbox.deliver(channel, "+971500003333", "first", message_id=first.id)
    await outbox.deliver(channel, "+971500003333", "second", message_id=second.id)

    result = await outbox.drain_channel(db_session, channel)

    assert result["delivered"] == 0
    assert result["remaining"] == 2, "neither may be dropped"
    assert first.delivery_status == outbox.QUEUED
    assert second.delivery_status == outbox.QUEUED

    parked = [json.loads(raw) for raw in fake_redis[outbox.queue_key(channel.id)]]
    assert [job["body"] for job in parked] == ["first", "second"]
    assert parked[0]["attempts"] == 2, "the failed head counts the retry"


@pytest.mark.asyncio
async def test_a_message_too_old_to_be_useful_is_given_up_on(
    db_session, fake_redis, monkeypatch
):
    """An answer to a question the customer has forgotten asking is worse than none."""
    channel = await _org_with_channel(db_session, "Stale Co")
    message = await _queued_message(db_session, channel, "sorry for the wait")

    stale = {
        "id": "job-old",
        "message_id": str(message.id),
        "to": "+971500003333",
        "body": "sorry for the wait",
        "media_urls": [],
        "attempts": 1,
        "enqueued_at": (
            datetime.now(timezone.utc)
            - timedelta(seconds=outbox.MAX_AGE_SECONDS + 60)
        ).isoformat(),
    }
    fake_redis[outbox.queue_key(channel.id)] = [json.dumps(stale)]
    fake_redis[outbox.CHANNELS_KEY] = {str(channel.id)}

    sends = []

    async def record(*args, **_kwargs):
        sends.append(args)
        return True, "WA-late"

    monkeypatch.setattr(outbox.whatsapp, "send_message", record)
    result = await outbox.drain_channel(db_session, channel)

    assert sends == [], "an expired message must not be sent hours later"
    assert result["dropped"] == 1
    assert message.delivery_status == outbox.FAILED
    assert message.twilio_sid is None


@pytest.mark.asyncio
async def test_a_message_that_keeps_failing_is_eventually_given_up_on(
    db_session, fake_redis, monkeypatch
):
    channel = await _org_with_channel(db_session, "Hopeless Co")
    message = await _queued_message(db_session, channel, "hello?")

    job = {
        "id": "job-tired",
        "message_id": str(message.id),
        "to": "+971500003333",
        "body": "hello?",
        "media_urls": [],
        "attempts": outbox.MAX_ATTEMPTS,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    fake_redis[outbox.queue_key(channel.id)] = [json.dumps(job)]

    async def dead(*_args, **_kwargs):
        return False, "qr-session-refused: session not connected"

    monkeypatch.setattr(outbox.whatsapp, "send_message", dead)
    result = await outbox.drain_channel(db_session, channel)

    assert result["dropped"] == 1
    assert message.delivery_status == outbox.FAILED


@pytest.mark.asyncio
async def test_two_drains_do_not_run_over_each_other(db_session, fake_redis, monkeypatch):
    """The periodic drain and a reconnect can fire together; only one may run."""
    channel = await _org_with_channel(db_session, "Locked Co")
    fake_redis[outbox.lock_key(channel.id)] = "1"

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("the locked-out drain must not send anything")

    monkeypatch.setattr(outbox.whatsapp, "send_message", unexpected)
    result = await outbox.drain_channel(db_session, channel)

    assert result["skipped"] == "locked"


# ------------------------------------------------ every send path is routed
@pytest.mark.asyncio
async def test_an_operators_own_message_goes_out_on_the_tenants_provider(
    org_a, db_session, monkeypatch
):
    """This was a real bug: the dashboard's manual send always called Twilio.

    On a tenant paired over WhatsApp Web that sent nothing at all, while the
    message appeared in the thread as though the customer had received it.
    """
    import uuid as _uuid

    organization_id = _uuid.UUID(org_a.organization_id)
    db_session.add(
        ChannelConfig(
            organization_id=organization_id,
            channel="whatsapp",
            provider="twilio",
            phone_number="+14155551234",
            whatsapp_provider="QR_SESSION",
        )
    )
    contact = CRMContact(
        organization_id=organization_id,
        phone_number="+971500004444",
        pipeline_stage="LEAD",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()

    routed = []

    # Mirrors the real signature, including to_jid — a stub that accepts less
    # than the caller passes turns a threading mistake into a passing test.
    async def capture(channel, to_number, body, media_urls=None, to_jid=None):
        routed.append(outbox.whatsapp.provider_of(channel))
        return True, "WA-manual"

    monkeypatch.setattr(outbox.whatsapp, "send_message", capture)

    response = await org_a.post(
        "/api/v1/messages/send",
        json={"contact_id": str(contact.id), "content": "on its way today"},
    )

    assert response.status_code == 201
    assert routed == ["QR_SESSION"], "the operator's message ignored the tenant's provider"
    assert response.json()["delivery_status"] == outbox.SENT


# ------------------------------------------------------- addressing the chat
@pytest.mark.asyncio
async def test_the_chat_jid_is_carried_to_the_bridge(monkeypatch):
    """WhatsApp addresses many chats by LID, not by phone number.

    A JID rebuilt from digits — 153231615328393@s.whatsapp.net — is a valid
    address for a phone number nobody has. Baileys does not refuse it, so the
    send reported success, the dashboard showed the reply as delivered, and it
    reached no one. The chat's own JID has to go back untouched.
    """
    from app.config import settings
    from app.services import whatsapp

    monkeypatch.setattr(settings, "wa_qr_shared_secret", "secret")
    seen = {}

    class Recorder:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, json=None, headers=None):
            seen.update(json or {})

            class Response:
                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"ok": True, "id": "WA-lid"}

            return Response()

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", Recorder)

    channel = ChannelConfig(
        organization_id=None,
        channel="whatsapp",
        provider="whatsapp-web",
        phone_number="+923097209908",
        whatsapp_provider="QR_SESSION",
    )
    channel.id = "chan-lid"

    sent, _ = await whatsapp.send_message(
        channel, "153231615328393", "we have those in black",
        to_jid="153231615328393@lid",
    )

    assert sent is True
    assert seen["toJid"] == "153231615328393@lid", (
        "without the original JID the bridge rebuilds a phone-number address "
        "that reaches nobody"
    )


@pytest.mark.asyncio
async def test_a_queued_message_remembers_the_chat_it_belongs_to(fake_redis, monkeypatch):
    """A retry an hour later still has to reach the same chat."""
    channel = ChannelConfig(
        organization_id=None,
        channel="whatsapp",
        provider="whatsapp-web",
        phone_number="+923097209908",
        whatsapp_provider="QR_SESSION",
    )
    channel.id = "chan-lid-2"

    async def dead(*_a, **_k):
        return False, "qr-session-refused: session not connected"

    monkeypatch.setattr(outbox.whatsapp, "send_message", dead)
    await outbox.deliver(
        channel, "153231615328393", "still here?",
        message_id="m-lid", to_jid="153231615328393@lid",
    )

    parked = json.loads(fake_redis[outbox.queue_key("chan-lid-2")][0])
    assert parked["to_jid"] == "153231615328393@lid"

    delivered_to = {}

    async def alive(_channel, to_number, body, media_urls=None, to_jid=None):
        delivered_to["jid"] = to_jid
        return True, "WA-9"

    monkeypatch.setattr(outbox.whatsapp, "send_message", alive)
    # A bare drain against the same fake Redis, without a database session.
    async with outbox._connection() as client:
        raw = json.loads(await client.lindex(outbox.queue_key("chan-lid-2"), 0))
    await outbox.whatsapp.send_message(
        channel, raw["to"], raw["body"], raw.get("media_urls"), to_jid=raw.get("to_jid")
    )
    assert delivered_to["jid"] == "153231615328393@lid"
