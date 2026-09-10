"""Token authentication.

There are no passwords and no sign-up. A client is issued an access token by
`scripts/create_token.py`, pastes it into the desktop app once, and the app
sends it as a bearer token from then on.

Every protected request re-checks that token against the `access_tokens`
table, so revoking one (`is_active = false`) locks the client out on their very
next call rather than whenever the token would have expired on its own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import claim_seat, current_token, current_user, resolve_token
from app.models import AccessToken, User
from app.schemas_tenancy import TokenLoginRequest, TokenSessionOut, UserOut

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


async def _session(token: AccessToken, db: AsyncSession) -> TokenSessionOut:
    user = await db.get(User, token.user_id) if token.user_id else None
    return TokenSessionOut(
        token=token.token,
        client_name=token.client_name,
        expires_at=token.expires_at,
        user_id=token.user_id,
        active_organization_id=user.active_organization_id if user else None,
    )


@router.post("/login", response_model=TokenSessionOut)
async def login(
    payload: TokenLoginRequest,
    x_pingpulse_device: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Exchange a raw access token for the session it represents.

    Nothing is minted here — the token already exists. This confirms it is
    valid and tells the app which organization it lands in, so the desktop
    client can show a real error at sign-in rather than failing later on a
    data call.

    The seat is claimed here too, so a client on a machine too many is told at
    sign-in rather than after the dashboard has already loaded.
    """
    token = await resolve_token(db, payload.token)
    await claim_seat(db, token, (x_pingpulse_device or "").strip()[:64])
    return await _session(token, db)


@router.post("/verify-token", response_model=TokenSessionOut)
async def verify_token(payload: TokenLoginRequest, db: AsyncSession = Depends(get_db)):
    """Alias of /login, for callers that only want to check a token."""
    token = await resolve_token(db, payload.token)
    return await _session(token, db)


@router.get("/session", response_model=TokenSessionOut)
async def session(
    token: AccessToken = Depends(current_token),
    db: AsyncSession = Depends(get_db),
):
    """Who the bearer token currently in use belongs to."""
    return await _session(token, db)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user
