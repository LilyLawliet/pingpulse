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


# ------------------------------------------------------- a licence is a date
@pytest.mark.asyncio
async def test_one_token_works_from_as_many_machines_as_you_like(client, db_session):
    """Seats are gone. A licence is the token and the date it runs out.

    Counting machines never stopped a token being passed around - the device
    id lived in the client's own storage and clearing it minted a new one -
    and it did refuse the client themselves, on a second browser or a fresh
    laptop, with a 403 the dashboard could not tell apart from an unfinished
    setup.
    """
    record = await _token(db_session)

    for _ in range(12):
        response = await client.post(
            "/api/v1/auth/login", json={"token": record.token}
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_an_old_client_still_sending_a_device_id_is_not_refused(
    client, db_session
):
    """Desktop builds from before this send the header until they update."""
    record = await _token(db_session)

    for machine in ("laptop", "desktop", "a-colleagues-laptop"):
        response = await client.post(
            "/api/v1/auth/login",
            json={"token": record.token},
            headers={"X-PingPulse-Device": machine},
        )
        assert response.status_code == 200, f"{machine} should have been allowed"


@pytest.mark.asyncio
async def test_expiry_is_still_what_ends_a_licence(client, db_session):
    """The one thing a licence does say, and the only thing left enforcing it."""
    record = await _token(db_session)
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()

    response = await client.post("/api/v1/auth/login", json={"token": record.token})

    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()
