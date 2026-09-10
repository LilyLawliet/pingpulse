"""Catalogue scoping, the price guardrail, and API-key rotation."""

from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.services import llm_service
from app.services.llm_service import (
    build_prompt,
    generate_reply,
    money_figures,
    scope_catalogue,
    split_catalogue,
    unsupported_prices,
)

CATALOGUE = """PRICE LIST
WOMEN'S HEELS
- Kitten Heel: black, nude - PKR 8,900
- Stiletto Classic: black, red - PKR 14,200
DESIGNER SNEAKERS
- Classic White - PKR 16,500
- Chunky Platform - PKR 24,500
MEN'S FORMAL
- Oxford: black, brown - PKR 12,900
- Brogue: tan - PKR 15,500
GENERAL
- Free delivery over PKR 15,000, otherwise PKR 350.
"""

ORG = SimpleNamespace(
    name="Irsa's shoe shop",
    sales_prompt="Sell shoes.",
    target_tone="Warm",
    product_rules=CATALOGUE,
)
CONTACT = SimpleNamespace(
    name="Hina",
    phone_number="+923337712004",
    pipeline_stage="LEAD",
    category_interest=None,
    city=None,
    shoe_size=None,
    colour_preference=None,
    budget_note=None,
)


# --------------------------- catalogue splitting ---------------------------
def test_split_finds_every_heading():
    sections = split_catalogue(CATALOGUE)

    for heading in ("PRICE LIST", "WOMEN'S HEELS", "DESIGNER SNEAKERS", "MEN'S FORMAL", "GENERAL"):
        assert heading in sections


def test_split_keeps_items_under_their_heading():
    sections = split_catalogue(CATALOGUE)

    assert any("Kitten Heel" in line for line in sections["WOMEN'S HEELS"])
    assert any("Brogue" in line for line in sections["MEN'S FORMAL"])


# ---------------------------- catalogue scoping ----------------------------
def test_scoping_keeps_the_category_being_discussed():
    scoped = scope_catalogue(CATALOGUE, "do you have heels in black?")

    assert "Kitten Heel" in scoped
    assert "Stiletto Classic" in scoped


def test_scoping_drops_detail_of_other_categories():
    scoped = scope_catalogue(CATALOGUE, "do you have heels in black?")

    assert "Chunky Platform" not in scoped
    assert "Brogue" not in scoped


def test_scoping_still_names_the_other_categories():
    """The agent must know they exist so it can offer to switch."""
    scoped = scope_catalogue(CATALOGUE, "do you have heels in black?")

    assert "DESIGNER SNEAKERS" in scoped
    assert "MEN'S FORMAL" in scoped


def test_general_terms_always_survive():
    scoped = scope_catalogue(CATALOGUE, "do you have heels in black?")

    assert "Free delivery over PKR 15,000" in scoped


def test_unclear_focus_keeps_the_whole_list():
    scoped = scope_catalogue(CATALOGUE, "hello there")

    assert "Kitten Heel" in scoped and "Chunky Platform" in scoped and "Brogue" in scoped


def test_list_without_headings_is_returned_whole():
    flat = "Kitten Heel PKR 8,900. Oxford PKR 12,900."

    assert scope_catalogue(flat, "heels") == flat


def test_prompt_uses_the_scoped_catalogue():
    prompt = build_prompt(ORG, CONTACT, [], "Are the heels leather?")

    assert "Kitten Heel" in prompt
    assert "Chunky Platform" not in prompt


# ------------------------------ price guard --------------------------------
def test_money_figures_are_normalised():
    assert money_figures("It is PKR 15,500 or $8,000.") == {"15500", "8000"}


def test_sizes_and_durations_are_not_treated_as_money():
    assert money_figures("size 44, hold for 48 hours, sizes 36-46") == set()


def test_a_listed_price_is_accepted():
    assert unsupported_prices("The Brogue is PKR 15,500.", CATALOGUE) == set()


def test_an_invented_price_is_caught():
    assert unsupported_prices("The Brogue is PKR 15,300.", CATALOGUE) == {"15300"}


def test_no_price_list_means_no_enforcement():
    assert unsupported_prices("It is PKR 99,999.", "") == set()


