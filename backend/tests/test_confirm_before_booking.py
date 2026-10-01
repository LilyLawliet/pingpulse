"""Nothing is booked, moved or cancelled without a yes to what was read back.

The ways this can go wrong are the ways a yes can be counterfeit: a no read
as a yes, a correction read as a yes to the old thing, a yes with nothing in
front of it, a yes that arrives after the slot has gone, a yes to a stale
read-back. Each is here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.models import APPOINTMENT_CONFIRMED, Appointment, CRMContact, Organization
from app.services import booking

from .conftest import confirmed

MIAMI = ZoneInfo("America/New_York")
ADDRESS = "1200 Brickell Ave, Unit 4, Miami FL 33131"


def weekday_ahead(days: int = 3):
    day = datetime.now(timezone.utc).astimezone(MIAMI).date() + timedelta(days=days)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day


def said(day) -> str:
    return f"{day:%B} {day.day}"


@pytest.fixture
async def shop(db_session):
    organization = Organization(name="Constrivo Group", sales_prompt="Remodeling.")
    organization.timezone = "America/New_York"
    organization.agent_config = {
        "business_hours": {
            d: {"open": "09:00", "close": "20:00"}
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
        "service_areas": ["Miami", "Brickell", "331"],
        "appointments": {"min_notice_minutes": 0, "duration_minutes": 60, "default_kind": "onsite"},
    }
    db_session.add(organization)
    await db_session.flush()
    return organization


async def customer(db_session, shop, phone="13055550100"):
    contact = CRMContact(
        organization_id=shop.id,
        phone_number=phone,
        name="Test customer",
        pipeline_stage="NEW_LEAD",
        qualification={},
        contact_metadata={"visit_address": ADDRESS},
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


async def rows(db, contact) -> int:
    return await db.scalar(select(func.count(Appointment.id)).where(Appointment.contact_id == contact.id))


@pytest.mark.asyncio
async def test_the_read_back_says_what_will_be_booked(db_session, shop):
    contact = await customer(db_session, shop)
    day = weekday_ahead()
    turn = await booking.handle_turn(db_session, shop, contact, f"Book {said(day)} at 10am")
    assert turn.performed is None and await rows(db_session, contact) == 0
    assert turn.reply.startswith("To confirm: site visit on ")
    assert "10:00 am" in turn.reply and "1200 Brickell Ave" in turn.reply and "Reply YES" in turn.reply
    # It is the reply - built here, not by a model - and the fallback says the same.
    assert turn.plain_reply(shop) == turn.reply


@pytest.mark.asyncio
@pytest.mark.parametrize("no", ["no", "No.", "nope", "wait", "not that"])
async def test_no_books_nothing(no, db_session, shop):
    contact = await customer(db_session, shop)
    await booking.handle_turn(db_session, shop, contact, f"Book {said(weekday_ahead())} at 10am")
    turn = await booking.handle_turn(db_session, shop, contact, no)
    assert turn.performed is None
    assert turn.refusal.reason == "declined"
    assert await rows(db_session, contact) == 0
    # And a yes after the no has nothing to agree to.
    after = await booking.handle_turn(db_session, shop, contact, "yes")
    assert after.performed is None and await rows(db_session, contact) == 0


@pytest.mark.asyncio
async def test_a_correction_is_read_back_again_not_booked(db_session, shop):
    contact = await customer(db_session, shop)
    day = weekday_ahead()
    await booking.handle_turn(db_session, shop, contact, f"Book {said(day)} at 10am")
    turn = await booking.handle_turn(db_session, shop, contact, f"actually make it {said(day)} at 11am instead")
    assert turn.performed is None and turn.proposed
    assert "11:00 am" in turn.reply
    booked = await booking.handle_turn(db_session, shop, contact, "yes")
    assert booked.performed == "booked"
    assert booking._aware(booked.appointment.starts_at).astimezone(MIAMI).hour == 11


@pytest.mark.asyncio
async def test_yes_with_the_same_time_said_again_is_the_yes(db_session, shop):
    contact = await customer(db_session, shop)
    day = weekday_ahead()
    await booking.handle_turn(db_session, shop, contact, f"Book {said(day)} at 10am")
    turn = await booking.handle_turn(db_session, shop, contact, f"Yes, {said(day)} at 10am is right")
    assert turn.performed == "booked", turn.prompt_block


@pytest.mark.asyncio
async def test_a_yes_with_nothing_read_back_books_nothing(db_session, shop):
    contact = await customer(db_session, shop)
    turn = await booking.handle_turn(db_session, shop, contact, "yes")
    assert turn.performed is None and await rows(db_session, contact) == 0


@pytest.mark.asyncio
async def test_a_stale_read_back_books_nothing(db_session, shop):
    contact = await customer(db_session, shop)
    await booking.handle_turn(db_session, shop, contact, f"Book {said(weekday_ahead())} at 10am")
    held = dict(contact.contact_metadata[booking.PENDING_KEY])
    held["made"] = (datetime.now(timezone.utc) - timedelta(minutes=booking.OFFER_VALID_MINUTES + 1)).isoformat()
    contact.contact_metadata = {**contact.contact_metadata, booking.PENDING_KEY: held}
    turn = await booking.handle_turn(db_session, shop, contact, "yes")
    assert turn.performed is None and await rows(db_session, contact) == 0


@pytest.mark.asyncio
async def test_a_slot_taken_while_they_decided_is_refused_at_the_yes(db_session, shop):
    first = await customer(db_session, shop)
    second = await customer(db_session, shop, phone="13055550199")
    day = weekday_ahead()
    await booking.handle_turn(db_session, shop, first, f"Book {said(day)} at 10am")
    taken = await confirmed(db_session, shop, second, f"Book {said(day)} at 10am")
    assert taken.performed == "booked"

    turn = await booking.handle_turn(db_session, shop, first, "yes")
    assert turn.performed is None
    assert turn.refusal is not None and turn.refusal.reason == "taken"
    assert turn.offered, "they were not offered another time"
    assert await rows(db_session, first) == 0


@pytest.mark.asyncio
async def test_a_cancel_read_back_names_their_own_appointment(db_session, shop):
    contact = await customer(db_session, shop)
    stranger = await customer(db_session, shop, phone="13055550177")
    mine = await confirmed(db_session, shop, contact, f"Book {said(weekday_ahead())} at 10am")
    theirs = await confirmed(db_session, shop, stranger, f"Book {said(weekday_ahead(4))} at 2pm")

    # A held cancellation pointed at somebody else's appointment does nothing.
    contact.contact_metadata = {
        **contact.contact_metadata,
        booking.PENDING_KEY: {
            "action": "cancel",
            "appointment_id": str(theirs.appointment.id),
            "made": datetime.now(timezone.utc).isoformat(),
        },
    }
    turn = await booking.handle_turn(db_session, shop, contact, "yes")
    assert turn.performed is None
    assert theirs.appointment.status == APPOINTMENT_CONFIRMED
    assert mine.appointment.status == APPOINTMENT_CONFIRMED


@pytest.mark.asyncio
async def test_yes_naming_another_time_is_not_a_yes_to_the_first(db_session, shop):
    """ "Yes, but make it 11" is agreement to 11, not to the 10 that was read back."""
    contact = await customer(db_session, shop)
    day = weekday_ahead()
    await booking.handle_turn(db_session, shop, contact, f"Book {said(day)} at 10am")
    turn = await booking.handle_turn(db_session, shop, contact, f"Yes, but make it {said(day)} at 11am")
    assert turn.performed is None, f"booked {turn.appointment and booking.describe(turn.appointment)}"
    assert turn.proposed and "11:00 am" in turn.reply
    assert await rows(db_session, contact) == 0
