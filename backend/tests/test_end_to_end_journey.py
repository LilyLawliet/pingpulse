"""One customer, start to finish, through the seams.

Every part of this is covered somewhere else. What is not covered anywhere
else is the *joins* - a document being read and the hours it produced actually
reaching the booking engine, a booked slot actually reaching the calendar
feed, a request for a person actually stopping the agent and raising an alert
somebody would see.

The seams are where the incidents have come from. Hours were parsed correctly
out of a document and never reached booking, because a timezone nobody set
blocked the save. A customer asked for a person three times and was answered
with a link, because the matcher and the classifier disagreed about what she
had said. Each component was doing its job.

The question this file exists to keep answering:

    Does a date the customer is told come from a row in the database?

Not from the model, not from a template, not from a regular expression run
over prose. A row. That is the difference between this product and the one
that told somebody her appointment was confirmed for 1am on a date she had
never chosen.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    APPOINTMENT_CONFIRMED,
    Appointment,
    CRMContact,
    Message,
    Notification,
    Organization,
)
from app.services import booking, calendar_feed

# A site visit booked by the agent goes somewhere.
VISIT = "1200 Brickell Ave, Unit 4, Miami FL 33131"


# ---------------------------------------------------------------- helpers
async def _org(db_session, tenant) -> Organization:
    return await db_session.get(Organization, uuid.UUID(tenant.organization_id))


async def _upload(tenant, body: str, name: str = "handbook.txt"):
    return await tenant._client.post(
        "/api/v1/knowledge/upload",
        headers=tenant.headers,
        files={"file": (name, body.encode("utf-8"), "text/plain")},
    )


async def _save_config(tenant, config, zone=None):
    return await tenant._client.put(
        "/api/v1/agent-config",
        headers=tenant.headers,
        json={"agent_config": config, "timezone": zone},
    )


HANDBOOK = """Beluga Group - Service Handbook

We remodel bathrooms across South Florida.

Opening hours
Monday - Friday: 9:00 AM - 6:00 PM
Saturday: 10:00 AM - 2:00 PM
Sunday: Closed

Services
Wet room conversion
Heated flooring
Walk-in showers

Areas we serve
Miami-Dade, Broward, Palm Beach

