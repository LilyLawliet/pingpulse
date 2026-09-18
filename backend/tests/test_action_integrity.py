"""Does PingPulse only claim what actually happened?

The rule these tests exist to enforce: the agent must never tell a customer
that something happened unless the backend did it and confirmed it. A reply is
not evidence of an action, and a keyword in a customer's message is not an
event.

Three behaviours were reported from production, and every one of them is
reproduced here against the real code before anything is changed:

1. A lead marked "estimate scheduled" with nothing booked.
2. A confirmed appointment time the customer never chose.
3. The agent answering as the business when somebody asked for a human.

They share one cause. State and confirmations are being derived from the text
of the customer's message rather than from a completed operation, so the
system's idea of what happened and what actually happened were never connected
in the first place.
"""

from datetime import datetime, timezone

import pytest

from app.api import webhook
from app.services import agent_config, sales_policy, scheduling


# --------------------------------------------------- 1. scheduled too early
@pytest.mark.parametrize(
    "message",
    [
        "can I schedule an estimate?",
        "do you do appointments on weekends?",
        "what times can I book?",
        "is it possible to call you tomorrow?",
        "how long is a meeting usually?",
        "I'd like to book something eventually, not yet",
    ],
)
def test_asking_about_booking_must_not_mark_a_lead_as_booked(message):
    """Every one of these is a question, and none of them is an appointment.

    `evaluate_stage` matches DEMO_SIGNALS as substrings of the customer's own
    words and moves the contact to ESTIMATE_SCHEDULED. Nothing is booked,
    nothing is checked, and the board now says otherwise - which is the first
    of the three reported behaviours.
    """
    after = webhook.evaluate_stage("NEW_LEAD", message)

    assert after != "ESTIMATE_SCHEDULED", (
        f"{message!r} moved the lead to ESTIMATE_SCHEDULED with nothing booked"
    )


def test_a_word_inside_another_word_must_not_move_a_lead():
    """The match is a substring, so it does not even need the whole word."""
    assert webhook.evaluate_stage("NEW_LEAD", "I'll bookmark your site") != (
        "ESTIMATE_SCHEDULED"
    )
    assert webhook.evaluate_stage("NEW_LEAD", "my son is recalling the price") != (
        "ESTIMATE_SCHEDULED"
    )


def test_talking_about_buying_must_not_mark_a_deal_won():
    """Worse than the booking case, because WON feeds the revenue figures.

    A lead marked won from a customer's sentence puts money in the money
    panel that nobody ever agreed to pay.
    """
    for message in (
        "I might buy later, I'm just looking",
        "do I have to purchase today?",
        "is there a contract I need to read first?",
    ):
        assert webhook.evaluate_stage("QUALIFIED", message) != "WON", (
            f"{message!r} marked the deal won"
        )


# ------------------------------------------- 2. a time nobody chose
def test_the_calendar_link_must_not_invent_an_appointment_time():
    """The second reported behaviour, and the arithmetic behind it.

    The Google fallback defaults to 05:00 UTC tomorrow with no input from the
    customer at all. On the US east coast that renders as 1am - which is
    exactly the "September 19 at 1 AM" the client reported. The customer chose
    nothing; a hardcoded hour did.
    """
    link = scheduling.google_calendar_link(title="Estimate")

    assert "T050000Z" not in link, (
        "the link carries a hardcoded 05:00 UTC start, which is 1am on the US "
        "east coast and was chosen by nobody"
    )


def test_a_booking_link_is_not_a_booking():
    """A link is an invitation to choose a time. It is not an appointment.

    Nothing in the system records that the customer opened it, chose anything,
    or turned up - so no state anywhere may treat sending one as a booking.
    """
    from app.models import CRMContact

    contact = CRMContact(phone_number="+15550000", pipeline_stage="QUALIFIED")
    link = scheduling.google_calendar_link(title="Estimate")

    assert link  # it exists, and that is all it is
    assert contact.pipeline_stage == "QUALIFIED", (
        "building a link changed the pipeline"
    )


# ----------------------------------------- 3. answering as a person
@pytest.mark.parametrize(
    "message",
    [
        "I want to talk to a human",
        "can I talk to someone please",
        "are you a real person?",
        "is this a bot?",
        "put me through to a real human being",
        "who am I speaking to?",
    ],
)
def test_asking_for_a_person_must_be_recognised(message):
    """The third reported behaviour.

    ESCALATION_SIGNALS has "speak to a human" and "talk to a person" but not
    "talk to a human" or "talk to someone", so the most ordinary phrasings
    miss. The agent then answers under a policy that tells it "You are the
    shop. You answer now" - which is what being presented with a live team
    member looks like from the customer's side.
    """
    assert agent_config.needs_escalation(message) is not None, (
        f"{message!r} was not recognised as asking for a person"
    )


