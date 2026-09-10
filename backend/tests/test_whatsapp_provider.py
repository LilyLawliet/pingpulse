"""Dual-provider routing.

A tenant sends either through Twilio's API or through a paired WhatsApp Web
session. The rule these protect is that the choice changes the transport and
nothing else: the same tables, the same pipeline, the same history.

The failure worth guarding against is drift — the two paths quietly growing
different behaviour, so a client who switches provider finds their
conversations gone or their agent behaving differently.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models import ChannelConfig, Organization
from app.services import whatsapp


def make_channel(provider=None, **kwargs) -> ChannelConfig:
    return ChannelConfig(
        organization_id=None,
        channel="whatsapp",
        provider="twilio",
        phone_number=kwargs.pop("phone_number", "+14155238886"),
        whatsapp_provider=provider,
        **kwargs,
    )


# ------------------------------------------------------------------ routing
def test_a_channel_with_no_provider_set_uses_twilio():
    """Existing rows predate the column. They must keep working untouched."""
    assert whatsapp.provider_of(make_channel(provider=None)) == whatsapp.TWILIO


def test_an_unrecognised_provider_falls_back_to_twilio():
    """A typo in the database must not silently route through the risky path."""
    assert whatsapp.provider_of(make_channel(provider="WHATEVER")) == whatsapp.TWILIO


def test_the_provider_is_read_from_the_channel():
    assert whatsapp.provider_of(make_channel(provider="QR_SESSION")) == whatsapp.QR_SESSION
    assert whatsapp.provider_of(make_channel(provider="TWILIO")) == whatsapp.TWILIO


def test_provider_matching_is_case_insensitive():
    assert whatsapp.provider_of(make_channel(provider="qr_session")) == whatsapp.QR_SESSION


def test_a_missing_channel_still_routes_somewhere():
    """resolve_organization can return no channel; sending must not explode."""
    assert whatsapp.provider_of(None) == whatsapp.TWILIO


# ------------------------------------------------------------- twilio path
@pytest.mark.asyncio
async def test_twilio_channels_go_to_the_twilio_service(monkeypatch):
    calls = []

    async def fake_send(to_number, body, media_urls=None, sender=None):
        calls.append((to_number, body, sender.auth_token))
        return True, "SM_twilio"

    monkeypatch.setattr(whatsapp.twilio_service, "send_whatsapp", fake_send)

    delivered, reference = await whatsapp.send_message(
        make_channel(provider="TWILIO", account_sid="ACx", auth_token="tenant-token"),
        "+971500000001",
        "hello",
    )

    assert delivered is True
    assert reference == "SM_twilio"
    # The tenant's own credentials, not the platform's.
    assert calls[0][2] == "tenant-token"


# ---------------------------------------------------------- qr session path
@pytest.mark.asyncio
async def test_qr_channels_go_to_the_bridge(monkeypatch):
    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "id": "BAE5F00D"}

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, json=None, headers=None):
            # Mirrors the real call. The stub used to accept only (url, json),
            # so a send that forgot its auth header still passed here while
            # the live bridge answered 403 — the test was blind to the one
            # thing that broke.
            sent["url"] = url
            sent["payload"] = json
            sent["headers"] = headers or {}
            return FakeResponse()

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", FakeClient)

    channel = make_channel(provider="QR_SESSION")
    delivered, reference = await whatsapp.send_message(channel, "+971500000002", "hi")

    assert delivered is True
    assert reference == "BAE5F00D"
    assert sent["url"].endswith("/send")
    assert "X-PingPulse-Bridge" in sent["headers"], "the bridge refuses an unauthenticated send"
    # The whatsapp: prefix is stripped here; the bridge strips the rest of the
    # non-digits itself when it builds the JID.
    assert sent["payload"]["to"] == "+971500000002"


@pytest.mark.asyncio
async def test_a_bridge_outage_is_reported_not_raised(monkeypatch):
    """The bridge being down must degrade like a Twilio failure, not 500."""

    class ExplodingClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_, **__):
            raise ConnectionError("bridge is down")

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", ExplodingClient)

    delivered, reference = await whatsapp.send_message(
        make_channel(provider="QR_SESSION"), "+971500000003", "hi"
    )

    assert delivered is False
    assert "qr-session-error" in reference


@pytest.mark.asyncio
async def test_a_refusal_from_the_bridge_is_reported(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "error": "session not connected"}

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_, **__):
            return FakeResponse()

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", FakeClient)

    delivered, reference = await whatsapp.send_message(
        make_channel(provider="QR_SESSION"), "+971500000004", "hi"
    )

    assert delivered is False
    assert "session not connected" in reference


# --------------------------------------------------------- unified storage
@pytest.mark.asyncio
async def test_switching_provider_keeps_the_conversation(client, db_session, monkeypatch):
    """The whole promise of the dual-provider design.

    A tenant flips from Twilio to a paired session; their contacts, messages
    and pipeline stages must all still be there afterwards.
    """
    monkeypatch.setattr(settings, "twilio_validate_signature", False)

    organization = Organization(name="Switcher Co", sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()

    channel = ChannelConfig(
        organization_id=organization.id,
        channel="whatsapp",
        provider="twilio",
        phone_number="+14155559999",
        whatsapp_provider="TWILIO",
    )
    db_session.add(channel)
    await db_session.flush()

    # A conversation arrives while on Twilio.
    await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "MessageSid": "SM_before_switch",
            "From": "whatsapp:+971500001111",
            "To": "whatsapp:+14155559999",
            "Body": "hello there",
            "NumMedia": "0",
        },
    )

    from sqlalchemy import func, select

    from app.models import CRMContact, Message

    before_contacts = await db_session.scalar(
        select(func.count()).select_from(CRMContact).where(
            CRMContact.organization_id == organization.id
        )
    )
    before_messages = await db_session.scalar(
        select(func.count()).select_from(Message).where(
            Message.organization_id == organization.id
        )
    )
    assert before_contacts >= 1, "the Twilio message should have created a contact"

    # Switch the transport.
    channel.whatsapp_provider = "QR_SESSION"
    await db_session.flush()

    after_contacts = await db_session.scalar(
        select(func.count()).select_from(CRMContact).where(
            CRMContact.organization_id == organization.id
        )
    )
    after_messages = await db_session.scalar(
        select(func.count()).select_from(Message).where(
            Message.organization_id == organization.id
        )
    )

    assert after_contacts == before_contacts
    assert after_messages == before_messages


@pytest.mark.asyncio
async def test_the_bridge_inbound_route_rejects_an_unknown_caller(client):
    """The bridge endpoint is internal; nothing else may post to it."""
    response = await client.post(
        "/api/v1/whatsapp/qr-inbound",
        json={"id": "x", "from": "971500000000", "to": "14155238886", "body": "hi"},
        headers={"X-PingPulse-Bridge": "not-the-secret"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_bridge_status_route_rejects_an_unknown_caller(client):
    response = await client.post(
        "/api/v1/whatsapp/qr-status",
        json={"sessionId": "x", "status": "AUTHENTICATED"},
    )

    assert response.status_code == 403


# ------------------------------------------------------- delivery honesty
@pytest.mark.asyncio
async def test_a_failed_send_is_not_recorded_as_delivered(client, db_session, monkeypatch):
    """A reply the customer never saw must not look like one they did.

    This is the bug it exists for: the bridge had no session after a restart,
    every send failed, and the dashboard still showed the agent replying — so
    the operator believed a conversation was handled when nothing had been
    sent. `twilio_sid` staying null is what the interface reads to tell the
    difference, so it has to stay null.
    """
    monkeypatch.setattr(settings, "twilio_validate_signature", False)

    organization = Organization(name="Undelivered Co", sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()
    db_session.add(
        ChannelConfig(
            organization_id=organization.id,
            channel="whatsapp",
            provider="twilio",
            phone_number="+14155557777",
            whatsapp_provider="QR_SESSION",
        )
    )
    await db_session.flush()

    # The bridge is unreachable, exactly as it was with no restored session.
    class DeadBridge:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_, **__):
            raise ConnectionError("session not connected")

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", DeadBridge)

    await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "MessageSid": "SM_undelivered",
            "From": "whatsapp:+971500002222",
            "To": "whatsapp:+14155557777",
            "Body": "are you there?",
            "NumMedia": "0",
        },
    )

    from sqlalchemy import select

    from app.models import Message

    replies = (
        await db_session.execute(
            select(Message).where(
                Message.organization_id == organization.id,
                Message.sender == "agent",
            )
        )
    ).scalars().all()

    assert replies, "the attempted reply should still be recorded for the operator"
    for reply in replies:
        assert reply.twilio_sid is None, (
            "a failed send must leave twilio_sid null so the dashboard can show "
            "it as undelivered rather than as sent"
        )


# ------------------------------------------------- one method at a time
@pytest.mark.asyncio
async def test_connecting_a_number_replaces_the_previous_one(org_a, monkeypatch):
    """A business runs on one WhatsApp method, never two at once.

    With both a Twilio number and a paired handset connected, every outbound
    message has to guess which number the conversation belongs to. Connecting
    one therefore disconnects the other.
    """
    from app.api import organizations

    unpaired = []

    async def record_unpair(channel_id):
        unpaired.append(str(channel_id))

    monkeypatch.setattr(organizations, "_unpair", record_unpair)

    first = await org_a.post(
        "/api/v1/organizations/active/channels",
        json={"phone_number": "+14155550001", "whatsapp_provider": "QR_SESSION"},
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = await org_a.post(
        "/api/v1/organizations/active/channels",
        json={"phone_number": "+14155550002", "whatsapp_provider": "TWILIO"},
    )
    assert second.status_code == 201

    listed = (await org_a.get("/api/v1/organizations/active/channels")).json()
    assert len(listed) == 1, "two WhatsApp methods must never be connected at once"
    assert listed[0]["phone_number"] == "+14155550002"
    assert listed[0]["whatsapp_provider"] == "TWILIO"
    assert unpaired == [first_id], "the replaced handset must be unpaired, not abandoned"


@pytest.mark.asyncio
async def test_reconnecting_the_same_number_is_not_a_clash(org_a, monkeypatch):
    """Re-pairing your own number must not collide with the row it replaces."""
    from app.api import organizations

    async def noop(_channel_id):
        return None

    monkeypatch.setattr(organizations, "_unpair", noop)

    payload = {"phone_number": "+14155550003", "whatsapp_provider": "QR_SESSION"}
    assert (await org_a.post("/api/v1/organizations/active/channels", json=payload)).status_code == 201
    again = await org_a.post("/api/v1/organizations/active/channels", json=payload)

    assert again.status_code == 201, again.text
    listed = (await org_a.get("/api/v1/organizations/active/channels")).json()
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_another_tenants_number_is_still_a_clash(org_a, org_b):
    """Inbound routing needs a number to identify exactly one organization."""
    payload = {"phone_number": "+14155550004", "whatsapp_provider": "TWILIO"}
    assert (await org_a.post("/api/v1/organizations/active/channels", json=payload)).status_code == 201

    taken = await org_b.post("/api/v1/organizations/active/channels", json=payload)
    assert taken.status_code == 409


@pytest.mark.asyncio
async def test_the_bridge_send_carries_the_shared_secret(monkeypatch):
    """Without this header the bridge answers 403 and nothing is ever sent.

    It failed in the worst possible way: the reply was generated, stored and
    shown in the dashboard as the agent's answer, while the customer's phone
    stayed silent. Every other call to the bridge — pair, session, logout —
    sends the header, so the one that mattered was the one that did not.
    """
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    seen = {}

    class Recorder:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["headers"] = headers or {}

            class Response:
                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"ok": True, "id": "WA-1"}

            return Response()

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", Recorder)

    channel = make_channel(provider="QR_SESSION")
    channel.id = "chan-secret"
    sent, reference = await whatsapp.send_message(channel, "+971500001111", "hello")

    assert sent is True
    assert reference == "WA-1"
    assert seen["headers"].get("X-PingPulse-Bridge") == "the-secret", (
        "the bridge rejects an unauthenticated send with 403, so the reply "
        "never reaches the customer"
    )
