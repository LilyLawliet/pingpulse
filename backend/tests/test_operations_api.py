"""Status, the sandbox, and counting things over a span of time.

Three screens' worth of backend, held together by one idea: an operator should
be able to answer "is it working?" and "what would it say?" without messaging a
real customer to find out.

The sandbox is the part with teeth. Its whole value is that nothing leaves the
building — no WhatsApp call, no contact created, no message stored — while
still exercising the *real* prompt assembly, knowledge base and operating
rules. A sandbox that tests a different prompt from the live one tests nothing,
and a sandbox that quietly writes a contact turns a trial run into a lead in
somebody's pipeline. Both are asserted.

Status degrades in pieces on purpose. An unreachable bridge still reports the
number and the last message from the database, because "we cannot tell you
anything" is the least useful possible answer to "have replies stopped?".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy import func, select

from app.models import ChannelConfig, CRMContact, Message


async def _channel(org_a, db_session, provider="QR_SESSION", status="AUTHENTICATED"):
    channel = ChannelConfig(
        organization_id=uuid.UUID(org_a.organization_id),
        channel="whatsapp",
        provider="twilio",
        whatsapp_provider=provider,
        phone_number="+923097209908",
        session_status=status,
    )
    db_session.add(channel)
    await db_session.flush()
    return channel


# -------------------------------------------------------------------- status
@pytest.mark.asyncio
async def test_a_shop_with_no_number_is_told_so_plainly(org_a):
    body = (await org_a.get("/api/v1/whatsapp/status")).json()

    assert body["connected"] is False
    assert "No WhatsApp number" in body["reason"]


@pytest.mark.asyncio
async def test_a_paired_session_reports_connected(org_a, db_session, monkeypatch):
    await _channel(org_a, db_session)
    monkeypatch.setattr(
        "app.api.operations._bridge_reachable", lambda _channel: _true()
    )

    body = (await org_a.get("/api/v1/whatsapp/status")).json()

    assert body["connected"] is True
    assert body["provider"] == "QR_SESSION"
    assert body["phone_number"] == "+923097209908"


async def _true():
    return True


@pytest.mark.asyncio
async def test_a_dropped_session_is_not_connected(org_a, db_session, monkeypatch):
    await _channel(org_a, db_session, status="DISCONNECTED")
    monkeypatch.setattr("app.api.operations._bridge_reachable", lambda _c: _true())

    body = (await org_a.get("/api/v1/whatsapp/status")).json()

    assert body["connected"] is False


@pytest.mark.asyncio
async def test_an_unreachable_bridge_still_reports_what_the_database_knows(
    org_a, db_session, monkeypatch
):
    """"We cannot tell you anything" is the least useful answer to "have
    replies stopped?"."""
    channel = await _channel(org_a, db_session)

    async def unreachable(_channel):
        return False

    monkeypatch.setattr("app.api.operations._bridge_reachable", unreachable)

    contact = CRMContact(
        organization_id=uuid.UUID(org_a.organization_id),
        phone_number="+15551111",
        pipeline_stage="NEW_LEAD",
        sales_stage="NEW",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()
    db_session.add(
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender="user",
            content="hello",
        )
    )
    await db_session.flush()

    body = (await org_a.get("/api/v1/whatsapp/status")).json()

    assert body["bridge_reachable"] is False
    assert body["phone_number"] == "+923097209908"
    assert body["last_inbound_at"] is not None


