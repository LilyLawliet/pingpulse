"""A question nothing answers, answered quickly and honestly.

Customers probe: "do you ship to Dubai?", "is this BPA-free?". The agent used
to be forbidden to say "our team will get back to you" and had nothing else,
so it tried, was refused, fell through to the second provider and replied
thirty seconds later with "could you tell me a little more?".
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import settings
from app.models import CRMContact, KnowledgeDocument, Organization
from app.services import llm_service, notifications, sales_policy, unanswered


@pytest.mark.asyncio
async def test_saying_it_does_not_know_is_one_call_not_four(monkeypatch):
    calls = []

    async def groq(prompt):
        calls.append(prompt)
        return "NEEDS_TEAM: shipping to Dubai"

    async def gemini(prompt):
        raise AssertionError("the second provider was asked")

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    monkeypatch.setattr(llm_service, "_call_gemini", gemini)
    result = await llm_service.generate_reply(None, None, [], "do you ship to Dubai?")
    assert len(calls) == 1
    assert result.needs_team == "shipping to Dubai"
    assert "NEEDS_TEAM" not in result.text
    assert "NEEDS_TEAM" in calls[0], "the model was not told it may say so"


@pytest.mark.asyncio
async def test_a_slow_provider_does_not_hold_the_customer_past_the_deadline(monkeypatch):
    monkeypatch.setattr(settings, "reply_deadline_seconds", 1)
    gemini_asked = []

    async def slow(prompt):
        await asyncio.sleep(5)
        return "too late"

    async def gemini(prompt):
        gemini_asked.append(prompt)
        return "never"

    monkeypatch.setattr(llm_service, "_call_groq", slow)
    monkeypatch.setattr(llm_service, "_call_gemini", gemini)
    started = time.perf_counter()
    result = await llm_service.generate_reply(
        None, None, [], "20 m of cable?", last_resort="It comes as a 100 m coil at PKR 18,750."
    )
    assert time.perf_counter() - started < 2.5
    assert result.text == "It comes as a 100 m coil at PKR 18,750."
    assert not gemini_asked, "no time was left for a second provider"


@pytest.mark.asyncio
async def test_nothing_known_and_no_worked_answer_means_the_team_not_filler(monkeypatch):
    async def down(prompt):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(llm_service, "_call_groq", down)
    monkeypatch.setattr(llm_service, "_call_gemini", down)
    result = await llm_service.generate_reply(None, None, [], "is it BPA-free?")
    assert result.needs_team == "is it BPA-free?"


def test_filler_is_not_a_last_resort():
    filler = sales_policy.deterministic_reply({}, [], None, message="is it BPA-free?")
    assert sales_policy.without_filler(filler) == ""
    assert sales_policy.without_filler("Cable is PKR 18,750 a coil.") == "Cable is PKR 18,750 a coil."


# ---------------------------------------------------------------- what the customer hears
@pytest.fixture
async def shop(db_session):
    organization = Organization(name="Miku's Stationery", sales_prompt="Stationery.")
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(organization_id=organization.id, phone_number="923001234567", name="Irsa")
    db_session.add(contact)
    db_session.add(
        KnowledgeDocument(
            organization_id=organization.id,
            title="About us",
            content="Questions? Email hello@mikus.example or call +92 300 1234567.",
            source="about.docx",
            doc_type="policy",
        )
    )
    await db_session.flush()
    return organization, contact


@pytest.mark.asyncio
async def test_passed_to_the_team_only_when_somebody_was_told(shop, db_session, monkeypatch):
    organization, contact = shop
    raised = []

    async def record(db, org, event, title, body, contact_id=None):
        raised.append((event, body))

    async def yes(db, org):
        return True

    monkeypatch.setattr(notifications, "raise_and_send", record)
    monkeypatch.setattr(notifications, "can_reach", yes)
    reply = await unanswered.handle(db_session, organization, contact, "shipping to Dubai", "do you ship to Dubai?")
    assert "passed it to the team" in reply
    assert raised and raised[0][0] == "unanswered" and "Dubai" in raised[0][1]


@pytest.mark.asyncio
async def test_with_nobody_to_tell_it_gives_the_business_s_own_contact(shop, db_session, monkeypatch):
    organization, contact = shop

    async def record(*_a, **_k):
        return None

    async def no(db, org):
        return False

    monkeypatch.setattr(notifications, "raise_and_send", record)
    monkeypatch.setattr(notifications, "can_reach", no)
    reply = await unanswered.handle(db_session, organization, contact, "", "do you ship to Dubai?")
    assert "team" not in reply.split("reach")[0], "no promise that anybody will reply"
    assert "hello@mikus.example" in reply


@pytest.mark.asyncio
async def test_the_sandbox_says_what_a_live_chat_would_do(org_a, monkeypatch):
    async def groq(prompt):
        return "NEEDS_TEAM: shipping to Dubai"

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    response = await org_a.post("/api/v1/agent/simulate", json={"message": "do you ship to Dubai?"})
    body = response.json()
    assert body["needs_team"], body
    assert "NEEDS_TEAM" not in body["reply"]


@pytest.mark.asyncio
async def test_a_live_chat_alerts_the_team_and_sends_what_happened(db_session, monkeypatch):
    from sqlalchemy import select

    from app.api.webhook import process_inbound_message
    from app.models import Message
    from app.schemas import GenerationResult, TwilioWebhookPayload
    from app.services.twilio_service import TwilioService

    organization = Organization(name="Acme Solar", sales_prompt="Sell solar installs.")
    db_session.add(organization)
    await db_session.flush()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out_1"

    async def fake_generate(*_a, **_k):
        return GenerationResult(
            provider="groq", text=llm_service.DONT_KNOW, prompt_used="p", latency_ms=5,
            needs_team="net metering paperwork",
        )

    raised = []

    async def record(db, org, event, title, body, contact_id=None):
        raised.append(event)

    async def yes(db, org):
        return True

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)
    monkeypatch.setattr(notifications, "raise_and_send", record)
    monkeypatch.setattr(notifications, "can_reach", yes)

    payload = TwilioWebhookPayload.model_validate(
        {"From": "whatsapp:+15551230000", "To": "whatsapp:+16602075318",
         "Body": "Do you handle the net metering paperwork?", "MessageSid": "SMnm1"}
    )
    await process_inbound_message(db_session, payload)
    sent = (await db_session.execute(select(Message).where(Message.sender == "agent"))).scalars().all()
    assert "unanswered" in raised
    assert sent and "passed it to the team" in sent[-1].content


@pytest.mark.asyncio
async def test_the_shop_is_told_when_the_ai_stops_answering(db_session, monkeypatch):
    from app.api.webhook import process_inbound_message
    from app.schemas import GenerationResult, TwilioWebhookPayload
    from app.services.twilio_service import TwilioService

    organization = Organization(name="Acme Solar", sales_prompt="Sell solar installs.")
    db_session.add(organization)
    await db_session.flush()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out_2"

    async def fake_generate(*_a, **_k):
        return GenerationResult(
            provider="none", text="Panels are PKR 31,800 each.", prompt_used="p", latency_ms=5,
            fallback_used=True, error="groq: HTTP 503 | gemini: HTTP 500",
        )

    raised = []

    async def record(db, org, event, title, body, contact_id=None):
        raised.append(event)

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)
    monkeypatch.setattr(notifications, "raise_and_send", record)
    payload = TwilioWebhookPayload.model_validate(
        {"From": "whatsapp:+15551230009", "To": "whatsapp:+16602075318",
         "Body": "Price of the 550W panel?", "MessageSid": "SMai1"}
    )
    await process_inbound_message(db_session, payload)
    assert "ai_down" in raised
