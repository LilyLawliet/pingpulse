"""Finding out what the job actually is, without interrogating anybody.

An agent that answers questions well and never learns what the work is produces
conversations nobody can act on. An agent that marches through a form loses the
customer. The whole of this sits between those two failures, and the tests are
mostly about the second one, because it is the one that looks like success:
every slot filled, and a customer who never came back.

Two rules carry it. The agent is told to ask for **at most one** thing, and
only where it follows from what was just said. And extraction only ever adds -
somebody who named a budget in their first message has not withdrawn it by not
repeating it in their fifth, so a later empty reading must never clear a slot
and set the agent asking again.

Summaries are here too, on the same principle: they run after the reply has
gone and never on every turn, because rewriting a summary after "ok thanks"
spends a model call to produce the same sentence.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import qualification, summarise


class _Org:
    def __init__(self, config=None):
        self.id = uuid.uuid4()
        self.agent_config = config or {}


# ------------------------------------------------------------------- slots
def test_a_business_that_configured_nothing_gets_the_general_slots():
    assert qualification.slots_for(_Org()) == qualification.DEFAULT_SLOTS


def test_a_business_can_ask_for_its_own():
    """A roofer needs ownership and a photo; a salon needs a date and a
    service. One fixed list would be wrong for most tenants."""
    org = _Org({"qualification_slots": [{"name": "roof_age", "asks": "how old the roof is"}]})

    assert qualification.slots_for(org) == (("roof_age", "how old the roof is"),)


def test_a_filled_slot_is_not_still_missing():
    org = _Org()
    outstanding = qualification.missing(org, {"job_type": "roof repair"})

    assert "job_type" not in [name for name, _ in outstanding]
    assert "budget" in [name for name, _ in outstanding]


def test_an_empty_value_does_not_count_as_answered():
    outstanding = qualification.missing(_Org(), {"job_type": "", "budget": None})

    assert {"job_type", "budget"} <= {name for name, _ in outstanding}


# ------------------------------------------------------------- the prompt
def test_the_prompt_says_ask_for_at_most_one():
    """The line that stands between a conversation and a questionnaire. A model
    handed six gaps asks for six."""
    block = qualification.as_prompt_block(_Org(), {})

    assert "AT MOST ONE" in block
    assert "Answer their question first" in block


def test_what_is_known_is_marked_never_to_be_asked_again():
    block = qualification.as_prompt_block(_Org(), {"budget": "around 2000"})

    assert "around 2000" in block
    assert "Never ask again" in block


def test_a_shop_can_turn_qualification_off_entirely():
    """An explicit empty list is an answer, not an absence. Some shops want an
    agent that answers questions and asks none, and without this there is no
    way to say so - a missing key cannot mean both "not configured" and "off"."""
    off = _Org({"qualification_slots": []})

    assert qualification.slots_for(off) == ()
    assert qualification.as_prompt_block(off, {}) == ""


def test_a_shop_that_has_configured_nothing_does_get_the_default_questions():
    """Stated plainly because it is a behaviour change for every tenant already
    running: from this deploy their agent starts asking one qualifying question
    where it fits. That is the feature, and the line above is how to decline
    it."""
    block = qualification.as_prompt_block(_Org(), {})

    assert "Still unknown" in block
    assert "AT MOST ONE" in block


# --------------------------------------------------------------- merging
def test_learning_nothing_new_does_not_erase_what_was_known():
    """The bug this prevents: a customer names their budget once, says nothing
    about it for three messages, and the agent asks for it again."""
    before = {"budget": "around 2000", "location": "Lahore"}

    after = qualification.merge(before, {})

    assert after == before


def test_a_later_reading_does_not_overwrite_an_answered_slot():
    before = {"budget": "around 2000"}

    after = qualification.merge(before, {"budget": "not sure", "location": "Lahore"})

    assert after["budget"] == "around 2000"
    assert after["location"] == "Lahore"


@pytest.mark.asyncio
async def test_extraction_keeps_only_the_slots_it_was_asked_for(monkeypatch):
    """A model returning extra keys must not write arbitrary fields onto a
    contact."""
    from app.services import llm_service

    async def reply(_prompt):
        return '{"job_type": "roof repair", "budget": null, "nonsense": "drop me"}'

    monkeypatch.setattr(llm_service, "_call_groq", reply)

    found = await qualification.extract(_Org(), [], "my roof is leaking")

    assert found == {"job_type": "roof repair"}


@pytest.mark.asyncio
async def test_extraction_failing_never_breaks_the_reply_path(monkeypatch):
    from app.services import llm_service

    async def boom(_prompt):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_service, "_call_groq", boom)

    assert await qualification.extract(_Org(), [], "hello") == {}


@pytest.mark.asyncio
async def test_a_non_answer_is_not_recorded_as_an_answer(monkeypatch):
    from app.services import llm_service

    async def reply(_prompt):
        return '{"budget": "not stated", "location": "unknown", "job_type": "fence"}'

    monkeypatch.setattr(llm_service, "_call_groq", reply)

    assert await qualification.extract(_Org(), [], "fence please") == {"job_type": "fence"}


# -------------------------------------------------------------- summaries
def test_the_first_summary_is_written_early():
    """A thread with three messages is already one somebody may open cold."""
    assert summarise.is_due(2, had_summary=False) is True
    assert summarise.is_due(1, had_summary=False) is False


def test_an_existing_summary_is_not_rewritten_every_turn():
    """Rewriting after "ok thanks" spends a model call to produce the same
    sentence."""
    assert summarise.is_due(5, had_summary=True) is False
    assert summarise.is_due(8, had_summary=True) is True


@pytest.mark.asyncio
async def test_a_summary_carries_an_action_for_a_person(monkeypatch):
    from app.services import llm_service

    async def reply(_prompt):
        return '{"summary": "Wants the back roof repaired.", "next_action": "Send a quote."}'

    monkeypatch.setattr(llm_service, "_call_groq", reply)

    written = await summarise.write([], "when can you come?")

    assert written["summary"] == "Wants the back roof repaired."
    assert written["next_action"] == "Send a quote."


@pytest.mark.asyncio
async def test_nothing_to_do_is_not_recorded_as_an_action(monkeypatch):
    from app.services import llm_service

    async def reply(_prompt):
        return '{"summary": "Just browsing.", "next_action": null}'

    monkeypatch.setattr(llm_service, "_call_groq", reply)

    written = await summarise.write([], "just looking thanks")

    assert "next_action" not in written


@pytest.mark.asyncio
async def test_a_failed_summary_returns_nothing_rather_than_raising(monkeypatch):
    from app.services import llm_service

    async def boom(_prompt):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_service, "_call_groq", boom)

    assert await summarise.write([], "hello") == {}
