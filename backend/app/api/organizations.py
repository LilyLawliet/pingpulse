"""Organizations: create, list, update, switch, members and channels.

Listing returns only organizations the caller is a member of. Every read or
write of a specific organization proves membership first — an id from the
client is never trusted on its own.
"""

from __future__ import annotations

import logging
import uuid
from zoneinfo import ZoneInfo

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import ADMIN_ROLES, Tenant, current_org, current_user, membership_of
from app.models import ChannelConfig, Organization, OrganizationMember, User
from app.services import outbox, whatsapp
from app.services import pipelines
from app.schemas_tenancy import (
    ChannelConfigCreate,
    ChannelConfigOut,
    InviteMemberRequest,
    MemberOut,
    OrganizationCreate,
    OrganizationMembershipOut,
    OrganizationOut,
    OrganizationUpdate,
    SwitchOrganizationRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])


@router.post("", response_model=OrganizationOut, status_code=201)
async def create_organization(
    payload: OrganizationCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an organization; the caller becomes its OWNER and switches to it."""
    organization = Organization(**payload.model_dump())
    db.add(organization)
    await db.flush()

    db.add(
        OrganizationMember(
            organization_id=organization.id, user_id=user.id, role="OWNER"
        )
    )
    user.active_organization_id = organization.id

    # Give it a board of its own straight away. Reading falls back to the
    # defaults without these rows, but a tenant cannot rename or reorder a
    # column that does not exist yet, so the first customisation would
    # otherwise have nothing to edit.
    await pipelines.seed(db, organization.id)

    await db.flush()
    await db.refresh(organization)
    return organization


@router.get("", response_model=list[OrganizationMembershipOut])
async def list_my_organizations(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Only organizations this user belongs to — never the whole table."""
    result = await db.execute(
        select(Organization, OrganizationMember)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user.id)
        .order_by(Organization.created_at)
    )
    return [
        OrganizationMembershipOut(
            organization=OrganizationOut.model_validate(organization),
            role=membership.role,
            is_active=organization.id == user.active_organization_id,
        )
        for organization, membership in result.all()
    ]


@router.get("/active", response_model=OrganizationOut)
async def active_organization(tenant: Tenant = Depends(current_org)):
    return tenant.organization


@router.post("/switch", response_model=OrganizationOut)
async def switch_organization(
    payload: SwitchOrganizationRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the active organization, if the caller is a member of it."""
    membership = await membership_of(db, user, payload.organization_id)
    if membership is None:
        # 404 rather than 403: do not confirm that an organization exists to
        # someone who has no business knowing.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    organization = await db.get(Organization, payload.organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    user.active_organization_id = organization.id
    await db.flush()
    return organization


@router.patch("/active", response_model=OrganizationOut)
async def update_active_organization(
    payload: OrganizationUpdate,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(ADMIN_ROLES)
    changes = payload.model_dump(exclude_unset=True)

    # Checked here as well as on the agent config, because there are now two
    # ways in and a zone the reply path cannot load would have the agent
    # apologising about the wrong opening hours rather than failing visibly.
    if changes.get("timezone") is not None:
        try:
            ZoneInfo(str(changes["timezone"]))
        except Exception:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail=f"'{changes['timezone']}' is not a timezone. "
                "Use an IANA name like America/New_York or Asia/Karachi.",
            )
        changes["timezone"] = str(changes["timezone"])[:64]

    for field, value in changes.items():
        setattr(tenant.organization, field, value)
    await db.flush()
    await db.refresh(tenant.organization)
    return tenant.organization


@router.get("/{organization_id}", response_model=OrganizationOut)
async def get_organization(
    organization_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if await membership_of(db, user, organization_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )
    organization = await db.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )
    return organization


@router.patch("/{organization_id}", response_model=OrganizationOut)
async def update_organization(
    organization_id: uuid.UUID,
    payload: OrganizationUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    membership = await membership_of(db, user, organization_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )
    if membership.role not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Your role does not allow that"
        )

    organization = await db.get(Organization, organization_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(organization, field, value)
    await db.flush()
    await db.refresh(organization)
    return organization


# ------------------------------- Members ----------------------------------
@router.get("/active/members", response_model=list[MemberOut])
async def list_members(
    tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(OrganizationMember)
        .where(OrganizationMember.organization_id == tenant.id)
        .order_by(OrganizationMember.created_at)
    )
    return result.scalars().all()


@router.post("/active/members", response_model=MemberOut, status_code=201)
async def add_member(
    payload: InviteMemberRequest,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Add an existing account to this organization."""
    tenant.require_role(ADMIN_ROLES)

    result = await db.execute(select(User).where(User.email == payload.email.lower().strip()))
    invitee = result.scalar_one_or_none()
    if invitee is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account with that email. Ask them to sign up first.",
        )

    if await membership_of(db, invitee, tenant.id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="They are already a member of this organization",
        )

    membership = OrganizationMember(
        organization_id=tenant.id, user_id=invitee.id, role=payload.role
    )
    db.add(membership)
    await db.flush()
    await db.refresh(membership)
    return membership


