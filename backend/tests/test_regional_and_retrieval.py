"""Regional settings reaching the prompt, and hybrid retrieval behaviour."""

from types import SimpleNamespace

import pytest

from app.services import retrieval
from app.services.embeddings import cosine_similarity, hashed_embedding
from app.services.llm_service import build_prompt, regional_rules
from app.services.retrieval import as_prompt_block, keyword_score

pytestmark = pytest.mark.asyncio


def org(**overrides):
    base = dict(
        name="Test Shop",
        sales_prompt="Sell things.",
        target_tone="Warm",
        product_rules="Widget - 100",
        default_currency="USD",
        default_language="en",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


CONTACT = SimpleNamespace(
    name="Sam",
    phone_number="+10000000000",
    pipeline_stage="LEAD",
    city=None,
    shoe_size=None,
    category_interest=None,
    colour_preference=None,
    budget_note=None,
)


# ------------------------------ currency ----------------------------------
def test_currency_reaches_the_prompt():
    prompt = build_prompt(org(default_currency="PKR"), CONTACT, [], "how much?")

    assert "Pakistani rupees" in prompt


def test_each_organization_gets_its_own_currency():
    usd = build_prompt(org(default_currency="USD"), CONTACT, [], "how much?")
    eur = build_prompt(org(default_currency="EUR"), CONTACT, [], "how much?")

    assert "US dollars" in usd and "euros" not in usd
    assert "euros" in eur and "US dollars" not in eur


def test_an_unknown_currency_code_is_still_passed_through():
    rules = regional_rules("XYZ", "en")

    assert any("XYZ" in rule for rule in rules)


def test_currency_rule_forbids_converting():
    rules = regional_rules("AED", "en")

    assert any("Never convert" in rule for rule in rules)


# ------------------------------ language ----------------------------------
def test_non_english_language_instructs_the_reply_language():
    rules = regional_rules("USD", "ur")

    assert any("Urdu" in rule for rule in rules)


def test_english_needs_no_language_instruction():
    """English is the model's default, so no rule is spent on it."""
    rules = regional_rules("USD", "en")

    assert not any("Write the reply in" in rule for rule in rules)


def test_regional_variant_resolves_to_its_base_language():
    rules = regional_rules("EUR", "fr-CA")

    assert any("French" in rule for rule in rules)


def test_language_reaches_the_prompt():
    prompt = build_prompt(org(default_language="ar"), CONTACT, [], "مرحبا")

    assert "Arabic" in prompt


def test_currency_and_language_are_independent():
    prompt = build_prompt(
        org(default_currency="PKR", default_language="ur"), CONTACT, [], "kitne ka hai?"
    )

    assert "Pakistani rupees" in prompt
    assert "Urdu" in prompt


# ----------------------------- embeddings ---------------------------------
def test_hashed_embedding_is_deterministic():
    assert hashed_embedding("free delivery over 15000") == hashed_embedding(
        "free delivery over 15000"
    )


def test_similar_text_scores_higher_than_unrelated_text():
    query = hashed_embedding("what is your warranty policy")
    close = hashed_embedding("our warranty policy lasts two years")
    far = hashed_embedding("bananas grow in tropical climates")

    assert cosine_similarity(query, close) > cosine_similarity(query, far)


def test_cosine_handles_missing_or_mismatched_vectors():
    assert cosine_similarity(None, [1.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0


# ---------------------------- keyword signal ------------------------------
def test_keyword_score_rewards_exact_terms():
    assert keyword_score("gearbox warranty", "the gearbox warranty is lifetime") == 1.0


def test_keyword_score_is_zero_without_overlap():
    assert keyword_score("gearbox warranty", "we sell shoes and sandals") == 0.0


# ------------------------------ retrieval ---------------------------------
async def test_search_ranks_the_better_match_first(db_session, offline_embeddings):
    from app.models import Organization

    organization = Organization(name="Shop", sales_prompt="Sell.")
    db_session.add(organization)
    await db_session.flush()

    await retrieval.index_document(
        db_session, organization.id, "Returns policy", "Returns accepted within 30 days."
    )
    await retrieval.index_document(
        db_session, organization.id, "Delivery times", "Orders arrive in 3 working days."
    )
    await db_session.flush()

    hits = await retrieval.search(db_session, organization.id, "how do I return an item?")

    assert hits
    assert hits[0].title == "Returns policy"


async def test_search_reports_both_signals(db_session, offline_embeddings):
    from app.models import Organization

    organization = Organization(name="Shop", sales_prompt="Sell.")
    db_session.add(organization)
    await db_session.flush()

    await retrieval.index_document(
        db_session, organization.id, "Warranty", "Gearbox warranty lasts a lifetime."
    )
    await db_session.flush()

    hits = await retrieval.search(db_session, organization.id, "gearbox warranty")

    assert hits[0].keyword_score > 0
    assert hits[0].vector_score > 0
    assert hits[0].score > 0


async def test_search_on_an_empty_knowledge_base_returns_nothing(
    db_session, offline_embeddings
):
    from app.models import Organization

    organization = Organization(name="Shop", sales_prompt="Sell.")
    db_session.add(organization)
    await db_session.flush()

    assert await retrieval.search(db_session, organization.id, "anything") == []


async def test_blank_query_short_circuits(db_session, offline_embeddings):
    from app.models import Organization

    organization = Organization(name="Shop", sales_prompt="Sell.")
    db_session.add(organization)
    await db_session.flush()

    assert await retrieval.search(db_session, organization.id, "   ") == []


def test_prompt_block_is_empty_without_hits():
    assert as_prompt_block([]) == ""


async def test_retrieved_knowledge_reaches_the_prompt(db_session, offline_embeddings):
    from app.models import Organization

    organization = Organization(name="Shop", sales_prompt="Sell.")
    db_session.add(organization)
    await db_session.flush()

    await retrieval.index_document(
        db_session, organization.id, "Warranty", "Gearbox warranty lasts a lifetime."
    )
    await db_session.flush()

    hits = await retrieval.search(db_session, organization.id, "gearbox warranty")
    prompt = build_prompt(org(), CONTACT, [], "gearbox warranty?", as_prompt_block(hits))

    assert "KNOWLEDGE BASE" in prompt
    assert "lifetime" in prompt
