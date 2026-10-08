"""Scanning a QR identifies the handset, so nobody types the number.

The pairing form asked for the number of the phone about to be scanned. The
bridge reads that number off the session the moment it authenticates, so the
typed value was a second, unverified copy of a fact already on its way - and
it was the copy the uniqueness rule was enforced against.

That is how one handset came to be held by two organizations: typed
"+923097209908" for one and "923097209908" for the other. Neither spelling
collided, both rows were accepted, and the clash only surfaced when the phone
paired and the bridge wrote back the bare form onto a constraint that refused
it, inside a callback that did not retry.

Twilio still supplies a number, because there is no handset to learn it from.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import ChannelConfig


async def _pair(tenant, **overrides):
    payload = {
        "channel": "whatsapp",
        "provider": "twilio",
        "whatsapp_provider": "QR_SESSION",
    }
    payload.update(overrides)
    return await tenant.post("/api/v1/organizations/active/channels", json=payload)


@pytest.mark.asyncio
async def test_a_qr_pairing_is_created_without_a_number(org_a, db_session):
    response = await _pair(org_a)
    assert response.status_code == 201, response.text
    assert response.json()["phone_number"] is None

    stored = await db_session.get(ChannelConfig, uuid.UUID(response.json()["id"]))
    assert stored.phone_number is None
    assert stored.whatsapp_provider == "QR_SESSION"


@pytest.mark.asyncio
async def test_a_typed_number_is_ignored_on_the_qr_path(org_a, db_session):
    """Not merely optional - refused, so the two spellings cannot come back."""
    response = await _pair(org_a, phone_number="+923097209908")
    assert response.status_code == 201, response.text
    assert response.json()["phone_number"] is None, (
        "a typed number was stored for a pairing that will report its own"
    )


@pytest.mark.asyncio
async def test_twilio_still_demands_a_number(org_a):
    """There is no scan to learn it from, so it has to be given."""
    blank = await org_a.post(
        "/api/v1/organizations/active/channels",
        json={"channel": "whatsapp", "provider": "twilio", "whatsapp_provider": "TWILIO"},
    )
    assert blank.status_code == 422, blank.text

    nonsense = await org_a.post(
        "/api/v1/organizations/active/channels",
        json={
            "channel": "whatsapp",
            "provider": "twilio",
            "whatsapp_provider": "TWILIO",
            "phone_number": "not a phone",
        },
    )
    assert nonsense.status_code == 422, nonsense.text


@pytest.mark.asyncio
async def test_two_pairings_in_flight_do_not_collide(org_a, org_b):
    """"Not known yet" is not a number and must not behave like one.

    Two shops setting up at the same time both have a channel with no number.
    If that were treated as a value, the second one would be refused as a
    duplicate of the first.
    """
    first = await _pair(org_a)
    second = await _pair(org_b)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["phone_number"] is None
    assert second.json()["phone_number"] is None


@pytest.mark.asyncio
async def test_the_number_arrives_when_the_session_authenticates(org_a, db_session, monkeypatch):
    """The whole point: the handset supplies what nobody typed."""
    from app.config import settings

    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")

    created = await _pair(org_a)
    channel_id = created.json()["id"]

    response = await org_a._client.post(
        "/api/v1/whatsapp/qr-status",
        headers={"X-PingPulse-Bridge": "the-secret"},
        json={
            "sessionId": channel_id,
            "status": "AUTHENTICATED",
            "phoneNumber": "923097209908",
        },
    )
    assert response.status_code == 200, response.text

    stored = await db_session.get(ChannelConfig, uuid.UUID(channel_id))
    await db_session.refresh(stored)
    assert stored.session_status == "AUTHENTICATED"
    assert stored.phone_number == "+923097209908", (
        "the number the handset reported was not stored, in one spelling"
    )


@pytest.mark.asyncio
async def test_a_pairing_cannot_take_a_number_another_shop_holds(
    org_a, org_b, db_session, monkeypatch
):
    """The check that matters now happens against the reported number."""
    from app.config import settings

    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")

    theirs = await org_b.post(
        "/api/v1/organizations/active/channels",
        json={
            "channel": "whatsapp",
            "provider": "twilio",
            "whatsapp_provider": "TWILIO",
            "phone_number": "+923097209908",
        },
    )
    assert theirs.status_code == 201, theirs.text

    mine = await _pair(org_a)
    channel_id = mine.json()["id"]

    # The same handset, reported bare - the spelling that used to slip past.
    response = await org_a._client.post(
        "/api/v1/whatsapp/qr-status",
        headers={"X-PingPulse-Bridge": "the-secret"},
        json={
            "sessionId": channel_id,
            "status": "AUTHENTICATED",
            "phoneNumber": "923097209908",
        },
    )
    assert response.status_code == 200, "the callback must not fail; the bridge never retries"

    stored = await db_session.get(ChannelConfig, uuid.UUID(channel_id))
    await db_session.refresh(stored)
    assert stored.session_status == "DUPLICATE", (
        "the session is linked but will not answer on a phone another shop "
        "holds; the status has to say so"
    )
    assert stored.phone_number is None, "the number was taken from the shop that holds it"
