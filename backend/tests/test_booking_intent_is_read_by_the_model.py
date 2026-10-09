"""What a customer wants done about an appointment is read by the model, not a word list.

Every retest found the next phrasing the English patterns did not cover: a
"not" governing a list, a Spanish cancellation, a hold-off worded some other
way. The analyzer reads every message with a model anyway, so where it gave
a clean answer about the appointment, that answer decides. The patterns are
the fallback for when no model answers - which every other test in this
suite exercises, since the suite runs with no model at all.

The model's reading never acts on its own: every booking, move and
cancellation is read back and needs a yes, read by the rules. The last test
here gives it a wrong reading on purpose.
"""

from __future__ import annotations

from app.services import analyzer, booking

from .test_the_retest_of_9_october import booked_for_monday, constrivo_tenant


def model_reads(monkeypatch, readings: dict[str, tuple[str, bool, bool]]):
    """The analyzer, answering for the messages named here as a model would."""
    real = analyzer.analyse

    async def analyse(history, message, stage):
        reading = await real(history, message, stage)
        for words, (action, not_yet, asks) in readings.items():
            if words in message:
                reading["appointment"] = {
                    "action": action, "not_yet": not_yet, "asks_about_existing": asks,
                }
        return reading

    monkeypatch.setattr(analyzer, "analyse", analyse)


async def test_a_cancellation_no_pattern_knows_is_a_cancellation(org_a, monkeypatch):
    chat = await constrivo_tenant(org_a, monkeypatch)
    monday = await booked_for_monday(chat)
    said = "Please call off Monday's visit - no new booking needed."
    assert not booking.wants_cancel(said), "the rules read this one; pick a phrasing they miss"
    model_reads(monkeypatch, {"call off": ("cancel", False, False)})

    asked = await chat.say(said)
    assert asked["reply"].startswith("To confirm: cancel your"), asked["reply"]
    assert not asked["booking"]["offered"]
    done = await chat.say("YES")
    assert done["booking"]["performed"] == "cancelled", done
    assert monday


async def test_a_cancellation_in_another_language(org_a, monkeypatch):
    chat = await constrivo_tenant(org_a, monkeypatch)
    await booked_for_monday(chat)
    model_reads(monkeypatch, {"Annulez": ("cancel", False, False)})
    asked = await chat.say("Annulez mon rendez-vous de lundi, je ne veux pas le déplacer.")
    assert "cancel your" in asked["reply"], asked["reply"]
    assert not asked["booking"]["offered"]


async def test_a_move_said_without_a_word_for_moving(org_a, monkeypatch):
    chat = await constrivo_tenant(org_a, monkeypatch)
    await booked_for_monday(chat)
    said = "Monday doesn't work for me now - Wednesday at 2pm would be better"
    assert not booking.wants_move(said)
    model_reads(monkeypatch, {"would be better": ("move", False, False)})
    asked = await chat.say(said)
    assert "move your" in asked["reply"] or asked["booking"]["offered"], asked
    assert asked["booking"]["performed"] is None, "a move was made before they said yes"


async def test_not_yet_said_in_other_words_books_nothing(org_a, monkeypatch):
    chat = await constrivo_tenant(org_a, monkeypatch)
    await chat.say("I need a bathroom remodel")
    await chat.say("The address is 1200 Brickell Ave, Miami FL 33131")
    said = "Book Wednesday at 2pm for me - actually, hold that thought, my husband has the final say"
    # The rules would book it: they know "hold off" and "ask my husband", not this.
    assert booking.wants_booking(said) and not booking.holding_off(said)
    model_reads(monkeypatch, {"hold that thought": ("book", True, False)})
    answer = await chat.say(said)
    assert "Reply YES" not in answer["reply"], "a booking was put to them while they were holding off"
    assert answer["booking"]["performed"] is None


async def test_a_wrong_reading_costs_a_question_not_an_appointment(org_a, monkeypatch):
    """The model misreads a question as a cancellation. Nothing happens without their yes."""
    chat = await constrivo_tenant(org_a, monkeypatch)
    await booked_for_monday(chat)
    model_reads(monkeypatch, {"still on": ("cancel", False, False)})
    asked = await chat.say("Is my Monday visit still on?")
    assert asked["booking"]["performed"] is None
    refused = await chat.say("no")
    assert refused["booking"]["performed"] is None
    model_reads(monkeypatch, {"still on": ("none", False, True)})
    check = await chat.say("is it still on?")
    assert "confirmed" in check["reply"].lower(), check["reply"]


def test_the_reading_is_only_for_the_message_it_was_made_for():
    booking.use_reading({"action": "cancel", "not_yet": False, "asks_about_existing": False}, "call it a day")
    try:
        assert booking.wants_cancel("call it a day")
        # Another message is read by the rules, not by a reading of this one.
        assert not booking.wants_cancel("what times do you have?")
    finally:
        booking.forget_reading()
    assert not booking.wants_cancel("call it a day")


def test_only_a_clean_answer_is_taken():
    assert analyzer._appointment({"action": "cancel"}) == {
        "action": "cancel", "not_yet": False, "asks_about_existing": False,
    }
    assert analyzer._appointment({"action": "maybe cancel"}) is None
    assert analyzer._appointment("cancel") is None
    assert analyzer._appointment(None) is None


def test_the_analyzer_hands_its_reading_on():
    read = analyzer._coerce(
        {"intent": "other", "appointment": {"action": "cancel", "not_yet": False}},
        "Cancela mi cita",
        "NEW",
    )
    assert read["appointment"] == {"action": "cancel", "not_yet": False, "asks_about_existing": False}