# ------------------------------------------------------------------- sandbox
@pytest.mark.asyncio
async def test_the_sandbox_sends_nothing_and_stores_nothing(
    org_a, db_session, monkeypatch, offline_embeddings
):
    """The whole point of it. A trial run that quietly created a contact would
    put a fake lead in somebody's pipeline."""
    from sqlalchemy import select

    from app.schemas import GenerationResult
    from app.services import llm_service, outbox

    sent: list = []
    monkeypatch.setattr(outbox, "deliver", lambda *a, **k: sent.append(a))

    async def generated(*_a, **_k):
        return GenerationResult(
            provider="groq", text="We do fix roofs, yes.", prompt_used="p", latency_ms=12
        )

    monkeypatch.setattr(llm_service, "generate_reply", generated)

    response = await org_a.post(
        "/api/v1/agent/simulate", json={"message": "do you fix roofs?"}
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "We do fix roofs, yes."
    assert response.json()["sent"] is False
    assert sent == [], "the sandbox sent a real WhatsApp message"

    contacts = (
        await db_session.execute(
            select(CRMContact).where(
                CRMContact.organization_id == uuid.UUID(org_a.organization_id)
            )
        )
    ).scalars().all()
    assert contacts == [], "the sandbox created a contact"

    messages = (
        await db_session.execute(
            select(Message).where(
                Message.organization_id == uuid.UUID(org_a.organization_id)
            )
        )
    ).scalars().all()
    assert messages == [], "the sandbox stored a message"


@pytest.mark.asyncio
async def test_the_sandbox_shows_an_escalation_instead_of_answering(
    org_a, offline_embeddings
):
    """Somebody testing "I want a refund" should see that a real conversation
    stops there, not a smooth reply that would never have been sent."""
    response = await org_a.post(
        "/api/v1/agent/simulate", json={"message": "I want a refund, this is terrible"}
    )

    body = response.json()
    assert body["escalated"] is True
    assert body["reason"] == "refund"
    assert body["reply"] is None


@pytest.mark.asyncio
async def test_an_empty_sandbox_message_is_refused(org_a):
    response = await org_a.post("/api/v1/agent/simulate", json={"message": "   "})

    assert response.status_code == 422


# --------------------------------------------------------------------- stats
@pytest.mark.asyncio
async def test_a_window_narrows_the_counting(org_a, db_session):
    """Messages are counted by when they were sent, so "7 days" means the same
    thing everywhere on the screen."""
    contact = CRMContact(
        organization_id=uuid.UUID(org_a.organization_id),
        phone_number="+15552222",
        pipeline_stage="NEW_LEAD",
        sales_stage="NEW",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    db_session.add_all([
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender="user",
            content="recent",
            created_at=now - timedelta(days=1),
        ),
        Message(
            organization_id=uuid.UUID(org_a.organization_id),
            contact_id=contact.id,
            sender="user",
            content="ancient",
            created_at=now - timedelta(days=60),
        ),
    ])
    await db_session.flush()

    everything = (await org_a.get("/api/v1/stats?window=all")).json()
    week = (await org_a.get("/api/v1/stats?window=7d")).json()

    assert everything["messages"] == 2
    assert week["messages"] == 1


@pytest.mark.asyncio
async def test_booked_stops_being_one_ambiguous_word(org_a, db_session):
    """An appointment, a job and a sale are three businesses' words for the
    same milestone. The column carries the meaning; the screen shows whatever
    that tenant calls it."""
    from app.services import pipelines

    org_id = uuid.UUID(org_a.organization_id)
    await pipelines.seed(db_session, org_id)
    db_session.add_all([
        CRMContact(
            organization_id=org_id,
            phone_number="+15553333",
            pipeline_stage="ESTIMATE_SCHEDULED",
            sales_stage="NEW",
            tags=[],
        ),
        CRMContact(
            organization_id=org_id,
            phone_number="+15554444",
            pipeline_stage="WON",
            sales_stage="CLOSED",
            tags=[],
        ),
    ])
    await db_session.flush()

    body = (await org_a.get("/api/v1/stats")).json()

    assert body["outcomes"]["booked"] == 1
    assert body["outcomes"]["won"] == 1
    assert body["outcome_labels"]["booked"] == "Estimate scheduled"


@pytest.mark.asyncio
async def test_a_renamed_board_reports_its_own_words(org_a, db_session):
    await org_a._client.put(
        "/api/v1/pipeline",
        headers=org_a.headers,
        json={
            "stages": [
                {"key": "ENQUIRY", "label": "Enquiry"},
                {"key": "APPT", "label": "Appointment booked", "outcome": "booked"},
            ]
        },
    )
    db_session.add(
        CRMContact(
            organization_id=uuid.UUID(org_a.organization_id),
            phone_number="+15555555",
            pipeline_stage="APPT",
            sales_stage="NEW",
            tags=[],
        )
    )
    await db_session.flush()

    body = (await org_a.get("/api/v1/stats")).json()

    assert body["outcome_labels"]["booked"] == "Appointment booked"
    assert body["outcomes"]["booked"] == 1


# ---------------------------------------------------------------- error log
@pytest.mark.asyncio
async def test_errors_are_listed_and_can_be_marked_done(org_a, db_session):
    from app.services import oplog

    await oplog.fail(
        db_session,
        oplog.WHATSAPP,
        "The bridge did not answer",
        organization_id=uuid.UUID(org_a.organization_id),
    )
    await db_session.flush()

    listed = (await org_a.get("/api/v1/errors")).json()["errors"]
    assert len(listed) == 1
    assert listed[0]["category"] == "whatsapp"

    resolved = await org_a.post(f"/api/v1/errors/{listed[0]['id']}/resolve")
    assert resolved.status_code == 200

    assert (await org_a.get("/api/v1/errors")).json()["errors"] == []


@pytest.mark.asyncio
async def test_one_tenants_errors_are_not_anothers(org_a, org_b, db_session):
    from app.services import oplog

    await oplog.fail(
        db_session,
        oplog.LLM,
        "Groq refused",
        organization_id=uuid.UUID(org_a.organization_id),
    )
    await db_session.flush()

    assert (await org_b.get("/api/v1/errors")).json()["errors"] == []


