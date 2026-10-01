"""Booking requests must be read as booking requests.

The bug: "can we book a call?" was classified as a product or price question,
so retrieval ran, the catalogue went into the prompt, and the agent answered a
request for a meeting with an FAQ. These pin down both halves of the fix — the
classification, and the directive that suppresses everything else once it fires.
"""

from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services import booking
from app.services.analyzer import _coerce, heuristic_analysis, wants_to_book
from app.services.sales_policy import contains_handoff, directives


@pytest.mark.parametrize(
    "message",
    [
        "I want to book a call with your team",
        "Can we schedule a demo?",
        "Can we book a call to discuss SaaS enterprise pricing?",
        "I'd like to set up a meeting next week",
        "can we arrange a call",
        "let's hop on a call",
        "I want to talk to sales",
        "Book a consultation call please",
    ],
)
def test_explicit_booking_requests_are_detected(message):
    assert wants_to_book(message) is True
    analysis = heuristic_analysis(message)
    assert analysis["intent"] == "book_call"
    assert analysis["next_action"] == "book_call"
    assert analysis["wants_meeting"] is True


@pytest.mark.parametrize(
    "message",
    [
        # "call" as a verb about naming, not a meeting.
        "What do you call this model?",
        "Do you have wireless earbuds?",
        "What's the pricing for the enterprise plan?",
        "How long is delivery to Dubai?",
    ],
)
def test_ordinary_questions_are_not_booking_requests(message):
    assert wants_to_book(message) is False
    assert heuristic_analysis(message)["intent"] != "book_call"


def test_booking_beats_a_price_question_in_the_same_message():
    """The mixed case is the one that regressed in production.

    "book a call to discuss pricing" contains a price marker, and the old
    keyword chain checked price before anything else, so it answered with a
    quote instead of a link.
    """
    analysis = heuristic_analysis("Can we book a call to discuss enterprise pricing?")
    assert analysis["intent"] == "book_call"


def test_pricing_is_still_read_as_a_price_question():
    """`pricing` does not contain `price` as a substring — it needs its own marker."""
    assert heuristic_analysis("What's the pricing for the enterprise plan?")["intent"] == (
        "price_question"
    )


def test_model_cannot_downgrade_a_booking_request():
    """The analyzer LLM calling it a price question must not win."""
    raw = {
        "intent": "price_question",
        "next_action": "answer_question",
        "wants_images": True,
        "stage": "DISCOVERY",
    }
    analysis = _coerce(raw, "Can we book a call to discuss pricing?", "NEW")

    assert analysis["intent"] == "book_call"
    assert analysis["next_action"] == "book_call"
    assert analysis["wants_meeting"] is True
    # Booking replies carry a link, never a photo carousel.
    assert analysis["wants_images"] is False


def test_booking_suppresses_every_other_directive():
    lines = directives(
        {
            "intent": "book_call",
            "next_action": "book_call",
            "stage": "PRESENTATION",
            "wants_images": True,
            "objection": "price",
        }
    )
    joined = " ".join(lines).lower()

    assert "booking link" in joined
    assert "do not list products" in joined
    # The stage and objection directives must not have survived.
    assert "present the matching items" not in joined
    assert "objection" not in joined.replace("do not list products", "")


def test_a_booking_request_is_at_least_a_qualified_lead():
    assert heuristic_analysis("Can we book a call?", "NEW")["stage"] == "QUALIFIED"
    # ...and never walks a later-stage lead backwards.
    assert heuristic_analysis("Can we book a call?", "READY_TO_BUY")["stage"] == "READY_TO_BUY"


def test_english_tenants_are_told_to_write_in_english():
    """English needs stating too.

    It used to be skipped as "the default anyway", which held only while the
    prompt was full of English context. A booking turn strips the catalogue
    out, and an English-speaking B2B tenant got a Roman Urdu reply.
    """
    from app.services.llm_service import regional_rules

    rules = " ".join(regional_rules("USD", "en")).lower()
    # Named, as the language to fall back on; the customer's own language
    # comes first, which is what lets a Spanish speaker be answered in Spanish.
    assert "use english" in rules
    assert "language and script of the customer's latest message" in rules

    urdu = " ".join(regional_rules("PKR", "ur")).lower()
    assert "use urdu" in urdu


