"""The two ways a conversation goes quiet on our side, and why they differ.

*Opt-out* is the customer's decision and it is the one with legal weight.
Somebody replied STOP. Nothing outbound may reach them again — not a generated
reply, not a nudge already sitting in the broker waiting to fire. The tests
below are mostly about that second case, because it is the one that gets
missed: the flag is easy, and the queued task that fires four hours later is
what actually sends a message to somebody who asked us to stop.

*Takeover* is the shop's decision, and it is deliberately weaker. A person has
stepped into this conversation, so the agent stays out of it — but messages
still arrive, are still recorded, and still appear on the dashboard. Only the
generating stops. A takeover that hid the conversation would be worse than no
takeover, because the operator who claimed it is the one who needs to read it.

The asymmetry is the point, and one test holds it down directly: an opted-out
customer must not receive a message a human typed either, whereas a taken-over
conversation is precisely one a human is typing in.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import CRMContact, Message
from app.services import consent


# ------------------------------------------------------- reading the words
def test_a_bare_stop_is_an_opt_out():
    assert consent.is_opt_out("STOP")
    assert consent.is_opt_out("stop")
    assert consent.is_opt_out("Please unsubscribe")
    assert consent.is_opt_out("remove me")


def test_stop_inside_a_sentence_is_not_an_opt_out():
    """Unsubscribing somebody who did not ask is its own failure, and the
    message that triggers it here is an enthusiastic customer."""
    assert not consent.is_opt_out("don't stop sending me pictures, I love them")
    assert not consent.is_opt_out(
        "I was going to stop by the shop tomorrow afternoon if that suits you"
    )


def test_a_long_message_mentioning_stop_is_a_conversation():
    assert not consent.is_opt_out(
        "hi there, my neighbour said you stop taking orders at five so I wanted "
        "to ask whether the boots I saw are still available in a 42"
    )


def test_start_brings_them_back():
    assert consent.is_opt_in("START")
    assert consent.is_opt_in("resume")
    assert not consent.is_opt_in("start by telling me what colours you have")


# --------------------------------------------------------------- the flags
def _contact(**kwargs):
    fields = {
        "organization_id": uuid.uuid4(),
        "phone_number": "+15550001",
        "pipeline_stage": "NEW_LEAD",
        "sales_stage": "NEW",
        "tags": [],
    }
    fields.update(kwargs)
    return CRMContact(**fields)


def test_opting_out_blocks_everything_including_a_human():
    """The asymmetry. A person may type into a taken-over conversation; nobody
    may type into an opted-out one."""
    contact = _contact()
    consent.record_opt_out(contact)

    assert consent.may_send(contact) is False
    assert consent.agent_may_reply(contact) is False


def test_takeover_stops_the_agent_but_not_a_person():
    contact = _contact(ai_enabled=False)

    assert consent.may_send(contact) is True
    assert consent.agent_may_reply(contact) is False


def test_opting_out_cancels_the_nudges_already_queued():
    """Setting the flag stops future sends. Clearing the token is what stops
    the two tasks already sitting in the broker."""
    contact = _contact(contact_metadata={"followup_token": "abc123"})
    consent.record_opt_out(contact)

    assert "followup_token" not in contact.contact_metadata
    assert contact.opt_out_at is not None


def test_opting_back_in_is_timestamped_not_assumed():
    contact = _contact()
    consent.record_opt_out(contact)
    consent.record_opt_in(contact)

    assert contact.opt_out is False
    assert contact.consent_at is not None, "consent was restored without a record of when"


# ------------------------------------------------- the queued nudge that fires
def test_a_queued_nudge_refuses_when_they_opted_out_in_the_meantime():
    """The case this exists for.

    A nudge is queued while the lead is warm and fires hours later. The opt-out
    almost always lands inside that window, so checking only at queue time
    sends a message to somebody who has since asked us to stop.
    """
    from app import tasks

    contact = _contact(
        sales_stage="QUALIFIED",
        opt_out=True,
        contact_metadata={"followup_token": "tok"},
    )

    assert tasks.refuse_followup(contact, "tok", attempt=1, manual=False) == tasks.OPTED_OUT


def test_an_opt_out_outranks_a_warm_stage_and_a_manual_request():
    """Somebody who said STOP gets nothing regardless of how good the lead
    looked, and regardless of an operator asking for the nudge by hand."""
    from app import tasks

    contact = _contact(
        sales_stage="NEGOTIATION",
        opt_out=True,
        contact_metadata={"followup_token": "tok"},
    )

    assert tasks.refuse_followup(contact, "tok", attempt=1, manual=True) == tasks.OPTED_OUT


def test_a_nudge_for_a_customer_who_replied_is_already_cancelled():
    from app import tasks

    contact = _contact(sales_stage="QUALIFIED", contact_metadata={})

    assert tasks.refuse_followup(contact, "tok", attempt=1, manual=False) == tasks.CANCELLED


def test_a_warm_lead_who_said_nothing_still_gets_their_nudge():
    """The guard has to let the ordinary case through, or the feature is off."""
    from app import tasks

    contact = _contact(sales_stage="QUALIFIED", contact_metadata={"followup_token": "tok"})

    assert tasks.refuse_followup(contact, "tok", attempt=1, manual=False) is None


def test_a_stale_task_past_the_cap_is_refused_rather_than_raising():
    from app import tasks

    contact = _contact(sales_stage="QUALIFIED", contact_metadata={"followup_token": "tok"})

    assert tasks.refuse_followup(contact, "tok", attempt=9, manual=False) is not None


def test_nothing_is_queued_for_somebody_who_opted_out():
    from app import tasks

    contact = _contact(sales_stage="QUALIFIED", opt_out=True)
    assert tasks.schedule_followups(contact) is None


def test_there_is_no_fourth_nudge():
    """Three is a promise to the customer, not a dial. A fourth automated
    message to somebody who answered none of the first three is the reason
    numbers get reported."""
    from app import tasks

    assert tasks.MAX_FOLLOWUPS == 3
    assert len(tasks.NUDGES) == 3


# ------------------------------------------------------------ the inbound path
@pytest.mark.asyncio
async def test_saying_stop_records_the_message_and_answers_nothing(
    org_a, db_session, monkeypatch
):
    """Recorded and shown, never answered. A shop still needs to see that the
    customer said it."""
    from sqlalchemy import select

    from app.api import webhook
    from app.schemas import TwilioWebhookPayload

    channel = await _qr_channel(org_a, db_session)
    replies: list = []
    monkeypatch.setattr(
        "app.services.outbox.deliver", lambda *a, **k: replies.append(a)
    )

    payload = TwilioWebhookPayload(
        From="whatsapp:+15550123",
        To="whatsapp:+923097209908",
        Body="STOP",
        MessageSid="SM-stop",
    )
    result = await webhook.process_inbound_message(db_session, payload, channel)

    assert result["status"] == "opted_out"
    assert replies == []

    contact = (
        await db_session.execute(
            select(CRMContact).where(CRMContact.phone_number == "+15550123")
        )
    ).scalar_one()
    assert contact.opt_out is True

    stored = (
        await db_session.execute(
            select(Message).where(Message.contact_id == contact.id)
        )
    ).scalars().all()
    assert [m.content for m in stored] == ["STOP"], "the opt-out message was not recorded"


@pytest.mark.asyncio
async def test_a_taken_over_conversation_records_but_does_not_answer(
    org_a, db_session, monkeypatch
):
    from sqlalchemy import select

    from app.api import webhook
    from app.schemas import TwilioWebhookPayload

    channel = await _qr_channel(org_a, db_session)
    contact = CRMContact(
        organization_id=uuid.UUID(org_a.organization_id),
        phone_number="+15550124",
        pipeline_stage="NEW_LEAD",
        sales_stage="NEW",
        tags=[],
        ai_enabled=False,
    )
    db_session.add(contact)
    await db_session.flush()

    replies: list = []
    monkeypatch.setattr("app.services.outbox.deliver", lambda *a, **k: replies.append(a))

    payload = TwilioWebhookPayload(
        From="whatsapp:+15550124",
        To="whatsapp:+923097209908",
        Body="how much are the boots?",
        MessageSid="SM-takeover",
    )
    result = await webhook.process_inbound_message(db_session, payload, channel)

    assert result["reason"] == "human_takeover"
    assert replies == []

    stored = (
        await db_session.execute(select(Message).where(Message.contact_id == contact.id))
    ).scalars().all()
    assert len(stored) == 1, "the customer's message was not recorded for the operator"


async def _qr_channel(org_a, db_session):
    from app.models import ChannelConfig

    channel = ChannelConfig(
        organization_id=uuid.UUID(org_a.organization_id),
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        phone_number="+923097209908",
        session_status="AUTHENTICATED",
    )
    db_session.add(channel)
    await db_session.flush()
    return channel


# ------------------------------------------------------------------- the API
@pytest.mark.asyncio
async def test_takeover_can_be_toggled_and_is_audited(org_a, db_session):
    """Who turned the agent off is asked in exactly the situations where
    nobody can remember."""
    from sqlalchemy import select

    from app.models import AuditLog

    contact = CRMContact(
        organization_id=uuid.UUID(org_a.organization_id),
        phone_number="+15550125",
        pipeline_stage="NEW_LEAD",
        sales_stage="NEW",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()

    response = await org_a.post(
        f"/api/v1/contacts/{contact.id}/takeover", json={"ai_enabled": False}
    )

    assert response.status_code == 200
    assert response.json()["ai_enabled"] is False

    logged = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.organization_id == uuid.UUID(org_a.organization_id))
        )
    ).scalars().all()
    assert [row.action for row in logged] == ["contact.takeover"]
    assert logged[0].user_id is not None, "an audit row with no author is not an audit row"


@pytest.mark.asyncio
async def test_another_tenants_contact_cannot_be_taken_over(org_a, org_b, db_session):
    contact = CRMContact(
        organization_id=uuid.UUID(org_b.organization_id),
        phone_number="+15550126",
        pipeline_stage="NEW_LEAD",
        sales_stage="NEW",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()

    response = await org_a.post(
        f"/api/v1/contacts/{contact.id}/takeover", json={"ai_enabled": False}
    )

    assert response.status_code == 404, "a cross-tenant miss must not be a 403"
