"""Picking up conversations a shop never answered, without becoming a spammer.

Somewhere in a shop's WhatsApp history are people who asked about a product and
never got a reply. They are the warmest leads the shop owns and nobody is
working them, which makes this the most valuable thing in the history sync.

It is also the most dangerous. The transport is an unofficial WhatsApp Web
client, and bulk outbound on one of those gets a number permanently banned —
the shop's number, which is their business. The whole design turns on one
distinction: finishing a conversation a customer started is not the same act as
messaging a list of people, even when the text is identical.

So these hold down the things that keep it the first and not the second:

  * a conversation the shop already answered is not a lead at all;
  * a complaint is never treated as a sales opportunity;
  * the window is short, because answering an eight-month-old question reads as
    a mailing list;
  * the caller cannot name a number that is not on the list;
  * and there is a daily cap that refuses rather than queues.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import ChannelConfig, CRMContact
from app.services import prospects


def _at(days_ago: float) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())


def _chat(jid, messages, name="Sara"):
    return {"jid": jid, "pushName": name, "messages": messages}


@pytest.fixture
async def qr_channel(org_a, db_session):
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


@pytest.fixture
def whatsapp_history(monkeypatch):
    def load(chats):
        async def read(_channel):
            return chats

        monkeypatch.setattr(prospects, "read_history", read)

    return load


# ------------------------------------------------------------ who counts
@pytest.mark.asyncio
async def test_an_unanswered_price_question_is_a_lead(org_a, qr_channel, whatsapp_history):
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "hi, how much for the black boots?", "at": _at(3)},
        ])
    ])

    body = (await org_a.get("/api/v1/prospects")).json()

    assert body["available"] is True
    assert len(body["prospects"]) == 1
    found = body["prospects"][0]
    assert found["number"] == "923001234567"
    assert "how much" in found["matched"]
    assert found["days_ago"] == 3


@pytest.mark.asyncio
async def test_a_conversation_the_shop_answered_is_left_alone(
    org_a, qr_channel, whatsapp_history
):
    """The shop got the last word, so this customer has their answer. Messaging
    them again is not a follow-up, it is pestering."""
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "how much for the black boots?", "at": _at(3)},
            {"fromMe": True, "text": "They are $189, in stock.", "at": _at(3)},
        ])
    ])

    assert (await org_a.get("/api/v1/prospects")).json()["prospects"] == []


@pytest.mark.asyncio
async def test_a_complaint_is_never_a_sales_lead(org_a, qr_channel, whatsapp_history):
    """It contains buying words and is the worst possible thing to answer with
    a pitch."""
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "the boots I ordered are broken, I want a refund", "at": _at(2)},
        ])
    ])

    assert (await org_a.get("/api/v1/prospects")).json()["prospects"] == []


@pytest.mark.asyncio
async def test_small_talk_is_not_a_lead(org_a, qr_channel, whatsapp_history):
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "salam bhai, kya haal hai", "at": _at(1)},
        ])
    ])

    assert (await org_a.get("/api/v1/prospects")).json()["prospects"] == []


@pytest.mark.asyncio
async def test_an_old_conversation_falls_outside_the_window(
    org_a, qr_channel, whatsapp_history
):
    """Answering a question from months ago reads as a mailing list."""
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "do you have these in size 42?", "at": _at(200)},
        ])
    ])

    assert (await org_a.get("/api/v1/prospects")).json()["prospects"] == []


@pytest.mark.asyncio
async def test_someone_already_in_the_crm_is_not_listed_again(
    org_a, qr_channel, whatsapp_history, db_session
):
    """They are already in a live conversation with the agent; offering to
    start a second one alongside it would be two shops talking at once."""
    db_session.add(
        CRMContact(
            organization_id=uuid.UUID(org_a.organization_id),
            phone_number="+923001234567",
            pipeline_stage="LEAD",
            sales_stage="NEW",
            tags=[],
        )
    )
    await db_session.flush()

    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "what is the price of the scarf?", "at": _at(2)},
        ])
    ])

    assert (await org_a.get("/api/v1/prospects")).json()["prospects"] == []


@pytest.mark.asyncio
async def test_the_shops_own_words_do_not_make_a_lead(org_a, qr_channel, whatsapp_history):
    """Our catalogue terms appear in our own replies by definition. Matching on
    those would make every conversation we ever had look like a lead."""
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": True, "text": "our boots are $189 and in stock, delivery next day", "at": _at(4)},
            {"fromMe": False, "text": "ok thanks", "at": _at(3)},
        ])
    ])

    assert (await org_a.get("/api/v1/prospects")).json()["prospects"] == []


# --------------------------------------------------------------- replying
@pytest.mark.asyncio
async def test_replying_answers_one_named_conversation(
    org_a, qr_channel, whatsapp_history, monkeypatch
):
    from app.services import outbox

    async def delivered(*_a, **_k):
        return outbox.Delivery(outbox.SENT, "SM-pickup", "delivered")

    monkeypatch.setattr(outbox, "deliver", delivered)
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "how much for the black boots?", "at": _at(3)},
        ])
    ])

    response = await org_a._client.post(
        "/api/v1/prospects/reply",
        headers=org_a.headers,
        json={
            "jid": "923001234567@s.whatsapp.net",
            "message": "Hi! Sorry for the slow reply — the black boots are $189.",
        },
    )

    assert response.status_code == 201
    assert response.json()["delivery_status"] == "SENT"
    assert response.json()["sent_today"] == 1


@pytest.mark.asyncio
async def test_a_number_not_on_the_list_cannot_be_messaged(
    org_a, qr_channel, whatsapp_history, monkeypatch
):
    """The guard that stops this becoming a send-to-anyone endpoint. The
    history is re-read rather than the caller's word being taken for it."""
    from app.services import outbox

    sent = []

    async def capture(*a, **k):
        sent.append(a)
        return outbox.Delivery(outbox.SENT, "SM", "delivered")

    monkeypatch.setattr(outbox, "deliver", capture)
    whatsapp_history([])

    response = await org_a._client.post(
        "/api/v1/prospects/reply",
        headers=org_a.headers,
        json={"jid": "923009999999@s.whatsapp.net", "message": "buy our shoes"},
    )

    assert response.status_code == 404
    assert sent == [], "a number nobody asked about was messaged"


