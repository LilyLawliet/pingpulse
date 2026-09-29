"""Dashboard endpoints for the active organization.

These back the live dashboard. Organization CRUD moved to
`app/api/organizations.py` and lead management to `app/api/crm.py`; what
remains here is the message stream and the at-a-glance figures.

Every statement filters on the tenant resolved from the caller's session.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import (
    SENDER_OPERATOR,
    STAGE_OPERATOR,
    ChannelConfig,
    CRMContact,
    LLMLog,
    Message,
)
from app.schemas import (
    FollowUpRequest,
    FollowUpState,
    LLMLogOut,
    MessageOut,
    OutboundMessageRequest,
)
from app.schemas_tenancy import CRMContactOut
from app.services import analytics, outbox, pipelines, whatsapp, ws_manager
from app.services.ws_manager import manager

router = APIRouter(prefix="/api/v1", tags=["dashboard"])


@router.get("/contacts", response_model=list[CRMContactOut])
async def list_contacts(
    pipeline_stage: str | None = None,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Contacts for the active organization (the dashboard's left pane)."""
    query = select(CRMContact).where(CRMContact.organization_id == tenant.id)
    if pipeline_stage:
        query = query.where(CRMContact.pipeline_stage == pipeline_stage.upper())
    result = await db.execute(query.order_by(CRMContact.created_at.desc()))
    return result.scalars().all()


