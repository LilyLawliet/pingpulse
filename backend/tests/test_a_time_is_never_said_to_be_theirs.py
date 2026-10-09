"""No sentence may give a customer a time the diary does not hold for them.

A client's retest, 6 October: six turns into a conversation that began with
an unsupported service, the agent wrote "Your 3:00 pm slot on Tuesday Oct 6
is noted", and then, after YES, "Your 3 pm slot on Tuesday Oct 6 is
confirmed". The backend was right throughout - it booked nothing, and
reported performed=None both times. The customer was told it was confirmed
anyway, and would have turned up.

`_BOOKING_CLAIMS` is a list of phrasings somebody thought of. It holds "slot
is confirmed" and missed this one, because "on Tuesday Oct 6" sat between the
noun and the verb; it holds "confirmed" and has no "noted" at all. Widening
it again would be the third time, and the fourth escape is already being
written somewhere.

So the guard here asks a structural question instead, and only where nothing
is booked: does this clause name a moment, and say that moment is settled?
False positives cost a rephrase. False negatives cost a customer standing
outside a locked door.
"""

from __future__ import annotations

import pytest

from app.services.booking import asserts_a_time_is_theirs, unverified_claims

# The two the client actually received, first.
CLAIMS = [
    "Your 3 pm slot on Tuesday Oct 6 is confirmed.",
    "Your 3:00 pm slot on Tuesday Oct 6 is noted.",
    "Your bathroom remodel site visit is confirmed for Tuesday Oct 6 at 10:00 am.",
    "You're all set for Tuesday at 3.",
    "Great - 10:00 am on Wednesday is locked in.",
    "I have you penciled in for tomorrow at 2pm.",
    "I have you pencilled in for tomorrow at 2pm.",
    "Tuesday 7 October at 11:00 am is now in the diary.",
    "That's booked for Friday at 9.",
    "We have you down for 24 Oct at 4pm.",
    "Your appointment on Monday at 10 is reserved.",
]

# Things the agent says constantly and must keep being able to say.
FINE = [
    "Monday 5 October at 10:00 am is free and they chose it, but it is NOT booked.",
    "Sorry, that time isn't possible: dog grooming is not something this business offers.",
    "the 12:00 pm slot on Wednesday 7 Oct is already booked. I can offer 10:00 am or 1:00 pm.",
    "Wednesday at 12 noon is already booked.",
    "Shall I book you in for Tuesday at 3pm?",
    "I can offer 10:00 am, 10:30 am, 11:00 am, 1:00 pm, 1:30 pm or 2:00 pm.",
    "We have openings on Wednesday 7 October at 10:00 am.",
    "Nothing is booked for Tuesday yet.",
    "We are fully booked on Tuesday.",
    "I could hold Tuesday at 3pm if you would like.",
    "Your 3 pm slot on Tuesday is not confirmed yet.",
    "To confirm: site visit on Tuesday 6 October at 10:00 am. Reply YES to book it.",
    "Our office is open Monday to Friday, 9:00 am to 8:00 pm.",
    "I can't confirm Tuesday at 3 until I have a working phone number.",
]


@pytest.mark.parametrize("said", CLAIMS)
def test_a_claim_is_caught(said):
    assert asserts_a_time_is_theirs(said), said


@pytest.mark.parametrize("said", FINE)
def test_an_honest_sentence_is_left_alone(said):
    found = asserts_a_time_is_theirs(said)
    assert found is None, f"{said!r} was wrongly read as a claim: {found!r}"


@pytest.mark.parametrize("said", CLAIMS)
def test_the_claim_is_reported_when_nothing_is_booked(said):
    problems = unverified_claims(said, appointment=None)
    assert problems, said
    assert "no confirmed appointment" in problems[0]


def test_nothing_is_claimed_about_a_booking_that_exists():
    """With a real appointment on record, saying so is the job, not a fault."""
    appointment = object()
    assert unverified_claims(
        "Your 3 pm slot on Tuesday Oct 6 is confirmed.", appointment=appointment
    ) == []


# --------------------------------------------- the widened guard, held open
# The client's pack asks for these in as many words: "still widen the existing
# claim patterns exactly as the report specifies" and "add the literal escaped
# sentences to the mutation harness". The harness had the fault; nothing
# failed when it was put back, which means the widening was not load-bearing.
# These make it so.
WIDENED = [
    "Your consultation is confirmed.",
    "Your consultation is now confirmed.",
    "The estimate visit is booked.",
    "Your appointment has been scheduled.",
    "That inspection has been reserved.",
    "Your site visit is set for Tuesday.",
    "The meeting is now booked.",
    "Your booking has been confirmed.",
    "We have your visit scheduled for Tuesday.",
    "We've got your consultation booked for Tuesday.",
    "I have a site visit confirmed for you on Thursday.",
    "We now have the appointment reserved for Friday.",
    "I've got your estimate scheduled on Monday.",
]


@pytest.mark.parametrize("said", WIDENED)
def test_the_widened_claim_phrasings_are_caught(said):
    """Each of these says a booking exists. None of them may pass unbooked."""
    problems = unverified_claims(said, appointment=None)
    assert problems, f"nothing objected to {said!r} with no appointment"


@pytest.mark.parametrize("said", WIDENED)
def test_the_same_phrasings_are_fine_once_it_is_real(said):
    """The guard is about the record, not about the words."""
    booked = type("Appointment", (), {"starts_at": None})()
    assert not unverified_claims(said, appointment=booked)
