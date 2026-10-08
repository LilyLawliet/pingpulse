"""The calendar on the dashboard: seeing the diary and changing it by hand.

Every change goes through `booking` - the same checks against the shop's
hours and its diary, and the same exclusion constraint - that a customer's
booking does. A person at the shop can fit somebody in at short notice, and
that is the only rule relaxed for them: they cannot book outside the opening
hours or on top of another appointment, because the agent would then be
offering customers a diary that is not the real one.

When a change is made here the customer can be told on WhatsApp. What they
are sent is rendered from the row that was just written, never from what was
typed, so it cannot say a time the calendar does not hold.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import (
    APPOINTMENT_KINDS,
    SENDER_CUSTOMER,
    SENDER_OPERATOR,
    STAGE_OPERATOR,
    Appointment,
    CRMContact,
    Message,
)
from app.services import agent_config, analytics, booking, invites, languages, outbox, pipelines, whatsapp
from app.services import ws_manager
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["appointments"])

# The longest span one request may read. A month view is the most any screen
# shows; anything more is somebody asking for the whole table.
MAX_DAYS = 42


class BookIn(BaseModel):
    contact_id: uuid.UUID
    starts_at: datetime
    kind: str | None = None
    location: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=2000)
    tell_customer: bool = False

    @field_validator("kind")
    @classmethod
    def _a_kind_this_shop_can_book(cls, value: str | None) -> str | None:
        """An unknown kind is refused here rather than quietly made the default.

        `book()` falls back to the shop's default for anything it does not
        recognise, and inside a conversation that is right: a reading that
        comes back "call" must not end the turn with nothing booked.

        At this boundary somebody has sent a value deliberately. Booking with
        kind "call" returned an appointment described as "site visit", with
        nothing anywhere to say it had been changed - the caller asked for one
        thing, the diary recorded another, and both sides thought they agreed.
        Say no instead, and name what this takes.
        """
        if value is None:
            return None
        chosen = value.strip().lower()
        if chosen not in APPOINTMENT_KINDS:
            raise ValueError("kind must be one of: " + ", ".join(APPOINTMENT_KINDS))
        return chosen


class MoveIn(BaseModel):
    starts_at: datetime
    tell_customer: bool = False


class CancelIn(BaseModel):
    tell_customer: bool = False


def _row(appointment: Appointment) -> dict:
    contact = appointment.contact
    return {
        **booking.as_summary(appointment),
        "starts_at": booking._aware(appointment.starts_at).isoformat(),
        "ends_at": booking._aware(appointment.ends_at).isoformat(),
        "notes": appointment.notes,
        "source": appointment.source,
        "replaces_id": str(appointment.replaces_id) if appointment.replaces_id else None,
        "cancelled_at": booking._aware(appointment.cancelled_at).isoformat()
        if appointment.cancelled_at
        else None,
        "contact": {
            "id": str(contact.id),
            "name": contact.name,
            "phone_number": contact.phone_number,
        }
        if contact is not None
        else None,
    }


def _hours(organization) -> dict:
    """Each weekday's opening hours, exactly as booking reads them."""
    out = {}
    for index, day in enumerate(agent_config.DAYS):
        # Any date with this weekday gives the same window.
        sample = date(2024, 1, 1) + timedelta(days=index)
        window = booking._window_for(organization, sample)
        out[day] = (
            {"open": window[0].strftime("%H:%M"), "close": window[1].strftime("%H:%M")}
            if window
            else None
        )
    return out


