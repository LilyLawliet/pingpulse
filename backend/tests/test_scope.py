"""The scope check: work and places a business does not take get no times.

From the October 3 regression run, where a Miami remodeller with its services
and areas left blank read back - and on a yes, booked - a site visit for dog
grooming and one for a roof in Seattle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import CRMContact, Organization
from app.services import booking, scope, understanding

MIAMI = ZoneInfo("America/New_York")
ADDRESS = "100 Test Avenue, Miami FL 33101"


def weekday_ahead(days: int = 3):
    day = datetime.now(timezone.utc).astimezone(MIAMI).date() + timedelta(days=days)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day


@pytest.fixture
async def remodeller(db_session):
    shop = Organization(
        name="Constrivo Group",
        sales_prompt="Residential and commercial remodeling in Miami / South Florida.",
    )
    shop.timezone = "America/New_York"
    shop.agent_config = {
        "business_hours": {
            d: {"open": "09:00", "close": "20:00"}
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
        "appointments": {"min_notice_minutes": 0},
    }
    db_session.add(shop)
    await db_session.flush()
    contact = CRMContact(
        organization_id=shop.id, phone_number="13055550111", name="Test",
        pipeline_stage="NEW_LEAD", qualification={}, contact_metadata={"visit_address": ADDRESS},
    )
    db_session.add(contact)
    await db_session.flush()
    return shop, contact


def model_says(monkeypatch, answer, seen=None):
    async def structured(prompt, timeout):
        if seen is not None:
            seen.append(prompt)
        return answer

    monkeypatch.setattr(understanding, "structured", structured)


def test_the_business_speaks_for_itself():
    shop = Organization(name="x", sales_prompt="Remodeling in Miami.")
    shop.agent_config = {
        "services": ["Kitchens", "Roofing"],
        "from_document": {"fields": {"service_areas": ["Miami-Dade", "Broward"]}},
    }
    said = scope.what_the_business_says(shop)
    assert "Services it offers: Kitchens; Roofing" in said
    assert "Areas, as its documents state them: Miami-Dade; Broward" in said
    assert "In its own description: Remodeling in Miami." in said


@pytest.mark.asyncio
async def test_nothing_to_judge_by_asks_no_model(monkeypatch):
    seen = []
    model_says(monkeypatch, {"service_fits": False}, seen)
    verdict = await scope.check(Organization(name="x", sales_prompt=""), "dog grooming please")
    assert not verdict.known and seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, {}, {"service_fits": "no"}, ["false"]])
async def test_an_unclear_answer_refuses_nothing(monkeypatch, answer):
    model_says(monkeypatch, answer)
    verdict = await scope.check(Organization(name="x", sales_prompt="Remodeling."), "anything")
    assert verdict.service_fits is None and verdict.area_fits is None


@pytest.mark.asyncio
async def test_dog_grooming_gets_no_times(remodeller, db_session, monkeypatch):
    shop, contact = remodeller
    model_says(monkeypatch, {"job": "dog grooming", "service_fits": False})
    day = weekday_ahead()
    turn = await booking.handle_turn(
        db_session, shop, contact, f"Can you book dog grooming for {day:%B} {day.day} at 10 AM?"
    )
    assert turn.refusal.reason == "outside_services"
    assert turn.offered == [] and turn.proposed is None


@pytest.mark.asyncio
async def test_seattle_gets_no_times_with_no_areas_listed(remodeller, db_session, monkeypatch):
    shop, contact = remodeller
    model_says(monkeypatch, {"job": "roof replacement", "service_fits": True, "place": "Seattle", "area_fits": False})
    turn = await booking.handle_turn(
        db_session, shop, contact, "I need a roof replacement at my home in Seattle, Washington. Can you book a visit?"
    )
    assert turn.refusal.reason == "outside_area"
    assert turn.offered == [] and turn.proposed is None


@pytest.mark.asyncio
async def test_the_shops_own_areas_decide_without_a_model(remodeller, db_session, monkeypatch):
    """A listed area is a string match. The model's view of the area is not asked for."""
    shop, contact = remodeller
    shop.agent_config = {**shop.agent_config, "service_areas": ["Miami", "331"]}
    model_says(monkeypatch, {"job": "kitchen", "service_fits": True, "place": "Miami", "area_fits": False})
    day = weekday_ahead()
    turn = await booking.handle_turn(db_session, shop, contact, f"Book a kitchen visit {day:%B} {day.day} at 10am")
    assert turn.proposed, turn.prompt_block