@router.patch("/contacts/{contact_id}", response_model=CRMContactOut)
async def update_contact_stage(
    contact_id: uuid.UUID,
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Move a lead between stages from the dashboard."""
    tenant.require_role(WRITE_ROLES)
    result = await db.execute(
        select(CRMContact).where(
            CRMContact.id == contact_id,
            CRMContact.organization_id == tenant.id,
        )
    )
    contact = result.scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    previous_stage = contact.pipeline_stage
    for field in ("name", "pipeline_stage", "notes"):
        if field in payload and payload[field] is not None:
            setattr(contact, field, payload[field])
    await db.flush()
    await db.refresh(contact)

    if contact.pipeline_stage != previous_stage:
        # A person dragged this, so it is filed as theirs. "The agent decided"
        # and "somebody moved it" are different facts about the same lead, and
        # a funnel that cannot tell them apart cannot say whether the agent is
        # working.
        await analytics.record_move(
            db,
            contact,
            contact.pipeline_stage,
            from_stage=previous_stage,
            source=STAGE_OPERATOR,
        )
        await manager.broadcast(
            ws_manager.EVENT_STAGE,
            {
                "contact_id": str(contact.id),
                "organization_id": str(tenant.id),
                "phone_number": contact.phone_number,
                "from": previous_stage,
                "to": contact.pipeline_stage,
                "source": "manual",
            },
        )
    return contact


@router.get("/contacts/{contact_id}/messages", response_model=list[MessageOut])
async def contact_messages(
    contact_id: uuid.UUID,
    limit: int = Query(default=200, le=500),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Message)
        .join(CRMContact, CRMContact.id == Message.contact_id)
        .where(
            Message.contact_id == contact_id,
            CRMContact.organization_id == tenant.id,
        )
        .order_by(Message.created_at)
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/messages", response_model=list[MessageOut])
async def list_messages(
    limit: int = Query(default=100, le=500),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Message)
        .where(Message.organization_id == tenant.id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


@router.post("/messages/send", response_model=MessageOut, status_code=201)
async def send_manual_message(
    payload: OutboundMessageRequest,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Operator takeover - send a hand-written WhatsApp reply from the dashboard."""
    tenant.require_role(WRITE_ROLES)
    result = await db.execute(
        select(CRMContact).where(
            CRMContact.id == payload.contact_id,
            CRMContact.organization_id == tenant.id,
        )
    )
    contact = result.scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    # An operator's message goes out on the same number the agent uses.
    channel = await whatsapp.active_channel(db, tenant.id)

    # Routed by the channel's provider, exactly as the agent's own replies are.
    # Calling Twilio directly here was a real bug: on a tenant paired over
    # WhatsApp Web it sent nothing, while the dashboard showed the operator's
    # message sitting in the thread as though it had gone.
    message = Message(
        organization_id=tenant.id,
        contact_id=contact.id,
        # A person wrote this, not the model. Stored as such because it is the
        # only honest record of how this shop talks to its customers, and a
        # persona learned from the model's own output learns nothing.
        sender=SENDER_OPERATOR,
        content=payload.content,
        delivery_status=outbox.QUEUED,
    )
    db.add(message)
    await db.flush()

    delivery = await outbox.deliver(
        channel,
        contact.phone_number,
        payload.content,
        message_id=message.id,
        organization_id=tenant.id,
        to_jid=(contact.contact_metadata or {}).get("wa_jid"),
    )
    message.delivery_status = delivery.status
    message.twilio_sid = delivery.reference
    sent = delivery.sent
    await db.flush()
    await db.refresh(message)

    await manager.broadcast(
        ws_manager.EVENT_OUTBOUND,
        {
            "message_id": str(message.id),
            "contact_id": str(contact.id),
            "organization_id": str(tenant.id),
            "phone_number": contact.phone_number,
            "content": payload.content,
            "provider": "manual",
            "latency_ms": 0,
            "delivered": sent,
            "delivery_status": delivery.status,
            "twilio_sid": delivery.reference,
        },
    )
    return message


@router.post("/contacts/{contact_id}/followup", response_model=FollowUpState, status_code=202)
async def schedule_followup(
    contact_id: uuid.UUID,
    payload: FollowUpRequest,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Queue one nudge for this contact, at a delay of the operator's choosing.

    The automatic sequence only fires for warm conversations at fixed hours,
    which makes it impossible to watch working. This is the same machinery
    driven by hand: same task, same transport, same cancellation — a customer
    who writes back before it lands clears the token and the nudge never goes.
    """
    tenant.require_role(WRITE_ROLES)

    contact = (
        await db.execute(
            select(CRMContact).where(
                CRMContact.id == contact_id,
                CRMContact.organization_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    from app.tasks import schedule_one

    message = (payload.message or "").strip() or None
    token = schedule_one(contact, payload.minutes, message)
    if token is None:
        raise HTTPException(
            status_code=503,
            detail="The scheduler is not reachable — the follow-up was not queued.",
        )

    due_at = datetime.now(timezone.utc) + timedelta(minutes=payload.minutes)
    metadata = dict(contact.contact_metadata or {})
    metadata["followup_token"] = token
    metadata["followup_due_at"] = due_at.isoformat()
    if message:
        metadata["followup_message"] = message
    else:
        metadata.pop("followup_message", None)
    contact.contact_metadata = metadata

    await manager.broadcast(
        ws_manager.EVENT_SYNC,
        {"contact_id": str(contact.id), "organization_id": str(tenant.id)},
    )
    return FollowUpState(scheduled=True, due_at=due_at, message=message)


@router.delete("/contacts/{contact_id}/followup", response_model=FollowUpState)
async def cancel_followup(
    contact_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Call off a pending nudge.

    Clearing the token is the cancellation: the queued task still runs at its
    appointed time, looks for a token that no longer matches, and exits. That
    is deliberate — revoking a scheduled task is unreliable across brokers,
    while a database write is not.
    """
    tenant.require_role(WRITE_ROLES)

    contact = (
        await db.execute(
            select(CRMContact).where(
                CRMContact.id == contact_id,
                CRMContact.organization_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    metadata = dict(contact.contact_metadata or {})
    for key in ("followup_token", "followup_due_at", "followup_message"):
        metadata.pop(key, None)
    contact.contact_metadata = metadata

    await manager.broadcast(
        ws_manager.EVENT_SYNC,
        {"contact_id": str(contact.id), "organization_id": str(tenant.id)},
    )
    return FollowUpState(scheduled=False)


@router.get("/logs", response_model=list[LLMLogOut])
async def list_logs(
    limit: int = Query(default=50, le=200),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(LLMLog)
        .where(LLMLog.organization_id == tenant.id)
        .order_by(LLMLog.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


# Both the dashboard strip and the analytics screen have to agree about when
# "7 days" begins, so the span lives in one place and both read it from there.
WINDOWS = analytics.WINDOWS


def _window_start(
    window: str | None, since: datetime | None, zone=None
) -> datetime | None:
    return analytics.window_start(window, since, zone)


@router.get("/stats")
async def dashboard_stats(
    window: str = Query(default="all", description="today | 7d | 30d | 90d | all"),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """The numbers on the dashboard, over a chosen span of time.

    Contacts are counted by when they arrived and messages by when they were
    sent, so "30 days" means the same thing in both. The pipeline itself is
    always current: a board showing only the leads created this week would be
    a different question from the one it looks like it is answering.
    """
    start = _window_start(
        window, since, analytics.zone_for(tenant.organization.timezone)
    )

    stage_query = select(CRMContact.pipeline_stage, func.count(CRMContact.id)).where(
        CRMContact.organization_id == tenant.id
    )
    provider_query = select(
        LLMLog.provider, func.count(LLMLog.id), func.avg(LLMLog.latency_ms)
    ).where(LLMLog.organization_id == tenant.id)
    message_query = select(func.count(Message.id)).where(
        Message.organization_id == tenant.id
    )
    new_contacts_query = select(func.count(CRMContact.id)).where(
        CRMContact.organization_id == tenant.id
    )

    if start is not None:
        provider_query = provider_query.where(LLMLog.created_at >= start)
        message_query = message_query.where(Message.created_at >= start)
        new_contacts_query = new_contacts_query.where(CRMContact.created_at >= start)
    if until is not None:
        provider_query = provider_query.where(LLMLog.created_at <= until)
        message_query = message_query.where(Message.created_at <= until)
        new_contacts_query = new_contacts_query.where(CRMContact.created_at <= until)

    stage_rows = await db.execute(stage_query.group_by(CRMContact.pipeline_stage))
    provider_rows = await db.execute(provider_query.group_by(LLMLog.provider))
    total_messages = await db.scalar(message_query)
    new_contacts = await db.scalar(new_contacts_query)

    by_stage = {stage: count for stage, count in stage_rows.all()}

    # What each column means for counting, so "Booked" stops being one word
    # that could mean an appointment, a job or a sale. The board says which
    # column carries which meaning and the screen shows the tenant's own label.
    stages = await pipelines.stages_for(db, tenant.id)
    outcomes: dict[str, int] = {}
    labels: dict[str, str] = {}
    for stage in stages:
        if stage.outcome:
            outcomes[stage.outcome] = outcomes.get(stage.outcome, 0) + by_stage.get(stage.key, 0)
            labels.setdefault(stage.outcome, stage.label)

    return {
        "organization": tenant.organization.name,
        "currency": tenant.organization.default_currency,
        "language": tenant.organization.default_language,
        "window": window,
        "since": start,
        "messages": total_messages or 0,
        "new_contacts": new_contacts or 0,
        "pipeline": by_stage,
        "outcomes": outcomes,
        "outcome_labels": labels,
        "providers": [
            {
                "provider": provider,
                "calls": calls,
                "avg_latency_ms": round(float(avg_latency), 1) if avg_latency else 0.0,
            }
            for provider, calls, avg_latency in provider_rows.all()
        ],
        "monitor_clients": manager.connection_count,
    }
