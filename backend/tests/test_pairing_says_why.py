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
