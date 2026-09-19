"""Webhook parsing and the full inbound pipeline (Phases 2-4)."""

import pytest
from sqlalchemy import select

from app.api.webhook import evaluate_stage, process_inbound_message
from app.models import Contact, LLMLog, Message, Organization
from app.schemas import GenerationResult, TwilioWebhookPayload


TWILIO_FORM = {
    "MessageSid": "SM1234567890abcdef",
    "AccountSid": "ACtestaccountsid0000000000000000",
    "From": "whatsapp:+15551230000",
    "To": "whatsapp:+16602075318",
    "Body": "Hi, what does your solar install cost?",
    "ProfileName": "Dana",
    "WaId": "15551230000",
    "NumMedia": "0",
}


# ----------------------------- payload parsing -----------------------------
def test_payload_parses_twilio_pascal_case_keys():
    payload = TwilioWebhookPayload.model_validate(TWILIO_FORM)

    assert payload.message_sid == "SM1234567890abcdef"
    assert payload.body == "Hi, what does your solar install cost?"
    assert payload.profile_name == "Dana"
    assert payload.num_media == 0


def test_payload_strips_whatsapp_prefix():
    payload = TwilioWebhookPayload.model_validate(TWILIO_FORM)

    assert payload.clean_from == "+15551230000"
    assert payload.clean_to == "+16602075318"


def test_payload_tolerates_missing_optional_fields():
    payload = TwilioWebhookPayload.model_validate(
        {"From": "whatsapp:+15559999999", "Body": "hello"}
    )

    assert payload.clean_from == "+15559999999"
    assert payload.profile_name is None
    assert payload.message_sid == ""


def test_payload_ignores_unknown_twilio_fields():
    payload = TwilioWebhookPayload.model_validate(
        {**TWILIO_FORM, "SmsStatus": "received", "ApiVersion": "2010-04-01"}
    )

    assert payload.clean_from == "+15551230000"


async def test_webhook_endpoint_accepts_form_and_returns_twiml(client, monkeypatch):
    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out_1"

    from app.services.twilio_service import TwilioService

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)

    async def fake_generate(organization, contact, history, latest_message, **kwargs):
        return GenerationResult(
            provider="groq", text="Plans start at $8,000.", prompt_used="p", latency_ms=42
        )

    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    response = await client.post("/api/v1/whatsapp/webhook", data=TWILIO_FORM)

    assert response.status_code == 200
    assert "<Response></Response>" in response.text


async def test_webhook_returns_200_even_for_garbage_payload(client):
    response = await client.post("/api/v1/whatsapp/webhook", data={"Nonsense": "x"})

    # A non-2xx would make Twilio retry and double-send the reply.
    assert response.status_code == 200


# --------------------------- inbound pipeline ------------------------------
async def test_process_inbound_creates_contact_message_and_log(db_session, monkeypatch):
    organization = Organization(
        name="Acme Solar",
        sales_prompt="Sell solar installs.",
        target_tone="Professional, energetic",
    )
    db_session.add(organization)
    await db_session.flush()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out_42"

    from app.services.twilio_service import TwilioService

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)

    async def fake_generate(org, contact, history, latest_message, **kwargs):
        return GenerationResult(
            provider="groq",
            text="Installs start at $8,000 — want a survey booked?",
            prompt_used="assembled-prompt",
            latency_ms=310,
        )

    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    payload = TwilioWebhookPayload.model_validate(TWILIO_FORM)
    result = await process_inbound_message(db_session, payload)

    assert result["new_contact"] is True
    assert result["provider"] == "groq"
    assert result["delivered"] is True

    contacts = (await db_session.execute(select(Contact))).scalars().all()
    assert len(contacts) == 1
    assert contacts[0].phone_number == "+15551230000"
    assert contacts[0].name == "Dana"
    # Asking what something costs is a question, not a qualification. This
    # asserted QUALIFIED on the strength of the word "cost" appearing, which
    # is the same rule that marked a lead's estimate as scheduled because
    # they used the word "schedule". A new lead stays a new lead until the
    # configured qualification is actually answered.
    assert contacts[0].pipeline_stage == "NEW_LEAD"

    messages = (await db_session.execute(select(Message))).scalars().all()
    assert {m.sender for m in messages} == {"user", "agent"}

    logs = (await db_session.execute(select(LLMLog))).scalars().all()
    assert len(logs) == 1
    assert logs[0].provider == "groq"
    assert logs[0].latency_ms == 310


async def test_second_message_reuses_existing_contact(db_session, default_org, monkeypatch):
    from app.services.twilio_service import TwilioService

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out"

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)

    async def fake_generate(org, contact, history, latest_message, **kwargs):
        return GenerationResult(provider="groq", text="ok", prompt_used="p", latency_ms=10)

    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    payload = TwilioWebhookPayload.model_validate(TWILIO_FORM)
    first = await process_inbound_message(db_session, payload)

    # A genuinely different message, with its own id. Sending the identical
    # payload twice is a redelivery, not a second message, and is now
    # recognised as one - so reusing it here would test the wrong thing.
    again = TwilioWebhookPayload.model_validate(
        {**TWILIO_FORM, "MessageSid": "SM_second_message", "Body": "and another thing"}
    )
    second = await process_inbound_message(db_session, again)

    assert second["new_contact"] is False
    assert first["contact_id"] == second["contact_id"]

    contacts = (await db_session.execute(select(Contact))).scalars().all()
    assert len(contacts) == 1


async def test_dispatch_failure_still_persists_the_reply(db_session, default_org, monkeypatch):
    from app.services.twilio_service import TwilioService

    async def failing_send(self, to_number, body, media_urls=None, sender=None):
        return False, "Twilio 401 unauthenticated"

    monkeypatch.setattr(TwilioService, "send_whatsapp", failing_send)

    async def fake_generate(org, contact, history, latest_message, **kwargs):
        return GenerationResult(provider="groq", text="reply", prompt_used="p", latency_ms=5)

    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    payload = TwilioWebhookPayload.model_validate(TWILIO_FORM)
    result = await process_inbound_message(db_session, payload)

    assert result["delivered"] is False
    agent_messages = (
        (await db_session.execute(select(Message).where(Message.sender == "agent")))
        .scalars()
        .all()
    )
    assert len(agent_messages) == 1
    assert agent_messages[0].twilio_sid is None


# ---------------------------- stage progression ----------------------------
@pytest.mark.parametrize(
    "current,message,expected",
    [
        ("NEW_LEAD", "just saying hi", "NEW_LEAD"),
        # These three used to assert the opposite, and the opposite is what
        # the client reported. Asking a price is a question, booking a demo is
        # a request, and wanting to buy is an intention - none of them is an
        # appointment or a sale, and none of them may move the board on its
        # own any more.
        ("NEW_LEAD", "what is the price?", "NEW_LEAD"),
        ("NEW_LEAD", "can we book a demo", "NEW_LEAD"),
        ("QUALIFIED", "I want to buy", "QUALIFIED"),
        ("ESTIMATE_SCHEDULED", "how much again?", "ESTIMATE_SCHEDULED"),
        ("WON", "hello there", "WON"),
        # A contact on a board this ladder knows nothing about is left exactly
        # where their operator put them.
        ("BOOKED_IN", "I want to buy", "BOOKED_IN"),
    ],
)
def test_evaluate_stage_only_moves_forward(current, message, expected):
    assert evaluate_stage(current, message) == expected



