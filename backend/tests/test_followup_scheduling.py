"""Follow-ups an operator schedules by hand.

The automatic sequence only nudges warm conversations, four hours later, twice
at most. That is right for customers and impossible to watch working, so the
dashboard can drive the same machinery with a delay of the operator's choosing.

Same machinery is the point. These protect the properties that make it one
feature rather than two:

  * a customer who writes back cancels a pending nudge, whichever way it was
    scheduled — the alternative is messaging someone who is already talking
    to you;
  * cancellation is a database write, not a broker operation, because revoking
    a queued task is unreliable and this must not be;
  * the stage gate belongs to the automatic sequence only. An operator asking
    for a follow-up has already made that judgement.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import CRMContact


async def _contact(org, db_session, stage: str = "NEW") -> CRMContact:
    contact = CRMContact(
        organization_id=uuid.UUID(org.organization_id),
        phone_number=f"+9715000{uuid.uuid4().int % 10000:04d}",
        pipeline_stage="LEAD",
        sales_stage=stage,
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


@pytest.fixture
def queued(monkeypatch):
    """Capture what would be handed to Celery, without a broker."""
    from app import tasks

    calls = []

    def fake_apply_async(kwargs=None, countdown=None, **_rest):
        calls.append({"kwargs": kwargs or {}, "countdown": countdown})

    monkeypatch.setattr(tasks.schedule_customer_followup, "apply_async", fake_apply_async)
    return calls


# ------------------------------------------------------------------ scheduling
@pytest.mark.asyncio
async def test_an_operator_can_schedule_a_nudge(org_a, db_session, queued):
    contact = await _contact(org_a, db_session)

    response = await org_a.post(
        f"/api/v1/contacts/{contact.id}/followup",
        json={"minutes": 2, "message": "Still thinking it over?"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["scheduled"] is True
    assert body["message"] == "Still thinking it over?"
    assert body["due_at"]

    assert len(queued) == 1
    assert queued[0]["countdown"] == 120, "two minutes, not two hours"
    assert queued[0]["kwargs"]["body"] == "Still thinking it over?"
    assert queued[0]["kwargs"]["manual"] is True


@pytest.mark.asyncio
async def test_the_wording_is_optional(org_a, db_session, queued):
    """Empty means the agent's own nudge, not an empty message."""
    contact = await _contact(org_a, db_session)

    response = await org_a.post(
        f"/api/v1/contacts/{contact.id}/followup", json={"minutes": 5, "message": "   "}
    )

    assert response.status_code == 202
    assert response.json()["message"] is None
    assert queued[0]["kwargs"]["body"] is None


@pytest.mark.asyncio
async def test_a_cold_conversation_can_still_be_followed_up(org_a, db_session, queued):
    """The stage gate is the automatic sequence's rule, not the operator's.

    schedule_followups refuses anything outside QUALIFIED / PRESENTATION /
    NEGOTIATION, which is correct for a policy that runs unattended. Applying
    it here would mean the button silently did nothing on exactly the
    conversations someone is most likely to want to chase.
    """
    contact = await _contact(org_a, db_session, stage="NEW")

    response = await org_a.post(
        f"/api/v1/contacts/{contact.id}/followup", json={"minutes": 10}
    )

    assert response.status_code == 202
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_the_pending_nudge_is_visible_on_the_contact(org_a, db_session, queued):
    """The dashboard reads this to show what is scheduled, and to cancel it."""
    contact = await _contact(org_a, db_session)
    await org_a.post(f"/api/v1/contacts/{contact.id}/followup", json={"minutes": 3})

    listed = (await org_a.get("/api/v1/contacts")).json()
    mine = next(row for row in listed if row["id"] == str(contact.id))

    assert mine["metadata"]["followup_due_at"], "nothing for the panel to show"
    assert mine["metadata"]["followup_token"]


# --------------------------------------------------------------- cancellation
@pytest.mark.asyncio
async def test_cancelling_clears_the_token_the_task_checks(org_a, db_session, queued):
    """Cancellation is a database write, deliberately.

    Revoking a queued task is unreliable across brokers, so the task always
    runs and exits when the token no longer matches. That makes "cancelled" a
    single row update rather than something that can half-happen.
    """
    contact = await _contact(org_a, db_session)
    await org_a.post(f"/api/v1/contacts/{contact.id}/followup", json={"minutes": 30})

    assert (contact.contact_metadata or {}).get("followup_token")

    response = await org_a.delete(f"/api/v1/contacts/{contact.id}/followup")

    assert response.status_code == 200
    assert response.json()["scheduled"] is False

    metadata = contact.contact_metadata or {}
    assert "followup_token" not in metadata
    assert "followup_due_at" not in metadata