def _aware_input(moment: datetime, organization) -> datetime:
    """A time from the dashboard, read in the shop's zone if it carries none."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=agent_config.zone_of(organization))
    return moment.astimezone(timezone.utc)


async def _appointment(db, tenant: Tenant, appointment_id: uuid.UUID) -> Appointment:
    found = (
        await db.execute(
            select(Appointment)
            .options(selectinload(Appointment.contact))
            .where(Appointment.id == appointment_id, Appointment.organization_id == tenant.id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return found


def _refused(result) -> None:
    if not result.ok:
        raise HTTPException(status_code=409, detail={"reason": result.reason, "message": result.message})


async def _tell(db, tenant: Tenant, contact: CRMContact, text: str) -> dict:
    """Send the customer what changed, in their language when it can be kept exact."""
    last = (
        await db.execute(
            select(Message.content)
            .where(Message.contact_id == contact.id, Message.sender == SENDER_CUSTOMER)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last:
        text = await languages.in_customer_language(text, last)

    channel = await whatsapp.active_channel(db, tenant.id)
    message = Message(
        organization_id=tenant.id,
        contact_id=contact.id,
        sender=SENDER_OPERATOR,
        content=text,
        delivery_status=outbox.QUEUED,
    )
    db.add(message)
    await db.flush()
    delivery = await outbox.deliver(
        channel,
        contact.phone_number,
        text,
        message_id=message.id,
        organization_id=tenant.id,
        to_jid=(contact.contact_metadata or {}).get("wa_jid"),
    )
    message.delivery_status = delivery.status
    message.twilio_sid = delivery.reference
    await db.flush()
    await manager.broadcast(
        ws_manager.EVENT_OUTBOUND,
        {
            "message_id": str(message.id),
            "contact_id": str(contact.id),
            "organization_id": str(tenant.id),
            "phone_number": contact.phone_number,
            "content": text,
            "provider": "manual",
            "latency_ms": 0,
            "delivered": delivery.sent,
            "delivery_status": delivery.status,
        },
    )
    return {"sent": delivery.sent, "status": delivery.status, "text": text}


async def _changed(db, tenant: Tenant, contact_id) -> None:
    await manager.broadcast(
        ws_manager.EVENT_SYNC,
        {
            "contact_id": str(contact_id) if contact_id else None,
            "organization_id": str(tenant.id),
        },
    )


@router.get("/appointments")
async def list_appointments(
    start: date | None = None,
    days: int = Query(default=7, ge=1, le=MAX_DAYS),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """The diary for a span of the shop's own days, with the hours it is read against."""
    organization = tenant.organization
    zone = agent_config.zone_of(organization)
    first = start or datetime.now(zone).date()
    since = datetime.combine(first, time(0, 0), tzinfo=zone).astimezone(timezone.utc)
    until = datetime.combine(first + timedelta(days=days), time(0, 0), tzinfo=zone).astimezone(
        timezone.utc
    )
    rows = (
        await db.execute(
            select(Appointment)
            .options(selectinload(Appointment.contact))
            .where(
                Appointment.organization_id == tenant.id,
                Appointment.ends_at > since,
                Appointment.starts_at < until,
            )
            .order_by(Appointment.starts_at.asc())
        )
    ).scalars().all()
    return {
        "start": first.isoformat(),
        "days": days,
        "timezone": str(zone),
        "hours": _hours(organization),
        "duration_minutes": booking.duration_minutes(organization),
        "slot_minutes": booking.SLOT_STEP_MINUTES,
        "kinds": list(APPOINTMENT_KINDS),
        "default_kind": booking.default_kind(organization),
        "readiness": booking.readiness(organization),
        "appointments": [_row(row) for row in rows],
    }