@pytest.mark.asyncio
async def test_asked_once_per_change_in_what_they_said(remodeller, db_session, monkeypatch):
    shop, contact = remodeller
    seen = []
    model_says(monkeypatch, {"job": "bathroom remodel", "service_fits": True}, seen)
    day = weekday_ahead()
    await booking.handle_turn(db_session, shop, contact, f"Book a bathroom remodel visit {day:%B} {day.day} at 10am")
    await booking.handle_turn(db_session, shop, contact, "yes")
    assert len(seen) == 1, "the yes asked the model again"


@pytest.mark.asyncio
async def test_the_yes_is_checked_again(remodeller, db_session, monkeypatch):
    """A reading that turns against the job between the read-back and the yes stops the booking."""
    shop, contact = remodeller
    model_says(monkeypatch, {"job": "bathroom remodel", "service_fits": True})
    day = weekday_ahead()
    proposal = await booking.handle_turn(db_session, shop, contact, f"Book a bathroom remodel visit {day:%B} {day.day} at 10am")
    assert proposal.proposed and "for bathroom remodel on " in proposal.reply

    held = dict(contact.contact_metadata[scope.SCOPE_KEY])
    held["verdict"] = '{"job": "dog grooming", "service_fits": false, "place": null, "area_fits": null}'
    contact.contact_metadata = {**contact.contact_metadata, scope.SCOPE_KEY: held}
    turn = await booking.handle_turn(db_session, shop, contact, "yes")
    assert turn.performed is None and turn.refusal.reason == "outside_scope"


@pytest.mark.asyncio
async def test_a_site_visit_needs_an_address_at_any_shop(remodeller, db_session, monkeypatch):
    """Not only at shops that set their appointment type: R12-R13 booked one with none."""
    shop, contact = remodeller
    contact.contact_metadata = {}
    model_says(monkeypatch, None)
    day = weekday_ahead()
    turn = await booking.handle_turn(
        db_session, shop, contact, f"Book a site visit {day:%B} {day.day} at 10am. I will not give an address."
    )
    assert turn.refusal.reason == "needs_address" and turn.proposed is None
    again = await booking.handle_turn(db_session, shop, contact, "yes")
    assert again.performed is None
    # And a shop that does not visit its customers says so.
    shop.agent_config = {**shop.agent_config, "appointments": {"min_notice_minutes": 0, "require_address": False}}
    assert not booking.requires_address(shop, "onsite")


# --------------------------------------------------------------- October 6
def test_the_state_backstop_reads_plainly():
    shop = Organization(name="x", sales_prompt="Construction and remodeling in Miami / South Florida.")
    assert scope.states_the_business_names(shop) == {"Florida"}
    assert scope.states_in("my home in Seattle, Washington") == {"Washington"}
    assert scope.states_in("400 Pine St, Seattle WA 98101") == {"Washington"}
    # Not where the job is, or not a state at all.
    assert scope.states_in("I live in California but the property is in Miami") == set()
    assert scope.states_in("100 Washington Ave, Miami Beach, FL 33139") == {"Florida"}


@pytest.mark.asyncio
async def test_seattle_is_refused_with_the_model_silent(remodeller, db_session, monkeypatch):
    shop, contact = remodeller
    contact.contact_metadata = {}
    model_says(monkeypatch, None)
    shop.sales_prompt = "Remodeling in Miami / South Florida."
    turn = await booking.handle_turn(
        db_session, shop, contact, "Roof replacement at my home in Seattle, Washington - can you book a visit?"
    )
    assert turn.refusal.reason == "outside_area" and turn.offered == []


@pytest.mark.asyncio
async def test_the_property_in_miami_is_not_refused_for_where_they_live(remodeller, db_session, monkeypatch):
    shop, contact = remodeller
    model_says(monkeypatch, None)
    turn = await booking.handle_turn(
        db_session, shop, contact, "I live in California but the property is in Miami. What times do you have?"
    )
    assert turn.offered, turn.prompt_block


