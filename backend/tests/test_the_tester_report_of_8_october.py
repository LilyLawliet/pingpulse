"""The confirmed faults from the live WhatsApp test of 8 October, one by one.

    1. One customer message triggered multiple replies - even the handover
       acknowledgement arrived twice.
    2. The agent confirmed an appointment, then said none existed.
    3. It kept asking what work was needed after being told.
    4. It asked for confirmation after the booking had succeeded.
    5. A cancelled appointment left the lead on "Estimate scheduled".
    6. Messages the customer received were missing from the inbox.

Most of these trace to the same two causes, tested first: a redelivered
message answered a second time, and two businesses paired to one handset,
each answering every message from its own diary and its own memory.
"""

from __future__ import annotations

import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import ChannelConfig, CRMContact, Message, Organization
from app.services import booking, outbox, pipelines, scope, understanding

from .test_the_requested_service_holds import ADDRESS, CONSTRIVO, EVERY_DAY, Model


class FakeRedis:
    """Enough of Redis for a set-if-absent claim."""

    def __init__(self):
        self.keys: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def delete(self, key):
        self.keys.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    @asynccontextmanager
    async def connection():
        yield fake

    monkeypatch.setattr(outbox, "_connection", connection)
    return fake


async def constrivo(db_session) -> Organization:
    shop = Organization(name="Constrivo", sales_prompt=CONSTRIVO, product_rules=CONSTRIVO)
    shop.timezone = "UTC"
    shop.agent_config = {"business_hours": EVERY_DAY, "appointments": {"min_notice_minutes": 0}}
    db_session.add(shop)
    await db_session.flush()
    return shop


class WhatsApp:
    """The live inbound path, with the reply model and the send stubbed."""

    def __init__(self, db_session, monkeypatch, behaviour="honest"):
        from app.api.webhook import TwilioWebhookPayload, process_inbound_message
        from app.services.llm_service import GenerationResult
        from app.services.twilio_service import TwilioService

        self.db = db_session
        self.sent: list[str] = []
        self.payload, self.process = TwilioWebhookPayload, process_inbound_message
        model = Model("kitchen remodel", "dog grooming", behaviour)
        monkeypatch.setattr(understanding, "structured", model.structured)
        self.model = model

        async def send(_self, to_number, body, media_urls=None, sender=None):
            self.sent.append(body)
            return True, f"SM{random.randint(0, 10**9)}"

        async def reply(org, contact, history, latest_message, **kwargs):
            # The reply model at its least helpful: asks again for what it has.
            return GenerationResult(
                provider="groq",
                text="Shall I confirm that booking for you? And what work do you need?",
                prompt_used="",
                latency_ms=1,
            )

        monkeypatch.setattr(TwilioService, "send_whatsapp", send)
        monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", reply)
        self.count = 0

    async def say(self, text: str, sid: str | None = None) -> dict:
        self.count += 1
        before = len(self.sent)
        result = await self.process(
            self.db,
            self.payload.model_validate(
                {
                    "From": "whatsapp:+13055550144",
                    "To": "whatsapp:+14155550000",
                    "Body": text,
                    "MessageSid": sid or f"SM_in_{self.count}",
                    "ProfileName": "Ali",
                }
            ),
        )
        result["replies"] = self.sent[before:]
        return result

    async def contact(self) -> CRMContact:
        return (await self.db.execute(select(CRMContact))).scalar_one()


# ------------------------------------------------------------- 1. duplicates
async def test_a_redelivered_message_is_answered_once(db_session, monkeypatch, redis):
    """Twilio redelivers a slow turn; the first delivery has not committed its row yet."""
    shop = await constrivo(db_session)
    chat = WhatsApp(db_session, monkeypatch)
    from app.api import webhook

    # The second delivery arrives while the first is still working: its stored
    # row is not visible, so only the claim can stop it.
    async def nothing_stored_yet(db, organization_id, message_sid):
        return False

    monkeypatch.setattr(webhook, "already_handled", nothing_stored_yet)
    first = await chat.say("Hi, I need a kitchen remodel", sid="SM_same")
    again = await chat.say("Hi, I need a kitchen remodel", sid="SM_same")
    assert first["replies"] and again["status"] == "duplicate" and again["replies"] == []
    assert shop.id  # one organization, one answer


