"""Booking, cancelling and moving appointments that actually exist.

The behaviour being defended here is narrow and absolute: nothing reports
success it has not verified. A customer was told her appointment was confirmed
for 1am on a date she never chose, and then told it had been cancelled, and
neither sentence referred to anything real.

So most of these tests are about refusals. A system that books correctly and
refuses badly is still the system that produced that conversation.
"""

from datetime import datetime, time, timedelta, timezone

import pytest

from app.models import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    Appointment,
    CRMContact,
    Organization,
)
from app.services import booking

# A Monday, deliberately. Weekday arithmetic that works on a Wednesday and
# not on a Sunday is the kind of thing that only shows up in production.
MONDAY = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

OPEN_ALL_WEEK = {
    day: {"open": "09:00", "close": "17:00"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
}


def shop(**appointments) -> Organization:
    organization = Organization(name="Beluga", sales_prompt="Remodels.")
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": OPEN_ALL_WEEK,
        "appointments": {"min_notice_minutes": 0, **appointments},
    }
    return organization


@pytest.fixture
async def booked_shop(db_session):
    organization = shop()
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(organization_id=organization.id, phone_number="+15550001")
    other = CRMContact(organization_id=organization.id, phone_number="+15550002")
    db_session.add_all([contact, other])
    await db_session.flush()
    return organization, contact, other


def at(days_ahead: int, hour: int, minute: int = 0) -> datetime:
    """A UTC time relative to now, landing on a weekday inside opening hours."""
    base = datetime.now(timezone.utc) + timedelta(days=days_ahead)
    while base.weekday() > 4:  # Saturday or Sunday
        base += timedelta(days=1)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


# --------------------------------------------------------------- refusals
@pytest.mark.asyncio
async def test_a_shop_with_no_hours_takes_no_bookings(db_session):
    """The fault this whole module exists to end.

    Offering times against hours nobody configured is inventing them, and an
    invented hour is how a customer was booked for 1am.
    """
    organization = Organization(name="No Hours", sales_prompt="x")
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(organization_id=organization.id, phone_number="+1")
    db_session.add(contact)
    await db_session.flush()

    assert booking.booking_enabled(organization) is False
    assert await booking.free_slots(db_session, organization) == []

    result = await booking.book(db_session, organization, contact, at(2, 10))
    assert result.ok is False
    assert result.reason == "not_configured"


@pytest.mark.asyncio
async def test_outside_opening_hours_is_refused(booked_shop, db_session):
    """One in the morning is the whole reason this is here."""
    organization, contact, _ = booked_shop

    result = await booking.book(db_session, organization, contact, at(2, 1))

    assert result.ok is False
    assert result.reason == "closed"
    assert "not open" in result.message


@pytest.mark.asyncio
async def test_an_appointment_may_not_run_past_closing(booked_shop, db_session):
    """Starting inside the window is not the same as fitting inside it."""
    organization, contact, _ = booked_shop

    # 16:30 with a 60 minute appointment would finish at 17:30.
    result = await booking.book(db_session, organization, contact, at(2, 16, 30))

    assert result.ok is False
    assert result.reason == "closed"


@pytest.mark.asyncio
async def test_a_closed_day_is_refused(booked_shop, db_session):
    """Saturday is not in the configured hours."""
    organization, contact, _ = booked_shop
    saturday = datetime.now(timezone.utc) + timedelta(days=1)
    while saturday.weekday() != 5:
        saturday += timedelta(days=1)

    result = await booking.book(
        db_session, organization, contact, saturday.replace(hour=11, minute=0)
    )

    assert result.ok is False
    assert result.reason == "closed"


@pytest.mark.asyncio
async def test_too_little_notice_is_refused(db_session):
    """Booking a site visit for nine minutes away is a promise nobody keeps."""
    organization = shop(min_notice_minutes=120)
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(organization_id=organization.id, phone_number="+1")
    db_session.add(contact)
    await db_session.flush()

    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    result = await booking.book(db_session, organization, contact, soon)

    assert result.ok is False
    assert result.reason in ("too_soon", "closed")


@pytest.mark.asyncio
async def test_the_far_future_is_refused(booked_shop, db_session):
    organization, contact, _ = booked_shop
    result = await booking.book(db_session, organization, contact, at(400, 10))

    assert result.ok is False
    assert result.reason == "too_far"


