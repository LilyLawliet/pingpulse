"""Work the business refused stays refused, however long the customer talks.

The client's retest, 6 October: dog grooming was refused on turns 1 to 4,
and on turn 5 - "the 3:00 pm one please" - six construction slots were
offered, because the scope check only ever sees the last three messages that
look like they are about a job. By then those were an address, a question
about times, and a time. The grooming had dropped out of the window.

The customer never argued their way past the refusal. They just kept
talking, which every real customer does.
"""

from __future__ import annotations

import pytest

from app.models import Organization
from app.services import scope, understanding


def _contact():
    return type("Contact", (), {"contact_metadata": {}})()


def answers(monkeypatch, *verdicts):
    """The model's answers, in order, one per check."""
    queue = list(verdicts)

    async def structured(prompt, timeout):
        return queue.pop(0) if queue else None

    monkeypatch.setattr(understanding, "structured", structured)


SHOP = None


def shop():
    organization = Organization(
        name="Constrivo Group",
        sales_prompt="Residential and commercial remodeling in Miami / South Florida.",
    )
    return organization


@pytest.mark.asyncio
async def test_a_refusal_survives_an_address_and_a_time(monkeypatch):
    """The client's exact sequence, as far as scope is concerned."""
    contact = _contact()
    answers(
        monkeypatch,
        {"job": "dog grooming", "service_fits": False},
        # The address turn: the model sees a Miami address and no job at all.
        {"job": None, "service_fits": None, "place": "Miami", "area_fits": True},
        # The time turn: likewise.
        {"job": None, "service_fits": None},
    )
    first = await scope.for_contact(shop(), contact, "I need my dog groomed, can you come out?")
    assert first.service_fits is False
    assert scope.refused_job(contact) == "dog grooming"

    after_address = await scope.for_contact(
        shop(), contact, "Ali, +1 305 555 0144, 1200 Brickell Ave, Miami FL 33131"
    )
    assert after_address.service_fits is False, "the address cleared the refusal"
    assert after_address.job == "dog grooming"

    after_time = await scope.for_contact(shop(), contact, "the 3:00 pm one please")
    assert after_time.service_fits is False, "the time cleared the refusal"


@pytest.mark.asyncio
async def test_naming_work_the_shop_does_take_lifts_it(monkeypatch):
    """It must be liftable, or a customer who asked wrongly once is stuck."""
    contact = _contact()
    answers(
        monkeypatch,
        {"job": "dog grooming", "service_fits": False},
        {"job": "bathroom remodel", "service_fits": True},
    )
    await scope.for_contact(shop(), contact, "can you groom my dog")
    assert scope.refused_job(contact) == "dog grooming"

    now = await scope.for_contact(shop(), contact, "actually I need a bathroom remodel")
    assert now.service_fits is True
    assert scope.refused_job(contact) is None


@pytest.mark.asyncio
async def test_a_silent_answer_does_not_lift_it(monkeypatch):
    """`service_fits: true` with no job named is not them naming other work."""
    contact = _contact()
    answers(
        monkeypatch,
        {"job": "dog grooming", "service_fits": False},
        {"job": None, "service_fits": True},
    )
    await scope.for_contact(shop(), contact, "can you groom my dog")
    after = await scope.for_contact(shop(), contact, "yes that works")
    assert after.service_fits is False
    assert scope.refused_job(contact) == "dog grooming"


@pytest.mark.asyncio
async def test_nothing_is_remembered_when_nothing_was_refused(monkeypatch):
    contact = _contact()
    answers(monkeypatch, {"job": "bathroom remodel", "service_fits": True})
    await scope.for_contact(shop(), contact, "I need a bathroom remodel in Miami")
    assert scope.refused_job(contact) is None


@pytest.mark.asyncio
async def test_the_first_refusal_is_the_one_that_has_to_stick(monkeypatch):
    """The turn the customer is actually told no on must write the refusal down.

    Production, 6 October, Constrivo: "I need dog grooming for my two dogs"
    was read as wanting a person, so it never reached a booking turn. It was
    refused correctly - and statelessly. Three turns later "Can you book me
    in please?" named no job at all, there was nothing to refuse, and six
    consultation slots were offered for work the shop had already declined.
    """
    from app.services import booking

    async def no_documents(db, organization):
        return []

    monkeypatch.setattr(booking, "_business_documents", no_documents)
    answers(monkeypatch, {"job": "dog grooming", "service_fits": False, "place": None})

    contact = _contact()
    assert await booking.not_our_trade(None, shop(), "I need dog grooming", contact) == (
        "dog grooming"
    )
    assert scope.refused_job(contact) == "dog grooming"


@pytest.mark.asyncio
async def test_a_question_the_shop_does_answer_leaves_no_refusal(monkeypatch):
    from app.services import booking

    async def no_documents(db, organization):
        return []

    monkeypatch.setattr(booking, "_business_documents", no_documents)
    answers(monkeypatch, {"job": "kitchen remodel", "service_fits": True, "place": None})

    contact = _contact()
    assert await booking.not_our_trade(None, shop(), "I want a kitchen remodel", contact) is None
    assert scope.refused_job(contact) is None
