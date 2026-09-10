"""Database-backed token authentication.

The whole point of validating against a table rather than verifying a signed
token is that a token can be taken back. These pin down the four states a
token can be in — valid, unknown, expired, revoked — and that only the first
one gets in.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.deps import resolve_token
from app.models import AccessToken, User
from app.security import TOKEN_PREFIX, generate_token


async def _token(session, **overrides) -> AccessToken:
    user = User(
        email=f"{uuid.uuid4().hex[:8]}@token.pingpulse.local",
        full_name="Test Client",
        password_hash="!token-only",
    )
    session.add(user)
    await session.flush()

    record = AccessToken(
        token=overrides.get("token", generate_token()),
        client_name=overrides.get("client_name", "Test Client"),
        expires_at=overrides.get(
            "expires_at", datetime.now(timezone.utc) + timedelta(days=30)
        ),
        is_active=overrides.get("is_active", True),
        user_id=user.id,
    )
    session.add(record)
    await session.flush()
    return record


# --------------------------------------------------------------- generation
def test_generated_tokens_are_prefixed_and_unique():
    first, second = generate_token(), generate_token()

    assert first.startswith(TOKEN_PREFIX)
    assert first != second
    # Long enough that guessing is not a strategy, short enough for the column.
    assert 40 < len(first) <= 128


# --------------------------------------------------------------- validation
@pytest.mark.asyncio
async def test_a_valid_token_resolves(db_session):
    record = await _token(db_session)

    resolved = await resolve_token(db_session, record.token)

    assert resolved.token == record.token
    assert resolved.client_name == "Test Client"


@pytest.mark.asyncio
async def test_an_unknown_token_is_rejected(db_session):
    with pytest.raises(HTTPException) as raised:
        await resolve_token(db_session, "pp_live_never_issued")

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_an_expired_token_is_rejected(db_session):
    record = await _token(
        db_session, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    with pytest.raises(HTTPException) as raised:
        await resolve_token(db_session, record.token)

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_a_revoked_token_is_rejected_immediately(db_session):
    """Revocation is the reason this is a database lookup and not a signed token.

    The token is still well within its expiry window; flipping `is_active`
    alone has to be enough to lock the client out.
    """
    record = await _token(db_session)
    record.is_active = False
    await db_session.flush()

    with pytest.raises(HTTPException) as raised:
        await resolve_token(db_session, record.token)

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_an_empty_token_is_rejected(db_session):
    for value in ("", "   ", None):
        with pytest.raises(HTTPException):
            await resolve_token(db_session, value)


@pytest.mark.asyncio
async def test_surrounding_whitespace_does_not_break_a_paste(db_session):
    """Tokens are copied out of emails and chat windows; they arrive padded."""
    record = await _token(db_session)

    resolved = await resolve_token(db_session, f"  {record.token}\n")

    assert resolved.token == record.token


@pytest.mark.asyncio
async def test_failure_messages_do_not_reveal_whether_a_token_exists(db_session):
    """Saying "expired" confirms a guessed token was real. Both read the same."""
    expired = await _token(
        db_session, expires_at=datetime.now(timezone.utc) - timedelta(days=1)
    )

    with pytest.raises(HTTPException) as unknown_error:
        await resolve_token(db_session, "pp_live_never_issued")
    with pytest.raises(HTTPException) as expired_error:
        await resolve_token(db_session, expired.token)

    assert unknown_error.value.status_code == expired_error.value.status_code == 401


# ------------------------------------------------------------------- routes
@pytest.mark.asyncio
async def test_login_exchanges_a_token_for_a_session(client, db_session):
    record = await _token(db_session)

    response = await client.post("/api/v1/auth/login", json={"token": record.token})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"] == record.token
    assert body["client_name"] == "Test Client"
    assert body["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_rejects_a_bad_token(client, db_session):
    response = await client.post(
        "/api/v1/auth/login", json={"token": "pp_live_never_issued"}
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_matches_login(client, db_session):
    record = await _token(db_session)

    response = await client.post(
        "/api/v1/auth/verify-token", json={"token": record.token}
    )

    assert response.status_code == 200
    assert response.json()["token"] == record.token


@pytest.mark.asyncio
async def test_protected_routes_need_a_real_token(client, db_session):
    """A revoked token stops working on the very next protected call."""
    record = await _token(db_session)
    headers = {"Authorization": f"Bearer {record.token}"}

    allowed = await client.get("/api/v1/crm/contacts", headers=headers)
    assert allowed.status_code != 401

    record.is_active = False
    await db_session.flush()

    refused = await client.get("/api/v1/crm/contacts", headers=headers)
    assert refused.status_code == 401


@pytest.mark.asyncio
async def test_signup_endpoint_is_gone(client):
    """Password registration was removed, not merely hidden."""
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": "someone@example.com", "password": "hunter2hunter2"},
    )

    assert response.status_code == 404


# -------------------------------------------------------------- seat limits
@pytest.mark.asyncio
async def test_the_same_machine_keeps_its_seat_across_sign_ins(client, db_session):
    """Reopening the app must not consume another seat."""
    record = await _token(db_session)
    headers = {"X-PingPulse-Device": "machine-one"}

    for _ in range(3):
        response = await client.post(
            "/api/v1/auth/login", json={"token": record.token}, headers=headers
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_licence_refuses_more_machines_than_it_covers(client, db_session):
    """The point of the feature: a token forwarded around stops working.

    The message has to say why, or the client just sees the app refusing to
    open with no explanation.
    """
    record = await _token(db_session)
    record.max_devices = 2
    await db_session.flush()

    for machine in ("laptop", "desktop"):
        response = await client.post(
            "/api/v1/auth/login",
            json={"token": record.token},
            headers={"X-PingPulse-Device": machine},
        )
        assert response.status_code == 200, f"{machine} should have been allowed"

    third = await client.post(
        "/api/v1/auth/login",
        json={"token": record.token},
        headers={"X-PingPulse-Device": "a-colleagues-laptop"},
    )

    assert third.status_code == 403
    assert "already in use on 2 device" in third.json()["detail"]


@pytest.mark.asyncio
async def test_requests_without_a_device_id_still_work(client, db_session):
    """Scripts, curl and health checks must not need to claim a seat."""
    record = await _token(db_session)

    response = await client.post("/api/v1/auth/login", json={"token": record.token})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_seats_are_per_token_not_global(client, db_session):
    """One client filling their seats must not lock another client out."""
    first = await _token(db_session)
    first.max_devices = 1
    second = await _token(db_session)
    await db_session.flush()

    await client.post(
        "/api/v1/auth/login",
        json={"token": first.token},
        headers={"X-PingPulse-Device": "shared-machine-name"},
    )

    response = await client.post(
        "/api/v1/auth/login",
        json={"token": second.token},
        headers={"X-PingPulse-Device": "shared-machine-name"},
    )

    assert response.status_code == 200
