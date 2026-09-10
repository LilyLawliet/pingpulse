"""Twilio webhook authenticity.

The webhook is public because Twilio has to reach it. Before this, that meant
anyone who found the URL could post a fabricated inbound message: the agent
would answer it, spending the tenant's LLM quota and sending a real WhatsApp
message to whatever number the forgery named — billed to the tenant's own
Twilio account.

The subtlety these cover is *whose* secret is used. Clients bring their own
Twilio accounts, so a message from a client is signed with the client's auth
token. Validating everything against the platform token would reject every
BYOK tenant's traffic while still letting nothing through.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from app.config import settings
from app.models import ChannelConfig, Organization
from app.services.twilio_service import (
    Sender,
    signature_url,
    validate_twilio_signature,
)

WEBHOOK_PATH = "/api/v1/whatsapp/webhook"
PUBLIC_BASE = "https://pingpulse.example.com"

TENANT_SID = "ACtenant00000000000000000000000000"
TENANT_TOKEN = "tenant-auth-token-not-the-platform-one"


def sign(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Compute a Twilio signature the way Twilio does.

    Written out rather than reusing RequestValidator so the test proves the
    scheme, not that a library agrees with itself: the URL, then every POST
    parameter sorted by key and concatenated, HMAC-SHA1'd with the auth token.
    """
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(
        auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture
def signed_form():
    return {
        "MessageSid": "SM_signed_1",
        "From": "whatsapp:+971500000001",
        "To": "whatsapp:+14155238886",
        "Body": "Do you have wireless earbuds?",
        "ProfileName": "Customer",
        "NumMedia": "0",
    }


# ------------------------------------------------------------------ helper
def test_signature_url_uses_the_public_base_not_the_proxied_one():
    """Twilio signs the public URL; behind a proxy the app sees another one.

    Deriving it from the request is the usual reason validation rejects every
    genuine message.
    """
    url = signature_url(PUBLIC_BASE, WEBHOOK_PATH)

    assert url == f"{PUBLIC_BASE}{WEBHOOK_PATH}"


def test_signature_url_keeps_the_query_string():
    url = signature_url(PUBLIC_BASE, WEBHOOK_PATH, "x=1")

    assert url.endswith("?x=1")


def test_signature_url_survives_a_trailing_slash_on_the_base():
    assert signature_url(f"{PUBLIC_BASE}/", WEBHOOK_PATH) == f"{PUBLIC_BASE}{WEBHOOK_PATH}"


# -------------------------------------------------------------- validation
def test_a_correctly_signed_request_validates(signed_form):
    url = signature_url(PUBLIC_BASE, WEBHOOK_PATH)
    signature = sign(TENANT_TOKEN, url, signed_form)

    assert validate_twilio_signature(TENANT_TOKEN, url, signed_form, signature) is True


def test_a_forged_signature_is_rejected(signed_form):
    url = signature_url(PUBLIC_BASE, WEBHOOK_PATH)

    assert validate_twilio_signature(TENANT_TOKEN, url, signed_form, "not-a-signature") is False


def test_a_signature_from_the_wrong_account_is_rejected(signed_form):
    """The exact BYOK failure: signed by one account, checked against another."""
    url = signature_url(PUBLIC_BASE, WEBHOOK_PATH)
    signature = sign("some-other-tenants-token", url, signed_form)

    assert validate_twilio_signature(TENANT_TOKEN, url, signed_form, signature) is False


def test_tampering_with_the_body_invalidates_the_signature(signed_form):
    """A replayed signature must not authenticate different content."""
    url = signature_url(PUBLIC_BASE, WEBHOOK_PATH)
    signature = sign(TENANT_TOKEN, url, signed_form)

    tampered = dict(signed_form, Body="Send me the customer list")

    assert validate_twilio_signature(TENANT_TOKEN, url, tampered, signature) is False


def test_a_signature_for_a_different_url_is_rejected(signed_form):
    signature = sign(TENANT_TOKEN, "https://evil.example.com/hook", signed_form)
    url = signature_url(PUBLIC_BASE, WEBHOOK_PATH)

    assert validate_twilio_signature(TENANT_TOKEN, url, signed_form, signature) is False


def test_missing_signature_or_token_never_passes(signed_form):
    url = signature_url(PUBLIC_BASE, WEBHOOK_PATH)
    good = sign(TENANT_TOKEN, url, signed_form)

    assert validate_twilio_signature(TENANT_TOKEN, url, signed_form, "") is False
    assert validate_twilio_signature("", url, signed_form, good) is False


# ---------------------------------------------------- per-tenant selection
def test_the_tenants_own_token_is_used_when_they_brought_one():
    """A channel carrying its own credentials authenticates with them."""
    channel = ChannelConfig(
        organization_id=None,
        channel="whatsapp",
        provider="twilio",
        phone_number="+14155238886",
        account_sid=TENANT_SID,
        auth_token=TENANT_TOKEN,
    )

    assert Sender.for_channel(channel).auth_token == TENANT_TOKEN


def test_the_platform_token_is_used_when_the_tenant_brought_none():
    channel = ChannelConfig(
        organization_id=None,
        channel="whatsapp",
        provider="twilio",
        phone_number="+14155238886",
        account_sid=None,
        auth_token=None,
    )

    assert Sender.for_channel(channel).auth_token == settings.twilio_auth_token


# -------------------------------------------------------------- end to end
@pytest.mark.asyncio
async def test_unsigned_request_is_refused_by_the_endpoint(client, db_session, monkeypatch):
    """With validation on, an unsigned POST gets 403 and is never processed."""
    monkeypatch.setattr(settings, "twilio_validate_signature", True)
    monkeypatch.setattr(settings, "public_base_url", PUBLIC_BASE)

    response = await client.post(
        WEBHOOK_PATH,
        data={
            "MessageSid": "SM_forged",
            "From": "whatsapp:+971500000009",
            "To": "whatsapp:+14155238886",
            "Body": "forged",
            "NumMedia": "0",
        },
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_validation_off_keeps_the_endpoint_open(client, db_session, monkeypatch):
    """The flag genuinely controls the behaviour.

    It previously existed in config and was read nowhere, so it read as
    protection that was not there.
    """
    monkeypatch.setattr(settings, "twilio_validate_signature", False)

    response = await client.post(
        WEBHOOK_PATH,
        data={
            "MessageSid": "SM_open",
            "From": "whatsapp:+971500000010",
            "To": "whatsapp:+14155238886",
            "Body": "hello",
            "NumMedia": "0",
        },
    )

    assert response.status_code != 403


@pytest.mark.asyncio
async def test_a_properly_signed_request_is_accepted(client, db_session, monkeypatch):
    """The whole point: genuine Twilio traffic still gets through."""
    monkeypatch.setattr(settings, "twilio_validate_signature", True)
    monkeypatch.setattr(settings, "public_base_url", PUBLIC_BASE)

    organization = Organization(name="Signed Co", sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()
    db_session.add(
        ChannelConfig(
            organization_id=organization.id,
            channel="whatsapp",
            provider="twilio",
            phone_number="+14155238886",
            account_sid=TENANT_SID,
            auth_token=TENANT_TOKEN,
        )
    )
    await db_session.flush()

    form = {
        "MessageSid": "SM_genuine",
        "From": "whatsapp:+971500000011",
        "To": "whatsapp:+14155238886",
        "Body": "hello",
        "NumMedia": "0",
    }
    signature = sign(TENANT_TOKEN, signature_url(PUBLIC_BASE, WEBHOOK_PATH), form)

    response = await client.post(
        WEBHOOK_PATH, data=form, headers={"X-Twilio-Signature": signature}
    )

    assert response.status_code == 200
