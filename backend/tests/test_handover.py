"""When the agent cannot do it, a person is told — and only then is one promised.

Beluga's `notify_config` was `{}` in production. Every event defaults to on,
so the escalation genuinely fired when ZO asked for a human, and there was
nowhere on earth for it to go: no address, no device. The alert was recorded
and delivered to nobody.

Meanwhile the agent had a customer asking to book, no opening hours configured
so nothing to offer, and a policy forbidding it from saying a colleague would
follow up. The only moves left were to invent a time or to become the
colleague. It tried both, in that order:

    Your appointment is confirmed for September 19, 2026 at 1:00 AM EST
    I am a live team member here

These cover the third move, which is the true one: raise the alert, check it
has somewhere to land, and only then let the reply mention a person.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.models import (
    NOTIFY_EVENTS,
    Organization,
    OrganizationMember,
    User,
)
from app.services import booking, notifications


# ------------------------------------------------------ the event was off
def test_the_agent_being_stuck_is_worth_telling_somebody_about():
    """`unanswered` defaulted to false. It is the event that fires when the
    agent is out of its depth, which is the exact moment a person needs to
    know - and it was the one nobody was subscribed to."""
    assert notifications.default_for("unanswered") is True


def test_the_noisy_events_are_still_quiet_by_default():
    """The reasoning for quiet defaults is sound and stays. A channel that
    cries wolf gets switched off within a week, and then the escalation that
    mattered is missed too."""
    assert notifications.default_for("new_lead") is False


def test_every_event_still_has_an_explicit_default():
    assert all(isinstance(on, bool) for _, _, on in NOTIFY_EVENTS)


# ------------------------------------------- alerts reach somebody by default
async def _org_with_owner(db_session, email: str, role: str = "OWNER") -> Organization:
    organization = Organization(name="Beluga Group", sales_prompt="Sell remodels.")
    db_session.add(organization)
    user = User(email=email, full_name="Owner", password_hash="x")
    db_session.add(user)
    await db_session.flush()

    db_session.add(
        OrganizationMember(
            organization_id=organization.id, user_id=user.id, role=role
        )
    )
    await db_session.flush()
    return organization


@pytest.mark.asyncio
async def test_a_shop_that_configured_nothing_still_has_an_address(db_session):
    """The production state, exactly: notify_config {} and a customer asking
    for a human. The alert had nowhere to go."""
    organization = await _org_with_owner(db_session, "owner@example.org")
    organization.notify_config = {}

    assert notifications.email_for(organization) == ""
    assert await notifications.address_for(db_session, organization) == "owner@example.org"


@pytest.mark.asyncio
async def test_a_configured_address_still_wins(db_session):
    organization = await _org_with_owner(db_session, "owner@example.org")
    organization.notify_config = {"email": "alerts@theshop.com", "events": {}}

    assert await notifications.address_for(db_session, organization) == "alerts@theshop.com"


@pytest.mark.asyncio
async def test_an_owner_is_preferred_over_an_admin(db_session):
    organization = await _org_with_owner(db_session, "admin@example.org", role="ADMIN")
    owner = User(email="owner@example.org", full_name="Owner", password_hash="x")
    db_session.add(owner)
    await db_session.flush()
    db_session.add(
        OrganizationMember(
            organization_id=organization.id, user_id=owner.id, role="OWNER"
        )
    )
    await db_session.flush()

    assert await notifications.address_for(db_session, organization) == "owner@example.org"


@pytest.mark.asyncio
async def test_a_placeholder_address_is_not_an_address(db_session):
    """An alert sent somewhere unreachable is the failure this is fixing, not
    a different one."""
    organization = await _org_with_owner(db_session, "owner@pingpulse.local")

    assert await notifications.address_for(db_session, organization) == ""


@pytest.mark.asyncio
async def test_an_organization_with_nobody_cannot_be_reached(db_session, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "alerts@pingpulse.app")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    organization = Organization(name="Orphan Co", sales_prompt="Sell things.")
    db_session.add(organization)
    await db_session.flush()

    assert await notifications.can_reach(db_session, organization) is False


@pytest.mark.asyncio
async def test_an_organization_with_an_owner_can_be_reached(db_session, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "alerts@pingpulse.app")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    organization = await _org_with_owner(db_session, "owner@example.org")

    assert await notifications.can_reach(db_session, organization) is True


# ------------------------------------------ the promise needs something behind it
@pytest.mark.asyncio
async def test_a_handoff_promise_is_refused_when_nobody_was_told(monkeypatch):
    """The original rule, unchanged for the case it was written for."""
    from app.services import llm_service
    from types import SimpleNamespace

    org = SimpleNamespace(
        name="Beluga Group",
        sales_prompt="Sell remodels.",
        target_tone="Warm",
        product_rules="",
        agent_config={},
        timezone="America/New_York",
    )
    contact = SimpleNamespace(
        name="ZO", phone_number="+13057483629", pipeline_stage="LEAD",
        category_interest=None, city=None, shoe_size=None,
        colour_preference=None, budget_note=None,
    )

    async def promises(prompt):
        return "Our team will get back to you shortly."

    monkeypatch.setattr(llm_service, "_call_groq", promises)
    monkeypatch.setattr(llm_service, "_call_gemini", promises)

    result = await llm_service.generate_reply(org, contact, [], "can someone call me?")

    assert "team will" not in result.text
    assert result.provider == "none"


@pytest.mark.asyncio
async def test_a_handoff_promise_is_allowed_once_a_person_has_been_alerted(monkeypatch):
    """Same sentence, same model, different backing. The permission comes from
    the alert having been raised, never from the model's judgement."""
    from app.services import llm_service
    from types import SimpleNamespace

    org = SimpleNamespace(
        name="Beluga Group",
        sales_prompt="Sell remodels.",
        target_tone="Warm",
        product_rules="",
        agent_config={},
        timezone="America/New_York",
    )
    contact = SimpleNamespace(
        name="ZO", phone_number="+13057483629", pipeline_stage="LEAD",
        category_interest=None, city=None, shoe_size=None,
        colour_preference=None, budget_note=None,
    )

    calls = []

    async def promises(prompt):
        calls.append(prompt)
        return "Our team will get back to you with available times."

    monkeypatch.setattr(llm_service, "_call_groq", promises)

    result = await llm_service.generate_reply(
        org, contact, [], "can someone call me?", handoff_allowed=True
    )

    assert "team will get back to you" in result.text
    assert result.provider == "groq"
    # Accepted on the first attempt. Without this the test passes even with an
    # unconditional guard, because the rewrite returns the same sentence and
    # the post-rewrite check is the only thing that looks at it again - so the
    # permission would be doing nothing except buying a second model call.
    assert len(calls) == 1, "the reply was rejected and regenerated"


