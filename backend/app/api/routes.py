"""Dashboard endpoints for the active organization.

These back the live dashboard. Organization CRUD moved to
`app/api/organizations.py` and lead management to `app/api/crm.py`; what
remains here is the message stream and the at-a-glance figures.

Every statement filters on the tenant resolved from the caller's session.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import ChannelConfig, CRMContact, LLMLog, Message
from app.schemas import LLMLogOut, MessageOut, OutboundMessageRequest
from app.schemas_tenancy import CRMContactOut
from app.services import outbox, ws_manager
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
    channel = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.organization_id == tenant.id,
                ChannelConfig.is_active.is_(True),
            )
        )
    ).scalars().first()

    # Routed by the channel's provider, exactly as the agent's own replies are.
    # Calling Twilio directly here was a real bug: on a tenant paired over
    # WhatsApp Web it sent nothing, while the dashboard showed the operator's
    # message sitting in the thread as though it had gone.
    message = Message(
        organization_id=tenant.id,
        contact_id=contact.id,
        sender="agent",
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


@router.get("/stats")
async def dashboard_stats(
    tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)
):
    stage_rows = await db.execute(
        select(CRMContact.pipeline_stage, func.count(CRMContact.id))
        .where(CRMContact.organization_id == tenant.id)
        .group_by(CRMContact.pipeline_stage)
    )
    provider_rows = await db.execute(
        select(LLMLog.provider, func.count(LLMLog.id), func.avg(LLMLog.latency_ms))
        .where(LLMLog.organization_id == tenant.id)
        .group_by(LLMLog.provider)
    )
    total_messages = await db.scalar(
        select(func.count(Message.id)).where(Message.organization_id == tenant.id)
    )

    return {
        "organization": tenant.organization.name,
        "currency": tenant.organization.default_currency,
        "language": tenant.organization.default_language,
        "messages": total_messages or 0,
        "pipeline": {stage: count for stage, count in stage_rows.all()},
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
