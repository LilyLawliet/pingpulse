"""Answering customers in their own language.

The prompt says to; these check what code can check: the script of a reply,
and that a translated sentence of the backend's own says the same figures.
"""

from __future__ import annotations

import pytest

from app.services import languages, llm_service, understanding


@pytest.mark.parametrize(
    "customer, reply, wrong",
    [
        ("كم سعر الدفتر؟", "The notebook is PKR 1,250.", True),
        ("كم سعر الدفتر؟", "الدفتر بسعر PKR 1,250", False),   # a price in Latin is fine
        ("नोटबुक कितने की है?", "Notebook PKR 1,250 ki hai", True),
        ("How much is it?", "यह 1,250 का है", True),
        ("¿Cuánto cuesta?", "Cuesta PKR 1,250.", False),       # same script: the model's call
        ("How much is it?", "It is PKR 1,250.", False),
    ],
)
def test_a_reply_in_the_wrong_script_is_caught(customer, reply, wrong):
    assert bool(languages.wrong_script(customer, reply)) is wrong


@pytest.mark.parametrize(
    "message, english",
    [
        ("How much is the notebook?", True),
        ("550W?", True),
        ("I want 20 gel pens", True),
        ("give me a discount", True),
        ("The Little Cat Café price", True),
        ("¿Hacéis envíos a Madrid?", False),
        ("Ich möchte zwei Hefte", False),
        ("Saya mau dua buku", False),
        ("Tem caderno rosa?", False),
        ("¿Cuánto cuesta el cuaderno?", False),
        ("Bonjour, je voudrais deux cahiers", False),
        ("kitne ka hai ye?", False),
        ("كم السعر", False),
    ],
)
def test_english_is_told_apart(message, english):
    assert languages.looks_english(message) is english


@pytest.mark.asyncio
async def test_the_model_is_asked_again_when_it_answers_arabic_in_english(monkeypatch):
    replies = iter(["The notebook is PKR 1,250.", "الدفتر بسعر PKR 1,250"])

    async def groq(prompt):
        return next(replies)

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    result = await llm_service.generate_reply(None, None, [], "كم سعر الدفتر؟", knowledge="PKR 1,250")
    assert result.text == "الدفتر بسعر PKR 1,250"


@pytest.mark.asyncio
async def test_the_backend_s_own_sentence_is_translated_with_its_figures_intact(monkeypatch):
    async def model(prompt, timeout):
        return {"text": "Le cahier coûte PKR 1,250. Voulez-vous le commander ?"}

    monkeypatch.setattr(understanding, "structured", model)
    out = await languages.in_customer_language(
        "The notebook is PKR 1,250. Shall I put that together for you?", "Bonjour, combien coûte le cahier ?"
    )
    assert out.startswith("Le cahier coûte PKR 1,250")


@pytest.mark.asyncio
async def test_a_translation_that_changes_a_figure_is_not_sent(monkeypatch):
    async def model(prompt, timeout):
        return {"text": "Le cahier coûte PKR 1,500."}

    monkeypatch.setattr(understanding, "structured", model)
    original = "The notebook is PKR 1,250."
    assert await languages.in_customer_language(original, "Bonjour, combien coûte le cahier ?") == original


@pytest.mark.asyncio
async def test_english_customers_cost_no_translation_call(monkeypatch):
    async def model(prompt, timeout):
        raise AssertionError("an English message was sent for translation")

    monkeypatch.setattr(understanding, "structured", model)
    assert await languages.in_customer_language("Hi there", "How much is the notebook?") == "Hi there"


@pytest.mark.asyncio
async def test_the_team_hand_over_reaches_a_spanish_speaker_in_spanish(db_session, monkeypatch):
    from sqlalchemy import select

    from app.api.webhook import process_inbound_message
    from app.models import Message, Organization
    from app.schemas import GenerationResult, TwilioWebhookPayload
    from app.services import notifications
    from app.services.twilio_service import TwilioService

    db_session.add(Organization(name="Tienda", sales_prompt="x"))
    await db_session.flush()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM1"

    async def fake_generate(*_a, **_k):
        return GenerationResult(provider="groq", text="x", prompt_used="p", latency_ms=1,
                                needs_team="envío a Madrid")

    async def yes(db, org):
        return True

    async def record(*_a, **_k):
        return None

    async def model(prompt, timeout):
        return {"text": "Buena pregunta: no tengo ese dato a mano. Se la he pasado al equipo y te responderán aquí."}

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)
    monkeypatch.setattr(notifications, "can_reach", yes)
    monkeypatch.setattr(notifications, "raise_and_send", record)
    monkeypatch.setattr(understanding, "structured", model)

    await process_inbound_message(db_session, TwilioWebhookPayload.model_validate(
        {"From": "whatsapp:+34600000000", "To": "whatsapp:+16602075318",
         "Body": "¿Hacéis envíos a Madrid?", "MessageSid": "SMes1"}
    ))
    sent = (await db_session.execute(select(Message).where(Message.sender == "agent"))).scalars().all()
    assert sent[-1].content.startswith("Buena pregunta")


# ------------------------------------------- a price is a price wherever it sits
def test_a_full_stop_after_a_price_is_not_part_of_it():
    """"Delivery is PKR 350." and "Delivery PKR 350 hai." are the same 350.

    Reading the full stop as part of the number made every rewrite that moved
    a price off the end of its sentence look like a changed price, and the
    customer was sent the English back.
    """
    english = "I've passed it to the team. Delivery is PKR 350. See https://shop.example/x"
    urdu = "Maine team ko bhej diya hai. Delivery PKR 350 hai. Dekhein https://shop.example/x."
    assert languages._figures(english) == languages._figures(urdu) == ["350"]
    assert languages._links(english) == languages._links(urdu)


def test_a_decimal_price_is_still_read_whole():
    assert languages._figures("PKR 1,250.50 and 8,500.") == ["1,250.50", "8,500"]


@pytest.mark.parametrize(
    "rewrite, kept",
    [
        ("Maine team ko bhej diya. Delivery PKR 350 hai.", True),
        ("Maine team ko bhej diya. Delivery PKR 450 hai.", False),   # changed
        ("Maine team ko bhej diya.", False),                          # dropped
        ("Maine team ko bhej diya. PKR 350, 10% off.", False),        # added
    ],
)
@pytest.mark.asyncio
async def test_a_rewrite_is_kept_only_when_every_figure_survives(monkeypatch, rewrite, kept):
    original = "I've passed it to the team. Delivery is PKR 350."

    async def answer(prompt, timeout):
        return {"text": rewrite}

    from app.services import understanding

    monkeypatch.setattr(understanding, "structured", answer)
    out = await languages.in_customer_language(original, "kitne ka hai?")
    assert (out != original) is kept, out
