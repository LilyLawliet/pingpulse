"""A whole booking conversation, from message to record and back.

The brief's acceptance test, in the order it names: customer message -> AI
response -> backend action -> database state -> customer confirmation. These
must agree, and the way they used to disagree is the reason this file exists.

The conversation replayed here is the real one, with the real messages, taken
from the production database - a customer asking about a bathroom remodel in
Miami who was told her appointment was confirmed for 1am on a day she never
chose, and then told it had been cancelled when nothing had.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    Appointment,
    CRMContact,
    Organization,
)
from app.services import booking

from .conftest import confirmed

OPEN_WEEKDAYS = {
    day: {"open": "09:00", "close": "17:00"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
}


@pytest.fixture
async def remodeller(db_session):
    """Beluga, near enough: a trade that drives to the customer."""
    organization = Organization(name="Beluga Group", sales_prompt="Bathroom remodels.")
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": OPEN_WEEKDAYS,
        "appointments": {
            "min_notice_minutes": 0,
            "default_kind": "onsite",
            "duration_minutes": 60,
        },
    }
    db_session.add(organization)
    await db_session.flush()

    contact = CRMContact(
        organization_id=organization.id,
        phone_number="13057483629",
        name="ZO",
        pipeline_stage="NEW_LEAD",
        qualification={},
        # A site visit is booked to somewhere. The address she gave earlier
        # in the conversation, the way a live contact carries it.
        contact_metadata={"visit_address": "1200 Brickell Ave, Unit 4, Miami"},
    )
    db_session.add(contact)
    await db_session.flush()
    return organization, contact


# ------------------------------------------------------ the original failure
@pytest.mark.asyncio
async def test_asking_for_a_consultation_books_nothing_and_offers_real_times(
    remodeller, db_session
):
    """Her actual message. It produced a booked estimate and a 1am confirmation.

    What it must produce: no appointment, and times that exist.
    """
    organization, contact = remodeller

    turn = await booking.handle_turn(
        db_session,
        organization,
        contact,
        "Yes, I'd like to schedule a consultation. What days and times do you have available?",
    )

    assert turn.performed is None, "something was booked by a question"
    assert turn.appointment is None
    assert await booking.upcoming_for(db_session, contact.id) is None

    assert turn.offered, "no times were offered at all"
    for slot in turn.offered:
        assert 9 <= slot.hour < 17, f"{slot} is outside opening hours"
        assert slot.weekday() <= 4

    # And the agent is told, in the prompt, that nothing is booked.
    assert "not say anything is confirmed" in turn.prompt_block.lower()


@pytest.mark.asyncio
async def test_asking_whether_it_is_confirmed_gets_the_truth(remodeller, db_session):
    """Her follow-up: "Is my appointment confirmed?"

    There is no appointment. The reply guard must reject any sentence saying
    otherwise, which is precisely what was sent last time.
    """
    organization, contact = remodeller
    await booking.handle_turn(db_session, organization, contact, "what times do you have?")

    live = await booking.upcoming_for(db_session, contact.id)
    assert live is None

    was_sent = (
        "Your appointment is confirmed for September 19, 2026 at 1:00 AM EST "
        "at your Miami property."
    )
    assert booking.unverified_claims(was_sent, appointment=live), (
        "the sentence that caused this would still get through"
    )


@pytest.mark.asyncio
async def test_asking_to_cancel_nothing_is_told_the_truth(remodeller, db_session):
    """"Please cancel any appointment you have for me."

    The agent said it had cancelled the September 19 appointment. There was
    none. The turn must say so and must not license the claim.
    """
    organization, contact = remodeller

    turn = await booking.handle_turn(
        db_session,
        organization,
        contact,
        "I never agreed to that time. Please cancel any appointment you have for me.",
    )

    assert turn.performed is None
    assert turn.refusal is not None and turn.refusal.reason == "not_found"
    assert "NO appointment" in turn.prompt_block

    was_sent = "I am sorry for the confusion; I have cancelled the September 19 appointment."
    assert booking.unverified_claims(was_sent)


# --------------------------------------------------- the conversation that works
@pytest.mark.asyncio
async def test_offer_choose_book_confirm(remodeller, db_session):
    """The whole exchange, and every claim backed by the record."""
    organization, contact = remodeller

    # 1. She asks what is available.
    offer = await booking.handle_turn(
        db_session, organization, contact, "what times can you come out?"
    )
    assert offer.offered
    first = offer.offered[0]

    # 2. She picks one, and is read back what would be booked. Nothing is yet.
    proposal = await booking.handle_turn(
        db_session, organization, contact, "the first one please"
    )
    assert proposal.performed is None
    assert await booking.upcoming_for(db_session, contact.id) is None
    assert "Reply YES" in proposal.reply and "site visit" in proposal.reply

    # 3. She says yes.
    chosen = await booking.handle_turn(db_session, organization, contact, "yes")

    assert chosen.performed == "booked"
    assert chosen.appointment is not None

    # 3. The database agrees.
    stored = await booking.upcoming_for(db_session, contact.id)
    assert stored is not None
    assert stored.starts_at == first
    assert stored.status == APPOINTMENT_CONFIRMED

    # 4. The confirmation says exactly what the row says.
    sentence = booking.describe(stored)
    assert "site visit" in sentence
    assert booking.unverified_claims(
        f"Your appointment is confirmed - {sentence}.", appointment=stored
    ) == []

    # 5. And the prompt tells the agent to say that and nothing more.
    assert "BOOKED, just now, successfully" in chosen.prompt_block
    assert "Do not add a detail that is not in it" in chosen.prompt_block


@pytest.mark.asyncio
async def test_an_unclear_answer_books_nothing_and_asks_again(remodeller, db_session):
    organization, contact = remodeller
    await booking.handle_turn(db_session, organization, contact, "when are you free?")

    reply = await booking.handle_turn(db_session, organization, contact, "yes please")

    assert reply.performed is None
    assert await booking.upcoming_for(db_session, contact.id) is None


# ---------------------------------------------------- cancelling for real
@pytest.mark.asyncio
async def test_cancelling_a_real_appointment_updates_the_record(remodeller, db_session):
    organization, contact = remodeller
    await booking.handle_turn(db_session, organization, contact, "what times are free?")
    booked = await confirmed(db_session, organization, contact, "the first one")
    assert booked.performed == "booked"

    cancelled = await confirmed(
        db_session, organization, contact, "please cancel my appointment"
    )

    assert cancelled.performed == "cancelled"
    assert await booking.upcoming_for(db_session, contact.id) is None
    assert booked.appointment.status == APPOINTMENT_CANCELLED
    # And it is said from the row, not by a model.
    assert cancelled.reply and "has been cancelled" in cancelled.reply, cancelled.reply


@pytest.mark.asyncio
async def test_moving_an_appointment_leaves_one_live_row(remodeller, db_session):
    from sqlalchemy import select

    organization, contact = remodeller
    await booking.handle_turn(db_session, organization, contact, "what times are free?")
    await confirmed(db_session, organization, contact, "the first one")

    # Ask to move, get offered times, pick a different one.
    asked = await booking.handle_turn(
        db_session, organization, contact, "can we move it to another time?"
    )
    assert asked.performed is None
    assert asked.offered

    moved = await confirmed(
        db_session, organization, contact, "move it to the last one"
    )

    assert moved.performed == "moved"
    live = (
        await db_session.execute(
            select(Appointment).where(
                Appointment.contact_id == contact.id,
                Appointment.status == APPOINTMENT_CONFIRMED,
            )
        )
    ).scalars().all()
    assert len(live) == 1


# --------------------------------------------------- qualification holds it back
@pytest.mark.asyncio
async def test_a_booking_waits_for_the_information_the_shop_requires(db_session):
    """A van with nowhere to go is not a booking.

    A trade that asks for the property address before sending somebody out is
    asking for a reason, and the appointment must not complete without it.
    """
    organization = Organization(name="Beluga", sales_prompt="Remodels.")
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": OPEN_WEEKDAYS,
        "appointments": {"min_notice_minutes": 0},
        "qualification_slots": [
            {"name": "project_type", "asks": "what they want done"},
            {"name": "address", "asks": "the address the work is at"},
        ],
    }
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(
        organization_id=organization.id,
        phone_number="+15550009",
        qualification={"project_type": "bathroom remodel"},
    )
    db_session.add(contact)
    await db_session.flush()

    await booking.handle_turn(db_session, organization, contact, "when can you come?")
    attempt = await booking.handle_turn(db_session, organization, contact, "the first one")

    assert attempt.performed is None
    assert attempt.refusal.reason == "needs_qualification"
    assert "address" in attempt.prompt_block
    assert "do NOT say it is" in attempt.prompt_block
    assert await booking.upcoming_for(db_session, contact.id) is None

    # Once the address is known, the same choice goes through.
    contact.qualification = {**contact.qualification, "address": "12 Mill Lane, Miami"}
    await db_session.flush()
    await booking.handle_turn(db_session, organization, contact, "when can you come?")
    now_booked = await confirmed(db_session, organization, contact, "the first one")

    assert now_booked.performed == "booked"


# ------------------------------------------------- a shop that does not book
@pytest.mark.asyncio
async def test_a_shop_with_no_hours_is_told_not_to_offer_anything(db_session):
    organization = Organization(name="No Diary", sales_prompt="x")
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(organization_id=organization.id, phone_number="+1")
    db_session.add(contact)
    await db_session.flush()

    turn = await booking.handle_turn(
        db_session, organization, contact, "can I book an appointment?"
    )

    assert turn.performed is None
    assert "not set up appointment booking" in turn.prompt_block


@pytest.mark.asyncio
async def test_a_customer_who_already_has_one_is_not_offered_another(
    remodeller, db_session
):
    organization, contact = remodeller
    await booking.handle_turn(db_session, organization, contact, "what times are free?")
    await confirmed(db_session, organization, contact, "the first one")

    again = await booking.handle_turn(
        db_session, organization, contact, "can I book an appointment?"
    )

    assert again.performed is None
    assert again.appointment is not None
    assert "They have a confirmed" in again.prompt_block
    assert not again.offered, "a second appointment was offered to somebody who has one"


@pytest.mark.asyncio
async def test_the_handler_never_raises_into_the_reply_path(remodeller, db_session):
    """An appointment failure must never be the reason a customer gets nothing."""
    organization, contact = remodeller

    class Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("the database is gone")

        def add(self, *a, **k):
            raise RuntimeError("the database is gone")

    turn = await booking.handle_turn(Broken(), organization, contact, "book me in")

    assert turn.performed is None
    assert turn.prompt_block == ""


# ------------------------------------------- what the dashboard can see
@pytest.mark.asyncio
async def test_the_contact_record_shows_the_appointment(client, org_a, db_session):
    """The brief asks the contact to expose appointment status.

    Everything else it names was already there - the customer, the history,
    the next action, whether a person took over. The appointment was the one
    that did not exist to show, and a lead standing in "Estimate scheduled"
    was the only evidence anything was booked.
    """
    from sqlalchemy import select

    from app.models import Organization
    from app.services import booking as bk

    organization = (
        await db_session.execute(
            select(Organization).where(Organization.id == org_a.organization_id)
        )
    ).scalars().one()
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": OPEN_WEEKDAYS,
        "appointments": {"min_notice_minutes": 0, "default_kind": "onsite"},
    }
    contact = CRMContact(
        organization_id=organization.id, phone_number="+15550123", name="Dana",
        contact_metadata={"visit_address": "88 Ocean Drive, Miami Beach"},
    )
    db_session.add(contact)
    await db_session.flush()

    # Nothing booked yet.
    empty = await client.get(
        f"/api/v1/crm/contacts/{contact.id}", headers=org_a.headers
    )
    assert empty.status_code == 200
    assert empty.json()["appointment"] is None

    await bk.handle_turn(db_session, organization, contact, "what times are free?")
    booked = await confirmed(db_session, organization, contact, "the first one")
    assert booked.performed == "booked"
    await db_session.commit()

    response = await client.get(
        f"/api/v1/crm/contacts/{contact.id}", headers=org_a.headers
    )

    assert response.status_code == 200
    shown = response.json()["appointment"]
    assert shown is not None
    assert shown["status"] == "confirmed"
    assert shown["kind"] == "onsite"
    assert "site visit" in shown["description"]

    # And on the board listing too, which is where an operator actually looks.
    listed = await client.get("/api/v1/crm/contacts", headers=org_a.headers)
    mine = [row for row in listed.json() if row["id"] == str(contact.id)]
    assert mine and mine[0]["appointment"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_a_cancelled_appointment_stops_showing(client, org_a, db_session):
    """Cancelled is not upcoming. The record must not keep advertising it."""
    from sqlalchemy import select

    from app.models import Organization
    from app.services import booking as bk

    organization = (
        await db_session.execute(
            select(Organization).where(Organization.id == org_a.organization_id)
        )
    ).scalars().one()
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": OPEN_WEEKDAYS,
        "appointments": {"min_notice_minutes": 0},
    }
    contact = CRMContact(organization_id=organization.id, phone_number="+15550456")
    db_session.add(contact)
    await db_session.flush()

    await bk.handle_turn(db_session, organization, contact, "what times are free?")
    await bk.handle_turn(db_session, organization, contact, "the first one")
    await bk.handle_turn(db_session, organization, contact, "cancel my appointment")
    await db_session.commit()

    response = await client.get(
        f"/api/v1/crm/contacts/{contact.id}", headers=org_a.headers
    )

    assert response.json()["appointment"] is None
