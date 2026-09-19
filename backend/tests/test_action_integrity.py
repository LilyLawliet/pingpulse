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
from types import SimpleNamespace

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


def test_the_model_cannot_assert_a_qualification_the_record_lacks():
    """The clamp that was not enough.

    Keeping the analyzer out of the verified stages still let it land on
    QUALIFIED, and the two candidates are compared with max() - so it could
    raise anybody to QUALIFIED with no qualification data at all. A live
    contact reached WON down this route from a test conversation about
    wedding shoes.
    """
    from app.services import analyzer

    # This is the mapping that did it: the model calls a chat CLOSED and the
    # board reads it as a sale.
    assert analyzer.STAGE_TO_PIPELINE["CLOSED"] == "WON"
    assert analyzer.STAGE_TO_PIPELINE["READY_TO_BUY"] == "ESTIMATE_SENT"

    # And the ceiling the conversation may reach without the data is below it.
    class Shop:
        agent_config = {"qualification_slots": [{"name": "project_type", "asks": "x"}]}

    ceiling = webhook.evaluate_stage("NEW_LEAD", "I'll take it", Shop(), {})
    assert ceiling == "NEW_LEAD"
    assert webhook.STAGE_ORDER.index(ceiling) < webhook.STAGE_ORDER.index("QUALIFIED")


# ------------------------------------------- promises that are not numbers
def test_a_shop_may_say_what_it_has_configured():
    """Beluga's own prompt offers a free consultation, so the agent may too.

    The corpus is the shop's configured rules and retrieved knowledge. This
    is the same rule the price guard uses, applied to the claims with no
    digits in them.
    """
    from app.services.llm_service import unsupported_promises

    beluga = (
        "Encourage qualified customers to schedule a free consultation or estimate. "
        "Do not invent prices, availability or project details."
    )

    assert unsupported_promises("We offer a free consultation to start.", beluga) == []


def test_a_shop_may_not_invent_a_promise_it_never_made():
    from app.services.llm_service import unsupported_promises

    bare = "We sell bathroom remodels in Miami."

    assert unsupported_promises("We offer a free estimate.", bare)
    assert unsupported_promises("We guarantee completion in three weeks.", bare)
    assert unsupported_promises("There is 20% off this month.", bare)
    assert unsupported_promises("We are fully licensed and insured.", bare)
    assert unsupported_promises("Same-day service available.", bare)


def test_an_ordinary_answer_is_not_a_promise():
    """A guard that fires on normal sentences is a guard that gets removed."""
    from app.services.llm_service import unsupported_promises

    bare = "We sell bathroom remodels in Miami."
    for innocent in (
        "Bathroom remodels usually take two to three weeks.",
        "I can look at what times are free this week.",
        "We work with marble, porcelain and ceramic.",
        "What is the approximate size of the room?",
    ):
        assert unsupported_promises(innocent, bare) == [], innocent


def test_a_shop_that_configured_nothing_is_not_silenced():
    """An empty corpus is a shop we know nothing about, not one lying.

    Refusing every sentence would leave it with an agent that cannot speak,
    which is a worse failure than the one being prevented.
    """
    from app.services.llm_service import unsupported_promises

    assert unsupported_promises("We guarantee everything.", "") == []


# --------------------------------------------- a retry must not do it twice
@pytest.mark.asyncio
async def test_a_redelivered_webhook_is_processed_once(db_session, monkeypatch):
    """Twilio retries what it did not hear back from quickly enough.

    Generating a reply takes seconds, so a slow turn is redelivered as a
    matter of course. Without this the customer's message is stored twice and
    answered twice - and since booking became real, the second pass would try
    to book the slot the first pass had just taken and tell them it was gone
    to somebody else. It had gone to them.
    """
    from sqlalchemy import select

    from app.models import ChannelConfig, Message, Organization
    from app.schemas import TwilioWebhookPayload
    from app.services import outbox

    organization = Organization(name="Retry Co", sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()

    channel = ChannelConfig(
        organization_id=organization.id,
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        phone_number="+15551110000",
        session_status="AUTHENTICATED",
    )
    db_session.add(channel)
    await db_session.flush()

    sent: list = []

    async def capture(*args, **kwargs):
        sent.append(args)
        return outbox.Delivery(status=outbox.SENT, reference="SM_out", detail="")

    monkeypatch.setattr(outbox, "deliver", capture)

    async def fake_generate(*args, **kwargs):
        from app.services.llm_service import GenerationResult

        return GenerationResult(
            provider="groq", text="Sure, how can I help?", prompt_used="p", latency_ms=5
        )

    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)

    payload = TwilioWebhookPayload(
        From="whatsapp:+15559998888",
        To="whatsapp:+15551110000",
        Body="hello there",
        MessageSid="SM-repeat-1",
    )

    first = await webhook.process_inbound_message(db_session, payload, channel)
    second = await webhook.process_inbound_message(db_session, payload, channel)

    assert second["status"] == "duplicate", "the repeat was answered again"

    inbound = (
        await db_session.execute(
            select(Message).where(
                Message.sender == "user", Message.twilio_sid == "SM-repeat-1"
            )
        )
    ).scalars().all()
    assert len(inbound) == 1, "the customer's message was stored twice"
    assert len(sent) == 1, "the customer was answered twice"