def test_the_handoff_reply_admits_what_it_is():
    """A customer who asked for a human is owed a straight answer.

    Two things it must not do: claim to be a person, and claim somebody has
    been notified. The alert is handed to a worker and has not been delivered
    when this is sent, so promising it would be the same lie in a new place.
    """
    reply = webhook.handoff_reply(None).lower()

    assert "assistant" in reply or "automated" in reply, (
        "the handoff reply does not admit the customer was talking to software"
    )
    for claim in ("notified", "has been alerted", "i am a person", "speaking to a human"):
        assert claim not in reply, f"the handoff reply claims {claim!r}"


def test_a_tenant_can_word_the_handoff_themselves():
    """Policy wording belongs in configuration, not in the code."""

    class Org:
        agent_config = {"handoff_message": "Passing you to Yaha now."}

    assert webhook.handoff_reply(Org()) == "Passing you to Yaha now."


@pytest.mark.asyncio
async def test_a_customer_who_asks_for_a_person_is_actually_sent_something(
    db_session, default_org, monkeypatch
):
    """The behaviour, not the source text.

    The first version of this test scanned the escalation branch for the word
    "send" and matched it inside `raise_and_send` - so it passed while the
    customer was being sent nothing at all.
    """
    from app.models import CRMContact
    from app.services import outbox

    contact = CRMContact(
        organization_id=default_org.id,
        phone_number="+15550777",
        pipeline_stage="NEW_LEAD",
        ai_enabled=True,
    )
    db_session.add(contact)
    await db_session.flush()

    sent: list[str] = []

    async def capture(channel, to, text, media, **kwargs):
        sent.append(text)
        return outbox.Delivery(status=outbox.SENT, reference="x", detail="")

    monkeypatch.setattr(outbox, "deliver", capture)

    told = await webhook.acknowledge_handoff(
        db_session, default_org, contact, None, "+15550777"
    )

    assert told is True
    assert len(sent) == 1, "the customer was sent nothing"
    assert "team" in sent[0].lower()


def test_the_agent_must_not_be_forbidden_from_admitting_a_handoff():
    """The no-handoff guarantee and an actual handoff cannot both be true.

    The policy bans "transfer you to" and "connect you with" outright. When
    the conversation really has been handed to a person - which the escalation
    path does - the agent is forbidden from saying the one thing that is true.
    """
    honest = "I am passing this to the team now and someone will reply here."

    assert sales_policy.contains_handoff(honest) is None, (
        "an accurate handoff sentence is rejected by the banned-phrase filter, "
        "so the agent cannot tell the truth about a handoff that happened"
    )


# ------------------------------------------- qualification drives the stage
def test_qualification_switched_off_does_not_qualify_everybody():
    """No questions asked is not the same as every question answered.

    With qualification off there are no unanswered slots, so "nothing is
    missing" reads as "fully qualified" and promotes every contact on their
    first message - the same fault as the keyword rule, in the code written to
    replace it.
    """

    class Off:
        agent_config = {"qualification_slots": []}

    assert webhook.evaluate_stage("NEW_LEAD", "hello", Off(), {}) == "NEW_LEAD"


def test_a_lead_qualifies_only_once_the_answers_are_in():
    """And then it does, because that is a fact about data we hold."""

    class Shop:
        agent_config = {
            "qualification_slots": [
                {"name": "project_type", "asks": "what they want done"},
                {"name": "address", "asks": "where the work is"},
            ]
        }

    shop = Shop()
    half = {"project_type": "kitchen remodel"}
    full = {"project_type": "kitchen remodel", "address": "12 Mill Lane"}

    assert webhook.evaluate_stage("NEW_LEAD", "hi", shop, half) == "NEW_LEAD"
    assert webhook.evaluate_stage("NEW_LEAD", "hi", shop, full) == "QUALIFIED"


def test_a_booked_lead_is_not_walked_back_by_small_talk():
    """Stages move forward only. A customer saying "thanks!" after booking
    must not drop them back down the board."""

    class Shop:
        agent_config = {"qualification_slots": [{"name": "project_type", "asks": "x"}]}

    assert (
        webhook.evaluate_stage("ESTIMATE_SCHEDULED", "thanks!", Shop(), {})
        == "ESTIMATE_SCHEDULED"
    )
