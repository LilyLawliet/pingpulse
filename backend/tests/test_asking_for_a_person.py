"""Asking for a person, in the ways people actually ask.

Every line in the first list was written by a real customer of a real client,
or is the same sentence with one word moved. Three of them were answered with
a booking link, twice, and then told "since you're ready for the team member
connection" - about somebody who had just said No.

Two faults, both here:

The verb and the preposition had to be adjacent, so any object between them
broke the match. "connect me to a human" did not escalate. Neither did "put me
through to someone". The commonest phrasings in English were the ones that
failed, and the ones that worked - "speak to a person" - are the ones nobody
types under stress.

And "team member" was absent entirely, which is the one that stings: the
client's own sales prompt says "a team member will confirm it". The agent
taught the customer a phrase its own matcher could not hear.

The second list is the reason this cannot simply be made greedy. A remodeling
business is asked to connect pipes and put in tiles all day.
"""

from __future__ import annotations

import pytest

from app.services import agent_config

# The conversation that produced this file, in order.
FROM_THE_INCIDENT = (
    "Connect me to a team member.",
    "No, I want to connect to a team member.",
    "I want to talk to them directly, put a team member",
)

ASKING_FOR_A_PERSON = FROM_THE_INCIDENT + (
    "Connect me to a human",
    "connect me with someone",
    "put me through to someone",
    "put me in touch with a person",
    "transfer me to an agent",
    "Can I speak with a real person?",
    "I need a human",
    "I want a team member",
    "get me someone",
    "send a person please",
    "give me a manager",
    "talk to a manager",
    "speak with someone from the team",
    "I would like to speak to a supervisor",
    "is this a bot?",
    "am I talking to a robot",
    "who am I speaking to",
    "human please",
)

# Ordinary sentences in the trades this product serves. A false escalation
# costs a person reading one message, so the bar is not zero - but these are
# the ones that would fire constantly.
ORDINARY = (
    "I talked to my wife about it",
    "Can you connect the shower to the water supply?",
    "How much to connect a new sink to the drain?",
    "Put the tiles in the bathroom please",
    "I want to talk about pricing",
    "Do you get staff discount?",
    "The team did a great job",
    "I need a quote",
    "Send me the price list",
    "Can I get a wet room?",
    "What time do you open",
    "Do you serve Palm Beach",
)


@pytest.mark.parametrize("text", ASKING_FOR_A_PERSON)
def test_a_request_for_a_person_is_heard(text):
    assert agent_config.needs_escalation(text) is not None, text


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_trade_talk_is_not_a_request_for_a_person(text):
    assert agent_config.needs_escalation(text) is None, text


def test_an_object_between_the_verb_and_the_preposition_is_allowed():
    """The exact fault. These three differ by one word and all three mean the
    same thing, and only the first one used to match."""
    for text in (
        "connect to a human",
        "connect me to a human",
        "connect me through to a human",
    ):
        assert agent_config.needs_escalation(text) is not None, text


def test_a_team_member_is_a_person():
    """What a business calls its own staff when talking to customers, and what
    its agent has been telling them to ask for."""
    for text in (
        "I want a team member",
        "can you put a team member on",
        "connect me to a team member",
        "someone from the team please",
    ):
        assert agent_config.needs_escalation(text) is not None, text


def test_a_refusal_of_the_agents_offer_still_escalates():
    """"No, I want to connect to a team member" arrived after a booking link
    and has to read as a rejection of it, not as a fresh request the next
    sentence can talk somebody out of."""
    assert agent_config.needs_escalation("No, I want a team member") is not None
    assert (
        agent_config.needs_escalation("no thanks, I need to speak to a person")
        is not None
    )


def test_the_gap_cannot_reach_across_a_whole_sentence():
    """Bounded, so an unrelated verb and an unrelated noun in one long
    sentence are not paired into a handover."""
    text = (
        "Can you connect the new pipework before the tiler arrives on site "
        "and let the person at the yard know"
    )
    assert agent_config.needs_escalation(text) is None


def test_a_tenants_own_words_still_win_first():
    """The shop's list is checked before any of this, because they added it
    knowing something about their trade that these patterns do not."""
    organization = type("Org", (), {"agent_config": {"escalate_on": ["asbestos"]}})()
    assert agent_config.needs_escalation("we found asbestos", organization) == "asbestos"


# ------------------------------------------- a booking is not a request for a person
from app.services import booking as _booking  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        "book one with ahmed name",
        "book it under Ahmed",
        "can you book a visit for my husband Carlos",
        "schedule an estimate with my name, Jamie",
    ],
)
def test_a_booking_read_as_wanting_a_person_is_still_a_booking(text):
    """The analyzer said "a person"; the message asks to book. It is not handed over."""
    assert not _booking.heard_as_a_person({"wants_person": True}, text)


@pytest.mark.parametrize("text", ["insaan se baat karao", "can i talk 2 sm1 real", "put me with team"])
def test_a_person_is_still_heard_when_nothing_is_being_booked(text):
    assert _booking.heard_as_a_person({"wants_person": True}, text)


def test_explicit_words_for_a_person_win_even_while_booking():
    """The keyword list runs before the analyzer's reading and is not affected."""
    assert agent_config.needs_escalation("I want to speak to a human to book my visit", None)
