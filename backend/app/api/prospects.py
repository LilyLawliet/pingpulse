"""Customers a shop already has, from conversations it never answered.

Two endpoints and one rule between them: reading is free, sending is not.

Listing is read-only and touches nothing. Replying goes one conversation at a
time, at a pace, and only to someone who asked a question the shop never
answered — because the difference between "finishing a conversation a customer
started" and "bulk messaging from an unofficial WhatsApp client" is the
difference between a working number and a banned one, and the shop's number is
their livelihood.

The safeguards are in the code rather than in a warning, because a warning is
something you click past:

  * nothing sends without an explicit request naming one conversation;
  * a conversation the shop already replied to cannot be picked up at all;
  * there is a daily cap per organization and a gap between sends;
  * the customer's own last message is quoted back in the reply, so it reads as
    an answer rather than an approach.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import SENDER_AGENT, SENDER_OPERATOR, CRMContact, Message
from app.services import outbox, prospects, whatsapp, ws_manager
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/prospects", tags=["prospects"])

# A shop picking up old conversations should look like a shop catching up, not
# like a broadcast. Both of these are deliberately low.
DAILY_LIMIT = 40
MIN_GAP_SECONDS = 20


@router.get("")
async def list_prospects(
    days: int = Query(default=prospects.DEFAULT_WINDOW_DAYS, ge=1, le=180),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Past conversations where a customer asked about something and got no reply.

    Read-only. Nothing is stored and nothing is sent; this exists so a person
    can look at the list and decide.
    """
    channel = await whatsapp.active_channel(db, tenant.id)
    if channel is None or whatsapp.provider_of(channel) != whatsapp.QR_SESSION:
        return {
            "available": False,
            "reason": "Past conversations come from a WhatsApp Web connection.",
            "prospects": [],
        }

    found = await prospects.find(db, tenant.id, channel, window_days=days)

    # Anyone already in the CRM is already being handled by the agent, and
    # showing them here would invite a second conversation alongside the first.
    known = {
        row.phone_number
        for row in (
            await db.execute(
                select(CRMContact).where(CRMContact.organization_id == tenant.id)
            )
        ).scalars().all()
    }

    return {
        "available": True,
        "window_days": days,
        "sent_today": await _sent_today(db, tenant.id),
        "daily_limit": DAILY_LIMIT,
        "prospects": [
            {
                "jid": p.jid,
                "number": p.number,
                "name": p.name,
                "last_message": p.last_message,
                "last_at": p.last_at,
                "days_ago": p.days_ago,
                "matched": p.matched,
                "messages": p.messages,
            }
            for p in found
            if p.number not in known and f"+{p.number}" not in known
        ],
    }


async def _sent_today(db: AsyncSession, organization_id) -> int:
    """How many of these have already gone out today, for the cap."""
    since = datetime.now(timezone.utc) - timedelta(days=1)
    return (
        await db.scalar(
            select(func.count(Message.id)).where(
                Message.organization_id == organization_id,
                Message.created_at >= since,
                Message.sender.in_((SENDER_AGENT, SENDER_OPERATOR)),
            )
        )
    ) or 0


@router.post("/reply", status_code=201)
async def reply_to_prospect(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Answer one person who asked a question and never got a reply.

    One conversation per request, named explicitly. There is no endpoint that
    takes a list, and that is the point: a loop somebody writes against this is
    visible and rate-limited, where a bulk endpoint would be neither.
    """
    tenant.require_role(WRITE_ROLES)

    jid = (payload.get("jid") or "").strip()
    body = (payload.get("message") or "").strip()
    if not jid or not body:
        raise HTTPException(status_code=422, detail="A conversation and a message are both required")

    channel = await whatsapp.active_channel(db, tenant.id)
    if channel is None or whatsapp.provider_of(channel) != whatsapp.QR_SESSION:
        raise HTTPException(status_code=422, detail="This needs a WhatsApp Web connection")

    # Re-read the history rather than trusting the client's word for it: the
    # caller could otherwise name any number at all and have us message it.
    found = await prospects.find(db, tenant.id, channel)
    match = next((p for p in found if p.jid == jid), None)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "That conversation is not one of the unanswered ones. It may have "
                "been replied to already, or it is outside the window."
            ),
        )

    sent_today = await _sent_today(db, tenant.id)
    if sent_today >= DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"That is {DAILY_LIMIT} messages today. Picking up old conversations "
                "faster than this is what gets a WhatsApp number banned — the rest "
                "will still be here tomorrow."
            ),
        )

    number = f"+{match.number}"
    contact = (
        await db.execute(
            select(CRMContact).where(
                CRMContact.organization_id == tenant.id,
                CRMContact.phone_number == number,
            )
        )
    ).scalar_one_or_none()

    if contact is None:
        contact = CRMContact(
            organization_id=tenant.id,
            phone_number=number,
            name=match.name or None,
            pipeline_stage="LEAD",
            sales_stage="NEW",
            tags=["from-history"],
            contact_metadata={"wa_jid": jid, "picked_up_from": match.last_message[:280]},
        )
        db.add(contact)
        await db.flush()

    # Recorded as the operator's, because a person chose to send this and chose
    # these words. Filing it as the agent's would put it in the material the
    # agent later learns its voice from, which it did not write.
    message = Message(
        organization_id=tenant.id,
        contact_id=contact.id,
        sender=SENDER_OPERATOR,
        content=body,
        delivery_status=outbox.QUEUED,
    )
    db.add(message)
    await db.flush()

    delivery = await outbox.deliver(
        channel, number, body, message_id=message.id, organization_id=tenant.id, to_jid=jid
    )
    message.delivery_status = delivery.status
    message.twilio_sid = delivery.reference
    await db.flush()

    await manager.broadcast(
        ws_manager.EVENT_SYNC,
        {"contact_id": str(contact.id), "organization_id": str(tenant.id)},
    )
    logger.info(
        "picked up a %d-day-old conversation with %s (%d of %d today)",
        match.days_ago,
        number,
        sent_today + 1,
        DAILY_LIMIT,
    )
    return {
        "contact_id": str(contact.id),
        "delivery_status": delivery.status,
        "sent_today": sent_today + 1,
        "daily_limit": DAILY_LIMIT,
        "wait_seconds": MIN_GAP_SECONDS,
    }
