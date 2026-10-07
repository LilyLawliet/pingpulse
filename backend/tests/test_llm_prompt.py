"""The dynamic prompt builder and the Groq -> Gemini fallback chain."""

from types import SimpleNamespace

import httpx
import pytest

from app.services import llm_service
from app.services.llm_service import build_prompt, format_history, generate_reply

ORG = SimpleNamespace(
    name="Acme Solar",
    sales_prompt="You sell residential solar installs.",
    target_tone="Professional, energetic, persuasive",
    product_rules="Installs from $8,000. Never quote below $8,000.",
)

CONTACT = SimpleNamespace(
    name="Dana", phone_number="+15551230000", pipeline_stage="QUALIFIED"
)

HISTORY = [
    SimpleNamespace(sender="user", content="Do you install in Dubai?"),
    SimpleNamespace(sender="agent", content="Yes, we cover all of the UAE."),
]


# ------------------------------ assembly ----------------------------------
def test_prompt_contains_every_blueprint_section():
    prompt = build_prompt(ORG, CONTACT, HISTORY, "What does it cost?")

    for header in (
        "=== BUSINESS RULES ===",
        "=== CUSTOMER METADATA ===",
        "=== CHAT HISTORY ===",
        "=== LATEST CUSTOMER MESSAGE ===",
    ):
        assert header in prompt


def test_prompt_sections_appear_in_blueprint_order():
    """Organization Prompt + Customer Metadata + Chat History + Latest Message."""
    prompt = build_prompt(ORG, CONTACT, HISTORY, "What does it cost?")

    order = [
        prompt.index("=== BUSINESS RULES ==="),
        prompt.index("=== CUSTOMER METADATA ==="),
        prompt.index("=== CHAT HISTORY ==="),
        prompt.index("=== LATEST CUSTOMER MESSAGE ==="),
    ]
    assert order == sorted(order)


def test_prompt_merges_organization_rules():
    prompt = build_prompt(ORG, CONTACT, HISTORY, "hi")

    assert "Acme Solar" in prompt
    assert "You sell residential solar installs." in prompt
    assert "Professional, energetic, persuasive" in prompt
    assert "Never quote below $8,000." in prompt


def test_prompt_merges_contact_metadata():
    prompt = build_prompt(ORG, CONTACT, HISTORY, "hi")

    assert "Name: Dana" in prompt
    assert "Phone: +15551230000" in prompt
    assert "Current pipeline stage: QUALIFIED" in prompt


def test_prompt_merges_chat_history_with_roles():
    prompt = build_prompt(ORG, CONTACT, HISTORY, "What does it cost?")

    assert "Customer: Do you install in Dubai?" in prompt
    assert "Agent: Yes, we cover all of the UAE." in prompt
    assert "What does it cost?" in prompt


def test_history_precedes_the_latest_message():
    prompt = build_prompt(ORG, CONTACT, HISTORY, "What does it cost?")

    assert prompt.index("Do you install in Dubai?") < prompt.index("=== LATEST CUSTOMER MESSAGE ===")


def test_empty_history_is_labelled_not_blank():
    prompt = build_prompt(ORG, CONTACT, [], "First message")

    assert "no prior messages" in prompt


def test_missing_organization_falls_back_to_default_prompt():
    prompt = build_prompt(None, CONTACT, [], "hello")

    assert llm_service.DEFAULT_SALES_PROMPT in prompt
    assert "the business" in prompt


def test_format_history_maps_sender_to_role():
    assert format_history(HISTORY) == (
        "Customer: Do you install in Dubai?\nAgent: Yes, we cover all of the UAE."
    )


# ------------------------------ fallback ----------------------------------
@pytest.mark.asyncio
async def test_groq_success_skips_gemini(monkeypatch):
    async def ok_groq(prompt):
        return "Groq reply"

    async def boom_gemini(prompt):
        raise AssertionError("Gemini must not be called when Groq succeeds")

    monkeypatch.setattr(llm_service, "_call_groq", ok_groq)
    monkeypatch.setattr(llm_service, "_call_gemini", boom_gemini)

    result = await generate_reply(ORG, CONTACT, HISTORY, "hi")

    assert result.provider == "groq"
    assert result.text == "Groq reply"
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_groq_rate_limit_falls_back_to_gemini(monkeypatch):
    async def rate_limited(prompt):
        raise httpx.HTTPStatusError(
            "429 Too Many Requests",
            request=httpx.Request("POST", llm_service.GROQ_URL),
            response=httpx.Response(429),
        )

    async def ok_gemini(prompt):
        return "Gemini reply"

    monkeypatch.setattr(llm_service, "_call_groq", rate_limited)
    monkeypatch.setattr(llm_service, "_call_gemini", ok_gemini)

    result = await generate_reply(ORG, CONTACT, HISTORY, "hi")

    assert result.provider == "gemini"
    assert result.text == "Gemini reply"
    assert result.fallback_used is True
    assert "groq:" in result.error


@pytest.mark.asyncio
async def test_both_providers_down_returns_safe_holding_reply(monkeypatch):
    async def down(prompt):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(llm_service, "_call_groq", down)
    monkeypatch.setattr(llm_service, "_call_gemini", down)

    result = await generate_reply(ORG, CONTACT, HISTORY, "hi")

    # The customer must still get an answer rather than silence.
    assert result.provider == "none"
    assert result.text
    assert "groq:" in result.error and "gemini:" in result.error


@pytest.mark.asyncio
async def test_generation_result_carries_the_prompt_that_was_used(monkeypatch):
    async def ok_groq(prompt):
        return "reply"

    monkeypatch.setattr(llm_service, "_call_groq", ok_groq)

    result = await generate_reply(ORG, CONTACT, HISTORY, "What does it cost?")

    assert "=== BUSINESS RULES ===" in result.prompt_used
    assert "What does it cost?" in result.prompt_used


# --------------------------- truncation guard ------------------------------
@pytest.mark.asyncio
async def test_truncated_groq_reply_falls_back(monkeypatch):
    """A half-written sentence must never be sent to a customer."""

    async def truncated(prompt):
        raise RuntimeError("Groq reply was truncated or empty (token budget exhausted)")

    async def ok_gemini(prompt):
        return "A complete reply."

    monkeypatch.setattr(llm_service, "_call_groq", truncated)
    monkeypatch.setattr(llm_service, "_call_gemini", ok_gemini)

    result = await generate_reply(ORG, CONTACT, HISTORY, "hi")

    assert result.provider == "gemini"
    assert result.text == "A complete reply."


def test_token_budget_exceeds_a_reply_length_and_no_more():
    """Room to think and to write a short reply - and not the minute's pool.

    Reasoning models spend this budget thinking before writing anything, so
    it is well above a 45-word reply. Groq also reserves all of it against
    the per-minute token limit, so it is not set higher than that needs.
    """
    from app.services import understanding

    assert 1024 <= llm_service.MAX_OUTPUT_TOKENS <= 1536
    assert understanding.MESSAGE_OUTPUT_TOKENS <= 1024
    assert understanding.DOCUMENT_OUTPUT_TOKENS >= 4096