@router.get("/appointments/free")
async def free_times(
    day: date,
    kind: str | None = None,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Every free start time on one day, for picking one by hand."""
    organization = tenant.organization
    zone = agent_config.zone_of(organization)
    start = datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(timezone.utc)
    slots = await booking.free_slots(
        db, organization, from_time=start, days=0, kind=kind, limit=200
    )
    slots = [slot for slot in slots if slot.astimezone(zone).date() == day]
    return {
        "day": day.isoformat(),
        "hours": booking.hours_on(organization, day),
        "slots": [slot.isoformat() for slot in slots],
    }


@router.post("/appointments", status_code=201)
async def book_appointment(
    payload: BookIn,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Book a customer in by hand, under the same rules the agent books by."""
    tenant.require_role(WRITE_ROLES)
    organization = tenant.organization
    contact = (
        await db.execute(
            select(CRMContact).where(
                CRMContact.id == payload.contact_id, CRMContact.organization_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    result = await booking.book(
        db,
        organization,
        contact,
        _aware_input(payload.starts_at, organization),
        kind=payload.kind,
        location=(payload.location or "").strip() or None,
        notes=(payload.notes or "").strip() or None,
        source="operator",
        notice=False,
    )
    _refused(result)
    appointment = result.appointment
    booking.forget_offer(contact)

    booked_stage = await pipelines.stage_with_outcome(db, tenant.id, "booked")
    if booked_stage and contact.pipeline_stage != booked_stage:
        previous = contact.pipeline_stage
        contact.pipeline_stage = booked_stage
        await analytics.record_move(
            db, contact, booked_stage, from_stage=previous, source=STAGE_OPERATOR
        )

    told = None
    if payload.tell_customer:
        told = await _tell(
            db, tenant, contact,
            f"Your {booking.describe(appointment)} is booked. See you then!",
        )
    await invites.send_for(db, organization, booked=appointment)
    await db.commit()
    appointment = await _appointment(db, tenant, appointment.id)
    await _changed(db, tenant, contact.id)
    return {"appointment": _row(appointment), "told": told}


@router.post("/appointments/{appointment_id}/move")
async def move_appointment(
    appointment_id: uuid.UUID,
    payload: MoveIn,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Move an appointment to another free time. The old time is freed."""
    tenant.require_role(WRITE_ROLES)
    existing = await _appointment(db, tenant, appointment_id)
    was = booking.describe(existing)
    result = await booking.reschedule(
        db,
        tenant.organization,
        existing,
        _aware_input(payload.starts_at, tenant.organization),
        source="operator",
        notice=False,
    )
    _refused(result)
    contact = existing.contact
    booking.forget_offer(contact)
    told = None
    if payload.tell_customer:
        told = await _tell(
            db, tenant, contact,
            f"Your {was} has been moved. Your booking is now: "
            f"{booking.describe(result.appointment)}.",
        )
    await invites.send_for(db, tenant.organization, booked=result.appointment, cancelled=existing)
    await db.commit()
    moved = await _appointment(db, tenant, result.appointment.id)
    await _changed(db, tenant, contact.id)
    return {"appointment": _row(moved), "told": told}


@router.post("/appointments/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: uuid.UUID,
    payload: CancelIn,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Cancel an appointment. The row stays, marked cancelled, and the time is free."""
    tenant.require_role(WRITE_ROLES)
    existing = await _appointment(db, tenant, appointment_id)
    result = await booking.cancel(db, existing, source="operator")
    _refused(result)
    contact = existing.contact
    told = None
    if contact is not None:
        booking.forget_offer(contact)
        if payload.tell_customer:
            told = await _tell(
                db, tenant, contact,
                f"Your {booking.describe(existing)} has been cancelled. Message us any time "
                "to book another.",
            )
        await invites.send_for(db, tenant.organization, cancelled=existing)
        back = await pipelines.stage_after_cancel(db, tenant.id, contact)
        if back:
            previous = contact.pipeline_stage
            contact.pipeline_stage = back
            await analytics.record_move(db, contact, back, from_stage=previous, source=STAGE_OPERATOR)
    await db.commit()
    cancelled = await _appointment(db, tenant, existing.id)
    await _changed(db, tenant, contact.id if contact is not None else None)
    return {"appointment": _row(cancelled), "told": told}


class BlockIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    note: str | None = Field(default=None, max_length=500)


@router.post("/appointments/block", status_code=201)
async def block_time(
    payload: BlockIn,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Block out time the owner is busy. The agent offers nobody a time inside it.

    Undone with the ordinary cancel endpoint, which frees the time again.
    """
    tenant.require_role(WRITE_ROLES)
    result = await booking.block(
        db,
        tenant.organization,
        _aware_input(payload.starts_at, tenant.organization),
        _aware_input(payload.ends_at, tenant.organization),
        note=payload.note,
    )
    _refused(result)
    await db.commit()
    blocked = await _appointment(db, tenant, result.appointment.id)
    await _changed(db, tenant, None)
    return {"appointment": _row(blocked)}
