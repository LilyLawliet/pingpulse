"""The rules a shop sets for how its agent behaves.

Hours, areas, services and refusals reach the model as prompt text rather than
as branches in code, because they are judgements about a business: a shop that
wants "we don't quote for anything under two hundred" served by an `if` would
need a release to change its mind.

Escalation is the deliberate exception and most of what is tested here.
Deciding a conversation needs a person must not depend on the model noticing,
because the cases that need it most — an angry customer, a refund demand, a
legal threat — are exactly the ones a sales-tuned model is inclined to smooth
over with an offer. So it is a keyword check on the customer's own words, and
it hands the conversation over rather than composing a reply.

Running through all of it: an organization that has configured nothing gets the
agent it has today. That is what makes this safe to deploy to a shop in the
middle of a conversation, and two tests hold it down directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.models import CRMContact, Organization
from app.services import agent_config


class _Org:
    """A stand-in, so the pure functions can be tested without a database."""

    def __init__(self, config=None, tz="UTC"):
        self.id = uuid.uuid4()
        self.agent_config = config or {}
        self.timezone = tz


# ------------------------------------------------------------ opening hours
def test_a_shop_that_has_not_said_its_hours_is_not_a_shut_shop():
    """None is not False. Treating "unconfigured" as "closed" would have the
    agent apologising for being shut at every hour of the day."""
    assert agent_config.is_open(_Org()) is None


def test_open_and_closed_are_read_in_the_shops_own_timezone():
    config = {"business_hours": {"tuesday": {"open": "09:00", "close": "17:00"}}}
    # 10:00 in Dubai on a Tuesday is 06:00 UTC.
    at = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)

    assert agent_config.is_open(_Org(config, "Asia/Dubai"), at) is True
    # The same instant is 02:00 in New York, where the shop would be shut.
    assert agent_config.is_open(_Org(config, "America/New_York"), at) is False


def test_a_day_that_is_not_listed_is_closed():
    config = {"business_hours": {"monday": {"open": "09:00", "close": "17:00"}}}
    tuesday = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

    assert agent_config.is_open(_Org(config), tuesday) is False


def test_hours_spanning_midnight_are_understood():
    config = {"business_hours": {"tuesday": {"open": "18:00", "close": "02:00"}}}
    late = datetime(2026, 9, 15, 23, 30, tzinfo=timezone.utc)

    assert agent_config.is_open(_Org(config), late) is True


def test_an_unusable_timezone_falls_back_instead_of_raising():
    """The worst case has to be a sentence about the wrong hours, not a reply
    path that throws."""
    assert agent_config.zone_of(_Org({}, "Mars/Olympus")) is not None
    assert agent_config.is_open(_Org({"business_hours": {}}, "Mars/Olympus")) is None


# ---------------------------------------------------------------- the block
def test_an_unconfigured_business_adds_nothing_to_the_prompt():
    """The promise to every client already running: this ships and their agent
    answers exactly as it did yesterday."""
    assert agent_config.as_prompt_block(_Org()) == ""


def test_a_config_with_nothing_usable_in_it_adds_nothing_either():
    """A block carrying only its own heading is worse than no block: it spends
    attention and says nothing."""
    assert agent_config.as_prompt_block(_Org({"services": [], "service_areas": []})) == ""


def test_services_and_areas_come_with_the_instruction_not_to_improvise():
    block = agent_config.as_prompt_block(
        _Org({"services": ["Roof repair", "Gutter cleaning"], "service_areas": ["Lahore"]})
    )

    assert "Roof repair" in block
    assert "Lahore" in block
    assert "not something this business does" in block
    assert "outside the area served" in block


def test_being_closed_is_stated_without_refusing_to_answer():
    """Closed is not an excuse to stop helping — it is a reason not to promise
    a call in ten minutes."""
    config = {"business_hours": {"monday": {"open": "09:00", "close": "17:00"}}}
    tuesday_noon = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

    block = agent_config.as_prompt_block(_Org(config), tuesday_noon)

    assert "CLOSED" in block
    assert "Answer the question anyway" in block


# ---------------------------------------------------------------- escalation
def test_a_complaint_is_handed_to_a_person():
    assert agent_config.needs_escalation("I want a refund, this is unacceptable")
    assert agent_config.needs_escalation("put me through to a manager")
    assert agent_config.needs_escalation("I'm going to speak to a lawyer")


def test_an_ordinary_question_is_not_escalated():
    assert agent_config.needs_escalation("how much are the black boots?") is None
    assert agent_config.needs_escalation("do you deliver on Sundays?") is None


def test_a_shop_can_add_its_own_escalation_words():
    org = _Org({"escalate_on": ["wholesale"]})

    assert agent_config.needs_escalation("do you do wholesale pricing?", org) == "wholesale"
    # And without the config it is an ordinary question.
    assert agent_config.needs_escalation("do you do wholesale pricing?") is None


@pytest.mark.asyncio
async def test_a_refund_demand_stops_the_agent_and_records_why(
    org_a, db_session, monkeypatch
):
    """The whole point. A sales-tuned model answering "I want a refund, this is
    terrible" with an offer is the worst outcome this system can produce, so
    the conversation is handed over before anything is generated."""
    from sqlalchemy import select

    from app.api import webhook
    from app.models import AuditLog, ChannelConfig
    from app.schemas import TwilioWebhookPayload

    channel = ChannelConfig(
        organization_id=uuid.UUID(org_a.organization_id),
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider="QR_SESSION",
        phone_number="+923097209908",
        session_status="AUTHENTICATED",
    )
    db_session.add(channel)
    await db_session.flush()

    replies: list = []
    monkeypatch.setattr("app.services.outbox.deliver", lambda *a, **k: replies.append(a))

    payload = TwilioWebhookPayload(
        From="whatsapp:+15550777",
        To="whatsapp:+923097209908",
        Body="the boots fell apart, I want a refund",
        MessageSid="SM-refund",
    )
    result = await webhook.process_inbound_message(db_session, payload, channel)

    assert result["status"] == "escalated"
    assert result["reason"] == "refund"
    assert replies == [], "a complaint was answered by the agent"

    contact = (
        await db_session.execute(
            select(CRMContact).where(CRMContact.phone_number == "+15550777")
        )
    ).scalar_one()
    assert contact.ai_enabled is False, "the agent was left running on a complaint"

    logged = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "contact.escalated")
        )
    ).scalars().all()
    assert logged and logged[0].changes["trigger"] == "refund"


# ------------------------------------------------------------------- the API
@pytest.mark.asyncio
async def test_operating_rules_round_trip(org_a, db_session):
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={
            "timezone": "Asia/Dubai",
            "agent_config": {
                "services": ["Roof repair"],
                "business_hours": {"monday": {"open": "09:00", "close": "17:00"}},
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["timezone"] == "Asia/Dubai"

    read = (await org_a.get("/api/v1/agent-config")).json()
    assert read["agent_config"]["services"] == ["Roof repair"]


@pytest.mark.asyncio
async def test_a_nonsense_timezone_is_refused_at_the_door(org_a):
    """Caught here rather than at reply time, where it would surface as the
    agent describing the wrong opening hours."""
    response = await org_a._client.put(
        "/api/v1/agent-config", headers=org_a.headers, json={"timezone": "Mars/Olympus"}
    )

    assert response.status_code == 422
    assert "IANA" in response.json()["detail"]


@pytest.mark.asyncio
async def test_the_rules_reach_the_prompt(org_a, db_session):
    from app.services.llm_service import build_prompt

    await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"services": ["Roof repair"], "never_promise": "same-day work"}},
    )

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    prompt = build_prompt(organization, None, [], "do you fix roofs?")

    assert "HOW THIS BUSINESS OPERATES" in prompt
    assert "Roof repair" in prompt
    assert "same-day work" in prompt


@pytest.mark.asyncio
async def test_an_untouched_organization_gets_the_prompt_it_had_before(org_a, db_session):
    from app.services.llm_service import build_prompt

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert "HOW THIS BUSINESS OPERATES" not in build_prompt(organization, None, [], "hello")