async def test_a_delivery_that_failed_can_be_answered_when_sent_again(db_session, monkeypatch, redis):
    await constrivo(db_session)
    chat = WhatsApp(db_session, monkeypatch)
    from app.api import webhook

    async def broken(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(webhook, "_recent_history", broken)
    with pytest.raises(RuntimeError):
        await chat.say("Hi", sid="SM_retry")
    assert not redis.keys, "a failed turn kept its claim, so its retry would be dropped"


async def test_a_second_business_on_the_same_handset_does_not_answer(client, db_session, monkeypatch):
    """Both linked sessions receive every message; only the holder may reply."""
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    holder_org = Organization(name="Constrivo", sales_prompt="Remodeling.")
    other_org = Organization(name="Test shop", sales_prompt="Remodeling.")
    db_session.add_all([holder_org, other_org])
    await db_session.flush()
    holder = ChannelConfig(
        organization_id=holder_org.id, channel="whatsapp", provider="twilio",
        phone_number="+923097209908", whatsapp_provider="QR_SESSION", session_status="AUTHENTICATED",
    )
    holder.created_at = datetime.now(timezone.utc) - timedelta(days=3)
    # Paired later onto the same phone: it never got the number.
    second = ChannelConfig(
        organization_id=other_org.id, channel="whatsapp", provider="twilio",
        phone_number=None, whatsapp_provider="QR_SESSION", session_status="AUTHENTICATED",
    )
    db_session.add_all([holder, second])
    await db_session.commit()

    answered = []
    from app.api import webhook

    async def process(db, payload, channel=None):
        answered.append(channel.organization_id)
        return {"delivered": True}

    monkeypatch.setattr(webhook, "process_inbound_message", process)
    for session in (holder, second):
        await client.post(
            "/api/v1/whatsapp/qr-inbound",
            headers={"X-PingPulse-Bridge": "the-secret"},
            json={"id": "ABC", "sessionId": str(session.id), "from": "+13055550144",
                  "to": "923097209908", "body": "hi"},
        )
    assert answered == [holder_org.id], "a business that does not hold the handset answered"


# ---------------------------------- 2, 4, 5. one booking, told the same way
async def test_a_booking_is_confirmed_from_the_record_and_never_asked_again(db_session, monkeypatch, redis):
    await constrivo(db_session)
    chat = WhatsApp(db_session, monkeypatch)
    await chat.say("Hi, I need a kitchen remodel")
    await chat.say(ADDRESS)
    read_back = await chat.say("can you come tomorrow at 10am?")
    assert read_back["replies"][0].startswith("To confirm: site visit for kitchen remodeling")

    booked = await chat.say("yes")
    assert booked["replies"][0].startswith("You're booked: site visit for kitchen remodeling"), booked
    assert "confirm" not in booked["replies"][0].lower(), "a booking that succeeded was put to them again"
    assert (await chat.contact()).pipeline_stage == "ESTIMATE_SCHEDULED"

    asked = await chat.say("is my appointment confirmed?")
    assert asked["replies"][0].startswith("Yes - your site visit for kitchen remodeling")

    await chat.say("please cancel my appointment")
    cancelled = await chat.say("yes")
    assert "has been cancelled" in cancelled["replies"][0]
    contact = await chat.contact()
    assert contact.pipeline_stage != "ESTIMATE_SCHEDULED", "the lead still says booked after the cancel"
    gone = await chat.say("is my appointment still on?")
    assert "can't find an appointment" in gone["replies"][0]

    # Every message they received is in the inbox, as sent.
    agent_rows = (
        await db_session.execute(select(Message).where(Message.sender == "agent"))
    ).scalars().all()
    assert len(agent_rows) == len(chat.sent)


async def test_the_board_goes_back_to_where_the_lead_was(db_session):
    shop = await constrivo(db_session)
    contact = CRMContact(organization_id=shop.id, phone_number="+1305555", pipeline_stage="QUALIFIED")
    db_session.add(contact)
    await db_session.flush()
    from app.services import analytics

    contact.pipeline_stage = "ESTIMATE_SCHEDULED"
    await analytics.record_move(db_session, contact, "ESTIMATE_SCHEDULED", from_stage="QUALIFIED")
    assert await pipelines.stage_after_cancel(db_session, shop.id, contact) == "QUALIFIED"

    # Somebody moved it on since: it is left where they put it.
    contact.pipeline_stage = "WON"
    assert await pipelines.stage_after_cancel(db_session, shop.id, contact) is None


# --------------------------------------------- 3. not asked again what work
async def test_what_the_record_holds_is_not_asked_for(db_session):
    from app.services import qualification

    shop = await constrivo(db_session)
    contact = CRMContact(organization_id=shop.id, phone_number="+1305555", contact_metadata={}, qualification={})
    scope._remember_acceptance(contact, scope.Verdict(job="kitchen remodel", service_fits=True))
    booking.note_where(contact, ADDRESS)
    booking.fill_from_record(shop, contact)
    block = qualification.as_prompt_block(shop, contact.qualification)
    unknown = block.split("Still unknown:", 1)[-1] if "Still unknown:" in block else ""
    assert "job_type" not in unknown and "location" not in unknown, block
    assert contact.qualification["job_type"] == "kitchen remodeling"


async def test_work_that_cannot_be_placed_goes_to_a_person_the_second_time(db_session, monkeypatch):
    """Not the same question again: their answer the second time is a person's call."""
    shop = await constrivo(db_session)
    contact = CRMContact(organization_id=shop.id, phone_number="+1305555", contact_metadata={}, qualification={})
    db_session.add(contact)
    await db_session.flush()

    async def busy(prompt, timeout):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(understanding, "structured", busy)
    first = await booking.handle_turn(db_session, shop, contact, "I need new cabinets and countertops, can you come tomorrow at 10am?")
    assert first.refusal.reason == "needs_job" and "which of those" in (first.reply or "").lower()
    second = await booking.handle_turn(db_session, shop, contact, "new cabinets and countertops, tomorrow at 10am")
    assert second.refusal is not None and second.refusal.reason == "needs_person", second
    assert not second.offered and second.performed is None

    # And the reply model is not told the job is unknown: their own words are on record.
    booking.fill_from_record(shop, contact)
    assert "cabinets" in (contact.qualification or {}).get("job_type", ""), contact.qualification


# ------------------------------------------------------- 6. the whole thread
async def test_the_inbox_shows_the_latest_messages(org_a, db_session):
    from .conftest import _session_for

    session = _session_for(org_a._client)
    contact = CRMContact(organization_id=uuid.UUID(org_a.organization_id), phone_number="+1305555")
    session.add(contact)
    await session.flush()
    start = datetime.now(timezone.utc) - timedelta(hours=5)
    for index in range(250):
        session.add(
            Message(
                organization_id=contact.organization_id, contact_id=contact.id,
                sender="user" if index % 2 else "agent", content=f"message {index}",
                created_at=start + timedelta(seconds=index),
            )
        )
    await session.flush()
    shown = (await org_a.get(f"/api/v1/contacts/{contact.id}/messages")).json()
    assert shown[-1]["content"] == "message 249", "the newest message was cut off"
    assert [m["content"] for m in shown] == sorted(
        (m["content"] for m in shown), key=lambda c: int(c.split()[1])
    ), "not oldest first"


# --------------------------------------- automation stops when a person has it
def test_no_automatic_nudge_after_a_handover():
    from app import tasks

    contact = CRMContact(
        phone_number="+1305555", ai_enabled=False, sales_stage="INTERESTED",
        contact_metadata={"followup_token": "t"},
    )
    assert tasks.refuse_followup(contact, "t", 1, manual=False) == tasks.HANDED_OVER
    # An operator's own follow-up is theirs to send.
    assert tasks.refuse_followup(contact, "t", 1, manual=True) != tasks.HANDED_OVER
