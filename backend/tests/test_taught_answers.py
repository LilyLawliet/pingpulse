"""The agent learning from the shop's own answers, and only once approved.

The loop: a customer asks something the documents don't answer; the agent says
the team will reply and the question is written down. A person replies from the
dashboard; the reply is kept beside the question. In Setup > Learning the owner
presses "Teach", and the next customer who asks gets that answer.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import CRMContact, KnowledgeDocument, LearnedAnswer, Organization
from app.services import outbox, retrieval, taught, unanswered

from .conftest import _session_for


async def _shop(tenant):
    session = _session_for(tenant._client)
    organization = await session.get(Organization, uuid.UUID(tenant.organization_id))
    contact = CRMContact(organization_id=organization.id, phone_number="923001230000", name="Irsa")
    session.add(contact)
    await session.commit()
    return session, organization, contact


@pytest.fixture
def quiet(monkeypatch):
    async def deliver(channel, to, text, **_kwargs):
        return outbox.Delivery(outbox.SENT, "x", "delivered")

    async def record(*_a, **_k):
        return None

    monkeypatch.setattr(outbox, "deliver", deliver)
    monkeypatch.setattr("app.services.notifications.raise_and_send", record)


async def test_question_reply_teach_and_the_agent_knows_it(org_a, quiet):
    session, organization, contact = await _shop(org_a)
    await unanswered.handle(session, organization, contact, "quickbooks", "does it connect to QuickBooks?")
    await session.commit()

    listed = (await org_a.get("/api/v1/learning/answers")).json()
    assert [a["question"] for a in listed["waiting"]] == ["does it connect to QuickBooks?"]

    # The team replies from the dashboard, in two messages.
    for text in ("Not yet - QuickBooks isn't supported.", "You can export sales to Excel instead."):
        sent = await org_a.post("/api/v1/messages/send", json={"contact_id": str(contact.id), "content": text})
        assert sent.status_code == 201, sent.text

    listed = (await org_a.get("/api/v1/learning/answers")).json()
    suggestion = listed["suggested"][0]
    assert "QuickBooks isn't supported" in suggestion["answer"] and "export sales" in suggestion["answer"]
    assert suggestion["customer"] == "Irsa"
    assert listed["waiting"] == []

    # Nothing is known until it is taught.
    known = (await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.source == taught.SOURCE))).scalars().all()
    assert known == []

    taught_now = await org_a.post(
        f"/api/v1/learning/answers/{suggestion['id']}/teach",
        json={"question": "Does Tallybird connect to QuickBooks?", "answer": suggestion["answer"]},
    )
    assert taught_now.status_code == 200, taught_now.text
    found = await retrieval.search(session, organization.id, "do you work with quickbooks", limit=3)
    assert any("QuickBooks isn't supported" in chunk.content for chunk in found)

    # And back out again in one press.
    await org_a.post(f"/api/v1/learning/answers/{suggestion['id']}/dismiss")
    session.expire_all()
    known = (await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.source == taught.SOURCE))).scalars().all()
    assert known == []


async def test_an_old_question_is_not_answered_by_a_reply_days_later(org_a, quiet):
    session, organization, contact = await _shop(org_a)
    row = await taught.record_question(session, organization.id, contact.id, "do you have a shop in Karachi?")
    row.asked_at = datetime.now(timezone.utc) - timedelta(days=5)
    await session.commit()
    await org_a.post("/api/v1/messages/send", json={"contact_id": str(contact.id), "content": "hi, following up"})
    listed = (await org_a.get("/api/v1/learning/answers")).json()
    assert listed["suggested"] == []


async def test_personal_details_are_pointed_out_before_teaching(org_a, quiet):
    session, organization, contact = await _shop(org_a)
    await taught.record_question(session, organization.id, contact.id, "where is my order?")
    await session.commit()
    await org_a.post(
        "/api/v1/messages/send",
        json={"contact_id": str(contact.id), "content": "Irsa, your order #1004 left today, call 0300-1234567"},
    )
    suggestion = (await org_a.get("/api/v1/learning/answers")).json()["suggested"][0]
    joined = " ".join(suggestion["watch_out"])
    assert "phone number" in joined and "order number" in joined and "Irsa" in joined


async def test_an_answer_can_be_typed_in_by_hand(org_a, quiet):
    session, organization, _ = await _shop(org_a)
    made = await org_a.post(
        "/api/v1/learning/answers",
        json={"question": "Do you work on Eid?", "answer": "We're closed for the three days of Eid."},
    )
    assert made.status_code == 201
    assert (await org_a.get("/api/v1/learning/answers")).json()["taught"][0]["question"] == "Do you work on Eid?"


async def test_another_business_cannot_see_or_teach_them(org_a, org_b, quiet):
    session, organization, contact = await _shop(org_a)
    row = await taught.record_question(session, organization.id, contact.id, "a private question")
    await session.commit()
    assert (await org_b.get("/api/v1/learning/answers")).json()["waiting"] == []
    stolen = await org_b.post(
        f"/api/v1/learning/answers/{row.id}/teach", json={"question": "x x x", "answer": "y"}
    )
    assert stolen.status_code == 404