@pytest.mark.asyncio
async def test_bad_price_triggers_one_corrective_retry(monkeypatch):
    calls = []

    async def groq(prompt):
        calls.append(prompt)
        return "The Brogue is PKR 15,300." if len(calls) == 1 else "The Brogue is PKR 15,500."

    monkeypatch.setattr(llm_service, "_call_groq", groq)

    result = await generate_reply(ORG, CONTACT, [], "how much is the brogue?")

    assert result.text == "The Brogue is PKR 15,500."
    assert len(calls) == 2
    assert "CORRECTION" in calls[1]


@pytest.mark.asyncio
async def test_persistently_wrong_price_never_reaches_the_customer(monkeypatch):
    async def always_wrong(prompt):
        return "The Brogue is PKR 15,300."

    monkeypatch.setattr(llm_service, "_call_groq", always_wrong)
    monkeypatch.setattr(llm_service, "_call_gemini", always_wrong)

    result = await generate_reply(ORG, CONTACT, [], "how much?")

    assert "15,300" not in result.text
    assert result.provider == "none"


@pytest.mark.asyncio
async def test_guard_can_be_switched_off(monkeypatch):
    async def wrong(prompt):
        return "The Brogue is PKR 15,300."

    monkeypatch.setattr(llm_service, "_call_groq", wrong)
    monkeypatch.setattr(llm_service.settings, "price_guard_enabled", False)

    result = await generate_reply(ORG, CONTACT, [], "how much?")

    assert result.text == "The Brogue is PKR 15,300."


# ------------------------------ key rotation -------------------------------
def test_keys_are_collected_in_order_without_blanks_or_duplicates():
    # _env_file=None so the real .env cannot leak keys into the assertion.
    settings = Settings(
        _env_file=None,
        groq_api_key="a", groq_api_key_2="", groq_api_key_3="b", groq_api_key_4="a",
    )

    assert settings.groq_api_keys == ["a", "b"]


def test_no_keys_configured_reads_as_empty():
    assert Settings(_env_file=None, groq_api_key="", gemini_api_key="").groq_api_keys == []


@pytest.mark.asyncio
async def test_rate_limited_key_rotates_to_the_next(monkeypatch):
    used = []

    async def once(prompt, api_key):
        used.append(api_key)
        if api_key == "first":
            raise httpx.HTTPStatusError(
                "429",
                request=httpx.Request("POST", llm_service.GROQ_URL),
                response=httpx.Response(429),
            )
        return "reply from the second key"

    monkeypatch.setattr(llm_service.settings, "groq_api_key", "first")
    monkeypatch.setattr(llm_service.settings, "groq_api_key_2", "second")
    monkeypatch.setattr(llm_service, "_groq_once", once)

    assert await llm_service._call_groq("hi") == "reply from the second key"
    assert used == ["first", "second"]


@pytest.mark.asyncio
async def test_a_non_quota_error_does_not_burn_other_keys(monkeypatch):
    used = []

    async def once(prompt, api_key):
        used.append(api_key)
        raise httpx.HTTPStatusError(
            "401",
            request=httpx.Request("POST", llm_service.GROQ_URL),
            response=httpx.Response(401),
        )

    monkeypatch.setattr(llm_service.settings, "groq_api_key", "first")
    monkeypatch.setattr(llm_service.settings, "groq_api_key_2", "second")
    monkeypatch.setattr(llm_service, "_groq_once", once)

    with pytest.raises(httpx.HTTPStatusError):
        await llm_service._call_groq("hi")
    assert used == ["first"]


# --------------------- decimal / thousands normalisation --------------------
def test_catalogue_decimals_match_human_written_prices():
    """Feeds say "1596.00"; people write "Rs. 1,596". Same price."""
    catalogue = "Basic Shirt PKR 1596.00"

    assert unsupported_prices("It is Rs. 1,596.", catalogue) == set()
    assert unsupported_prices("It is PKR 1596", catalogue) == set()
    assert unsupported_prices("It is Rs. 1,597.", catalogue) == {"1597"}


def test_money_figures_fold_trailing_zeros():
    assert money_figures("PKR 1596.00 and Rs. 1,596 and PKR 1596") == {"1596"}
