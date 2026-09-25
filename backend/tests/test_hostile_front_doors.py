"""The two public front doors, attacked.

`/qr-inbound` and `/qr-status` trust a shared secret in a header. `/webhook`
trusts a Twilio signature. Both are reachable by anyone who finds the URL, and
both, if fooled, send a real WhatsApp message on the tenant's bill or write to
their CRM. So the question is not "does the happy path work" - other tests
cover that - but "what does a forged request get".

The bridge secret is compared in constant time. That is not paranoia for its
own sake: the comparison was `!=`, which returns the instant two bytes differ,
so a caller timing the rejection can recover the secret one byte at a time.
These tests pin the behaviour; the timing property itself is argued in the
code and cannot be asserted reliably in a unit test.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.models import ChannelConfig, Organization


async def _channel(db_session, name, number):
    organization = Organization(name=name, sales_prompt="Sell.")
    db_session.add(organization)
    await db_session.flush()
    channel = ChannelConfig(
        organization_id=organization.id,
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        phone_number=number,
        is_active=True,
    )
    db_session.add(channel)
    await db_session.flush()
    return channel


# ============================================================ the bridge
@pytest.mark.asyncio
async def test_qr_inbound_without_the_secret_is_refused(client, monkeypatch):
    """No header at all. This is the drive-by: find the URL, POST a message,
    and a reply goes out on the tenant's number to whoever you named."""
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    response = await client.post(
        "/api/v1/whatsapp/qr-inbound",
        json={"from": "15550001", "to": "15550002", "body": "hi", "id": "x"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_qr_inbound_with_a_wrong_secret_is_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    response = await client.post(
        "/api/v1/whatsapp/qr-inbound",
        json={"from": "15550001", "to": "15550002", "body": "hi", "id": "x"},
        headers={"X-PingPulse-Bridge": "not-the-secret"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_secret_that_is_a_prefix_of_the_real_one_is_refused(client, monkeypatch):
    """The byte-at-a-time attack made concrete: the right first characters must
    not be treated as any closer to right than the wrong ones."""
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    for guess in ("", "t", "the", "the-secre", "the-secret-and-more"):
        response = await client.post(
            "/api/v1/whatsapp/qr-inbound",
            json={"from": "1", "to": "2", "body": "hi", "id": "x"},
            headers={"X-PingPulse-Bridge": guess},
        )
        assert response.status_code == 403, guess


@pytest.mark.asyncio
async def test_an_unconfigured_bridge_secret_locks_the_door(client, monkeypatch):
    """An empty configured secret must fail every request, including one that
    also sends an empty header. A missing secret is a locked door, not a match
    against an empty string."""
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "")
    for header in ({}, {"X-PingPulse-Bridge": ""}):
        response = await client.post(
            "/api/v1/whatsapp/qr-inbound",
            json={"from": "1", "to": "2", "body": "hi", "id": "x"},
            headers=header,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_qr_status_is_guarded_the_same_way(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    channel = await _channel(db_session, "Shop", "+13055550000")
    await db_session.commit()

    forged = await client.post(
        "/api/v1/whatsapp/qr-status",
        json={"sessionId": str(channel.id), "status": "AUTHENTICATED"},
        headers={"X-PingPulse-Bridge": "wrong"},
    )
    assert forged.status_code == 403

    await db_session.refresh(channel)
    # A forged status must not have moved the channel.
    assert channel.session_status != "AUTHENTICATED"


def test_a_non_ascii_header_does_not_crash_the_check(monkeypatch):
    """compare_digest raises TypeError on a str carrying non-ASCII, and Starlette
    hands header values through as latin-1-decoded strings that can contain
    exactly those bytes. Encoding to bytes first is what keeps a hostile header
    a clean 403 rather than a 500. Exercised against the helper directly,
    because the HTTP client refuses to transmit such a header at all."""
    from types import SimpleNamespace

    from app.api import webhook

    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    request = SimpleNamespace(headers={"X-PingPulse-Bridge": "ééé"})
    # Must return cleanly, not raise.
    assert webhook._bridge_authorised(request) is False


def test_the_right_secret_still_authorises(monkeypatch):
    """The guard has to reject everything wrong without also rejecting the one
    thing that is right."""
    from types import SimpleNamespace

    from app.api import webhook

    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    request = SimpleNamespace(headers={"X-PingPulse-Bridge": "the-secret"})
    assert webhook._bridge_authorised(request) is True


@pytest.mark.asyncio
async def test_a_forged_bridge_message_writes_no_contact(client, db_session, monkeypatch):
    """The whole point of the door: a rejected message must leave no trace -
    no contact, no message row, no reply queued."""
    from sqlalchemy import func, select

    from app.models import CRMContact, Message

    monkeypatch.setattr(settings, "wa_qr_shared_secret", "the-secret")
    await _channel(db_session, "Shop", "+13055550000")
    await db_session.commit()

    before = await db_session.scalar(select(func.count()).select_from(CRMContact))
    await client.post(
        "/api/v1/whatsapp/qr-inbound",
        json={"from": "15559999", "to": "13055550000", "body": "hello", "id": "x"},
        headers={"X-PingPulse-Bridge": "wrong"},
    )
    after = await db_session.scalar(select(func.count()).select_from(CRMContact))
    messages = await db_session.scalar(select(func.count()).select_from(Message))
    assert after == before
    assert messages == 0


# ============================================================ Twilio
@pytest.mark.asyncio
async def test_an_unsigned_twilio_webhook_is_refused(client, db_session, monkeypatch):
    """Without the signature check, anyone who finds the URL can post a
    fabricated inbound message and spend the tenant's Twilio balance sending a
    reply to any number they name."""
    monkeypatch.setattr(settings, "twilio_validate_signature", True)
    channel = await _channel(db_session, "Shop", "+13055550000")
    channel.provider = "twilio"
    channel.auth_token = "tenant-token"
    await db_session.commit()

    response = await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "From": "whatsapp:+15559999",
            "To": "whatsapp:+13055550000",
            "Body": "send money",
            "MessageSid": "SMfake",
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_mis_signed_twilio_webhook_is_refused(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "twilio_validate_signature", True)
    channel = await _channel(db_session, "Shop", "+13055550000")
    channel.provider = "twilio"
    channel.auth_token = "tenant-token"
    await db_session.commit()

    response = await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "From": "whatsapp:+15559999",
            "To": "whatsapp:+13055550000",
            "Body": "send money",
            "MessageSid": "SMfake",
        },
        headers={"X-Twilio-Signature": "obviously-wrong=="},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_forged_twilio_webhook_writes_no_contact(client, db_session, monkeypatch):
    from sqlalchemy import func, select

    from app.models import CRMContact

    monkeypatch.setattr(settings, "twilio_validate_signature", True)
    channel = await _channel(db_session, "Shop", "+13055550000")
    channel.provider = "twilio"
    channel.auth_token = "tenant-token"
    await db_session.commit()

    before = await db_session.scalar(select(func.count()).select_from(CRMContact))
    await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "From": "whatsapp:+15559999",
            "To": "whatsapp:+13055550000",
            "Body": "send money",
            "MessageSid": "SMfake",
        },
        headers={"X-Twilio-Signature": "wrong"},
    )
    after = await db_session.scalar(select(func.count()).select_from(CRMContact))
    assert after == before


# ====================================================== media fetch (SSRF)
# The inbound fetch downloads a URL and follows redirects, and this service
# shares a network with Redis, Postgres and the bridge. It sits behind the two
# secrets above today, so a URL only reaches it through our own verified
# bridge - but the fetch is one refactor from being reachable, and the cost of
# an internal request is a read of infrastructure the internet should not
# touch. The guard is defence in depth, and cheap.
import pytest as _pytest


@_pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://localhost:6379/",                     # redis
        "http://127.0.0.1/",
        "http://redis:6379/",
        "http://10.0.0.5/photo.jpg",
        "http://192.168.1.1/",
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://internal/secret",
    ],
)
def test_a_url_pointing_inward_is_not_fetched(url):
    from app.services.media_service import _fetchable

    assert _fetchable(url) is False, url


@_pytest.mark.parametrize(
    "url",
    [
        "https://mmg.whatsapp.net/d/photo.jpg",
        "https://api.twilio.com/2010-04-01/media/ME123",
        "https://example.com/photo.png",
    ],
)
def test_a_real_cdn_url_is_still_fetched(url):
    from app.services.media_service import _fetchable

    assert _fetchable(url) is True, url


@_pytest.mark.asyncio
async def test_a_redirect_to_an_internal_host_is_refused(monkeypatch):
    """The bypass the per-URL check alone would miss: a public URL that answers
    302 -> http://169.254.169.254/. Each hop is re-checked, so the redirect is
    refused and nothing is written."""
    import httpx

    from app.services import media_service

    hops = {
        "https://example.com/start.jpg": httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"},
            request=httpx.Request("GET", "https://example.com/start.jpg"),
        ),
    }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, auth=None):
            response = hops.get(str(url))
            if response is None:  # pragma: no cover - would mean the guard failed
                raise AssertionError(f"fetched a URL it should have refused: {url}")
            return response

    monkeypatch.setattr(media_service.httpx, "AsyncClient", FakeClient)
    result = await media_service.download_inbound("https://example.com/start.jpg", "image/jpeg")
    assert result is None
