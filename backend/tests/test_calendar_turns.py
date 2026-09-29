"""Booking, moving and cancelling the way customers actually write it.

The earlier tests drove the diary with the words the code was listening for.
These use what people send: "the second one" after asking to move, "Friday at
3pm" without being offered anything, "is Thursday free?", a plain "yes". Each
one either does exactly what was asked, checked against the shop's hours and
diary, or does nothing and says why.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import APPOINTMENT_CANCELLED, APPOINTMENT_CONFIRMED, Appointment, CRMContact, Organization
from app.services import booking
from sqlalchemy import select

KARACHI = ZoneInfo("Asia/Karachi")
HOURS = {
    day: {"open": "11:00", "close": "20:00"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
}


@pytest.fixture
async def shop(db_session):
    organization = Organization(name="Miku's Stationery", sales_prompt="Stationery.")
    organization.timezone = "Asia/Karachi"
    organization.agent_config = {
        "business_hours": HOURS,
        "appointments": {"min_notice_minutes": 60, "default_kind": "other", "duration_minutes": 30},
    }
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(
        organization_id=organization.id, phone_number="923001234567", name="Aiko",
        pipeline_stage="NEW_LEAD", qualification={},
    )
    db_session.add(contact)
    await db_session.flush()
    return organization, contact


def _ahead(weekday: int, weeks: int = 1) -> date:
    """A date at least a week out on this weekday, so notice never gets in the way."""
    today = datetime.now(KARACHI).date()
    return today + timedelta(days=(weekday - today.weekday()) % 7 + 7 * weeks)


def _written(day: date) -> str:
    return f"{day.day} {day.strftime('%B')}"


def _local(appointment) -> datetime:
    moment = appointment.starts_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(KARACHI)


async def _live(db, contact):
    return (
        await db.execute(
            select(Appointment).where(
                Appointment.contact_id == contact.id, Appointment.status == APPOINTMENT_CONFIRMED
            )
        )
    ).scalars().all()


# ------------------------------------------------------------- reading
def test_reads_a_plain_day_and_time():
    friday = _ahead(4)
    named = booking.named_time(f"can I come on {_written(friday)} at 3pm?", KARACHI)
    assert named.days == (friday,)
    assert named.clocks == (time(15, 0),)


def test_reads_weekdays_tomorrow_and_24_hour_clocks():
    today = datetime.now(KARACHI).date()
    assert booking.named_time("tomorrow 15:30", KARACHI).days == (today + timedelta(days=1),)
    assert time(15, 30) in booking.named_time("tomorrow 15:30", KARACHI).clocks
    assert booking.named_time("next friday", KARACHI).days[0].weekday() == 4
    assert booking.named_time("Saturday at noon", KARACHI).clocks == (time(12, 0),)


@pytest.mark.parametrize(
    "text",
    [
        "I want 20 gel pens",
        "2 packs of gel pens, how much with delivery?",
        "1000 sheets of A4",
        "the cat sat on the mat",
        "may I have the pink one",
        "sun is out, send the stickers",
        "order is PKR 9,999",
    ],
)
def test_orders_and_ordinary_sentences_name_no_time(text):
    assert not booking.named_time(text, KARACHI).any


def test_an_hour_without_am_or_pm_is_read_against_the_hours(shop):
    organization, _ = shop
    friday = _ahead(4)
    named = booking.named_time(f"{_written(friday)} at 3", KARACHI)
    moment = booking.named_moment(organization, named)
    assert moment.astimezone(KARACHI).time() == time(15, 0)  # 3am is shut


def test_two_days_name_no_single_moment(shop):
    organization, _ = shop
    named = booking.named_time("monday or tuesday at 4pm", KARACHI)
    assert len(named.days) == 2
    assert booking.named_moment(organization, named) is None


# ------------------------------------------------------------- booking
@pytest.mark.asyncio
async def test_a_named_time_is_booked_exactly(shop, db_session):
    organization, contact = shop
    friday = _ahead(4)
    turn = await booking.handle_turn(
        db_session, organization, contact, f"please book me for {_written(friday)} at 3pm"
    )
    assert turn.performed == "booked"
    local = _local(turn.appointment)
    assert (local.date(), local.time()) == (friday, time(15, 0))
    assert "3:00 pm" in turn.prompt_block


@pytest.mark.asyncio
async def test_a_time_outside_the_hours_is_refused_with_the_hours(shop, db_session):
    organization, contact = shop
    friday = _ahead(4)
    turn = await booking.handle_turn(
        db_session, organization, contact, f"book {_written(friday)} at 9pm"
    )
    assert turn.performed is None
    assert await _live(db_session, contact) == []
    assert "open 11:00 am to 8:00 pm" in turn.prompt_block
    assert turn.offered, "no alternatives were offered"
    assert all(_local_slot.astimezone(KARACHI).date() == friday for _local_slot in turn.offered)


@pytest.mark.asyncio
async def test_sunday_is_shut_and_says_so(shop, db_session):
    organization, contact = shop
    sunday = _ahead(6)
    turn = await booking.handle_turn(
        db_session, organization, contact, f"can I book {_written(sunday)} at 2pm"
    )
    assert turn.performed is None
    assert "closed on Sunday" in turn.prompt_block


@pytest.mark.asyncio
async def test_asking_whether_free_asks_first_then_yes_books_it(shop, db_session):
    organization, contact = shop
    thursday = _ahead(3)
    asked = await booking.handle_turn(
        db_session, organization, contact, f"is {_written(thursday)} at 4pm free?"
    )
    assert asked.performed is None
    assert "is FREE" in asked.prompt_block
    assert await _live(db_session, contact) == []

    yes = await booking.handle_turn(db_session, organization, contact, "yes please")
    assert yes.performed == "booked"
    assert _local(yes.appointment).time() == time(16, 0)


@pytest.mark.asyncio
async def test_a_day_alone_offers_that_days_times(shop, db_session):
    organization, contact = shop
    wednesday = _ahead(2)
    turn = await booking.handle_turn(
        db_session, organization, contact, f"any appointments on {_written(wednesday)}?"
    )
    assert turn.performed is None
    assert turn.offered
    assert {slot.astimezone(KARACHI).date() for slot in turn.offered} == {wednesday}


@pytest.mark.asyncio
async def test_a_taken_time_is_never_booked_twice(shop, db_session):
    organization, contact = shop
    friday = _ahead(4)
    other = CRMContact(organization_id=organization.id, phone_number="923009999999", qualification={})
    db_session.add(other)
    await db_session.flush()
    first = await booking.handle_turn(db_session, organization, other, f"book {_written(friday)} 3pm")
    assert first.performed == "booked"

    second = await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")
    assert second.performed is None
    assert "already been booked" in second.prompt_block
    assert all(slot != first.appointment.starts_at for slot in second.offered)


# ------------------------------------------------------------- moving
@pytest.mark.asyncio
async def test_asking_to_move_then_picking_by_number_moves_it(shop, db_session):
    """The fault: the pick said nothing about moving, and nothing moved."""
    organization, contact = shop
    friday = _ahead(4)
    await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")

    offer = await booking.handle_turn(db_session, organization, contact, "can I reschedule?")
    assert offer.performed is None and offer.offered
    second = offer.offered[1]

    picked = await booking.handle_turn(db_session, organization, contact, "the second one")
    assert picked.performed == "moved"
    assert picked.appointment.starts_at == second
    live = await _live(db_session, contact)
    assert len(live) == 1 and live[0].id == picked.appointment.id


@pytest.mark.asyncio
async def test_a_new_time_with_instead_moves_it(shop, db_session):
    organization, contact = shop
    friday, monday = _ahead(4), _ahead(0, weeks=2)
    booked = await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")
    old_id = booked.appointment.id

    moved = await booking.handle_turn(
        db_session, organization, contact, f"can we do {_written(monday)} at 5pm instead"
    )
    assert moved.performed == "moved"
    assert _local(moved.appointment).date() == monday
    assert _local(moved.appointment).time() == time(17, 0)
    old = await db_session.get(Appointment, old_id)
    assert old.status == APPOINTMENT_CANCELLED
    assert moved.appointment.replaces_id == old_id


@pytest.mark.asyncio
async def test_dont_want_friday_can_we_do_monday_is_a_move_not_a_cancel(shop, db_session):
    organization, contact = shop
    friday, monday = _ahead(4), _ahead(0, weeks=2)
    await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")
    turn = await booking.handle_turn(
        db_session, organization, contact,
        f"I don't want the appointment on friday, can we do {_written(monday)} 2pm",
    )
    assert turn.performed == "moved"
    assert len(await _live(db_session, contact)) == 1


@pytest.mark.asyncio
async def test_a_move_to_a_shut_time_keeps_the_original(shop, db_session):
    organization, contact = shop
    friday, sunday = _ahead(4), _ahead(6)
    booked = await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")
    turn = await booking.handle_turn(
        db_session, organization, contact, f"move it to {_written(sunday)} at 1pm"
    )
    assert turn.performed is None
    live = await _live(db_session, contact)
    assert [row.id for row in live] == [booked.appointment.id]
    assert "still have their original" in turn.prompt_block


@pytest.mark.asyncio
async def test_a_time_named_by_someone_already_booked_is_asked_about(shop, db_session):
    organization, contact = shop
    friday, tuesday = _ahead(4), _ahead(1, weeks=2)
    await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")
    asked = await booking.handle_turn(
        db_session, organization, contact, f"can I book {_written(tuesday)} at 12pm?"
    )
    assert asked.performed is None
    assert "moved to it" in asked.prompt_block
    yes = await booking.handle_turn(db_session, organization, contact, "yes")
    assert yes.performed == "moved"
    assert _local(yes.appointment).date() == tuesday


# ------------------------------------------------------------- cancelling
@pytest.mark.asyncio
async def test_cancel_cancels_and_the_record_says_so(shop, db_session):
    organization, contact = shop
    friday = _ahead(4)
    booked = await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")
    turn = await booking.handle_turn(db_session, organization, contact, "please cancel my appointment")
    assert turn.performed == "cancelled"
    assert turn.appointment.id == booked.appointment.id
    assert await _live(db_session, contact) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "is delivery possible tomorrow?",
        "can it come today?",
        "are you open on saturday?",
        "will my order arrive by friday at 5pm?",
        "is the planner available?",
    ],
)
async def test_questions_about_orders_and_hours_touch_no_diary(shop, db_session, text):
    organization, contact = shop
    turn = await booking.handle_turn(db_session, organization, contact, text)
    assert turn.performed is None
    assert turn.offered == []
    assert turn.prompt_block == ""


# ------------------------------------------------------------- the shop is told
@pytest.mark.asyncio
async def test_the_shop_is_alerted_when_a_chat_books_moves_and_cancels(db_session, monkeypatch):
    from app.api.webhook import process_inbound_message
    from app.schemas import GenerationResult, TwilioWebhookPayload
    from app.services import llm_service, notifications
    from app.services.twilio_service import TwilioService

    organization = Organization(name="Miku's Stationery", sales_prompt="Stationery.")
    organization.timezone = "Asia/Karachi"
    organization.agent_config = {
        "business_hours": HOURS,
        "appointments": {"min_notice_minutes": 60, "default_kind": "other", "duration_minutes": 30},
    }
    db_session.add(organization)
    await db_session.flush()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out"

    async def fake_generate(*_a, **_k):
        return GenerationResult(provider="groq", text="Done.", prompt_used="p", latency_ms=5)

    raised = []

    async def record(db, org, event, title, body, contact_id=None):
        raised.append((event, title, body))

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)
    monkeypatch.setattr(notifications, "raise_and_send", record)

    async def say(text, sid):
        payload = TwilioWebhookPayload.model_validate(
            {"From": "whatsapp:+923001112222", "To": "whatsapp:+16602075318",
             "Body": text, "MessageSid": sid}
        )
        await process_inbound_message(db_session, payload)
        return [entry for entry in raised if entry[0] == "booking"]

    friday, monday = _ahead(4), _ahead(0, weeks=2)
    diary = await say(f"please book {_written(friday)} at 3pm", "SMb1")
    assert diary[-1][1] == "New appointment booked"
    assert "3:00 pm" in diary[-1][2] and "Friday" in diary[-1][2]

    diary = await say(f"can we do {_written(monday)} at 5pm instead", "SMb2")
    assert diary[-1][1] == "Appointment moved"
    assert "Friday" in diary[-1][2] and "Monday" in diary[-1][2] and "5:00 pm" in diary[-1][2]

    diary = await say("please cancel my appointment", "SMb3")
    assert diary[-1][1] == "Appointment cancelled"
    assert len(diary) == 3, "an alert was raised for something that did not happen"


@pytest.mark.asyncio
async def test_cancel_after_asking_to_move_cancels(shop, db_session):
    organization, contact = shop
    friday = _ahead(4)
    await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")
    await booking.handle_turn(db_session, organization, contact, "can I reschedule?")
    turn = await booking.handle_turn(db_session, organization, contact, "no, just cancel it please")
    assert turn.performed == "cancelled"
    assert await _live(db_session, contact) == []


@pytest.mark.asyncio
async def test_with_no_model_the_customer_still_gets_the_row(shop, db_session):
    organization, contact = shop
    friday = _ahead(4)
    booked = await booking.handle_turn(db_session, organization, contact, f"book {_written(friday)} 3pm")
    assert booked.plain_reply(organization) == f"You're booked: {booking.describe(booked.appointment)}."

    offer = await booking.handle_turn(db_session, organization, contact, "can I reschedule?")
    text = offer.plain_reply(organization)
    assert text.startswith("These times are free:") and "1. " in text

    shut = await booking.handle_turn(
        db_session, organization, contact, f"move it to {_written(_ahead(6))} at 1pm"
    )
    assert "isn't possible" in shut.plain_reply(organization)
    assert booking.TurnResult().plain_reply(organization) is None
