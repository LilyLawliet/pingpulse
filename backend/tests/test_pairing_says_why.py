"""A pairing that has failed must not look like one that is slow.

The QR panel showed "Asking WhatsApp for a code..." whatever happened behind
it - WhatsApp refusing, the bridge giving up after five tries - so a phone
already linked to another business looked like a slow network, forever.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.models import ChannelConfig

from .conftest import _session_for


@pytest.mark.asyncio
async def test_the_bridge_s_reason_reaches_the_screen(org_a, monkeypatch):
    session = _session_for(org_a._client)
    channel = ChannelConfig(
        organization_id=uuid.UUID(org_a.organization_id),
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        session_status="GENERATING_QR",
    )
    session.add(channel)
    await session.flush()

    class Bridge:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return httpx.Response(
                200,
                json={"status": "DISCONNECTED", "qr": None, "reason": "unreachable",
                      "tries": 5, "gaveUp": True},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("app.api.organizations.httpx.AsyncClient", Bridge)
    body = (await org_a.get(f"/api/v1/organizations/active/channels/{channel.id}/qr")).json()
    assert body["reason"] == "unreachable" and body["gave_up"] is True and body["tries"] == 5


@pytest.mark.asyncio
async def test_disconnect_unlinks_the_phone_and_keeps_the_connection(org_a, monkeypatch):
    session = _session_for(org_a._client)
    channel = ChannelConfig(
        organization_id=uuid.UUID(org_a.organization_id),
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        phone_number="+923001112222",
        session_status="AUTHENTICATED",
    )
    session.add(channel)
    await session.flush()
    unpaired = []

    async def fake_unpair(channel_id):
        unpaired.append(channel_id)

    monkeypatch.setattr("app.api.organizations._unpair", fake_unpair)
    response = await org_a.post(f"/api/v1/organizations/active/channels/{channel.id}/unpair")
    assert response.status_code == 200
    assert unpaired == [channel.id]
    await session.refresh(channel)
    assert channel.session_status == "DISCONNECTED" and channel.phone_number is None
    assert await session.get(ChannelConfig, channel.id) is not None, "the connection was deleted"
