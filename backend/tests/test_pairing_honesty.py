"""A pairing is connected when PingPulse knows it is, not when the bridge does.

Reported from the dashboard: the header said "Not connected", the card said
"waiting for a scan", and between them sat a green line saying the phone was
linked and sending. Three answers on one screen.

All three were correct about what they were reading, which is the whole
problem. The header and the card read the channel record. The green line read
the bridge directly. The bridge genuinely had an authenticated session, so the
line was not lying - it was answering a question nobody had asked. Messages
route because the *backend* holds a channel marked authenticated. A session
the backend has never heard of sends and receives nothing.

The backend had never heard of it, and the reason is here:

    "phoneNumber":"923097209908","msg":"session authenticated"
    POST /api/v1/whatsapp/qr-status -> 500
    UniqueViolationError: duplicate key value violates "uq_channel_number"

The same handset was already registered to another organization, in a
different spelling - "923097209908" against "+923097209908" - so neither the
unique constraint nor the create-time clash check saw a duplicate until the
bridge wrote back the bare form. Then the callback died, and the bridge does
not retry. The session ran for days with nothing in the application aware of
it.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.models import ChannelConfig, Organization
from app.services import whatsapp


# ------------------------------------------------------- one spelling per phone
@pytest.mark.parametrize(
    "written,expected",
    [
        ("+923097209908", "+923097209908"),
        ("923097209908", "+923097209908"),
        ("whatsapp:+923097209908", "+923097209908"),
        ("+92 309 720 9908", "+923097209908"),
        ("+1 (305) 748-3629", "+13057483629"),
        ("", ""),
        (None, ""),
        ("+", ""),
    ],
)
def test_a_number_has_one_spelling(written, expected):
    assert whatsapp.normalise_number(written) == expected


def test_the_two_spellings_in_production_are_the_same_phone():
    """The exact pair that was sitting on two organizations."""
    assert whatsapp.same_number("+923097209908", "923097209908")


def test_different_numbers_are_still_different():
    assert not whatsapp.same_number("+13057483629", "+13067483629")
    assert not whatsapp.same_number(None, None)
    assert not whatsapp.same_number("", "")


# --------------------------------------------------- claiming a number twice
@pytest.mark.asyncio
async def test_a_second_organization_cannot_claim_one_handset(org_a, org_b):
    """Caught while somebody is typing it in, rather than days later inside a
    callback that fails and is never retried.

    Written against QR pairing, which is where the production fault happened.
    The QR path no longer takes a typed number at all - the handset reports its
    own - so the two-spellings trap has been removed rather than guarded, and
    the equivalent case now lives in test_pairing_needs_no_number.py, where the
    clash is checked against the number the scan reports.

    Twilio still accepts a typed number, because there is no scan to learn one
    from, so the guard still has to hold here.
    """
    first = await org_a._client.post(
        "/api/v1/organizations/active/channels",
        headers=org_a.headers,
        json={
            "channel": "whatsapp",
            "provider": "twilio",
            "phone_number": "+923097209908",
            "whatsapp_provider": "TWILIO",
        },
    )
    assert first.status_code in (200, 201), first.text

    # The same phone, spelled the way the bridge reports it. This was accepted.
    second = await org_b._client.post(
        "/api/v1/organizations/active/channels",
        headers=org_b.headers,
        json={
            "channel": "whatsapp",
            "provider": "twilio",
            "phone_number": "923097209908",
            "whatsapp_provider": "TWILIO",
        },
    )

    assert second.status_code == 409, "two organizations took the same handset"


@pytest.mark.asyncio
async def test_a_number_that_is_not_one_is_refused(org_a):
    response = await org_a._client.post(
        "/api/v1/organizations/active/channels",
        headers=org_a.headers,
        json={
            "channel": "whatsapp",
            "provider": "twilio",
            "phone_number": "not a phone",
            # Twilio, because that is the only path that still takes one.
            "whatsapp_provider": "TWILIO",
        },
    )

    assert response.status_code == 422


# ------------------------------------------------- the callback must survive
async def _channel(db_session, name: str, number: str) -> ChannelConfig:
    organization = Organization(name=name, sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()

    channel = ChannelConfig(
        organization_id=organization.id,
        channel="whatsapp",
        provider="twilio",
        phone_number=number,
        whatsapp_provider="QR_SESSION",
        session_status="GENERATING_QR",
    )
    db_session.add(channel)
    await db_session.flush()
    return channel


@pytest.mark.asyncio
async def test_a_successful_pairing_is_recorded(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    channel = await _channel(db_session, "Only Shop", "+13055550000")
    await db_session.commit()

    response = await client.post(
        "/api/v1/whatsapp/qr-status",
        json={
            "sessionId": str(channel.id),
            "status": "AUTHENTICATED",
            "phoneNumber": "13055550000",
        },
        headers={"X-PingPulse-Bridge": "the-secret"},
    )

    assert response.status_code == 200
    await db_session.refresh(channel)
    assert channel.session_status == "AUTHENTICATED"
    assert channel.session_connected_at is not None


@pytest.mark.asyncio
async def test_a_pairing_onto_somebody_elses_number_does_not_500(
    client, db_session, monkeypatch
):
    """The production failure, exactly. It returned 500, the bridge logged one
    line and never tried again, and the channel stayed on GENERATING_QR while
    a live session ran on the other side of it."""
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")

    await _channel(db_session, "First Shop", "923097209908")
    second = await _channel(db_session, "Second Shop", "+923097209908")
    await db_session.commit()

    response = await client.post(
        "/api/v1/whatsapp/qr-status",
        json={
            "sessionId": str(second.id),
            "status": "AUTHENTICATED",
            # Bare, which is how the bridge reports it.
            "phoneNumber": "923097209908",
        },
        headers={"X-PingPulse-Bridge": "the-secret"},
    )

    assert response.status_code == 200, "the callback died again"


@pytest.mark.asyncio
async def test_a_clashing_pairing_is_recorded_as_a_duplicate_session(
    client, db_session, monkeypatch
):
    """A second business paired to a phone another holds does not answer on it.

    This used to keep the clashing session AUTHENTICATED, because it was still
    answering customers - and that was the fault: both businesses answered
    every message from one phone, each from its own diary and memory. The
    holder answers now and this one is silent, so its status says so.
    """
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")

    from datetime import datetime, timedelta, timezone

    first = await _channel(db_session, "First Shop", "923097209908")
    first.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    second = await _channel(db_session, "Second Shop", "+923097209908")
    await db_session.commit()

    await client.post(
        "/api/v1/whatsapp/qr-status",
        json={
            "sessionId": str(second.id),
            "status": "AUTHENTICATED",
            "phoneNumber": "923097209908",
        },
        headers={"X-PingPulse-Bridge": "the-secret"},
    )

    await db_session.refresh(second)
    assert second.session_status == "DUPLICATE"
    assert second.session_connected_at is not None
    # And it did not take the number off the organization that holds it.
    assert second.phone_number == "+923097209908"


@pytest.mark.asyncio
async def test_the_clash_is_reported_on_the_channel(org_a, org_b, db_session):
    """The operator scanned a code and it worked, so every explanation they
    reach for on their own is wrong. The reason has to be on the screen.

    Derived when the channel is read, not stored, so that resolving the clash
    clears it without anybody remembering to.
    """
    from app.models import ChannelConfig

    for organization_id, number in (
        (org_a.organization_id, "+923097209908"),
        (org_b.organization_id, "923097209908"),
    ):
        db_session.add(
            ChannelConfig(
                organization_id=uuid.UUID(organization_id),
                channel="whatsapp",
                provider="twilio",
                phone_number=number,
                whatsapp_provider="QR_SESSION",
                session_status="AUTHENTICATED",
            )
        )
    await db_session.commit()

    listed = (await org_a.get("/api/v1/organizations/active/channels")).json()

    assert listed, "no channel came back"
    assert listed[0]["number_conflict"], "the duplicate handset was not reported"


@pytest.mark.asyncio
async def test_a_number_nobody_else_holds_is_not_flagged(org_a, db_session):
    from app.models import ChannelConfig

    db_session.add(
        ChannelConfig(
            organization_id=uuid.UUID(org_a.organization_id),
            channel="whatsapp",
            provider="twilio",
            phone_number="+13055550123",
            whatsapp_provider="QR_SESSION",
            session_status="AUTHENTICATED",
        )
    )
    await db_session.commit()

    listed = (await org_a.get("/api/v1/organizations/active/channels")).json()

    assert listed[0]["number_conflict"] is None


@pytest.mark.asyncio
async def test_the_other_organization_keeps_its_number(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")

    first = await _channel(db_session, "First Shop", "923097209908")
    second = await _channel(db_session, "Second Shop", "+923097209908")
    await db_session.commit()

    await client.post(
        "/api/v1/whatsapp/qr-status",
        json={
            "sessionId": str(second.id),
            "status": "AUTHENTICATED",
            "phoneNumber": "923097209908",
        },
        headers={"X-PingPulse-Bridge": "the-secret"},
    )

    await db_session.refresh(first)
    assert first.phone_number == "923097209908"


@pytest.mark.asyncio
async def test_a_disconnect_is_still_recorded(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    channel = await _channel(db_session, "Dropped Shop", "+13055550001")
    channel.session_status = "AUTHENTICATED"
    await db_session.commit()

    response = await client.post(
        "/api/v1/whatsapp/qr-status",
        json={"sessionId": str(channel.id), "status": "DISCONNECTED"},
        headers={"X-PingPulse-Bridge": "the-secret"},
    )

    assert response.status_code == 200
    await db_session.refresh(channel)
    assert channel.session_status == "DISCONNECTED"


@pytest.mark.asyncio
async def test_an_unknown_session_is_not_an_error(client, monkeypatch):
    """The bridge holds sessions for channels that may have been deleted. It
    must not be handed a 500 for saying so."""
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")

    response = await client.post(
        "/api/v1/whatsapp/qr-status",
        json={"sessionId": str(uuid.uuid4()), "status": "AUTHENTICATED"},
        headers={"X-PingPulse-Bridge": "the-secret"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
