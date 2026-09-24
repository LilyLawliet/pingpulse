"""Learning the words that really did precede a person stepping in.

The signal is an operator message: somebody at the shop read a conversation
and typed into it themselves. That is a human judgement, made at the time,
about a real customer, and it is the only evidence here that the agent was out
of its depth.

What these tests mostly hold down is the *restraint*. Producing candidates is
easy and nearly useless; the value is in what gets thrown away. A trigger that
fires on ordinary messages sends an alert on every third conversation, and a
shop that stops reading its alerts misses the one it needed - so a word has to
be disproportionately present before handovers, seen in several separate
conversations, and not already covered by the rules that exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    SENDER_AGENT,
    SENDER_CUSTOMER,
    SENDER_OPERATOR,
    CRMContact,
    Message,
    Organization,
)
from app.services import handover_signals


def _when(days_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


async def _contact(db_session, org, phone):
    contact = CRMContact(
        organization_id=uuid.UUID(org.organization_id),
        phone_number=phone,
        pipeline_stage="LEAD",
        sales_stage="NEW",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


async def _conversation(db_session, org, phone, turns, day):
    """One thread. `turns` is [(sender, text)] in order."""
    contact = await _contact(db_session, org, phone)
    for index, (sender, text) in enumerate(turns):
        db_session.add(
            Message(
                organization_id=uuid.UUID(org.organization_id),
                contact_id=contact.id,
                sender=sender,
                content=text,
                # Spaced within the day so the ordering is unambiguous under
                # SQLite, which stores these without a timezone.
                created_at=_when(day) + timedelta(minutes=index),
            )
        )
    await db_session.flush()


async def _org(db_session, org):
    return await db_session.get(Organization, uuid.UUID(org.organization_id))


# ================================================== pulling phrases apart
def test_phrases_are_one_two_and_three_words():
    found = handover_signals.phrases("the shower is leaking badly")
    assert "leaking" in found
    assert "shower is leaking" in found
    assert "leaking badly" in found


def test_a_phrase_never_starts_or_ends_on_filler():
    """"the leak" and "leak" are the same finding, and offering both asks a
    shop to choose between two triggers that fire on the same messages."""
    found = handover_signals.phrases("the leak is bad")
    assert "leak" in found
    assert "the leak" not in found
    assert "leak is" not in found


def test_a_phrase_of_pure_filler_is_not_a_phrase():
    assert handover_signals.phrases("hi there please can you") == set()


def test_a_phrase_needs_at_least_one_real_word():
    """Otherwise two initials in a signature, or a lettered list, become a
    candidate trigger."""
    assert "b c" not in handover_signals.phrases("option b c below")


def test_single_letters_and_stray_marks_are_not_phrases():
    found = handover_signals.phrases("I a b c £££ 4747")
    assert found == set()


# ============================================ what counts as evidence
@pytest.mark.asyncio
async def test_words_before_a_person_stepped_in_are_proposed(org_a, db_session):
    """The whole feature in one case: three separate customers said the same
    thing, a person took over each time, and nobody had written it down."""
    for index in range(3):
        await _conversation(
            db_session,
            org_a,
            f"+92300000{index:04d}",
            [
                (SENDER_CUSTOMER, "Hi, the shower tray has water damage underneath"),
                (SENDER_AGENT, "I can help with that."),
                (SENDER_OPERATOR, "This is Ali, let me take a look."),
            ],
            day=index + 1,
        )

    # Ordinary traffic that also mentions water and showers, which is what a
    # bathroom business's history really looks like. Without it every word in
    # the sentence looks equally damning, and the pair would be pruned in
    # favour of the single words it contains.
    for index in range(10):
        await _conversation(
            db_session,
            org_a,
            f"+92301000{index:04d}",
            [
                (
                    SENDER_CUSTOMER,
                    "Will fitting damage the existing tiles? Can you move the "
                    "shower water inlet?",
                ),
                (SENDER_AGENT, "No, and yes - we can move it."),
            ],
            day=index + 20,
        )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )

    phrases = {c.phrase for c in report.candidates}
    assert "water damage" in phrases
    # Each word on its own is ordinary in this trade - customers ask about
    # water, showers and damage all day - so none of them survives alone. The
    # pair is what carries the signal, and keeping it is the point of counting
    # phrases rather than words.
    assert "water" not in phrases
    assert "shower" not in phrases
    assert "damage" not in phrases
    assert report.handovers == 3


@pytest.mark.asyncio
async def test_two_conversations_are_not_enough(org_a, db_session):
    """Below the floor a shop would be adding a trigger on the strength of one
    bad afternoon."""
    for index in range(2):
        await _conversation(
            db_session,
            org_a,
            f"+92311000{index:04d}",
            [
                (SENDER_CUSTOMER, "The grout is crumbling everywhere"),
                (SENDER_OPERATOR, "Let me look into it."),
            ],
            day=index + 1,
        )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )
    assert "grout" not in {c.phrase for c in report.candidates}


@pytest.mark.asyncio
async def test_a_word_that_shows_up_in_ordinary_chat_too_is_dropped(org_a, db_session):
    """The rule that matters. "booking" precedes three handovers and twelve
    perfectly ordinary conversations, so making it a trigger would alert on
    most of the shop's traffic."""
    for index in range(3):
        await _conversation(
            db_session,
            org_a,
            f"+92322000{index:04d}",
            [
                (SENDER_CUSTOMER, "I want to change my booking arrangement"),
                (SENDER_OPERATOR, "Sure, one moment."),
            ],
            day=index + 1,
        )
    for index in range(12):
        await _conversation(
            db_session,
            org_a,
            f"+92333000{index:04d}",
            [
                (SENDER_CUSTOMER, "Can I make a booking arrangement for Friday?"),
                (SENDER_AGENT, "Yes, Friday works."),
            ],
            day=index + 10,
        )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )

    phrases = {c.phrase for c in report.candidates}
    assert "booking" not in phrases
    assert "booking arrangement" not in phrases