@router.delete("/active/members/{member_id}", status_code=204)
async def remove_member(
    member_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(ADMIN_ROLES)

    # Scoped by organization: a member id from another tenant simply is not found.
    result = await db.execute(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == tenant.id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    if membership.role == "OWNER":
        owners = await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == tenant.id,
                OrganizationMember.role == "OWNER",
            )
        )
        if len(owners.scalars().all()) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An organization must keep at least one owner",
            )

    await db.delete(membership)
    return None


# ------------------------------- Channels ---------------------------------
async def _flag_number_conflicts(db: AsyncSession, channels) -> None:
    """Note any channel whose phone another organization also holds.

    The unique constraint is on the stored string, so one handset written two
    ways - "+923097209908" and "923097209908" - sits on two organizations
    without anything objecting. It stays invisible until the phone is paired,
    at which point inbound routes by session id to whichever channel was
    scanned, and the other organization's number silently means nothing.

    Read from the channels table each time rather than stored, so it clears
    itself the moment somebody resolves it.
    """
    if not channels:
        return

    everything = (
        await db.execute(
            select(ChannelConfig.phone_number, ChannelConfig.id, Organization.name)
            .join(Organization, Organization.id == ChannelConfig.organization_id)
        )
    ).all()

    for channel in channels:
        channel.number_conflict = next(
            (
                name
                for number, other_id, name in everything
                if other_id != channel.id
                and whatsapp.same_number(number, channel.phone_number)
            ),
            None,
        )


@router.get("/active/channels", response_model=list[ChannelConfigOut])
async def list_channels(
    tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ChannelConfig)
        .where(ChannelConfig.organization_id == tenant.id)
        .order_by(ChannelConfig.created_at)
    )
    channels = result.scalars().all()
    await _flag_number_conflicts(db, channels)
    return channels