@pytest.mark.asyncio
async def test_a_customer_writing_back_cancels_a_scheduled_nudge(
    org_a, db_session, queued, monkeypatch, client
):
    """Never nudge someone who is already talking to you.

    The webhook clears the token on every inbound message, which is what makes
    the automatic sequence safe. A manually scheduled nudge uses the same
    token, so it inherits that — and this is the test that says so, because a
    second mechanism here would be easy to add and would quietly break it.
    """
    from app.config import settings
    from app.models import ChannelConfig
    from app.services import outbox

    monkeypatch.setattr(settings, "twilio_validate_signature", False)

    contact = await _contact(org_a, db_session)
    db_session.add(
        ChannelConfig(
            organization_id=contact.organization_id,
            channel="whatsapp",
            provider="twilio",
            phone_number="+14155552222",
        )
    )
    await db_session.flush()

    await org_a.post(f"/api/v1/contacts/{contact.id}/followup", json={"minutes": 60})
    assert (contact.contact_metadata or {}).get("followup_token")

    async def sent(*_a, **_k):
        return True, "SM-reply"

    monkeypatch.setattr(outbox.whatsapp, "send_message", sent)

    await client.post(
        "/api/v1/whatsapp/webhook",
        data={
            "MessageSid": "SM_they_replied",
            "From": f"whatsapp:{contact.phone_number}",
            "To": "whatsapp:+14155552222",
            "Body": "yes still interested!",
            "NumMedia": "0",
        },
    )

    assert "followup_token" not in (contact.contact_metadata or {}), (
        "a customer who replied is about to be nudged anyway"
    )


# ------------------------------------------------------------------- tenancy
@pytest.mark.asyncio
async def test_another_tenants_contact_cannot_be_scheduled(org_a, org_b, db_session, queued):
    contact = await _contact(org_a, db_session)

    response = await org_b.post(
        f"/api/v1/contacts/{contact.id}/followup", json={"minutes": 5}
    )

    assert response.status_code == 404, "an id must not be probeable for existence"
    assert queued == []


@pytest.mark.asyncio
async def test_a_nonsense_delay_is_refused(org_a, db_session, queued):
    contact = await _contact(org_a, db_session)

    for minutes in (0, -5, 20000):
        response = await org_a.post(
            f"/api/v1/contacts/{contact.id}/followup", json={"minutes": minutes}
        )
        assert response.status_code == 422, f"{minutes} should not be accepted"
    assert queued == []


# ---------------------------------------------------- what the worker does
def test_a_manual_nudge_uses_the_wording_it_was_given(monkeypatch):
    """The task is shared with the automatic sequence; only the inputs differ.

    Both the fake worker and asyncio.run are replaced. The real entry point
    calls asyncio.run, which builds and then tears down an event loop — doing
    that inside the test session disposes of the loop every other async test
    is using, and they all fail with "coroutine was never awaited". What is
    being checked here is argument forwarding, which needs no loop at all.
    """
    from app import tasks

    seen = {}

    def fake_run(contact_id, organization_id, token, attempt, body=None, manual=False):
        seen.update({"body": body, "manual": manual, "attempt": attempt, "token": token})
        return "sent"

    monkeypatch.setattr(tasks, "_run_followup", fake_run)
    monkeypatch.setattr(tasks.asyncio, "run", lambda result: result)

    outcome = tasks.schedule_customer_followup(
        contact_id=str(uuid.uuid4()),
        organization_id=str(uuid.uuid4()),
        token="tok",
        attempt=1,
        body="Any thoughts on the black pair?",
        manual=True,
    )

    assert outcome == "sent"
    assert seen["body"] == "Any thoughts on the black pair?"
    assert seen["manual"] is True