@pytest.mark.asyncio
async def test_a_message_with_no_id_is_still_answered(db_session):
    """Dropping a real message is worse than a rare duplicate.

    Not every transport gives us an id, and refusing to process what we
    cannot deduplicate would be silence for whoever sent it.
    """
    assert await webhook.already_handled(db_session, None, None) is False
    assert await webhook.already_handled(db_session, None, "") is False


# --------------------------------------------------- it is not a person
# "I am a live team member here" reached a real customer on 18 September, in
# the same message as a cancellation that never happened. The booking guard
# objects to the cancellation. Nothing objected to this half, and once a real
# cancellation has occurred the whole sentence goes through.
#
# Every other guard consults the record: is there an appointment, is that
# price listed, has this shop said it offers free estimates. This one needs no
# state, because the answer does not depend on the conversation. The thing
# writing the sentence is not a person.
@pytest.mark.parametrize(
    "reply",
    [
        "I am a live team member here.",
        "I'm a real person, not a bot.",
        "I am human.",
        "I'm not a bot!",
        "You are speaking with a real person.",
        "This is a live team member.",
        "I am an actual human.",
        "you are chatting with a human",
        "I am a genuine employee of the company.",
    ],
)
def test_the_agent_may_not_say_it_is_a_person(reply):
    from app.services.llm_service import claims_to_be_human

    assert claims_to_be_human(reply), f"{reply!r} was allowed"


@pytest.mark.parametrize(
    "reply",
    [
        # Speaking for the business is the whole arrangement and must not be
        # touched. Only the claim to be a human being is refused.
        "We can come out on Tuesday.",
        "One of our team will be at your property at 2pm.",
        "I am the right person to ask about tiling.",
        "I am a bathroom remodeling specialist.",
        "Our team members are all licensed.",
        "I am not sure about that, let me check.",
        "I am happy to help.",
        "A human will never touch your data.",
        "I am not a plumber, but I can find out.",
    ],
)
def test_speaking_for_the_business_is_still_allowed(reply):
    from app.services.llm_service import claims_to_be_human

    assert claims_to_be_human(reply) is None, f"{reply!r} was wrongly refused"


def test_the_incident_sentence_is_refused_even_after_a_real_cancellation():
    """The precise gap. Both halves of the message a customer actually got:
    the cancellation guard clears once a cancellation really happens, and
    without this check the rest of the sentence rides along with it."""
    from app.services import booking
    from app.services.llm_service import claims_to_be_human

    sent = (
        "I am sorry for the confusion; I have cancelled the September 19 "
        "appointment. I am a live team member here."
    )

    assert booking.unverified_claims(sent, cancelled=True) == []
    assert claims_to_be_human(sent) == "I am a live team member"


# The detector above is only half of it. These drive the real reply path, so
# that wiring the check into `guard` is what the test depends on rather than
# the function merely existing.
_PERSONHOOD_ORG = SimpleNamespace(
    name="Beluga Group",
    sales_prompt="Sell bathroom remodeling.",
    target_tone="Warm",
    product_rules="",
    agent_config={},
    timezone="America/New_York",
)
_PERSONHOOD_CONTACT = SimpleNamespace(
    name="ZO",
    phone_number="+13057483629",
    pipeline_stage="LEAD",
    category_interest=None,
    city=None,
    shoe_size=None,
    colour_preference=None,
    budget_note=None,
)


@pytest.mark.asyncio
async def test_claiming_to_be_human_is_corrected_before_sending(monkeypatch):
    from app.services import llm_service

    calls = []

    async def model(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "I am a live team member here, happy to help."
        return "Happy to help - what would you like to know?"

    monkeypatch.setattr(llm_service, "_call_groq", model)

    result = await llm_service.generate_reply(
        _PERSONHOOD_ORG, _PERSONHOOD_CONTACT, [], "is this a real person?"
    )

    assert "live team member" not in result.text
    assert len(calls) == 2, "the reply was not regenerated"


@pytest.mark.asyncio
async def test_a_model_that_insists_it_is_human_sends_nothing(monkeypatch):
    """One corrective retry, then silence rather than a lie. The customer is
    better served by no answer than by being told they are talking to a person
    who does not exist."""
    from app.services import llm_service

    async def insists(prompt):
        return "I'm a real person, I promise."

    monkeypatch.setattr(llm_service, "_call_groq", insists)
    monkeypatch.setattr(llm_service, "_call_gemini", insists)

    result = await llm_service.generate_reply(
        _PERSONHOOD_ORG, _PERSONHOOD_CONTACT, [], "are you a bot?"
    )

    assert "real person" not in result.text
    assert result.provider == "none"


@pytest.mark.asyncio
async def test_a_model_that_insists_on_a_promise_sends_nothing(monkeypatch):
    """The other half of the same gap. The rewrite used to be checked for
    prices, handoffs, appointment claims and Roman Urdu - but not for the
    promises added last week. A guard that only inspects the first attempt is
    a retry, not a guard."""
    from app.services import llm_service

    org = SimpleNamespace(
        name="Beluga Group",
        sales_prompt="Sell bathroom remodeling.",
        target_tone="Warm",
        product_rules="Remodels start at $12,000.",
        agent_config={},
        timezone="America/New_York",
    )

    async def insists(prompt):
        return "We offer a lifetime guarantee on all work."

    monkeypatch.setattr(llm_service, "_call_groq", insists)
    monkeypatch.setattr(llm_service, "_call_gemini", insists)

    result = await llm_service.generate_reply(
        org, _PERSONHOOD_CONTACT, [], "do you guarantee the work?"
    )

    assert "guarantee" not in result.text
    assert result.provider == "none"
