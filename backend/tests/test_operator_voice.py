"""The business speaks with two voices, and they have to be told apart.

A reply from "the business" used to be one thing in the database. It is two: the
model's output, and a person typing in their own words when they take a
conversation over from the dashboard. Both were stored as `agent`.

That matters more than bookkeeping. A shop's own replies are the only honest
record of how that shop actually talks to its customers, and that is what a
persona should be learned from. Conflated with the model's output, learning from
them means learning from the model's own words — a copy of a copy, drifting
further from the shop each round.

And it cannot be fixed later. Nothing distinguishes the two once both are
written down as `agent`, so the distinction has to be recorded when the message
is written or it is gone. These tests hold that line.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import SENDER_AGENT, SENDER_CUSTOMER, SENDER_OPERATOR, CRMContact, Message


@pytest.fixture
async def contact(org_a, db_session):
    row = CRMContact(
        organization_id=uuid.UUID(org_a.organization_id),
        phone_number="+971500001234",
        pipeline_stage="LEAD",
        sales_stage="NEW",
        tags=[],
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.fixture
def delivered(monkeypatch):
    from app.services import outbox

    async def sent(*_a, **_k):
        return outbox.Delivery(outbox.SENT, "SM-manual", "delivered")

    monkeypatch.setattr(outbox, "deliver", sent)


# --------------------------------------------------------- writing it down
@pytest.mark.asyncio
async def test_a_takeover_is_recorded_as_the_person_who_wrote_it(
    org_a, db_session, contact, delivered
):
    """The whole point. Stored as `agent`, this reply would be indistinguishable
    from the model's own, and the shop's voice would be unrecoverable."""
    response = await org_a.post(
        "/api/v1/messages/send",
        json={"contact_id": str(contact.id), "content": "bhai I can do 2400, final"},
    )

    assert response.status_code == 201
    assert response.json()["sender"] == SENDER_OPERATOR, (
        "a person's words were filed as the model's"
    )

    stored = (
        await db_session.execute(select(Message).where(Message.contact_id == contact.id))
    ).scalars().all()
    assert [m.sender for m in stored] == [SENDER_OPERATOR]


@pytest.mark.asyncio
async def test_the_agents_own_replies_are_still_the_agents(
    client, db_session, monkeypatch
):
    """The separation must not relabel what the model writes."""
    from app.config import settings
    from app.models import ChannelConfig, Organization
    from app.services import outbox

    monkeypatch.setattr(settings, "twilio_validate_signature", False)

    organization = Organization(name="Voice Co", sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()
    db_session.add(
        ChannelConfig(
            organization_id=organization.id,
            channel="whatsapp",
            provider="twilio",
            phone_number="+14155557777",
        )
    )
    await db_session.flush()

    async def sent(*_a, **_k):
        return True, "SM-auto"

    monkeypatch.setattr(outbox.whatsapp, "send_message", sent)

    await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "MessageSid": "SM_in",
            "From": "whatsapp:+971500002222",
            "To": "whatsapp:+14155557777",
            "Body": "do you deliver?",
            "NumMedia": "0",
        },
    )

    senders = {
        m.sender
        for m in (
            await db_session.execute(
                select(Message).where(Message.organization_id == organization.id)
            )
        ).scalars().all()
    }
    assert senders == {SENDER_CUSTOMER, SENDER_AGENT}, (
        "an automatic reply must not be filed as a person's"
    )


@pytest.mark.asyncio
async def test_a_takeover_still_goes_out_over_the_real_transport(
    org_a, db_session, contact, delivered
):
    """Relabelling must not change what actually happens to the message."""
    response = await org_a.post(
        "/api/v1/messages/send",
        json={"contact_id": str(contact.id), "content": "sending you the photo now"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["delivery_status"] == "SENT"
    assert body["twilio_sid"] == "SM-manual"


# ------------------------------------------------------ what the model sees
def test_the_transcript_names_the_shop_separately_from_the_agent():
    """Three roles, because the model should follow the shop and not itself.

    A `Shop:` line is a person who took over in front of this same customer.
    Rendering it as `Agent:` tells the model its own previous output is an
    example to imitate, which is exactly the feedback loop being removed.
    """
    from app.services.llm_service import format_history

    class Row:
        def __init__(self, sender, content):
            self.sender = sender
            self.content = content

    transcript = format_history([
        Row(SENDER_CUSTOMER, "how much for the black pair?"),
        Row(SENDER_AGENT, "They are $89."),
        Row(SENDER_OPERATOR, "for you 2400, deal"),
    ])

    assert "Customer: how much for the black pair?" in transcript
    assert "Agent: They are $89." in transcript
    assert "Shop: for you 2400, deal" in transcript
    assert "Agent: for you 2400, deal" not in transcript


def test_an_unknown_sender_is_read_as_the_business_not_the_customer():
    """Old rows, and anything unexpected, must never be read as the customer.

    Mistaking our own words for theirs would have the model answering itself.
    """
    from app.services.llm_service import format_history

    class Row:
        sender = "something_new"
        content = "hello"

    assert format_history([Row()]).startswith("Agent:")