# ------------------------------------------------------------ the happy path
@pytest.mark.asyncio
async def test_a_booking_produces_a_row_that_can_be_read_back(booked_shop, db_session):
    organization, contact, _ = booked_shop
    when = at(3, 10)

    result = await booking.book(
        db_session, organization, contact, when, kind="onsite", location="12 Mill Lane"
    )

    assert result.ok is True
    row = result.appointment
    assert row.status == APPOINTMENT_CONFIRMED
    assert row.starts_at == when
    assert row.ends_at == when + timedelta(minutes=60)
    assert row.location == "12 Mill Lane"

    # And the record is what answers "am I booked?".
    found = await booking.upcoming_for(db_session, contact.id)
    assert found is not None and found.id == row.id


@pytest.mark.asyncio
async def test_the_confirmation_is_rendered_from_the_row(booked_shop, db_session):
    """Not assembled from the conversation, which is where 1am came from."""
    organization, contact, _ = booked_shop
    result = await booking.book(
        db_session,
        organization,
        contact,
        at(3, 14),
        kind="onsite",
        location="12 Mill Lane",
    )

    sentence = booking.describe(result.appointment)

    assert "site visit" in sentence
    assert "2:00 pm" in sentence
    assert "12 Mill Lane" in sentence


def test_the_confirmation_says_which_kind_of_appointment():
    """"Confirmed for Tuesday at 2pm" does not say whether somebody is
    ringing you or turning up at your house."""
    phone = Appointment(
        starts_at=datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 6, 14, 30, tzinfo=timezone.utc),
        timezone_name="UTC",
        kind="phone",
    )
    onsite = Appointment(
        starts_at=datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
        timezone_name="UTC",
        kind="onsite",
    )

    assert "phone consultation" in booking.describe(phone)
    assert "site visit" in booking.describe(onsite)


def test_the_confirmation_speaks_the_zone_it_was_agreed_in():
    """Stored in UTC, spoken locally. 18:00 UTC is 2pm in New York, and the
    customer agreed to the local one."""
    appointment = Appointment(
        starts_at=datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc),
        timezone_name="America/New_York",
        kind="onsite",
    )

    assert "2:00 pm" in booking.describe(appointment)


# ------------------------------------------------------------- the same slot
@pytest.mark.asyncio
async def test_two_customers_cannot_hold_one_slot(booked_shop, db_session):
    """The scenario named in the brief."""
    organization, first, second = booked_shop
    when = at(4, 11)

    held = await booking.book(db_session, organization, first, when)
    assert held.ok is True

    clash = await booking.book(db_session, organization, second, when)

    assert clash.ok is False
    assert clash.reason == "taken"
    assert "already been booked" in clash.message


@pytest.mark.asyncio
async def test_an_overlap_counts_as_taken(booked_shop, db_session):
    """Not just an identical start. Half an hour into an hour is a clash."""
    organization, first, second = booked_shop
    await booking.book(db_session, organization, first, at(4, 11))

    clash = await booking.book(db_session, organization, second, at(4, 11, 30))

    assert clash.ok is False
    assert clash.reason == "taken"


@pytest.mark.asyncio
async def test_back_to_back_is_not_a_clash(booked_shop, db_session):
    organization, first, second = booked_shop
    await booking.book(db_session, organization, first, at(4, 11))

    after = await booking.book(db_session, organization, second, at(4, 12))

    assert after.ok is True


@pytest.mark.asyncio
async def test_a_travel_buffer_keeps_the_next_slot_clear(db_session):
    """A trade that drives to the customer cannot be in two places at once."""
    organization = shop(buffer_minutes=60, min_notice_minutes=0)
    db_session.add(organization)
    await db_session.flush()
    first = CRMContact(organization_id=organization.id, phone_number="+1")
    second = CRMContact(organization_id=organization.id, phone_number="+2")
    db_session.add_all([first, second])
    await db_session.flush()

    await booking.book(db_session, organization, first, at(4, 10))

    # 11:00 is back to back, which the hour of travel time forbids.
    too_close = await booking.book(db_session, organization, second, at(4, 11))
    assert too_close.ok is False and too_close.reason == "taken"

    # 12:00 leaves the hour.
    far_enough = await booking.book(db_session, organization, second, at(4, 12))
    assert far_enough.ok is True