@pytest.mark.asyncio
async def test_what_the_existing_rules_already_catch_teaches_nothing(org_a, db_session):
    """"refund" is already in `ESCALATION_SIGNALS`. Counting it as coverage is
    useful; proposing it as new is noise."""
    for index in range(4):
        await _conversation(
            db_session,
            org_a,
            f"+92344000{index:04d}",
            [
                (SENDER_CUSTOMER, "I want a refund for the broken tiles"),
                (SENDER_OPERATOR, "Let me sort that out."),
            ],
            day=index + 1,
        )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )

    assert report.handovers == 4
    assert report.already_caught == 4
    assert report.candidates == []


@pytest.mark.asyncio
async def test_the_agent_answering_is_not_a_handover(org_a, db_session):
    """Only a person typing counts. The agent replying to itself would make
    every conversation evidence of everything."""
    for index in range(5):
        await _conversation(
            db_session,
            org_a,
            f"+92355000{index:04d}",
            [
                (SENDER_CUSTOMER, "Do you fit underfloor heating?"),
                (SENDER_AGENT, "Yes, we do."),
            ],
            day=index + 1,
        )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )
    assert report.handovers == 0
    assert report.candidates == []


@pytest.mark.asyncio
async def test_one_person_replying_twice_is_one_handover(org_a, db_session):
    """A person deciding once is one piece of evidence, however many messages
    they then send."""
    await _conversation(
        db_session,
        org_a,
        "+923660000001",
        [
            (SENDER_CUSTOMER, "The extractor fan is dead"),
            (SENDER_OPERATOR, "Taking a look."),
            (SENDER_OPERATOR, "Can you send a photo?"),
            (SENDER_OPERATOR, "Thanks."),
        ],
        day=1,
    )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )
    assert report.handovers == 1


@pytest.mark.asyncio
async def test_an_agent_reply_does_not_end_the_customers_run(org_a, db_session):
    """A person very often steps in after reading a reply that missed the
    point, and what the customer said is still what they reacted to."""
    for index in range(3):
        await _conversation(
            db_session,
            org_a,
            f"+92377000{index:04d}",
            [
                (SENDER_CUSTOMER, "There is asbestos behind the panel"),
                (SENDER_AGENT, "We offer bathroom fitting from Monday."),
                (SENDER_OPERATOR, "Sorry - let me get someone qualified."),
            ],
            day=index + 1,
        )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )
    assert "asbestos" in {c.phrase for c in report.candidates}


@pytest.mark.asyncio
async def test_one_tenants_conversations_never_reach_another(org_a, org_b, db_session):
    for index in range(4):
        await _conversation(
            db_session,
            org_a,
            f"+92388000{index:04d}",
            [
                (SENDER_CUSTOMER, "The underfloor heating failed inspection"),
                (SENDER_OPERATOR, "Looking now."),
            ],
            day=index + 1,
        )

    mine = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )
    theirs = await handover_signals.evidence(
        db_session, uuid.UUID(org_b.organization_id), await _org(db_session, org_b)
    )

    assert mine.candidates
    assert theirs.candidates == []
    assert theirs.handovers == 0