def test_the_offline_fallback_also_answers_a_booking_request():
    """A provider outage must not turn a booking request into a catalogue prompt.

    Both providers being down once produced "tell me the item and colour" in
    reply to "can we book a call?" — on a B2B SaaS tenant, which has neither
    items nor colours.
    """
    from app.services.sales_policy import deterministic_reply

    analysis = {"intent": "book_call", "next_action": "book_call"}

    with_link = deterministic_reply(
        analysis, [], None, None, booking_url="https://cal.example/x"
    )
    assert "https://cal.example/x" in with_link
    assert "colour" not in with_link.lower()

    # No link configured: still asks about timing, still promises no callback.
    without = deterministic_reply(analysis, [], None, None)
    assert "time" in without.lower()
    assert not contains_handoff(without)


def test_offline_fallback_is_not_written_for_a_clothing_shop():
    """The same words have to work for an electronics shop and a SaaS company."""
    from app.services.sales_policy import deterministic_reply

    generic = deterministic_reply({"intent": "product_question"}, [], None, None)
    images = deterministic_reply({"intent": "image_request"}, [], None, None)

    for reply in (generic, images):
        assert "outfit" not in reply.lower()
        assert "colour" not in reply.lower()


# --------------------------------------------------------------------------
# Deferring in the words a trade customer uses
# --------------------------------------------------------------------------
# `holding_off` is the only thing between "I named a time" and a booking: in
# book(), a named time with no hold and no question is taken outright. The
# first list of people to defer to was domestic - spouse, wife, husband,
# landlord, HOA - and the shops on this product are trade suppliers and POS
# vendors. "Tuesday at 3 works, but let me run it by my team" named a time,
# matched no hold, and was booked.


@pytest.mark.parametrize(
    "message",
    [
        "tuesday at 3 works, but let me check with my team first",
        "3pm thursday is good, I just need to run it by my team",
        "friday 10am suits me, but I need approval from finance first",
        "book tuesday 2pm once I confirm with my colleagues",
        "wednesday at 11 maybe, let me sleep on it",
        "thursday 4pm, I'll get back to you to confirm",
        "pencil me in for monday at 9",
        "monday at 2 looks right, let me run it by the board",
        "I'll take friday 9am after I check with procurement",
    ],
)
def test_naming_a_time_while_deferring_books_nothing(message):
    assert booking.holding_off(message) is True, message


@pytest.mark.parametrize(
    "message",
    [
        "book it",
        "yes please book tuesday at 3",
        "go ahead and confirm",
        "that works, lock it in",
        "schedule me for friday",
        "I want to book a site visit",
        "monday does not work, book tuesday instead",
        "I'm not free monday but book me tuesday",
    ],
)
def test_a_plain_booking_is_not_mistaken_for_deferring(message):
    """The cost of this failing is the opposite one: a customer who asked to
    book is told nothing was booked."""
    assert booking.holding_off(message) is False, message


# --------------------------------------------------------------------------
# A digit picks a slot only when it is the choice
# --------------------------------------------------------------------------
# "Unit 4" in an address booked the fourth time offered. Tightening that to
# a bare digit alone also dropped "I'll take 2", which used to work, so a
# choosing verb counts when the number is last: "I want 2 bathrooms" is the
# counting case again and does not end there.

_WHEN = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
_OFFERED = [_WHEN + timedelta(hours=n) for n in range(4)]
_ZONE = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    "message",
    ["2", "option 2", "#2", "2 please", "let us do 2", "ill take 2",
     "go with 2", "lets do 2 please", "the second one"],
)
def test_choosing_the_second_time_picks_it(message):
    assert booking.chosen_slot(message, _OFFERED, _ZONE) == _OFFERED[1], message


@pytest.mark.parametrize(
    "message",
    ["Unit 4, 220 Ocean Drive", "2 bathrooms need doing", "I want 2 bathrooms",
     "we need 2 units installed", "we have 3 kids", "my number is 0300 1234567",
     "I have 2 dogs", "apartment 3b", "the quote was 2500", "I work until 4",
     "can you do 2 bathrooms"],
)
def test_a_number_that_counts_something_picks_nothing(message):
    """A pick here books a time the customer never chose."""
    assert booking.chosen_slot(message, _OFFERED, _ZONE) is None, message
