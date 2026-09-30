"""What the chat in the screenshots got wrong, answered properly."""

import pytest

from app.services import agent_config, analyzer, llm_service, sales_policy


@pytest.mark.parametrize("text", ["put me with team", "put me with the team", "put me in touch with someone"])
def test_asking_for_the_team_is_a_hand_over(text):
    assert agent_config.needs_escalation(text)


def test_the_model_s_reading_of_a_request_for_a_person_is_kept():
    kept = analyzer._coerce({"intent": "other", "stage": "NEW", "wants_person": True}, "insaan se baat karao", "NEW")
    assert kept["wants_person"] is True
    assert analyzer._coerce({"intent": "other", "stage": "NEW"}, "hello", "NEW")["wants_person"] is False


@pytest.mark.parametrize("text", ["Ok", "ok thanks", "👍", "theek hai", "Thanks so much!"])
async def test_an_acknowledgement_is_never_passed_to_the_team(text, monkeypatch):
    async def says_needs_team(prompt):
        return "NEEDS_TEAM: ok"

    monkeypatch.setattr(llm_service, "_call_groq", says_needs_team)
    result = await llm_service.generate_reply(None, None, [], text, last_resort="")
    assert result.needs_team is None
    assert "good question" not in result.text.lower() and "team" not in result.text.lower()


async def test_with_the_ai_down_an_acknowledgement_still_gets_a_kind_reply(monkeypatch):
    async def down(prompt):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(llm_service, "_call_groq", down)
    monkeypatch.setattr(llm_service, "_call_gemini", down)
    result = await llm_service.generate_reply(None, None, [], "Ok", last_resort="")
    assert result.needs_team is None and result.text.startswith("Great")


def test_a_real_question_is_not_an_acknowledgement():
    for text in ("ok but how much is it?", "ok send the price list", "what?"):
        assert not sales_policy.just_acknowledging(text)


def test_no_photos_is_said_plainly_and_answered():
    lines = sales_policy.directives({"wants_images": True, "stage": "NEW"}, photos_available=False, photos_attached=False)
    joined = " ".join(lines)
    assert "no product photos" in joined and "name the products" in joined
    assert "being attached" not in joined


def test_a_booked_demo_does_not_hold_back_every_later_answer():
    analysis = {"intent": "book_call", "next_action": "book_call", "stage": "NEW"}
    held = sales_policy.directives(analysis, about_booking=True)
    free = sales_policy.directives(analysis, about_booking=False)
    assert any("Do NOT list products" in line for line in held)
    assert not any("Do NOT list products" in line for line in free)


def test_the_agent_is_told_to_read_people_as_they_write():
    prompt = llm_service.build_prompt(None, None, [], "wat u sell")
    assert "HOW TO READ THE CUSTOMER" in prompt and "typos" in prompt


async def test_a_request_for_a_person_the_keywords_missed_still_hands_over(db_session, monkeypatch):
    from app.api.webhook import process_inbound_message
    from app.models import CRMContact, Organization
    from app.schemas import TwilioWebhookPayload
    from app.services import notifications
    from app.services.twilio_service import TwilioService
    from sqlalchemy import select

    organization = Organization(name="Tallybird POS", sales_prompt="POS software.")
    db_session.add(organization)
    await db_session.flush()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out"

    async def reads_it(history, message, stage):
        return {**analyzer.heuristic_analysis(message, stage), "wants_person": True}

    raised = []

    async def record(db, org, event, title, body, contact_id=None):
        raised.append(event)

    async def generate(*_a, **_k):
        raise AssertionError("the agent answered a request for a person")

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.analyzer.analyse", reads_it)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", generate)
    monkeypatch.setattr(notifications, "raise_and_send", record)

    payload = TwilioWebhookPayload.model_validate(
        {"From": "whatsapp:+923009990000", "To": "whatsapp:+16602075318",
         "Body": "bhai kisi banday se baat karwa do", "MessageSid": "SMp1"}
    )
    result = await process_inbound_message(db_session, payload)
    assert result["status"] == "escalated"
    assert "escalation" in raised
    contact = (await db_session.execute(select(CRMContact))).scalars().first()
    assert contact.ai_enabled is False