# ------------------------------------------------------------- cancellation
@pytest.mark.asyncio
async def test_cancelling_nothing_says_so(db_session):
    """The exact sentence the agent got wrong: "I have cancelled it"."""
    result = await booking.cancel(db_session, None)

    assert result.ok is False
    assert result.reason == "not_found"
    assert "no appointment" in result.message


@pytest.mark.asyncio
async def test_cancelling_updates_rather_than_deletes(booked_shop, db_session):
    organization, contact, _ = booked_shop
    held = await booking.book(db_session, organization, contact, at(5, 10))

    result = await booking.cancel(db_session, held.appointment)

    assert result.ok is True
    assert held.appointment.status == APPOINTMENT_CANCELLED
    assert held.appointment.cancelled_at is not None
    # Still readable afterwards, because "did you cancel that?" is a question.
    assert await db_session.get(Appointment, held.appointment.id) is not None
    assert await booking.upcoming_for(db_session, contact.id) is None


@pytest.mark.asyncio
async def test_cancelling_twice_is_refused_not_repeated(booked_shop, db_session):
    organization, contact, _ = booked_shop
    held = await booking.book(db_session, organization, contact, at(5, 10))
    await booking.cancel(db_session, held.appointment)

    again = await booking.cancel(db_session, held.appointment)

    assert again.ok is False
    assert again.reason == "already_cancelled"


@pytest.mark.asyncio
async def test_a_cancelled_slot_can_be_sold_again(booked_shop, db_session):
    organization, first, second = booked_shop
    when = at(5, 13)
    held = await booking.book(db_session, organization, first, when)
    await booking.cancel(db_session, held.appointment)

    resold = await booking.book(db_session, organization, second, when)

    assert resold.ok is True


# ------------------------------------------------------------ rescheduling
@pytest.mark.asyncio
async def test_rescheduling_leaves_exactly_one_live_appointment(booked_shop, db_session):
    """Moving an appointment must not quietly leave the customer with two."""
    from sqlalchemy import select

    organization, contact, _ = booked_shop
    held = await booking.book(db_session, organization, contact, at(6, 10))

    moved = await booking.reschedule(
        db_session, organization, held.appointment, at(6, 15)
    )

    assert moved.ok is True
    live = (
        await db_session.execute(
            select(Appointment).where(
                Appointment.contact_id == contact.id,
                Appointment.status == APPOINTMENT_CONFIRMED,
            )
        )
    ).scalars().all()
    assert len(live) == 1
    assert live[0].starts_at == at(6, 15)
    assert live[0].replaces_id == held.appointment.id


@pytest.mark.asyncio
async def test_moving_an_appointment_an_hour_does_not_clash_with_itself(
    booked_shop, db_session
):
    """The overlap it has to ignore is its own."""
    organization, contact, _ = booked_shop
    held = await booking.book(db_session, organization, contact, at(6, 10))

    moved = await booking.reschedule(
        db_session, organization, held.appointment, at(6, 10, 30)
    )

    assert moved.ok is True


@pytest.mark.asyncio
async def test_rescheduling_onto_a_taken_slot_keeps_the_original(booked_shop, db_session):
    """A failed move must not cost the customer the appointment they had."""
    organization, first, second = booked_shop
    mine = await booking.book(db_session, organization, first, at(7, 10))
    theirs = await booking.book(db_session, organization, second, at(7, 14))

    attempt = await booking.reschedule(
        db_session, organization, mine.appointment, at(7, 14)
    )

    assert attempt.ok is False
    assert attempt.reason == "taken"
    assert mine.appointment.status == APPOINTMENT_CONFIRMED
    assert await booking.upcoming_for(db_session, first.id) is not None


@pytest.mark.asyncio
async def test_rescheduling_nothing_says_so(db_session):
    organization = shop()
    result = await booking.reschedule(db_session, organization, None, at(3, 10))

    assert result.ok is False
    assert result.reason == "not_found"


# ------------------------------------------------------------- what to offer
@pytest.mark.asyncio
async def test_offered_slots_are_inside_opening_hours(booked_shop, db_session):
    organization, _, _ = booked_shop

    slots = await booking.free_slots(db_session, organization, days=5)

    assert slots, "no times were offered at all"
    for slot in slots:
        assert 9 <= slot.hour < 17, f"{slot} is outside 09:00-17:00"
        assert slot.weekday() <= 4, f"{slot} is a weekend"