@pytest.mark.asyncio
async def test_a_longer_phrase_that_adds_nothing_is_dropped(org_a, db_session):
    """"damage" and "water damage" both surviving asks a shop to pick between
    two triggers that fire on the same messages."""
    for index in range(4):
        await _conversation(
            db_session,
            org_a,
            f"+92399000{index:04d}",
            [
                (SENDER_CUSTOMER, "There is water damage in the ceiling"),
                (SENDER_OPERATOR, "On it."),
            ],
            day=index + 1,
        )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )
    phrases = {c.phrase for c in report.candidates}
    assert not ({"water", "damage"} <= phrases and "water damage" in phrases)


@pytest.mark.asyncio
async def test_examples_have_other_peoples_details_taken_out(org_a, db_session):
    """These lines were written to one customer and are read back in front of
    whoever is at the dashboard."""
    for index in range(3):
        await _conversation(
            db_session,
            org_a,
            f"+92400000{index:04d}",
            [
                (
                    SENDER_CUSTOMER,
                    "The wetroom flooded, email me at bob@example.com",
                ),
                (SENDER_OPERATOR, "Calling you now."),
            ],
            day=index + 1,
        )

    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )
    for candidate in report.candidates:
        for example in candidate.examples:
            assert "bob@example.com" not in example


@pytest.mark.asyncio
async def test_a_shop_with_no_history_gets_an_empty_report(org_a, db_session):
    """Not an error and not a guess. A brand-new tenant has nothing to learn
    from, and saying so is the honest answer."""
    report = await handover_signals.evidence(
        db_session, uuid.UUID(org_a.organization_id), await _org(db_session, org_a)
    )
    assert report.candidates == []
    assert report.as_dict()["enough_history"] is False


# ================================================================ endpoint
@pytest.mark.asyncio
async def test_suggestions_endpoint_returns_evidence(org_a, db_session):
    for index in range(3):
        await _conversation(
            db_session,
            org_a,
            f"+92411000{index:04d}",
            [
                (SENDER_CUSTOMER, "The wetroom is flooding through the ceiling"),
                (SENDER_OPERATOR, "On my way."),
            ],
            day=index + 1,
        )
    await db_session.commit()

    response = await org_a.get("/api/v1/agent-config/suggestions")
    assert response.status_code == 200

    body = response.json()
    assert body["handovers"] == 3
    assert body["min_conversations"] == handover_signals.MIN_CONVERSATIONS
    for candidate in body["candidates"]:
        # The count is the argument. A suggestion a shop cannot check is one
        # it has to take on trust, which is the thing this avoids.
        assert candidate["handovers"] >= handover_signals.MIN_CONVERSATIONS
        assert 0 <= candidate["precision"] <= 1


@pytest.mark.asyncio
async def test_a_word_already_configured_is_not_suggested_again(org_a, db_session):
    for index in range(3):
        await _conversation(
            db_session,
            org_a,
            f"+92422000{index:04d}",
            [
                (SENDER_CUSTOMER, "The wetroom is flooding through the ceiling"),
                (SENDER_OPERATOR, "On my way."),
            ],
            day=index + 1,
        )
    organization = await _org(db_session, org_a)
    organization.agent_config = {"escalate_on": ["flooding"]}
    await db_session.commit()

    body = (await org_a.get("/api/v1/agent-config/suggestions")).json()
    assert "flooding" not in {row["phrase"] for row in body["candidates"]}


@pytest.mark.asyncio
async def test_suggestions_write_nothing(org_a, db_session):
    """Advice, not a setting. Asking for it must not configure anybody."""
    for index in range(3):
        await _conversation(
            db_session,
            org_a,
            f"+92433000{index:04d}",
            [
                (SENDER_CUSTOMER, "There is water damage in the ceiling"),
                (SENDER_OPERATOR, "On it."),
            ],
            day=index + 1,
        )
    await db_session.commit()

    await org_a.get("/api/v1/agent-config/suggestions")
    await db_session.commit()

    organization = await _org(db_session, org_a)
    await db_session.refresh(organization)
    assert not (organization.agent_config or {}).get("escalate_on")