@pytest.mark.asyncio
async def test_an_answered_conversation_cannot_be_picked_up(
    org_a, qr_channel, whatsapp_history, monkeypatch
):
    """Not merely hidden from the list — refused at the point of sending."""
    from app.services import outbox

    sent = []

    async def capture(*a, **k):
        sent.append(a)
        return outbox.Delivery(outbox.SENT, "SM", "delivered")

    monkeypatch.setattr(outbox, "deliver", capture)
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "price for boots?", "at": _at(3)},
            {"fromMe": True, "text": "$189", "at": _at(3)},
        ])
    ])

    response = await org_a._client.post(
        "/api/v1/prospects/reply",
        headers=org_a.headers,
        json={"jid": "923001234567@s.whatsapp.net", "message": "still interested?"},
    )

    assert response.status_code == 404
    assert sent == []


@pytest.mark.asyncio
async def test_the_daily_cap_refuses_rather_than_queues(
    org_a, qr_channel, whatsapp_history, monkeypatch
):
    """Sending faster than this is what gets a number banned, so the cap is a
    refusal — queueing would only delay the same outcome."""
    from app.api import prospects as api
    from app.services import outbox

    async def delivered(*_a, **_k):
        return outbox.Delivery(outbox.SENT, "SM", "delivered")

    monkeypatch.setattr(outbox, "deliver", delivered)

    async def already_sent(*_a, **_k):
        return api.DAILY_LIMIT

    monkeypatch.setattr(api, "_sent_today", already_sent)
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "how much are the boots?", "at": _at(1)},
        ])
    ])

    response = await org_a._client.post(
        "/api/v1/prospects/reply",
        headers=org_a.headers,
        json={"jid": "923001234567@s.whatsapp.net", "message": "hello"},
    )

    assert response.status_code == 429
    assert "banned" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_twilio_tenant_has_no_history_to_read(org_a, db_session):
    db_session.add(
        ChannelConfig(
            organization_id=uuid.UUID(org_a.organization_id),
            channel="whatsapp",
            provider="twilio",
            whatsapp_provider="TWILIO",
            phone_number="+14155552671",
        )
    )
    await db_session.flush()

    body = (await org_a.get("/api/v1/prospects")).json()

    assert body["available"] is False
    assert body["prospects"] == []


@pytest.mark.asyncio
async def test_a_picked_up_reply_is_recorded_as_the_operators(
    org_a, qr_channel, whatsapp_history, db_session, monkeypatch
):
    """A person chose to send this and chose these words. Filing it as the
    agent's would put it in the material the agent learns its voice from."""
    from sqlalchemy import select

    from app.models import SENDER_OPERATOR, Message
    from app.services import outbox

    async def delivered(*_a, **_k):
        return outbox.Delivery(outbox.SENT, "SM", "delivered")

    monkeypatch.setattr(outbox, "deliver", delivered)
    whatsapp_history([
        _chat("923001234567@s.whatsapp.net", [
            {"fromMe": False, "text": "cost of the scarf?", "at": _at(2)},
        ])
    ])

    await org_a._client.post(
        "/api/v1/prospects/reply",
        headers=org_a.headers,
        json={"jid": "923001234567@s.whatsapp.net", "message": "It is $34."},
    )

    stored = (
        await db_session.execute(
            select(Message).where(Message.organization_id == uuid.UUID(org_a.organization_id))
        )
    ).scalars().all()
    assert [m.sender for m in stored] == [SENDER_OPERATOR]
