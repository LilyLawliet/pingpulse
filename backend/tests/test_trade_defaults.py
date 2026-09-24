"""The starting drafts, and the promises they have to keep.

A draft is offered to a shop as the beginning of its own policy, and it is
read by a model as the rules customers are answered under. So it is held to
two standards that an ordinary constant is not.

*It must save.* A draft that the settings form would refuse is worse than no
draft, because a person has to work out which of four lines the server is
objecting to before they can get past a screen that was supposed to save them
time.

*It must not overlap the documents.* `document_facts` fills hours, services
and areas; a trade fills policy. Where both touched a field, the later one
would silently win and nobody could say which had written what is in the box.
"""

from __future__ import annotations

import pytest

from app.services import agent_config, trade_defaults


# ============================================================ the drafts
def test_every_draft_is_one_the_form_would_accept():
    """A draft the server refuses is a dead end with a person sitting in it."""
    for trade in trade_defaults.TRADES:
        config = {
            "never_promise": trade.never_promise,
            "pricing_rules": trade.pricing_rules,
            "escalate_on": list(trade.escalate_on),
        }
        assert agent_config.validate(config) == [], trade.key


def test_a_draft_fills_only_policy_and_never_a_documents_fields():
    """The two sources are disjoint, so neither can silently overwrite the
    other. A field written by both is a field nobody can explain."""
    assert not set(trade_defaults.DRAFT_FIELDS) & set(agent_config.DOCUMENT_FIELDS)

    for trade in trade_defaults.TRADES:
        filled = {key for key, value in trade.as_dict()["draft"].items() if value}
        assert not filled & set(agent_config.DOCUMENT_FIELDS), trade.key


def test_escalation_words_are_not_single_common_words():
    """A trigger that fires on ordinary chat teaches a shop to stop reading
    its alerts, and then the one that mattered is missed too."""
    too_ordinary = {"price", "cost", "time", "help", "question", "order", "book"}
    for trade in trade_defaults.TRADES:
        for word in trade.escalate_on:
            assert word not in too_ordinary, f"{trade.key}: {word}"
            assert len(word) >= 3, f"{trade.key}: {word}"


def test_escalation_words_are_fragments_that_survive_a_sentence():
    """Matched as substrings against the customer's own words, so a trailing
    full stop or a capital letter would stop one matching at all."""
    for trade in trade_defaults.TRADES:
        for word in trade.escalate_on:
            assert word == word.lower().strip(), f"{trade.key}: {word!r}"
            assert not word.endswith(("."))


def test_every_trade_draft_actually_fires_its_own_words():
    """The round trip: a shop that accepts a draft gets escalation on those
    words. This is the test that would catch a stored word which no longer
    matches the matcher it was written for."""
    for trade in trade_defaults.TRADES:
        organization = type(
            "Org", (), {"agent_config": {"escalate_on": list(trade.escalate_on)}}
        )()
        for word in trade.escalate_on:
            found = agent_config.needs_escalation(
                f"Hello, {word} and I need help please", organization
            )
            assert found is not None, f"{trade.key}: {word}"


def test_lookup_is_forgiving_about_an_unknown_trade():
    """A client sending a key this build does not have is asking for a draft.
    The right answer is no draft; the form behind it works with empty boxes."""
    assert trade_defaults.for_trade("home_improvement").key == "home_improvement"
    assert trade_defaults.for_trade("  HOME_IMPROVEMENT ").key == "home_improvement"
    assert trade_defaults.for_trade("underwater basket weaving") is None
    assert trade_defaults.for_trade(None) is None
    assert trade_defaults.for_trade("") is None


def test_there_is_always_a_trade_for_a_business_that_is_none_of_them():
    """"Something else" is the difference between a picker and a dead end."""
    assert "general" in trade_defaults.BY_KEY
    general = trade_defaults.BY_KEY["general"]
    assert general.never_promise and general.pricing_rules and general.escalate_on


# ============================================================== endpoint
@pytest.mark.asyncio
async def test_trades_are_listed_with_their_drafts(org_a):
    response = await org_a.get("/api/v1/agent-config/trades")
    assert response.status_code == 200

    body = response.json()
    assert len(body["trades"]) == len(trade_defaults.TRADES)
    assert body["fields"] == list(trade_defaults.DRAFT_FIELDS)

    first = body["trades"][0]
    assert {"key", "label", "examples", "draft"} <= set(first)
    assert isinstance(first["draft"]["escalate_on"], list)


@pytest.mark.asyncio
async def test_listing_trades_stores_nothing(org_a, db_session):
    """Reading the drafts must not configure anybody. The config changes on a
    form somebody pressed save on, and nowhere else."""
    import uuid

    from app.models import Organization

    await org_a.get("/api/v1/agent-config/trades")
    await db_session.commit()

    organization = await db_session.get(
        Organization, uuid.UUID(org_a.organization_id)
    )
    assert not (organization.agent_config or {})


@pytest.mark.asyncio
async def test_another_tenant_sees_the_same_drafts(org_a, org_b):
    """Conventions of a trade, not a finding about a business. Nothing here
    is tenant data, and treating it as such would be the bug."""
    a = (await org_a.get("/api/v1/agent-config/trades")).json()
    b = (await org_b.get("/api/v1/agent-config/trades")).json()
    assert a == b
