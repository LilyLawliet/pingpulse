"""Customer memory: facts survive the chat-history window and get reused.

The failure this exists to prevent: a customer says "I'm in Multan, size 44",
the conversation runs past CHAT_HISTORY_LIMIT, and the agent asks again.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.webhook import process_inbound_message
from app.models import Contact, Organization
from app.schemas import GenerationResult, TwilioWebhookPayload
from app.services import llm_service
from app.services.llm_service import build_prompt, extract_profile

ORG = SimpleNamespace(
    name="Irsa's shoe shop",
    sales_prompt="Sell shoes.",
    target_tone="Warm",
    product_rules="Brogue tan PKR 15,500.",
)


def contact(**overrides):
    base = dict(
        name="Bilal",
        phone_number="+923009900777",
        pipeline_stage="LEAD",
        city=None,
        shoe_size=None,
        category_interest=None,
        colour_preference=None,
        budget_note=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------- prompt injection ------------------------------
def test_known_facts_appear_in_the_prompt():
    prompt = build_prompt(
        ORG,
        contact(city="Multan", shoe_size="44", category_interest="men's formals"),
        [],
        "I'll take the Brogue in tan.",
    )

    assert "City: Multan" in prompt
    assert "Shoe size: 44" in prompt
    assert "Shopping for: men's formals" in prompt


def test_known_facts_carry_a_do_not_ask_again_instruction():
    prompt = build_prompt(ORG, contact(city="Multan"), [], "I'll take it.")

    assert "do NOT ask for any of these again" in prompt


def test_nothing_known_adds_no_memory_block():
    prompt = build_prompt(ORG, contact(), [], "Hello")

    assert "Known from earlier" not in prompt


def test_memory_survives_an_empty_history():
    """The whole point: history can be truncated away, memory still applies."""
    prompt = build_prompt(ORG, contact(city="Multan", shoe_size="44"), [], "I'll take the tan one.")

    assert "no prior messages" in prompt  # history really is empty
    assert "Multan" in prompt and "44" in prompt


# ----------------------------- extraction ----------------------------------
@pytest.mark.asyncio
async def test_extract_parses_plain_json(monkeypatch):
    async def fake(prompt):
        return '{"city": "Multan", "shoe_size": "44", "category_interest": "men\'s formals", "colour_preference": null, "budget_note": null}'

    monkeypatch.setattr(llm_service, "_call_groq", fake)

    assert await extract_profile([], "I'm in Multan, size 44") == {
        "city": "Multan",
        "shoe_size": "44",
        "category_interest": "men's formals",
    }


@pytest.mark.asyncio
async def test_extract_survives_code_fences_and_prose(monkeypatch):
    async def fake(prompt):
        return 'Sure!\n```json\n{"city": "Lahore", "shoe_size": null}\n```'

    monkeypatch.setattr(llm_service, "_call_groq", fake)

    assert await extract_profile([], "I'm in Lahore") == {"city": "Lahore"}


@pytest.mark.asyncio
async def test_extract_drops_placeholder_values(monkeypatch):
    async def fake(prompt):
        return '{"city": "unknown", "shoe_size": "  ", "category_interest": "N/A", "colour_preference": "black"}'

    monkeypatch.setattr(llm_service, "_call_groq", fake)

    assert await extract_profile([], "black please") == {"colour_preference": "black"}


@pytest.mark.asyncio
async def test_extract_ignores_unexpected_keys(monkeypatch):
    async def fake(prompt):
        return '{"city": "Karachi", "credit_card": "4111111111111111"}'

    monkeypatch.setattr(llm_service, "_call_groq", fake)

    assert await extract_profile([], "hi") == {"city": "Karachi"}


@pytest.mark.asyncio
async def test_extract_returns_empty_when_the_provider_fails(monkeypatch):
    async def boom(prompt):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_service, "_call_groq", boom)

    assert await extract_profile([], "anything") == {}


@pytest.mark.asyncio
async def test_extract_returns_empty_on_non_json(monkeypatch):
    async def fake(prompt):
        return "I could not find any details."

    monkeypatch.setattr(llm_service, "_call_groq", fake)

    assert await extract_profile([], "anything") == {}


# ------------------------------ pipeline -----------------------------------
@pytest.mark.asyncio
async def test_learned_facts_are_persisted_on_the_contact(db_session, monkeypatch):
    organization = Organization(name="Irsa's shoe shop", sales_prompt="Sell shoes.")
    db_session.add(organization)
    await db_session.flush()

    from app.services.twilio_service import TwilioService

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_mem"

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)

    async def fake_generate(org, c, history, latest, **kwargs):
        return GenerationResult(provider="groq", text="Sure.", prompt_used="p", latency_ms=5)

    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    async def fake_extract(history, latest):
        return {"city": "Multan", "shoe_size": "44"}

    monkeypatch.setattr("app.api.webhook.llm_service.extract_profile", fake_extract)

    payload = TwilioWebhookPayload.model_validate(
        {"From": "whatsapp:+923009900777", "Body": "I'm in Multan and wear size 44", "ProfileName": "Bilal"}
    )
    await process_inbound_message(db_session, payload)

    saved = (
        await db_session.execute(
            select(Contact).where(Contact.phone_number == "+923009900777")
        )
    ).scalar_one()
    assert saved.city == "Multan"
    assert saved.shoe_size == "44"


@pytest.mark.asyncio
async def test_extraction_failure_does_not_break_the_reply(db_session, default_org, monkeypatch):
    from app.services.twilio_service import TwilioService

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_ok"

    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)

    async def fake_generate(org, c, history, latest, **kwargs):
        return GenerationResult(provider="groq", text="Reply sent.", prompt_used="p", latency_ms=5)

    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    async def failing_extract(history, latest):
        return {}

    monkeypatch.setattr("app.api.webhook.llm_service.extract_profile", failing_extract)

    payload = TwilioWebhookPayload.model_validate(
        {"From": "whatsapp:+923009900778", "Body": "hello", "ProfileName": "X"}
    )
    result = await process_inbound_message(db_session, payload)

    assert result["delivered"] is True
    assert result["reply"] == "Reply sent."




# ---------------------------------------------------------------------------
# Regressions found while recording the lifecycle demo.
# ---------------------------------------------------------------------------


def test_normalise_does_not_alias_the_dict_it_was_given():
    """Memory updates must not mutate the value SQLAlchemy loaded.

    `contact.memory` is a plain JSON column with no mutation tracking. When
    `normalise` aliased the nested containers, appending a rejection also
    changed the loaded value, the before and after images compared equal, and
    the UPDATE was never emitted — so nothing after the first message was ever
    saved.
    """
    from app.services import customer_memory

    loaded = customer_memory.reject_item(customer_memory.empty(), "green")

    updated = customer_memory.apply_analysis(loaded, {"rejected_items": ["red"]})

    assert updated["rejected_items"] == ["green", "red"]
    # The dict we were handed is untouched, so the two really do differ.
    assert loaded["rejected_items"] == ["green"]
    assert updated["rejected_items"] is not loaded["rejected_items"]
    assert updated["facts"] is not loaded["facts"]


def test_apply_analysis_leaves_a_new_object_each_time():
    """Two rounds in a row must each produce a distinguishable value."""
    from app.services import customer_memory

    first = customer_memory.apply_analysis(
        customer_memory.empty(), {"new_requirements": ["blue"]}
    )
    second = customer_memory.apply_analysis(first, {"new_requirements": ["cotton"]})

    assert set(first["requirements"]) == {"blue"}
    assert set(second["requirements"]) == {"blue", "cotton"}


@pytest.mark.parametrize(
    "message, wanted, rejected",
    [
        ("i don't like red, show me blue.", "blue", ["red"]),
        # The mirror image. Reading COLOUR_WORDS in tuple order rejected red
        # here — the colour the customer had just asked for.
        ("i don't like blue, show me red.", "red", ["blue"]),
        ("not a fan of green, do you have black?", "black", ["green"]),
        # Roman Urdu puts the dislike after the colour.
        ("red pasand nahi, blue dikhayen", "blue", ["red"]),
        ("show me blue", "blue", []),
        ("i don't like red", None, ["red"]),
        ("kuch aur dikhayen", None, []),
    ],
)
def test_read_colours_pairs_each_colour_with_its_own_clause(message, wanted, rejected):
    from app.services.analyzer import read_colours

    assert read_colours(message) == (wanted, rejected)


def test_heuristic_keeps_the_colour_that_was_asked_for():
    """A compound message names a dislike and a request; both must survive."""
    from app.services.analyzer import heuristic_analysis

    analysis = heuristic_analysis("I don't like blue, show me red.")

    assert analysis["rejected_items"] == ["blue"]
    assert analysis["colour_preference"] == "red"