@router.post("/active/channels", response_model=ChannelConfigOut, status_code=201)
async def add_channel(
    payload: ChannelConfigCreate,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Claim a phone number for this organization.

    Numbers are globally unique per channel, because inbound routing depends on
    the destination number identifying exactly one tenant.
    """
    tenant.require_role(ADMIN_ROLES)

    provider_name = (payload.whatsapp_provider or "TWILIO").upper()

    # One spelling per phone. Compared raw, "+923097209908" and "923097209908"
    # are two different numbers, and two organizations claimed the same
    # handset that way - discovered only when it was paired and the bridge
    # wrote back the bare form onto a constraint that refused it.
    number = whatsapp.normalise_number(payload.phone_number)

    if provider_name == whatsapp.QR_SESSION:
        # Not asked for, and not taken if offered. The handset reports its own
        # number the moment the session authenticates, so a typed one is a
        # second copy of a fact that is about to arrive - and the copy the
        # uniqueness rule would be enforced against. Typing it is how one
        # phone came to be claimed by two organizations under two spellings.
        number = None
    elif not number:
        raise HTTPException(
            status_code=422, detail="That does not look like a phone number."
        )

    others = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.channel == payload.channel,
                # A number this organization already holds is not a clash: it
                # is the row about to be replaced below.
                ChannelConfig.organization_id != tenant.id,
            )
        )
    ).scalars().all()

    # `same_number` is false for an unknown number, so a pairing in flight
    # never collides with anything. The real check happens when the scan
    # reports the number, against the row that already holds it.
    if number and any(whatsapp.same_number(number, other.phone_number) for other in others):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That number is already connected to an organization",
        )

    # One WhatsApp method at a time. Twilio and a paired handset are two ways
    # to reach the same customers, and having both connected means every
    # outbound message has to guess which number the conversation belongs to.
    # Connecting one therefore disconnects the other — unpairing the handset
    # and clearing anything still queued for it, so nothing is left half-alive.
    existing = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.organization_id == tenant.id,
                ChannelConfig.channel == payload.channel,
            )
        )
    ).scalars().all()

    for previous in existing:
        if whatsapp.provider_of(previous) == whatsapp.QR_SESSION:
            await _unpair(previous.id)
        await outbox.discard(previous.id)
        logger.info(
            "replacing %s channel %s with a new %s connection",
            whatsapp.provider_of(previous),
            previous.phone_number,
            provider_name,
        )
        await db.delete(previous)

    await db.flush()

    config = ChannelConfig(
        organization_id=tenant.id,
        channel=payload.channel,
        provider=payload.provider,
        phone_number=number,
        whatsapp_provider=provider_name,
        account_sid=payload.account_sid,
        auth_token=payload.auth_token,
    )
    db.add(config)
    await db.flush()
    await db.refresh(config)
    return config


@router.delete("/active/channels/{channel_id}", status_code=204)
async def remove_channel(
    channel_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(ADMIN_ROLES)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.id == channel_id,
            ChannelConfig.organization_id == tenant.id,
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    # Unpair the handset before the row goes, and clear anything still queued
    # for it. Deleting only the row left the phone linked on the bridge, with
    # its credentials on disk: it kept forwarding the customer's messages after
    # every restart, to a channel that no longer existed, and every one of them
    # was dropped. Best-effort — a bridge that is down must not block a delete.
    if whatsapp.provider_of(config) == whatsapp.QR_SESSION:
        await _unpair(config.id)
    await outbox.discard(config.id)

    await db.delete(config)
    return None


async def _unpair(channel_id: uuid.UUID) -> bool:
    """Tell the bridge to log this session out and forget its credentials.

    Returns whether the bridge confirmed it. A caller about to delete the row
    can ignore that — the channel is going either way. A caller telling an
    operator their phone is unlinked cannot: an unreachable bridge still holds
    the credentials, and resumes the session with them on its next restart.
    """
    try:
        async with httpx.AsyncClient(timeout=settings.wa_qr_timeout_seconds) as client:
            response = await client.post(
                f"{settings.wa_qr_service_url.rstrip('/')}/logout",
                json={"sessionId": str(channel_id)},
                headers={"X-PingPulse-Bridge": settings.wa_qr_shared_secret},
            )
            response.raise_for_status()
        logger.info("unpaired session %s", channel_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not unpair session %s (%s) — the phone may still be linked; "
            "remove it from WhatsApp's linked devices screen",
            channel_id,
            exc,
        )
        return False


# --------------------------------------------------------------------------
# WhatsApp Web pairing
# --------------------------------------------------------------------------
# The bridge is not reachable from the internet and holds a shared secret the
# desktop app must never see. These two routes are the only way in: they prove
# the caller owns the channel, then talk to the bridge on their behalf.
@router.post("/active/channels/{channel_id}/pair", status_code=202)
async def start_pairing(
    channel_id: uuid.UUID,
    fresh: bool = Query(default=False),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Ask the bridge to begin a WhatsApp Web pairing for this channel.

    `fresh` throws away whatever keys the bridge holds for it first - what
    "Try again" sends after WhatsApp refused every attempt, because refused
    keys are refused again.
    """
    tenant.require_role(ADMIN_ROLES)
    channel = await _own_channel(db, tenant, channel_id)

    async with httpx.AsyncClient(timeout=settings.wa_qr_timeout_seconds) as client:
        try:
            response = await client.post(
                f"{settings.wa_qr_service_url.rstrip('/')}/pair",
                json={"sessionId": str(channel.id), "fresh": bool(fresh)},
                headers={"X-PingPulse-Bridge": settings.wa_qr_shared_secret},
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"The WhatsApp bridge is not reachable: {exc}",
            )

    channel.session_status = "GENERATING_QR"
    return {"ok": True, "status": "GENERATING_QR"}


@router.post("/active/channels/{channel_id}/unpair")
async def unpair_channel(
    channel_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Unlink the phone, keep the connection: "Show QR" links it again.

    Deleting the channel was the only way to disconnect, and reconnecting then
    meant setting it up from nothing. The phone is logged out on the bridge and
    its stored keys are removed; the number goes too, since it arrives with the
    next scan and may be a different phone.
    """
    tenant.require_role(ADMIN_ROLES)
    channel = await _own_channel(db, tenant, channel_id)
    if whatsapp.provider_of(channel) != whatsapp.QR_SESSION:
        raise HTTPException(status_code=422, detail="Only a scanned phone can be unlinked")
    # Nothing is recorded until the bridge says the phone is off. Writing
    # DISCONNECTED regardless would tell the operator their handset is
    # unlinked while it still holds live credentials and comes back with them
    # the next time the bridge starts.
    if not await _unpair(channel.id):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The WhatsApp connection service did not confirm the phone was "
                "unlinked, so nothing was changed. Try again in a moment."
            ),
        )
    channel.session_status = "DISCONNECTED"
    channel.phone_number = None
    await db.flush()
    return {"ok": True, "status": "DISCONNECTED"}


@router.get("/active/channels/{channel_id}/qr")
async def pairing_state(
    channel_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Where the pairing has got to, and the current QR if one is waiting.

    Polled by the desktop app while the pairing panel is open. A QR rotates
    every twenty seconds or so, which is why this is polled rather than
    fetched once.
    """
    channel = await _own_channel(db, tenant, channel_id)

    async with httpx.AsyncClient(timeout=settings.wa_qr_timeout_seconds) as client:
        try:
            response = await client.get(
                f"{settings.wa_qr_service_url.rstrip('/')}/session/{channel.id}",
                headers={"X-PingPulse-Bridge": settings.wa_qr_shared_secret},
            )
            response.raise_for_status()
            state = response.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"The WhatsApp bridge is not reachable: {exc}",
            )

    return {
        "status": state.get("status", "UNKNOWN"),
        "qr": state.get("qr"),
        "connected": bool(state.get("connected")),
        "phone_number": channel.phone_number,
        # Why no code is showing, when none is: the screen says this instead
        # of a spinner that never ends.
        "reason": state.get("reason"),
        "code": state.get("code"),
        "tries": state.get("tries") or 0,
        "gave_up": bool(state.get("gaveUp")),
    }


async def _own_channel(db: AsyncSession, tenant: Tenant, channel_id: uuid.UUID) -> ChannelConfig:
    """The channel, if it belongs to the caller's organization.

    Another tenant's channel returns 404 rather than 403 — the same rule the
    rest of the API follows, so an id cannot be probed for existence.
    """
    channel = await db.get(ChannelConfig, channel_id)
    if channel is None or channel.organization_id != tenant.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        )
    return channel