@pytest.mark.asyncio
async def test_being_allowed_to_hand_over_does_not_license_claiming_to_be_human(
    monkeypatch,
):
    """The two are separate permissions. Saying a colleague will call is a
    statement about the backend; saying "I am a live team member" is false
    whatever the backend did."""
    from app.services import llm_service
    from types import SimpleNamespace

    org = SimpleNamespace(
        name="Beluga Group", sales_prompt="Sell remodels.", target_tone="Warm",
        product_rules="", agent_config={}, timezone="America/New_York",
    )
    contact = SimpleNamespace(
        name="ZO", phone_number="+13057483629", pipeline_stage="LEAD",
        category_interest=None, city=None, shoe_size=None,
        colour_preference=None, budget_note=None,
    )

    async def pretends(prompt):
        return "I am a live team member here and will get back to you."

    monkeypatch.setattr(llm_service, "_call_groq", pretends)
    monkeypatch.setattr(llm_service, "_call_gemini", pretends)

    result = await llm_service.generate_reply(
        org, contact, [], "are you a person?", handoff_allowed=True
    )

    assert "live team member" not in result.text


# ------------------------------------------------- the message that started it
def test_zos_actual_message_reads_as_a_booking_request():
    """If this stops matching, the handover never fires and the agent is back
    to having only the two bad moves."""
    assert booking.wants_booking(
        "Yes, I'd like to schedule a consultation. What times do you have?"
    )


@pytest.mark.parametrize(
    "message",
    [
        "can we book a time?",
        "I'd like to schedule a consultation",
        "when can you come out?",
        "what times do you have available?",
        "can someone come and look at the bathroom?",
    ],
)
def test_the_ways_people_ask_to_book(message):
    assert booking.wants_booking(message), f"{message!r} was not read as a booking ask"


def test_a_shop_with_no_hours_cannot_book(db_session):
    """Beluga's real configuration. Booking is correctly off, which is what
    leaves the agent with nothing to offer and makes the handover necessary."""

    class Beluga:
        timezone = "America/New_York"
        agent_config = {}

    assert booking.booking_enabled(Beluga()) is False
