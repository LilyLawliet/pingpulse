"""The release blockers from the September 30 readiness test, one by one.

A tester drove the Test agent through forty turns as a Miami bathroom
remodeller's customer and found the reply and the booking underneath it
disagreeing: "I'll hold off on booking" over a booking, a refusal of a Seattle
job over a Seattle visit, "cancel, I do not want to reschedule" turned into a
move. Each case here is that turn, and each asserts on the record - what was
written - rather than on what the agent would have said about it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.api.webhook import evaluate_stage
from app.models import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    Appointment,
    CRMContact,
    Organization,
)
from app.services import booking, llm_service, qualification

from .conftest import _session_for, confirmed

MIAMI = ZoneInfo("America/New_York")
WEEKDAYS = {
    day: {"open": "09:00", "close": "20:00"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
}
CONFIG = {
    "business_hours": WEEKDAYS,
    "service_areas": ["Miami", "Miami Beach", "Coral Gables", "Brickell", "331"],
    "appointments": {"min_notice_minutes": 0, "duration_minutes": 60, "default_kind": "onsite"},
}
ADDRESS = "1200 Brickell Ave, Unit 4, Miami FL 33131"


def weekday_ahead(days: int = 3) -> date:
    """A weekday at least this many days out, in Miami."""
    day = datetime.now(timezone.utc).astimezone(MIAMI).date() + timedelta(days=days)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day


def said(day: date) -> str:
    return f"{day:%B} {day.day}"


@pytest.fixture
async def beluga(db_session):
    organization = Organization(name="Beluga Group", sales_prompt="Bathroom remodels.")
    organization.timezone = "America/New_York"
    organization.agent_config = dict(CONFIG)
    db_session.add(organization)
    await db_session.flush()

    contact = CRMContact(
        organization_id=organization.id,
        phone_number="13055550100",
        name="Test customer",
        pipeline_stage="NEW_LEAD",
        qualification={},
        contact_metadata={},
    )
    db_session.add(contact)
    await db_session.flush()
    return organization, contact


async def rows(db, contact, status=None) -> int:
    query = select(func.count(Appointment.id)).where(Appointment.contact_id == contact.id)
    if status:
        query = query.where(Appointment.status == status)
    return await db.scalar(query)


# --------------------------------------------------------------- T32
@pytest.mark.asyncio
async def test_do_not_book_it_yet_books_nothing(beluga, db_session):
    organization, contact = beluga
    contact.contact_metadata = {"visit_address": ADDRESS}
    day = weekday_ahead()

    turn = await booking.handle_turn(
        db_session, organization, contact,
        f"I am considering a bathroom estimate on {said(day)} at 10 AM but DO NOT book it "
        "yet. I need to ask my spouse first.",
    )

    assert turn.performed is None
    assert await rows(db_session, contact) == 0
    assert "NOT to book" in turn.prompt_block

    # And when they come back and say so, that time is the one booked.
    later = await confirmed(db_session, organization, contact, "ok, go ahead and book it")
    assert later.performed == "booked", later
    assert booking._aware(later.appointment.starts_at).astimezone(MIAMI).hour == 10


@pytest.mark.parametrize(
    "message",
    [
        "Don't book anything, just tell me if Friday at 10am is free",
        "Friday at 10am might work but I'm not ready to book yet",
        "Hold off on booking Friday at 10am, I have to check with my husband",
    ],
)
def test_holding_off_is_read(message):
    assert booking.holding_off(message)


@pytest.mark.parametrize("message", ["Book Friday at 10am please", "the first one", "yes"])
def test_a_plain_request_is_not_holding_off(message):
    assert not booking.holding_off(message)


# --------------------------------------------------------------- T40-41
@pytest.mark.asyncio
async def test_cancel_and_do_not_reschedule_cancels(beluga, db_session):
    organization, contact = beluga
    day = weekday_ahead()
    when = datetime(day.year, day.month, day.day, 11, tzinfo=MIAMI).astimezone(timezone.utc)
    held = await booking.book(db_session, organization, contact, when, location=ADDRESS)
    assert held.ok

    turn = await confirmed(
        db_session, organization, contact,
        f"Cancel my {day:%A}, {said(day)}, {day.year}, 11 AM appointment. "
        "I do not want to reschedule.",
    )

    assert turn.performed == "cancelled", turn
    assert await rows(db_session, contact, APPOINTMENT_CANCELLED) == 1
    assert await rows(db_session, contact, APPOINTMENT_CONFIRMED) == 0
    assert await rows(db_session, contact) == 1, "a new appointment was written"


def test_a_refused_reschedule_is_not_a_move():
    assert not booking.wants_move("I do not want to reschedule")
    assert not booking.wants_move("no need to move it, just cancel")
    assert booking.wants_move("I want to reschedule")
    assert booking.wants_cancel("Cancel my appointment. I do not want to reschedule.")


@pytest.mark.asyncio
async def test_a_move_to_its_own_time_changes_nothing(beluga, db_session):
    organization, contact = beluga
    day = weekday_ahead()
    when = datetime(day.year, day.month, day.day, 11, tzinfo=MIAMI).astimezone(timezone.utc)
    held = await booking.book(db_session, organization, contact, when, location=ADDRESS)
    result = await booking.reschedule(db_session, organization, held.appointment, when)
    assert result.reason == "unchanged"


# --------------------------------------------------------------- T24
def test_a_pacific_time_is_read_in_pacific():
    day = weekday_ahead()
    named = booking.named_time(f"{said(day)} at 7 p.m. Pacific", MIAMI)
    assert named.zone == ZoneInfo("America/Los_Angeles")

    organization = Organization(name="x", agent_config=dict(CONFIG))
    organization.timezone = "America/New_York"
    moment = booking.named_moment(organization, named)
    assert moment.astimezone(MIAMI).hour == 22


@pytest.mark.parametrize("text", ["at 3pm PT", "3pm pacific time", "3:00 PST", "3 pm (PDT)"])
def test_pacific_spellings(text):
    assert booking.named_time(f"Friday {text}", MIAMI).zone == ZoneInfo("America/Los_Angeles")


def test_the_shops_own_zone_or_none_changes_nothing():
    assert booking.named_time("Friday at 3pm Eastern", MIAMI).zone is None
    assert booking.named_time("Friday at 3pm", MIAMI).zone is None


@pytest.mark.asyncio
async def test_seven_pacific_is_after_closing_in_miami(beluga, db_session):
    organization, contact = beluga
    contact.contact_metadata = {"visit_address": ADDRESS}
    day = weekday_ahead()

    turn = await booking.handle_turn(
        db_session, organization, contact,
        f"Please book a visit for {said(day)} at 7 p.m. Pacific",
    )

    assert turn.performed is None
    assert await rows(db_session, contact) == 0
    assert turn.refusal is not None and turn.refusal.reason == "closed"
    assert "10:00 pm" in turn.prompt_block


@pytest.mark.asyncio
async def test_two_pacific_books_five_eastern(beluga, db_session):
    organization, contact = beluga
    contact.contact_metadata = {"visit_address": ADDRESS}
    day = weekday_ahead()

    proposal = await booking.handle_turn(
        db_session, organization, contact, f"Book me in for {said(day)} at 2pm Pacific"
    )
    # Read back in both zones, so a misread zone is seen before anything is booked.
    assert "5:00 pm" in proposal.reply and "2:00 pm" in proposal.reply, proposal.reply
    turn = await booking.handle_turn(db_session, organization, contact, "yes")

    assert turn.performed == "booked", turn
    assert booking._aware(turn.appointment.starts_at).astimezone(MIAMI).hour == 17
    assert "America/Los_Angeles" in turn.prompt_block


# --------------------------------------------------------------- T03, T33
@pytest.mark.asyncio
async def test_no_address_no_visit(beluga, db_session):
    organization, contact = beluga
    day = weekday_ahead()

    turn = await booking.handle_turn(
        db_session, organization, contact,
        f"Book {said(day)} at 10 a.m. I'd rather not give my address.",
    )

    assert turn.performed is None
    assert turn.refusal.reason == "needs_address"
    assert await rows(db_session, contact) == 0

    # The address, given next, books the time they chose.
    turn = await confirmed(db_session, organization, contact, ADDRESS)
    assert turn.performed == "booked", turn
    assert "1200 Brickell Ave" in turn.appointment.location


@pytest.mark.asyncio
async def test_invalid_contact_details_hold_the_visit(beluga, db_session):
    organization, contact = beluga
    day = weekday_ahead()

    turn = await booking.handle_turn(
        db_session, organization, contact,
        f"Book {said(day)} at 10 a.m. at {ADDRESS}. My phone is 123 and my email is "
        "'not-an-email'.",
    )

    assert turn.performed is None
    assert turn.refusal.reason == "contact_invalid"
    assert await rows(db_session, contact) == 0

    turn = await confirmed(
        db_session, organization, contact, "sorry - phone is 305 555 0100, email is dana@example.com"
    )
    assert turn.performed == "booked", turn


def test_the_agent_cannot_book_a_visit_without_somewhere_to_go(beluga):
    organization, _ = beluga
    assert booking.visit_refusal(organization, "onsite", None).reason == "needs_address"
    assert booking.visit_refusal(organization, "phone", None) is None
    assert booking.visit_refusal(organization, "onsite", ADDRESS) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("It's at 1200 Brickell Ave, Unit 4, Miami", "1200 Brickell Ave, Unit 4, Miami"),
        ("4500 Sunny Isles Beach Blvd.", "4500 Sunny Isles Beach Blvd"),
        ("I need 2 bathrooms done at my place", None),
        ("Can you come at 10 am on Main St?", None),
    ],
)
def test_reading_an_address(text, expected):
    found = booking.address_in(text)
    assert (found or None) == expected or (expected and found and found.startswith(expected))


def test_contact_values():
    assert booking.contact_problems("my email is 'not-an-email' and phone: 123")
    assert not booking.contact_problems("my email is dana@example.com, phone is +1 305 555 0100")
    assert not booking.contact_problems("my email is the same as before")


# --------------------------------------------------------------- Seattle
@pytest.mark.asyncio
async def test_seattle_gets_no_times_and_no_visit(beluga, db_session):
    organization, contact = beluga
    day = weekday_ahead()

    first = await booking.handle_turn(
        db_session, organization, contact,
        "I need a roof replacement in Seattle. When can you come out?",
    )
    assert first.offered == [], "times were offered to a job outside the area"
    assert first.refusal.reason == "outside_area"

    second = await booking.handle_turn(
        db_session, organization, contact, f"{said(day)} at 9am works, book it"
    )
    assert second.performed is None
    assert await rows(db_session, contact) == 0


@pytest.mark.asyncio
async def test_a_seattle_address_is_refused_at_the_write(beluga, db_session):
    organization, contact = beluga
    day = weekday_ahead()
    when = datetime(day.year, day.month, day.day, 10, tzinfo=MIAMI).astimezone(timezone.utc)

    result = await booking.book(
        db_session, organization, contact, when, location="400 Pine St, Seattle WA 98101"
    )
    assert result.reason == "outside_area"
    # A person at the shop decides for themselves.
    by_hand = await booking.book(
        db_session, organization, contact, when, location="400 Pine St, Seattle WA 98101",
        source="operator",
    )
    assert by_hand.ok


def test_areas_read_against_the_shops_own_list(beluga):
    organization, _ = beluga
    assert booking.in_area(organization, ADDRESS) is True
    assert booking.in_area(organization, "12 Main St, Little Havana 33125") is True  # ZIP prefix 331
    assert booking.in_area(organization, "Seattle") is False
    nowhere = Organization(name="x", agent_config={"business_hours": WEEKDAYS})
    assert booking.in_area(nowhere, "Seattle") is None, "no list, no refusal on a guess"


# --------------------------------------------------------------- T22, T28-29
def test_a_date_just_gone_is_not_next_year():
    now = datetime(2026, 9, 30, 14, tzinfo=MIAMI)
    assert booking.named_time("September 29 at 10am", MIAMI, now=now).days == (date(2026, 9, 29),)
    assert booking.named_time("yesterday at 10am", MIAMI, now=now).days == (date(2026, 9, 29),)
    # Early next year is still next year.
    assert booking.named_time("January 5 at 10am", MIAMI, now=now).days == (date(2027, 1, 5),)


@pytest.mark.asyncio
async def test_yesterday_is_refused_and_not_offered(beluga, db_session):
    organization, contact = beluga
    contact.contact_metadata = {"visit_address": ADDRESS}

    turn = await booking.handle_turn(
        db_session, organization, contact, "Can I book yesterday at 10am?"
    )

    assert turn.performed is None
    assert turn.refusal.reason == "past"
    now = datetime.now(timezone.utc)
    assert turn.offered and all(slot > now for slot in turn.offered)
    assert await rows(db_session, contact) == 0


def test_a_stale_offer_drops_times_that_have_passed():
    contact = CRMContact(contact_metadata={})
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    booking.remember_offer(contact, [past, future])
    assert booking.remembered_offer(contact) == [future]


# --------------------------------------------------------------- T30
@pytest.mark.parametrize(
    "text",
    [
        "The September 29, 9 a.m. appointment has been removed.",
        "I've removed it from the calendar.",
        "Your booking was deleted.",
    ],
)
def test_removed_is_a_cancellation_claim(text):
    assert booking.unverified_claims(text, cancelled=False)


# --------------------------------------------------------------- the board
def test_a_refused_answer_is_not_an_answer():
    assert not qualification.usable("Refused to give address")
    assert not qualification.usable("customer prefers not to say")
    assert not qualification.usable("N/A")
    assert qualification.usable("Miami")


def test_no_address_is_not_qualified(beluga):
    organization, contact = beluga
    organization.agent_config = {
        **CONFIG,
        "qualification_slots": [{"name": "job_type"}, {"name": "location"}],
    }
    answered = {"job_type": "bathroom remodel", "location": "Miami"}

    contact.contact_metadata = {}
    assert evaluate_stage("NEW_LEAD", "hi", organization, answered, contact) == "NEW_LEAD"

    contact.contact_metadata = {"visit_address": "400 Pine St, Seattle WA 98101"}
    assert evaluate_stage("NEW_LEAD", "hi", organization, answered, contact) == "NEW_LEAD"

    contact.contact_metadata = {"visit_address": ADDRESS, "unusable_details": ["bad email"]}
    assert evaluate_stage("NEW_LEAD", "hi", organization, answered, contact) == "NEW_LEAD"

    contact.contact_metadata = {"visit_address": ADDRESS}
    assert evaluate_stage("NEW_LEAD", "hi", organization, answered, contact) == "QUALIFIED"

    refused = {"job_type": "bathroom remodel", "location": "won't say"}
    assert evaluate_stage("NEW_LEAD", "hi", organization, refused, contact) == "NEW_LEAD"


def test_a_shop_that_does_not_visit_is_not_held_to_an_address():
    shop = Organization(
        name="Yaha",
        agent_config={
            "business_hours": WEEKDAYS,
            "qualification_slots": [{"name": "job_type"}],
        },
    )
    contact = CRMContact(contact_metadata={})
    assert evaluate_stage("NEW_LEAD", "hi", shop, {"job_type": "shoes"}, contact) == "QUALIFIED"


# --------------------------------------------------------------- the sandbox
@pytest.mark.asyncio
async def test_the_test_agent_takes_the_same_path(org_a, monkeypatch):
    session = _session_for(org_a._client)
    organization = await session.get(Organization, uuid.UUID(org_a.organization_id))
    organization.timezone = "America/New_York"
    organization.agent_config = dict(CONFIG)
    await session.flush()

    async def groq(prompt):
        return "I'll hold off on booking."

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    day = weekday_ahead()
    body = (
        await org_a.post(
            "/api/v1/agent/simulate",
            json={
                "message": f"I am considering a bathroom estimate on {said(day)} at 10 AM at "
                f"{ADDRESS} but DO NOT book it yet. I need to ask my spouse first."
            },
        )
    ).json()
    assert body["booking"]["performed"] is None, body
    assert body["booking"]["note"] is None or "would have" not in body["booking"]["note"]


# --------------------------------------------------------------- temperature
@pytest.mark.asyncio
async def test_readers_run_at_zero_and_replies_do_not(monkeypatch):
    """The same message must be read the same way; replies keep their voice."""
    from app.services import analyzer, summarise

    seen = []

    async def groq(prompt):
        seen.append(llm_service._temperature.get())
        return "{}"

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    monkeypatch.setattr(analyzer, "_call_groq", groq)

    shop = Organization(name="x", agent_config={})
    await qualification.extract(shop, [], "a bathroom in Miami")
    await analyzer.analyse([], "can I book Friday?")
    await summarise.write([], "hello")
    await llm_service.extract_profile([], "size 42")
    assert seen == [0.0, 0.0, 0.0, 0.0], seen

    assert llm_service._temperature.get() == llm_service.REPLY_TEMPERATURE == 0.7


# --------------------------------------------------------------- found on replay
def test_a_number_in_an_address_does_not_pick_a_time():
    """ "Unit 4" booked the fourth time offered."""
    offered = [datetime(2026, 10, 1, 13 + i, tzinfo=timezone.utc) for i in range(6)]
    assert booking.chosen_slot("The condo is 1200 Brickell Ave, Unit 4, Miami", offered, MIAMI) is None
    assert booking.chosen_slot("I have 2 bathrooms to redo", offered, MIAMI) is None
    assert booking.chosen_slot("2", offered, MIAMI) == offered[1]
    assert booking.chosen_slot("option 3 please", offered, MIAMI) == offered[2]
    assert booking.chosen_slot("#4", offered, MIAMI) == offered[3]


def test_where_they_are_and_where_the_job_is(beluga):
    organization, _ = beluga
    place = booking.place_in("I am in California but the property is in Miami.")
    assert booking.in_area(organization, place) is True
    assert booking.in_area(organization, booking.place_in("my house in Seattle, Washington.")) is False


# --------------------------------------------------------------- October 1, Test agent
@pytest.mark.parametrize(
    "reply",
    [
        "Bathroom remodeling usually takes 2-6 weeks, including permits. We'd like to make it easy.",
        "Our design team can help with a chair-height vanity, regarding your layout.",
    ],
)
def test_english_is_not_roman_urdu(reply):
    assert not llm_service.is_roman_urdu(reply)


@pytest.mark.parametrize("reply", ["Ji bilkul, ye kitne ka hai bhai?", "Aap ko kya chahiye?"])
def test_roman_urdu_is_still_read(reply):
    assert llm_service.is_roman_urdu(reply)


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("You're booked for Monday 5 October at 9:30 AM.", True),
        ("All set - Monday at 9.30am.", True),
        ("Which two Monday slots work best for you?", False),
        ("You're booked in.", False),
    ],
)
def test_a_booking_reply_states_the_time(reply, expected):
    assert llm_service.confirms(reply, "site visit on Monday 5 October at 9:30 am") is expected


def test_a_time_on_the_hour_may_be_said_short():
    assert llm_service.confirms("See you Monday at 10 AM", "site visit on Monday 5 October at 10:00 am")


# --------------------------------------------------------------- October 6
@pytest.mark.parametrize(
    "message, reading, kept",
    [
        ("I am considering an estimate on October 2 at 10 AM but DO NOT book it yet.",
         "I am considering an estimate on October 2 at 10 AM.", False),
        ("I am considering an estimate on October 2 at 10 AM but DO NOT book it yet.",
         "Do not book the October 2 10 AM estimate yet.", True),
        ("Cancel my Monday appointment. I do not want to reschedule.", "Cancel my Monday appointment.", False),
        ("hw mch yrly 4 it", "How much is the Growth plan per year?", True),
    ],
)
def test_a_reading_must_keep_what_they_ruled_out(message, reading, kept):
    from app.services.analyzer import keeps_what_they_ruled_out

    assert keeps_what_they_ruled_out(message, reading) is kept


def test_a_reading_that_lost_do_not_is_discarded():
    """The reply is told to answer the reading; one without "DO NOT" is not given to it."""
    from app.services.analyzer import _coerce

    message = "I am considering an estimate on October 2 at 10 AM but DO NOT book it yet."
    analysis = _coerce({"meaning": "Book an estimate on October 2 at 10 AM."}, message, "NEW")
    assert analysis["meaning"] is None


@pytest.mark.parametrize(
    "text, problems",
    [
        ("phone 123, email not-an-email", 2),
        ("my phone is 123 and my email is not-an-email", 2),
        ("reach me at jamie@example", 1),
        ("phone: 0000000", 1),
        ("my number is +1 305 555 0100 and email jamie@example.com", 0),
        ("email me tomorrow", 0),
        ("follow @constrivo", 0),
    ],
)
def test_contact_details_read_without_is(text, problems):
    assert len(booking.contact_problems(text)) == problems


def test_a_corrected_phone_leaves_a_bad_email_standing():
    contact = CRMContact(contact_metadata={})
    booking.note_details(contact, "phone 123, email not-an-email")
    assert len(booking.unusable_details(contact)) == 2
    booking.note_details(contact, "my phone is 305 555 0100")
    assert booking.unusable_details(contact) == ['the email address "not-an-email" is not a valid email address']
    booking.note_details(contact, "email jamie@example.com")
    assert booking.unusable_details(contact) == []
