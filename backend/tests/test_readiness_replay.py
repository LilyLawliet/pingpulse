"""The tester's forty turns, replayed against the real booking code.

`corpus/readiness_2026_09_30.json` is the evidence a client's tester sent on
September 30: every prompt they typed into the Test agent, what it replied,
and what the action trace said it would have done. Here each conversation is
played again, in the order they played it, with the clock stopped on the
morning they ran it, against a shop set up the way that business is - Miami,
weekdays 9 to 8, site visits, the areas it covers.

What is asserted is the record: what was booked, moved or cancelled, and what
was offered. The replies a model writes cannot be replayed offline, but the
ones they got are checked against the reply guard, so a sentence that claimed
something that never happened is one the guard now stops.

When a new report arrives, add its turns to the corpus and its conversations
here. A failure in this file is a customer-facing fault somebody has already
found once.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.models import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    Appointment,
    CRMContact,
    Organization,
)
from app.services import booking

MIAMI = ZoneInfo("America/New_York")
# The morning of the test. "Tomorrow" in their prompts is October 1.
THEN = datetime(2026, 9, 30, 10, 0, tzinfo=MIAMI)

CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "readiness_2026_09_30.json").read_text(encoding="utf-8")
)
TURNS = {turn["id"]: turn for turn in CORPUS["turns"]}

SHOP = {
    "business_hours": {
        day: {"open": "09:00", "close": "20:00"}
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
    },
    # What the business covers, as its owner would list it. Its own
    # description says South Florida; the list is what the agent can check.
    "service_areas": [
        "Miami", "Miami Beach", "Miami-Dade", "Brickell", "Hialeah", "Dania Beach",
        "Fort Lauderdale", "330", "331", "333",
    ],
    "appointments": {"default_kind": "onsite", "duration_minutes": 60},
}


class _Then(datetime):
    """`datetime` whose now() is the morning of the test."""

    @classmethod
    def now(cls, tz=None):
        moment = THEN.astimezone(timezone.utc)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def that_morning(monkeypatch):
    monkeypatch.setattr(booking, "datetime", _Then)


@pytest.fixture
async def shop(db_session):
    organization = Organization(name=CORPUS["business"], sales_prompt="Construction and remodeling.")
    organization.timezone = "America/New_York"
    organization.agent_config = json.loads(json.dumps(SHOP))
    db_session.add(organization)
    await db_session.flush()
    return organization


@pytest.fixture
def customer(db_session, shop):
    made = []

    async def new() -> CRMContact:
        contact = CRMContact(
            organization_id=shop.id,
            phone_number=f"1305555{len(made):04d}",
            name="Test customer",
            pipeline_stage="NEW_LEAD",
            qualification={},
            contact_metadata={},
        )
        db_session.add(contact)
        await db_session.flush()
        made.append(contact)
        return contact

    return new


async def say(db, shop, contact, turn_id: str):
    return await booking.handle_turn(db, shop, contact, TURNS[turn_id]["prompt"])


async def count(db, contact, status=None) -> int:
    query = select(func.count(Appointment.id)).where(Appointment.contact_id == contact.id)
    if status:
        query = query.where(Appointment.status == status)
    return await db.scalar(query)


def local(moment) -> datetime:
    return booking._aware(moment).astimezone(MIAMI)


def test_the_corpus_is_the_whole_report():
    assert len(CORPUS["turns"]) == 40
    assert {t["verdict_then"] for t in CORPUS["turns"]} == {"PASS", "FAIL", "REVIEW"}


# ------------------------------------------------- nothing here books anything
# Questions, pressure, handoffs, opt-outs: none of them may touch the diary.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn_id",
    ["T01", "T06", "T07", "T08", "T09R", "T10", "T11", "T13", "T14", "T15", "T16", "T17",
     "T18", "T19", "T20", "T23", "T25", "T34", "T36", "T37"],
)
async def test_no_write(turn_id, db_session, shop, customer):
    contact = await customer()
    turn = await say(db_session, shop, contact, turn_id)
    assert turn.performed is None, f"{turn_id}: {TURNS[turn_id]['scenario']}"
    assert await count(db_session, contact) == 0


# ------------------------------------------------- T01-T04, the Brickell lead
@pytest.mark.asyncio
async def test_brickell_lead_without_an_address(db_session, shop, customer):
    contact = await customer()
    await say(db_session, shop, contact, "T01")

    # T02: 1am is refused; what is offered is inside the hours.
    turn = await say(db_session, shop, contact, "T02")
    assert turn.performed is None
    assert turn.refusal is not None and turn.refusal.reason == "closed"
    assert turn.offered and all(9 <= local(s).hour < 20 for s in turn.offered)

    # T03: "the Brickell bathroom project" is a neighbourhood, not somewhere
    # to drive to. Asked for the address; nothing booked.
    turn = await say(db_session, shop, contact, "T03")
    assert turn.performed is None
    assert turn.refusal.reason == "needs_address"
    assert await count(db_session, contact) == 0

    # T04: nothing to move, and still nowhere to go.
    turn = await say(db_session, shop, contact, "T04")
    assert turn.performed is None
    assert await count(db_session, contact) == 0


@pytest.mark.asyncio
async def test_brickell_lead_with_an_address_books_then_moves(db_session, shop, customer):
    contact = await customer()
    await say(db_session, shop, contact, "T01")
    await say(db_session, shop, contact, "T02")
    await booking.handle_turn(db_session, shop, contact, "The condo is 1200 Brickell Ave, Unit 4, Miami FL 33131")

    turn = await say(db_session, shop, contact, "T03")
    assert turn.performed == "booked", turn.prompt_block
    assert (local(turn.appointment.starts_at).date().isoformat(), local(turn.appointment.starts_at).hour) == ("2026-10-01", 9)
    assert "1200 Brickell Ave" in turn.appointment.location

    turn = await say(db_session, shop, contact, "T04")
    assert turn.performed == "moved", turn.prompt_block
    assert (local(turn.appointment.starts_at).date().isoformat(), local(turn.appointment.starts_at).hour) == ("2026-10-02", 10)
    assert await count(db_session, contact, APPOINTMENT_CONFIRMED) == 1


# ------------------------------------------------- Seattle, three times over
@pytest.mark.asyncio
@pytest.mark.parametrize("opening, acceptance", [("T12", None), ("T26", "T27"), ("T38", "T39")])
async def test_seattle_is_never_offered_or_booked(opening, acceptance, db_session, shop, customer):
    contact = await customer()
    turn = await say(db_session, shop, contact, opening)
    assert turn.offered == [], f"{opening} offered times to a Seattle job"
    assert turn.refusal is not None and turn.refusal.reason == "outside_area"
    if acceptance:
        turn = await say(db_session, shop, contact, acceptance)
        assert turn.performed is None
        assert turn.offered == []
    assert await count(db_session, contact) == 0


# ------------------------------------------------- hours, days, dates
@pytest.mark.asyncio
@pytest.mark.parametrize("turn_id", ["T21", "T35"])
async def test_closed_times_are_refused(turn_id, db_session, shop, customer):
    contact = await customer()
    turn = await say(db_session, shop, contact, turn_id)
    assert turn.performed is None
    assert turn.refusal is not None and turn.refusal.reason == "closed", turn.prompt_block
    assert await count(db_session, contact) == 0


@pytest.mark.asyncio
async def test_seven_pacific_is_ten_eastern_and_closed(db_session, shop, customer):
    contact = await customer()
    turn = await say(db_session, shop, contact, "T24")
    assert turn.performed is None
    assert turn.refusal.reason == "closed"
    assert "10:00 pm" in turn.prompt_block and "America/Los_Angeles" in turn.prompt_block
    assert await count(db_session, contact) == 0


@pytest.mark.asyncio
async def test_yesterday_is_past(db_session, shop, customer):
    contact = await customer()
    turn = await say(db_session, shop, contact, "T22")
    assert turn.performed is None
    assert turn.refusal.reason == "past"
    assert all(local(s) > THEN for s in turn.offered)


@pytest.mark.asyncio
async def test_september_29_offered_accepted_cancelled(db_session, shop, customer):
    contact = await customer()

    turn = await say(db_session, shop, contact, "T28")
    assert turn.refusal.reason == "past"
    assert all(local(s) > THEN for s in turn.offered), "a past time was offered"

    turn = await say(db_session, shop, contact, "T29")
    assert turn.performed is None
    assert turn.refusal.reason == "past"

    # T30: there is nothing to cancel, and the reply they got - "has been
    # removed" - is one the guard now refuses.
    turn = await say(db_session, shop, contact, "T30")
    assert turn.performed is None
    assert turn.refusal.reason == "not_found"
    assert booking.unverified_claims(TURNS["T30"]["reply_then"], cancelled=False)
    assert await count(db_session, contact) == 0


@pytest.mark.asyncio
async def test_nothing_on_file_to_cancel(db_session, shop, customer):
    contact = await customer()
    turn = await say(db_session, shop, contact, "T31")
    assert turn.performed is None
    assert turn.refusal.reason == "not_found"


# ------------------------------------------------- consent and details
@pytest.mark.asyncio
async def test_do_not_book_it_yet(db_session, shop, customer):
    contact = await customer()
    contact.contact_metadata = {"visit_address": "100 Test Avenue, Miami, FL 33101, unit 2"}
    turn = await say(db_session, shop, contact, "T32")
    assert turn.performed is None
    assert turn.refusal.reason == "holding_off"
    assert await count(db_session, contact) == 0


@pytest.mark.asyncio
async def test_invalid_details_and_no_address(db_session, shop, customer):
    contact = await customer()
    turn = await say(db_session, shop, contact, "T33")
    assert turn.performed is None
    assert turn.refusal.reason in ("needs_address", "contact_invalid")
    assert await count(db_session, contact) == 0
    # The confirmation they were sent is one the guard refuses.
    assert booking.unverified_claims(TURNS["T33"]["reply_then"], appointment=None)


# ------------------------------------------------- T40-T41, book then cancel
@pytest.mark.asyncio
async def test_full_details_book_and_cancel_cancels(db_session, shop, customer):
    contact = await customer()

    turn = await say(db_session, shop, contact, "T40")
    assert turn.performed == "booked", turn.prompt_block
    starts = local(turn.appointment.starts_at)
    assert (starts.date().isoformat(), starts.hour) == ("2026-10-05", 11)
    assert "100 Test Avenue" in turn.appointment.location

    turn = await say(db_session, shop, contact, "T41")
    assert turn.performed == "cancelled", turn.prompt_block
    assert await count(db_session, contact, APPOINTMENT_CANCELLED) == 1
    assert await count(db_session, contact, APPOINTMENT_CONFIRMED) == 0
    assert await count(db_session, contact) == 1, "a replacement appointment was written"


# ------------------------------------------------- what they were told
@pytest.mark.parametrize("turn_id", ["T03", "T24", "T33"])
def test_false_confirmations_they_received_are_now_refused(turn_id):
    """Each of these confirmed a visit the record, as it now stands, would not hold."""
    assert booking.unverified_claims(TURNS[turn_id]["reply_then"], appointment=None), (
        TURNS[turn_id]["reply_then"]
    )
