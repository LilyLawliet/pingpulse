"""Sign-up, sign-in, and who-am-I."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import current_user
from app.models import Organization, OrganizationMember, User
from app.schemas_tenancy import (
    LoginRequest,
    SignUpRequest,
    TokenResponse,
    UserOut,
)
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def sign_up(payload: SignUpRequest, db: AsyncSession = Depends(get_db)):
    """Create an account, and optionally its first organization.

    The creator becomes OWNER of that organization and it is made active, so a
    new account is never stranded without a tenant to work in.
    """
    email = payload.email.lower().strip()
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists",
        )

    user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    await db.flush()

    if payload.organization_name:
        organization = Organization(
            name=payload.organization_name,
            sales_prompt="You are a helpful sales agent.",
        )
        db.add(organization)
        await db.flush()
        db.add(
            OrganizationMember(
                organization_id=organization.id, user_id=user.id, role="OWNER"
            )
        )
        user.active_organization_id = organization.id

    await db.flush()
    return TokenResponse(
        access_token=create_access_token(str(user.id)),
        user_id=user.id,
        active_organization_id=user.active_organization_id,
    )


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email.lower().strip()))
    user = result.scalar_one_or_none()

    # Same message either way — never reveal whether an email is registered.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account is disabled"
        )

    return TokenResponse(
        access_token=create_access_token(str(user.id)),
        user_id=user.id,
        active_organization_id=user.active_organization_id,
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user
