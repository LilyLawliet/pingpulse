"""A yes cannot book on contact details the business cannot use.

Details that cannot be used are raised on every booking turn: a message
giving "phone 123" is answered with the times and the plain sentence
"nothing can be booked until it is [corrected]". It was not true. The
read-back gate asked only whether they had said yes, so a yes booked the
visit anyway and the shop got an appointment with a number that reaches
nobody - having told the customer, in writing, that it would not.

Telling a customer something the backend then contradicts is the one failure
this file's subject exists to prevent, so the sentence is made true at the
yes rather than softened at the offer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import CRMContact, Organization
from app.services import booking

MIAMI = ZoneInfo("America/New_York")


def weekday_ahead(days: int = 3):
    day = datetime.now(timezone.utc).astimezone(MIAMI).date() + timedelta(days=days)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day


@pytest.fixture
async def shop_and_contact(db_session):
    shop = Organization(name="Phone Shop", sales_prompt="We sell and repair phones.")
    shop.timezone = "America/New_York"
    shop.agent_config = {
        "business_hours": {
            d: {"open": "09:00", "close": "20:00"}
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
        "appointments": {"min_notice_minutes": 0, "require_address": False},
    }
    db_session.add(shop)
    await db_session.flush()
    contact = CRMContact(
        organization_id=shop.id, phone_number="13055550333", name="Test",
        pipeline_stage="NEW_LEAD", qualification={},
        # They have already said what they want. This file is about what a
        # bad phone number does to a booking, and a shop that lists its
        # services does not offer times for work nobody has named - so
        # without this the turn stops one gate earlier, for a different
        # reason, and these would be testing that instead.
        contact_metadata={"job_accepted": {"job": "phone repair"}},
    )
    db_session.add(contact)
    await db_session.flush()
    return shop, contact


async def test_a_number_that_cannot_be_used_stops_the_offer(shop_and_contact, db_session):
    """The first gate: nothing is read back while a detail cannot be used."""
    shop, contact = shop_and_contact
    day = weekday_ahead()
    offer = await booking.handle_turn(
        db_session, shop, contact,
        f"Book me in on {day:%B} {day.day} at 10am. My phone number is 123.",
    )
    assert offer.proposed is None and offer.performed is None
    assert offer.refusal.reason == "contact_invalid"
    assert "not a usable phone number" in offer.prompt_block


async def test_a_yes_does_not_book_while_a_number_cannot_be_used(shop_and_contact, db_session):
    """The second gate, and the one that makes the sentence true.

    The detail is flagged here the way the first gate would have flagged it,
    because the question this answers is what happens if it ever does not:
    a yes must not book on a number that reaches nobody, having told them in
    writing that it would not.
    """
    shop, contact = shop_and_contact
    day = weekday_ahead()
    offer = await booking.handle_turn(
        db_session, shop, contact, f"Book me in on {day:%B} {day.day} at 10am"
    )
    assert offer.proposed, offer.prompt_block

    contact.contact_metadata = {
        **(contact.contact_metadata or {}),
        booking.UNUSABLE_DETAILS_KEY: {
            "phone": 'the phone number "123" is not a usable phone number'
        },
    }
    turn = await booking.handle_turn(db_session, shop, contact, "yes")
    assert turn.performed is None, turn.prompt_block
    assert turn.refusal.reason == "bad_details"
    assert "not a usable phone number" in turn.prompt_block
    assert await booking.upcoming_for(db_session, contact.id) is None


async def test_the_same_yes_books_once_the_number_is_corrected(shop_and_contact, db_session):
    shop, contact = shop_and_contact
    day = weekday_ahead()
    await booking.handle_turn(
        db_session, shop, contact,
        f"Book me in on {day:%B} {day.day} at 10am. My phone number is 123.",
    )
    await booking.handle_turn(db_session, shop, contact, "yes")
    again = await booking.handle_turn(
        db_session, shop, contact,
        f"Sorry, my phone number is +1 305 555 0199. {day:%B} {day.day} at 10am please.",
    )
    assert again.proposed, again.prompt_block
    assert "not a usable phone number" not in again.prompt_block
    turn = await booking.handle_turn(db_session, shop, contact, "yes")
    assert turn.performed == "booked", turn.prompt_block


async def test_a_bad_detail_does_not_strand_somebody_who_already_has_a_time(
    shop_and_contact, db_session
):
    """A move is for somebody with an appointment. Refusing it over a mistyped
    email leaves them with a time they cannot make, for an unrelated reason."""
    shop, contact = shop_and_contact
    day = weekday_ahead()
    await booking.handle_turn(db_session, shop, contact, f"Book me in on {day:%B} {day.day} at 10am")
    booked = await booking.handle_turn(db_session, shop, contact, "yes")
    assert booked.performed == "booked", booked.prompt_block

    later = weekday_ahead(5)
    await booking.handle_turn(
        db_session, shop, contact,
        f"My email is not-an-email. Can you move it to {later:%B} {later.day} at 2pm?",
    )
    turn = await booking.handle_turn(db_session, shop, contact, "yes")
    assert turn.performed == "moved", turn.prompt_block