# ================================ a question that is not a booking at all
# "Can you groom my dog this week?" names no time and asks for nothing to be
# booked, so nothing above ever looked at it. The analyzer read it as somebody
# wanting a person - which is what a model does with a request the agent
# plainly cannot serve - and the customer was offered a colleague for a
# question the shop answers itself in four words.
@pytest.mark.asyncio
async def test_work_the_business_does_not_do_is_named_outside_a_booking(
    remodeller, db_session, monkeypatch
):
    shop, _ = remodeller
    model_says(monkeypatch, {"job": "dog grooming", "service_fits": False})
    assert await booking.not_our_trade(db_session, shop, "Can you groom my dog this week?") == (
        "dog grooming"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"job": "bathroom remodel", "service_fits": True},
        {"job": "something", "service_fits": None},
        None,
    ],
)
async def test_anything_but_a_plain_no_is_still_the_persons_to_answer(
    remodeller, db_session, monkeypatch, answer
):
    """It fails open here too: only a clear no takes the handover away."""
    shop, _ = remodeller
    model_says(monkeypatch, answer)
    assert await booking.not_our_trade(db_session, shop, "Can you do my bathroom?") is None


@pytest.mark.asyncio
async def test_an_empty_message_asks_no_model(remodeller, db_session, monkeypatch):
    seen = []
    shop, _ = remodeller
    model_says(monkeypatch, {"service_fits": False}, seen)
    assert await booking.not_our_trade(db_session, shop, "   ") is None
    assert seen == []


@pytest.mark.asyncio
async def test_the_question_does_not_overwrite_a_booking_in_the_same_chat(
    remodeller, db_session, monkeypatch
):
    """Stateless on purpose: the verdict a yes is re-checked against is left alone."""
    shop, contact = remodeller
    model_says(monkeypatch, {"job": "bathroom remodel", "service_fits": True})
    day = weekday_ahead()
    await booking.handle_turn(
        db_session, shop, contact, f"Book a bathroom remodel visit {day:%B} {day.day} at 10am"
    )
    held = dict(contact.contact_metadata)
    model_says(monkeypatch, {"job": "dog grooming", "service_fits": False})
    await booking.not_our_trade(db_session, shop, "and can you groom my dog?")
    assert contact.contact_metadata == held


async def test_the_webhook_answers_it_instead_of_offering_a_colleague(db_session, monkeypatch):
    """End to end: the reading says "they want a person", the scope says "we do not
    do that", and the customer is told so rather than asked if they would like one."""
    from app.api.webhook import process_inbound_message
    from app.models import CRMContact, Organization
    from app.schemas import GenerationResult, TwilioWebhookPayload
    from app.services import analyzer
    from app.services.twilio_service import TwilioService

    organization = Organization(
        name="Constrivo Group",
        sales_prompt="Residential and commercial remodeling in Miami / South Florida.",
    )
    db_session.add(organization)
    await db_session.flush()
    db_session.add(
        CRMContact(
            organization_id=organization.id, phone_number="13055550222", name="Test",
            pipeline_stage="NEW_LEAD", qualification={}, contact_metadata={},
        )
    )
    await db_session.flush()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out"

    async def reads_it(history, message, stage):
        return {**analyzer.heuristic_analysis(message, stage), "wants_person": True}

    model_says(monkeypatch, {"job": "dog grooming", "service_fits": False})
    prompts = []

    async def generate(*args, **kwargs):
        prompts.append(kwargs.get("knowledge") or "")
        return GenerationResult(
            provider="test", text="We don't do dog grooming.", prompt_used="", latency_ms=1
        )

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.analyzer.analyse", reads_it)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", generate)

    payload = TwilioWebhookPayload.model_validate(
        {"From": "whatsapp:+13055550222", "To": "whatsapp:+16602075318",
         "Body": "Can you groom my dog this week?", "MessageSid": "SMdog1"}
    )
    result = await process_inbound_message(db_session, payload)
    assert result.get("status") != "asked_about_handover", result
    assert prompts, "no reply was generated"
    assert any("NOT something this business does" in str(p) for p in prompts), prompts