async def test_a_sent_followup_is_put_on_the_operators_screen(monkeypatch):
    """Sending it and storing it is not the same as showing it.

    The worker did both and the conversation on screen stayed empty, because
    announcing was the one step nobody had written. Two events, because the
    dashboard uses them differently: `outbound_message` puts the bubble in the
    open thread, `sync` refreshes the list and the counts.
    """
    import uuid as _uuid

    from app import tasks
    from app.services.ws_manager import manager

    seen = []

    async def capture(event_type, payload=None):
        seen.append((event_type, payload or {}))

    monkeypatch.setattr(manager, "broadcast", capture)

    contact = type("C", (), {"id": _uuid.uuid4(), "organization_id": _uuid.uuid4()})()
    nudge = type("M", (), {"id": _uuid.uuid4(), "twilio_sid": "SM1", "delivery_status": "SENT"})()

    await tasks._announce(contact, nudge, "Still thinking about those shoes?")

    assert [name for name, _ in seen] == ["outbound_message", "sync"]

    payload = seen[0][1]
    assert payload["contact_id"] == str(contact.id), "the thread it belongs to"
    assert payload["content"] == "Still thinking about those shoes?"
    assert payload["message_id"] == str(nudge.id), "so a later re-read does not double it"
    assert payload["delivery_status"] == "SENT"


async def test_a_broken_socket_never_costs_a_delivered_followup(monkeypatch):
    """The message is already on the customer's phone by this point."""
    import uuid as _uuid

    from app import tasks
    from app.services.ws_manager import manager

    async def explode(*_a, **_k):
        raise RuntimeError("no dashboards, no redis, nothing")

    monkeypatch.setattr(manager, "broadcast", explode)

    contact = type("C", (), {"id": _uuid.uuid4(), "organization_id": _uuid.uuid4()})()
    nudge = type("M", (), {"id": _uuid.uuid4(), "twilio_sid": None, "delivery_status": "SENT"})()

    await tasks._announce(contact, nudge, "nudge")  # must not raise


async def test_the_worker_stores_the_nudge_and_announces_it(tmp_path, monkeypatch):
    """The whole worker path, on its own database, as it runs in production.

    Worth the setup because this is the path that failed silently. The worker
    opens its own engine — it has no FastAPI lifespan and no request session —
    so nothing above this test exercised it, and the nudge reaching WhatsApp
    while the dashboard stayed empty looked like a delivery problem when it was
    a reporting one.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import tasks
    from app.config import settings
    from app.database import Base
    from app.models import ChannelConfig, CRMContact, Message, Organization
    from app.services import outbox, whatsapp
    from app.services.ws_manager import manager

    url = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/worker.db"
    monkeypatch.setattr(settings, "database_url", url)

    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    token = tasks.new_token()
    async with factory() as setup:
        organization = Organization(name="Worker Co", sales_prompt="Sell things.")
        setup.add(organization)
        await setup.flush()
        setup.add(
            ChannelConfig(
                organization_id=organization.id,
                channel="whatsapp",
                provider="twilio",
                phone_number="+14155554444",
            )
        )
        contact = CRMContact(
            organization_id=organization.id,
            phone_number="+971500001111",
            pipeline_stage="LEAD",
            sales_stage="NEW",
            tags=[],
            contact_metadata={"followup_token": token},
        )
        setup.add(contact)
        await setup.commit()
        contact_id, organization_id = str(contact.id), str(organization.id)

    async def delivered(*_a, **_k):
        return outbox.Delivery(outbox.SENT, "SM-nudge", "delivered")

    monkeypatch.setattr(outbox, "deliver", delivered)

    announced = []

    async def capture(event_type, payload=None):
        announced.append((event_type, payload or {}))

    monkeypatch.setattr(manager, "broadcast", capture)

    outcome = await tasks._run_followup(
        contact_id, organization_id, token, attempt=1,
        body_override="Still thinking about those shoes?", manual=True,
    )

    assert outcome == "sent"

    async with factory() as check:
        stored = (await check.execute(select(Message))).scalars().all()
        assert len(stored) == 1, "the operator has nothing to catch up on"
        assert stored[0].content == "Still thinking about those shoes?"
        assert stored[0].delivery_status == outbox.SENT

    assert [name for name, _ in announced] == ["outbound_message", "sync"], (
        "stored but never announced — the dashboard stays empty, which is the bug"
    )
    assert announced[0][1]["message_id"] == str(stored[0].id)

    await engine.dispose()