Delivery of fittings takes 2-3 working days.
"""


# ======================================================= the setup journey
@pytest.mark.asyncio
async def test_a_document_becomes_a_diary(org_a, db_session):
    """The whole setup path, end to end.

    Upload -> hours read -> timezone set -> confirmed -> booking live. Every
    step of this existed and worked, and a client still ran for weeks unable
    to take an appointment because the chain broke silently in the middle.
    """
    # 1. Nothing configured: the agent cannot book, and says why.
    before = (await org_a.get("/api/v1/agent-config")).json()
    assert before["booking"]["can_book"] is False

    # 2. The document is read.
    response = await _upload(org_a, HANDBOOK)
    assert response.status_code == 201
    # The response reports which fields were filled and what they say, in
    # words; the values themselves are held as a proposal on the config.
    report = response.json()["from_document"]
    assert report["proposed"] is True
    assert set(report["fields"]) == {"business_hours", "services", "service_areas"}
    assert "3 area(s)" in report["found"]

    # 3. Read, but not in force. Booking is still off, and the reason now
    #    names the document rather than telling somebody to set hours they
    #    can see on their screen.
    await db_session.commit()
    stalled = (await org_a.get("/api/v1/agent-config")).json()
    assert stalled["booking"]["can_book"] is False
    assert "hours_unconfirmed" in {b["key"] for b in stalled["booking"]["blockers"]}

    offered = stalled["agent_config"]["from_document"]["fields"]
    assert offered["business_hours"]["monday"] == {"open": "09:00", "close": "18:00"}
    assert "Wet room conversion" in offered["services"]
    # Three places, not one line that reads like three. The handbook writes
    # them on one line with a trailing sentence underneath, and both halves of
    # that used to go wrong at once.
    assert offered["service_areas"] == ["Miami-Dade", "Broward", "Palm Beach"]

    # 4. Hours cannot be saved without a timezone. This is the step that
    #    silently stopped a real client.
    refused = await _save_config(org_a, offered)
    assert refused.status_code == 422
    assert any("timezone" in problem.lower() for problem in refused.json()["detail"])

    # 5. With one, they save.
    accepted = await _save_config(
        org_a, {**offered, "appointments": {"enabled": True}}, zone="America/New_York"
    )
    assert accepted.status_code == 200

    # 6. And the diary is live.
    await db_session.commit()
    after = (await org_a.get("/api/v1/agent-config")).json()
    assert after["booking"]["can_book"] is True
    assert after["booking"]["blockers"] == []
    assert after["booking"]["timezone"] == "America/New_York"
    assert after["booking"]["days_open"] == 6


# ================================================ the date is a real date
@pytest.mark.asyncio
async def test_every_offered_time_is_a_real_gap_in_a_real_diary(org_a, db_session):
    """The question this whole subsystem answers.

    A customer was once told her appointment was confirmed for 1am on a date
    she never chose, because a time was constructed rather than looked up. So:
    every slot offered must fall inside the hours that were actually saved, be
    in the future, and land on a day the business is actually open.
    """
    organization = await _org(db_session, org_a)
    organization.timezone = "America/New_York"
    organization.agent_config = {
        "business_hours": {
            day: {"open": "09:00", "close": "17:00"}
            for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
        "appointments": {"enabled": True, "min_notice_minutes": 0},
    }
    await db_session.flush()

    slots = await booking.free_slots(db_session, organization, days=7, limit=50)
    assert slots, "a shop open five days a week has gaps"

    zone = booking.agent_config.zone_of(organization)
    now = datetime.now(timezone.utc)
    for slot in slots:
        local = slot.astimezone(zone)
        # Not in the past.
        assert slot > now - timedelta(minutes=1), local
        # Never at 1am. Inside the hours that were saved, in the zone chosen.
        assert local.weekday() < 5, f"offered on a closed day: {local}"
        assert local.hour >= 9, f"offered before opening: {local}"
        assert local.hour < 17, f"offered after closing: {local}"


@pytest.mark.asyncio
async def test_a_booking_the_customer_made_is_a_row_they_can_be_shown(
    org_a, db_session
):
    """Booked from an offered slot, and the confirmation is rendered from the
    row rather than from whatever the model remembered."""
    organization = await _org(db_session, org_a)
    organization.timezone = "America/New_York"
    organization.agent_config = {
        "business_hours": {
            day: {"open": "09:00", "close": "17:00"}
            for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
        "appointments": {"enabled": True, "min_notice_minutes": 0},
    }
    contact = CRMContact(
        organization_id=organization.id, phone_number="+13055550101", name="Irsa"
    )
    db_session.add(contact)
    await db_session.flush()

    slots = await booking.free_slots(db_session, organization, days=7, limit=10)
    chosen = slots[0]

    result = await booking.book(db_session, organization, contact, chosen, location=VISIT)
    appointment = getattr(result, "appointment", None)
    assert appointment is not None, result

    # The row is the truth, and it froze the zone it was agreed in.
    assert appointment.starts_at == chosen
    assert appointment.timezone_name == "America/New_York"
    assert appointment.status == APPOINTMENT_CONFIRMED

    # What the customer is told comes out of that row, and names the day and
    # the hour that row holds.
    spoken = booking.describe(appointment)
    local = chosen.astimezone(booking.agent_config.zone_of(organization))
    assert local.strftime("%A") in spoken, spoken
    assert str(int(local.strftime("%I"))) in spoken, spoken

    # And the same row is what the phone subscribes to.
    rows = await calendar_feed.appointments_for(db_session, organization.id)
    ics = calendar_feed.render(organization.name, rows)
    assert chosen.strftime("%Y%m%dT%H%M%SZ") in ics
    assert "Irsa" in ics


@pytest.mark.asyncio
async def test_the_same_slot_cannot_be_given_to_two_people(org_a, db_session):
    organization = await _org(db_session, org_a)
    organization.timezone = "America/New_York"
    organization.agent_config = {
        "business_hours": {
            day: {"open": "09:00", "close": "17:00"}
            for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
        "appointments": {"enabled": True, "min_notice_minutes": 0},
    }
    first = CRMContact(organization_id=organization.id, phone_number="+13055550101")
    second = CRMContact(organization_id=organization.id, phone_number="+13055550102")
    db_session.add_all([first, second])
    await db_session.flush()

    slots = await booking.free_slots(db_session, organization, days=7, limit=10)
    chosen = slots[0]

    assert getattr(await booking.book(db_session, organization, first, chosen, location=VISIT), "appointment", None)
    clash = await booking.book(db_session, organization, second, chosen, location=VISIT)
    assert getattr(clash, "appointment", None) is None

    # And it stops being offered.
    after = await booking.free_slots(db_session, organization, days=7, limit=50)
    assert chosen not in after


# ==================================== a fabricated date never reaches anyone
@pytest.mark.asyncio
async def test_a_reply_claiming_a_booking_that_does_not_exist_is_caught():
    """The 1am incident, as a unit. A model that announces an appointment
    nobody made is contradicted by the record, not trusted."""
    problems = booking.unverified_claims(
        "You're all set — I've booked you in for Saturday at 1am.",
        appointment=None,
    )
    assert problems
    assert "no confirmed appointment" in problems[0]


@pytest.mark.asyncio
async def test_a_reply_claiming_a_cancellation_that_did_not_happen_is_caught():
    problems = booking.unverified_claims(
        "That's cancelled for you.", appointment=None
    )
    assert problems


@pytest.mark.asyncio
async def test_a_true_confirmation_is_allowed_through(org_a, db_session):
    """The guard must not block the honest case, or the agent can never
    confirm anything and the feature is dead."""
    organization = await _org(db_session, org_a)
    organization.timezone = "America/New_York"
    organization.agent_config = {
        "business_hours": {"monday": {"open": "09:00", "close": "17:00"}},
        "appointments": {"enabled": True, "min_notice_minutes": 0},
    }
    contact = CRMContact(organization_id=organization.id, phone_number="+13055550101")
    db_session.add(contact)
    await db_session.flush()

    slots = await booking.free_slots(db_session, organization, days=14, limit=5)
    booked = await booking.book(db_session, organization, contact, slots[0], location=VISIT)
    appointment = getattr(booked, "appointment", None)
    assert appointment is not None

    assert (
        booking.unverified_claims(
            "You're booked in, see you then.", appointment=appointment
        )
        == []
    )


# ============================================== asking for a person, live
@pytest.mark.asyncio
async def test_asking_for_a_person_stops_the_agent_and_raises_an_alert(
    db_session, default_org, monkeypatch
):
    """The Beluga conversation, driven through the real inbound path.

    Both halves have to happen. The agent must stop, and somebody must be
    told - stopping silently leaves the customer waiting on nobody, which is
    the failure the alert exists to prevent.
    """
    from app.api.webhook import TwilioWebhookPayload, process_inbound_message
    from app.services.llm_service import GenerationResult
    from app.services.twilio_service import TwilioService

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_ack"

    async def fake_generate(org, contact, history, latest_message, **kwargs):
        # If this is ever called, the escalation did not short-circuit and the
        # agent is still answering somebody who asked for a person.
        raise AssertionError("the agent replied to a request for a person")

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    payload = TwilioWebhookPayload.model_validate(
        {
            "From": "whatsapp:+13055550111",
            "To": "whatsapp:+14155550000",
            "Body": "Connect me to a team member.",
            "MessageSid": "SM_esc_1",
            "ProfileName": "Irsa",
        }
    )
    result = await process_inbound_message(db_session, payload)

    assert result["status"] == "escalated"
    assert result["reason"]

    contact = (
        await db_session.execute(
            select(CRMContact).where(CRMContact.phone_number == "+13055550111")
        )
    ).scalar_one()
    assert contact.ai_enabled is False

    alerts = (
        await db_session.execute(
            select(Notification).where(Notification.event == "escalation")
        )
    ).scalars().all()
    assert alerts, "the agent went quiet and nobody was told"


@pytest.mark.asyncio
async def test_an_alert_is_stored_for_somebody_to_see(org_a, db_session):
    """An escalation nobody is told about is the agent going quiet."""
    from app.services import notifications

    organization = await _org(db_session, org_a)
    contact = CRMContact(organization_id=organization.id, phone_number="+13055550101")
    db_session.add(contact)
    await db_session.flush()

    await notifications.raise_and_send(
        db_session,
        organization,
        "escalation",
        "Someone needs a person",
        "Irsa said: Connect me to a team member.",
        contact_id=contact.id,
    )
    await db_session.flush()

    rows = (
        await db_session.execute(
            select(Notification).where(
                Notification.organization_id == organization.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].event == "escalation"
    assert "team member" in rows[0].body


# ================================================= messages in and out
@pytest.mark.asyncio
async def test_an_inbound_message_produces_a_stored_reply(db_session, monkeypatch):
    """Both directions land in the table the dashboard reads."""
    from app.api.webhook import TwilioWebhookPayload, process_inbound_message
    from app.services.llm_service import GenerationResult
    from app.services.twilio_service import TwilioService

    organization = Organization(name="Beluga", sales_prompt="Remodel bathrooms.")
    db_session.add(organization)
    await db_session.flush()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out_1"

    async def fake_generate(org, contact, history, latest_message, **kwargs):
        return GenerationResult(
            provider="groq",
            text="We do wet rooms across Miami-Dade. What size is the room?",
            prompt_used="prompt",
            latency_ms=120,
        )

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    payload = TwilioWebhookPayload.model_validate(
        {
            "From": "whatsapp:+13055550101",
            "To": "whatsapp:+14155550000",
            "Body": "Do you do wet rooms?",
            "MessageSid": "SM_in_1",
            "ProfileName": "Irsa",
        }
    )
    result = await process_inbound_message(db_session, payload)
    assert result["delivered"] is True

    messages = (await db_session.execute(select(Message))).scalars().all()
    senders = {m.sender for m in messages}
    assert senders == {"user", "agent"}
    inbound = next(m for m in messages if m.sender == "user")
    outbound = next(m for m in messages if m.sender == "agent")
    assert inbound.content == "Do you do wet rooms?"
    assert "wet rooms" in outbound.content
    assert outbound.delivery_status == "SENT"


@pytest.mark.asyncio
async def test_a_failed_send_is_recorded_as_failed_not_as_sent(
    db_session, default_org, monkeypatch
):
    """A message the dashboard shows as delivered, that was not, is how a
    business finds out from the customer."""
    from app.api.webhook import TwilioWebhookPayload, process_inbound_message
    from app.services.llm_service import GenerationResult
    from app.services.twilio_service import TwilioService

    async def failing_send(self, to_number, body, media_urls=None, sender=None):
        return False, None

    async def fake_generate(org, contact, history, latest_message, **kwargs):
        return GenerationResult(
            provider="groq", text="Here you go.", prompt_used="p", latency_ms=90
        )

    monkeypatch.setattr(TwilioService, "send_whatsapp", failing_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    payload = TwilioWebhookPayload.model_validate(
        {
            "From": "whatsapp:+13055550109",
            "To": "whatsapp:+14155550000",
            "Body": "Hello",
            "MessageSid": "SM_in_2",
        }
    )
    await process_inbound_message(db_session, payload)

    outbound = (
        await db_session.execute(select(Message).where(Message.sender == "agent"))
    ).scalars().all()
    assert outbound
    assert all(m.delivery_status != "SENT" for m in outbound)


# ============================== what a reply is allowed to claim, widened
# A probe of two dozen phrasings a model actually produces found eighteen
# walking straight through the first version of these guards. Each one is the
# 1am incident waiting to happen with different words, and the guards are the
# last thing between a confident sentence and a customer who believes it.
CLAIMS_A_BOOKING = (
    "You're all set for Tuesday",
    "I booked you in for Tuesday",
    "That's booked for you",
    "Done, booked",
    "I've booked you in",
    "See you on Tuesday",
    "I reserved Tuesday 10am for you",
    "Great, locked in for Tuesday",
)

CLAIMS_A_CANCELLATION = (
    "I have cancelled the September 19 appointment",
    "That's cancelled for you",
    "I cancelled it",
    "Cancelled!",
    "Your Tuesday slot is cancelled",
    "Done, cancelled",
    "No problem, I have called it off",
)

CLAIMS_A_MOVE = (
    "I have moved it to Wednesday",
    "That's moved to Wednesday",
    "I rescheduled you",
    "Moved to Wednesday",
    "Your appointment is now Wednesday",
)

# Honest replies the agent has to stay free to write. A guard that blocks
# these is a guard somebody turns off, and then it protects nothing.
HONEST = (
    "Would you like me to book you in?",
    "I can book you in for Tuesday",
    "Shall I cancel that?",
    "Do you want me to move it?",
    "Tuesday at 10 is available",
    "We are fully booked on Tuesday",
    "I could not book that time",
    "That time is not available",
    "Happy to cancel that for you",
    "Nothing has been cancelled",
    "No appointment has been cancelled",
    "I was not able to book that",
)


@pytest.mark.parametrize("text", CLAIMS_A_BOOKING)
def test_a_sentence_asserting_a_booking_is_caught(text):
    assert booking.claims_appointment(text), text
    assert booking.unverified_claims(text, appointment=None), text


@pytest.mark.parametrize("text", CLAIMS_A_CANCELLATION)
def test_a_sentence_asserting_a_cancellation_is_caught(text):
    assert booking.claims_cancellation(text), text
    assert booking.unverified_claims(text, appointment=None), text


@pytest.mark.parametrize("text", CLAIMS_A_MOVE)
def test_a_sentence_asserting_a_move_is_caught(text):
    assert booking.claims_reschedule(text), text


@pytest.mark.parametrize("text", HONEST)
def test_an_honest_reply_is_not_blocked(text):
    assert booking.claims_appointment(text) is None, text
    assert booking.claims_cancellation(text) is None, text
    assert booking.claims_reschedule(text) is None, text


def test_a_denial_is_not_a_claim():
    """"Nothing has been cancelled" is what the agent should say when nothing
    has. Reading it as a cancellation would block the correction and leave the
    customer holding the original mistake."""
    assert booking.claims_cancellation("Nothing has been cancelled") is None
    # But a negation elsewhere in the sentence does not excuse the claim.
    assert booking.claims_cancellation("I cancelled Tuesday, not Wednesday")


# ============================ what a document is allowed to put in a list
def test_a_trailing_sentence_is_not_a_place_the_business_serves():
    """Found end to end: "Delivery of fittings takes 2-3 working days." sits
    under the areas list in a real handbook and was read as an area.

    Worse than the stray entry: its presence made the section two items long,
    which stopped the comma-splitting from running at all, so the list read
    "Miami-Dade, Broward, Palm Beach" as one single place.
    """
    from app.services import document_facts

    found = document_facts.extract(
        "Areas we serve\n"
        "Miami-Dade, Broward, Palm Beach\n"
        "\n"
        "Delivery of fittings takes 2-3 working days.\n"
    )
    assert found["service_areas"] == ["Miami-Dade", "Broward", "Palm Beach"]


def test_a_price_is_not_split_on_its_thousands_separator():
    """"Wet room conversion - from $9,500" is one service. Splitting it
    produces a service called "500"."""
    from app.services import document_facts

    found = document_facts.extract(
        "Services\nWet room conversion - from $9,500\n"
    )
    assert found["services"] == ["Wet room conversion - from $9,500"]


def test_a_short_entry_ending_in_a_full_stop_is_still_an_entry():
    """The prose test needs both signals. Length alone judged "Wet room
    conversion - from $9,500" to be a sentence and threw away every service
    in the document."""
    from app.services import document_facts

    found = document_facts.extract("Services\nWet rooms.\nTiling.\n")
    assert found["services"] == ["Wet rooms", "Tiling"]