@pytest.mark.asyncio
async def test_offered_slots_skip_what_is_already_booked(booked_shop, db_session):
    organization, contact, _ = booked_shop
    taken = at(1, 10)
    await booking.book(db_session, organization, contact, taken)

    slots = await booking.free_slots(db_session, organization, days=3, limit=40)

    assert taken not in slots


@pytest.mark.asyncio
async def test_a_fully_booked_shop_offers_nothing_rather_than_inventing(db_session):
    """The honest answer to "when are you free?" is sometimes "nothing"."""
    organization = Organization(name="Busy", sales_prompt="x")
    organization.timezone = "UTC"
    # Open one hour a week, and that hour is taken.
    organization.agent_config = {
        "business_hours": {"monday": {"open": "09:00", "close": "10:00"}},
        "appointments": {"min_notice_minutes": 0},
    }
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(organization_id=organization.id, phone_number="+1")
    db_session.add(contact)
    await db_session.flush()

    monday = datetime.now(timezone.utc) + timedelta(days=1)
    while monday.weekday() != 0:
        monday += timedelta(days=1)
    await booking.book(
        db_session, organization, contact, monday.replace(hour=9, minute=0, second=0, microsecond=0)
    )

    slots = await booking.free_slots(db_session, organization, days=6)

    assert slots == []


# ------------------------------------------- a clash must cost only the clash
@pytest.mark.asyncio
async def test_a_clash_does_not_discard_the_rest_of_the_request(
    booked_shop, db_session
):
    """A slot clash undoes the appointment and nothing else.

    Booking happens partway through handling an inbound message, and the
    customer's own message is already pending on the same session. A plain
    rollback on the clash would throw that away too - so the message asking
    for the booking would vanish because the booking failed, which is a far
    stranger bug than the clash it came from.
    """
    from app.models import Message

    organization, first, second = booked_shop
    when = at(8, 11)
    await booking.book(db_session, organization, first, when)

    # Something else in flight, exactly as the webhook would have.
    inbound = Message(
        organization_id=organization.id,
        contact_id=second.id,
        sender="user",
        content="can I have 11am?",
    )
    db_session.add(inbound)
    await db_session.flush()

    clash = await booking.book(db_session, organization, second, when)
    assert clash.ok is False and clash.reason == "taken"

    # The message survived the failed booking.
    assert await db_session.get(Message, inbound.id) is not None


@pytest.mark.asyncio
async def test_a_failed_move_leaves_the_original_confirmed(booked_shop, db_session):
    """Checked through the record rather than the returned object, because
    the half-done reschedule is the one that costs somebody their slot."""
    from sqlalchemy import select

    organization, first, second = booked_shop
    mine = await booking.book(db_session, organization, first, at(9, 10))
    await booking.book(db_session, organization, second, at(9, 14))

    attempt = await booking.reschedule(
        db_session, organization, mine.appointment, at(9, 14)
    )
    assert attempt.ok is False

    live = (
        await db_session.execute(
            select(Appointment).where(
                Appointment.contact_id == first.id,
                Appointment.status == APPOINTMENT_CONFIRMED,
            )
        )
    ).scalars().all()
    assert len(live) == 1, "the customer lost their appointment to a failed move"
    assert live[0].starts_at == at(9, 10)


# -------------------------------------------------- offering and choosing
from zoneinfo import ZoneInfo  # noqa: E402

UTC = ZoneInfo("UTC")
OFFERED = [
    datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc),   # Tuesday 2pm
    datetime(2026, 10, 6, 15, 30, tzinfo=timezone.utc),  # Tuesday 3:30pm
    datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc),   # Wednesday 10am
]


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("the first one", 0),
        ("number 2", 1),
        ("2pm please", 0),
        ("tuesday at 3:30pm", 1),
        ("wednesday", 2),
        ("10:00 on wednesday", 2),
        ("the last one", 2),
        ("15:30", 1),
    ],
)
def test_a_customer_choice_maps_to_the_time_they_were_offered(reply, expected):
    assert booking.chosen_slot(reply, OFFERED, UTC) == OFFERED[expected]


@pytest.mark.parametrize(
    "reply",
    [
        "whenever suits you",
        "yes please",
        "sounds good",
        "sometime next week",
        "tuesday or wednesday",
        "morning is better",
        "",
    ],
)
def test_an_unclear_answer_books_nothing(reply):
    """None whenever there is doubt.

    Asking again costs one message. Guessing costs somebody a morning, and
    guessing is how a customer ended up with 1am.
    """
    assert booking.chosen_slot(reply, OFFERED, UTC) is None


def test_a_time_that_matches_two_offered_days_is_not_a_choice():
    """"2pm" when both Tuesday and Wednesday at 2pm were offered."""
    two_days = [
        datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc),
    ]

    assert booking.chosen_slot("2pm", two_days, UTC) is None
    # Naming the day resolves it.
    assert booking.chosen_slot("tuesday 2pm", two_days, UTC) == two_days[0]


def test_nothing_offered_means_nothing_chosen():
    assert booking.chosen_slot("the first one", [], UTC) is None


def test_an_offer_is_forgotten_once_it_is_stale(db_session):
    """"Yes, the first one" three days later means a different Tuesday."""
    contact = CRMContact(phone_number="+1")
    booking.remember_offer(contact, OFFERED)
    assert len(booking.remembered_offer(contact)) == 3

    stale = dict(contact.contact_metadata)
    stale[booking.OFFER_KEY]["at"] = (
        datetime.now(timezone.utc) - timedelta(hours=6)
    ).isoformat()
    contact.contact_metadata = stale

    assert booking.remembered_offer(contact) == []


# ------------------------------------------------------- what the agent is told
def test_a_shop_without_booking_is_told_not_to_offer_it(db_session):
    organization = Organization(name="No Hours", sales_prompt="x")
    block = booking.as_prompt_block(organization, None, [])

    assert "not set up appointment booking" in block
    assert "Do NOT offer to book" in block


def test_an_empty_diary_says_so_rather_than_encouraging(db_session):
    """There is deliberately no branch that sounds hopeful with no times
    behind it, because that is the gap the model filled in by itself."""
    organization = shop()
    block = booking.as_prompt_block(organization, None, [])

    assert "nothing free" in block
    assert "do not say anything is booked" in block.lower()


def test_offered_times_reach_the_prompt_exactly(db_session):
    organization = shop()
    block = booking.as_prompt_block(organization, None, OFFERED)

    # No leading zero on the day: "Tuesday 06 October" reads like a receipt.
    assert "Tuesday 6 October at 2:00 pm" in block
    assert "Wednesday 7 October at 10:00 am" in block
    assert "not say anything is confirmed" in block.lower()


def test_a_booked_customer_is_described_from_the_record(db_session):
    organization = shop()
    appointment = Appointment(
        starts_at=datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
        timezone_name="UTC",
        kind="onsite",
        location="12 Mill Lane",
    )

    block = booking.as_prompt_block(organization, None, [], appointment=appointment)

    assert "site visit" in block
    assert "2:00 pm" in block
    assert "do not state that it has been changed or cancelled" in block


# ------------------------------------------------- claims against the record
def test_the_two_sentences_from_the_incident_are_both_caught():
    """Verbatim from the conversation that started this."""
    confirmed = (
        "Your appointment is confirmed for September 19, 2026 at 1:00 AM EST "
        "at your Miami property."
    )
    cancelled = (
        "I am sorry for the confusion; I have cancelled the September 19 "
        "appointment. I am a live team member here."
    )

    assert booking.unverified_claims(confirmed, appointment=None)
    assert booking.unverified_claims(cancelled, cancelled=False)


def test_a_claim_backed_by_the_record_is_allowed():
    appointment = Appointment(
        starts_at=datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
        timezone_name="UTC",
        kind="onsite",
    )

    assert booking.unverified_claims(
        "Your appointment is confirmed for Tuesday at 2pm.", appointment=appointment
    ) == []
    assert booking.unverified_claims(
        "I have cancelled that for you.", cancelled=True
    ) == []


def test_ordinary_sentences_are_not_mistaken_for_claims():
    """A guard that fires on normal conversation gets switched off."""
    for innocent in (
        "We are open Monday to Friday, nine to five.",
        "I can offer Tuesday at 2pm or Wednesday at 10am - which suits you?",
        "Bathroom remodels usually take two to three weeks.",
        "Would you like me to look at what is free this week?",
    ):
        assert booking.unverified_claims(innocent) == [], innocent
