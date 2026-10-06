"""Booking in the Test agent: real times from the real diary, nothing kept.

The sandbox used to skip the booking step entirely - while suggesting "Can I
book for tomorrow?" as a thing to try - so the only way to test the calendar
flow was on a real customer.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.models import Appointment, Organization
from app.services import llm_service

from .conftest import _session_for

EVERY_DAY = {
    day: {"open": "00:00", "close": "23:59"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


@pytest.mark.asyncio
async def test_times_are_offered_one_is_booked_and_nothing_is_kept(org_a, monkeypatch):
    session = _session_for(org_a._client)
    organization = await session.get(Organization, uuid.UUID(org_a.organization_id))
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": EVERY_DAY,
        "appointments": {"min_notice_minutes": 0, "duration_minutes": 60, "default_kind": "onsite"},
    }
    await session.flush()

    prompts = []

    async def groq(prompt):
        prompts.append(prompt)
        return "Here are the times I can offer."

    monkeypatch.setattr(llm_service, "_call_groq", groq)

    first = (await org_a.post("/api/v1/agent/simulate", json={"message": "Can I book an appointment tomorrow? It's at 1200 Brickell Ave, Miami"})).json()
    assert first["booking"]["offered"], first
    assert "APPOINTMENTS" in prompts[-1], "the offered times did not reach the prompt"

    async def confirms(prompt):
        prompts.append(prompt)
        return "You're booked in."

    monkeypatch.setattr(llm_service, "_call_groq", confirms)

    async def say(message, state):
        return (
            await org_a.post(
                "/api/v1/agent/simulate",
                json={
                    "message": message,
                    "history": [
                        {"sender": "user", "content": "Can I book an appointment tomorrow? It's at 1200 Brickell Ave, Miami"},
                        {"sender": "agent", "content": first["reply"]},
                    ],
                    "booking_state": state,
                },
            )
        ).json()

    # The pick is read back from the record, not written by the model, and
    # nothing is booked by it.
    picked = await say("The first one please", first["booking_state"])
    assert picked["booking"]["performed"] is None, picked
    assert picked["provider"] == "booking"
    assert picked["reply"].startswith("To confirm: site visit on ") and "Reply YES" in picked["reply"]
    assert "asked to confirm" in picked["booking"]["note"]

    second = await say("yes", picked["booking_state"])
    assert second["booking"]["performed"] == "booked", second
    assert "Nothing was saved" in second["booking"]["note"]
    # "You're booked in." says nothing about when. A booking the reply does
    # not state is replaced by the confirmation rendered from the row.
    assert second["reply"].startswith("You're booked:"), second["reply"]
    assert " at " in second["reply"]

    async def with_the_time(prompt):
        import re

        when = re.search(r"BOOKED, just now, successfully: (.+?)\. Confirm", prompt).group(1)
        return f"Done - your {when} is confirmed."

    monkeypatch.setattr(llm_service, "_call_groq", with_the_time)
    again = await say("yes", picked["booking_state"])
    assert again["booking"]["performed"] == "booked", again
    assert again["reply"].startswith("Done - your "), "a reply that states the booking is used as written"

    kept = await session.scalar(
        select(func.count(Appointment.id)).where(Appointment.organization_id == organization.id)
    )
    assert kept == 0, "the sandbox left an appointment in the diary"


@pytest.mark.asyncio
async def test_without_hours_it_says_what_a_live_chat_would_do(org_a, monkeypatch):
    async def groq(prompt):
        return "Let me check."

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    body = (await org_a.post("/api/v1/agent/simulate", json={"message": "Can I book for tomorrow?"})).json()
    assert "no opening hours" in body["booking"]["note"]


@pytest.mark.asyncio
async def test_a_pretend_booking_can_be_moved_and_cancelled(org_a, monkeypatch):
    """Cancel and move could not be tried: the pretend booking vanished each turn."""
    session = _session_for(org_a._client)
    organization = await session.get(Organization, uuid.UUID(org_a.organization_id))
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": EVERY_DAY,
        "appointments": {"min_notice_minutes": 0, "duration_minutes": 60, "default_kind": "onsite"},
    }
    await session.flush()

    async def groq(prompt):
        return "Okay."

    monkeypatch.setattr(llm_service, "_call_groq", groq)

    async def say(message, state):
        return (
            await org_a.post(
                "/api/v1/agent/simulate", json={"message": message, "booking_state": state}
            )
        ).json()

    first = await say("Can I book an appointment? The house is 1200 Brickell Ave, Miami", {})
    booked = await say("yes", (await say("the first one", first["booking_state"]))["booking_state"])
    assert booked["booking"]["performed"] == "booked", booked
    assert "sandbox_appointment" in booked["booking_state"]

    offer = await say("can I reschedule?", booked["booking_state"])
    assert offer["booking"]["offered"], offer
    moved = await say("yes", (await say("the last one", offer["booking_state"]))["booking_state"])
    assert moved["booking"]["performed"] == "moved", moved

    asked = await say("please cancel my appointment", moved["booking_state"])
    assert asked["booking"]["performed"] is None and "Reply YES to cancel" in asked["reply"], asked
    gone = await say("yes", asked["booking_state"])
    assert gone["booking"]["performed"] == "cancelled", gone
    assert "sandbox_appointment" not in gone["booking_state"]

    kept = await session.scalar(
        select(func.count(Appointment.id)).where(Appointment.organization_id == organization.id)
    )
    assert kept == 0


@pytest.mark.asyncio
async def test_a_booking_the_analyzer_reads_as_wanting_a_person_is_not_handed_over(org_a, monkeypatch):
    """ "book one with ahmed name" was handed to a person in the Test agent on October 1."""
    from app.api import operations

    session = _session_for(org_a._client)
    organization = await session.get(Organization, uuid.UUID(org_a.organization_id))
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": EVERY_DAY,
        "appointments": {"min_notice_minutes": 0, "duration_minutes": 60, "default_kind": "onsite"},
    }
    await session.flush()

    real = operations.analyzer.analyse

    async def reads_a_person(history, message, stage="NEW"):
        analysis = await real(history, message, stage)
        # What the analyzer did on October 1: a name read as a person.
        return {**analysis, "wants_person": "ahmed" in message.lower()}

    async def groq(prompt):
        return "Here are the times."

    monkeypatch.setattr(operations.analyzer, "analyse", reads_a_person)
    monkeypatch.setattr(llm_service, "_call_groq", groq)

    body = (await org_a.post("/api/v1/agent/simulate", json={"message": "book one with ahmed name"})).json()
    assert body["escalated"] is False, body
    assert body["booking"]["offered"], "the booking request was not answered with times"

    # A guess that they want a person is asked, not acted on; their yes hands over.
    person = (await org_a.post("/api/v1/agent/simulate", json={"message": "can I talk to Ahmed please"})).json()
    assert person["escalated"] is False and person["provider"] == "handover", person
    assert "pass this conversation to the team" in person["reply"]
    yes = (
        await org_a.post(
            "/api/v1/agent/simulate", json={"message": "yes", "booking_state": person["booking_state"]}
        )
    ).json()
    assert yes["escalated"] is True, yes

    # Anything but a yes lets the question lapse.
    other = (await org_a.post("/api/v1/agent/simulate", json={"message": "can I talk to Ahmed please"})).json()
    moved_on = (
        await org_a.post(
            "/api/v1/agent/simulate",
            json={"message": "actually what are your hours?", "booking_state": other["booking_state"]},
        )
    ).json()
    assert moved_on["escalated"] is False and "pending_handover" not in moved_on["booking_state"]


@pytest.mark.asyncio
async def test_work_the_shop_does_not_do_is_answered_not_handed_on(org_a, monkeypatch):
    """The Test agent is where a shop finds this out, so it must answer the
    same way the live chat does: "we do not do that", not "shall I pass you
    to the team?"."""
    from app.services import analyzer, understanding

    session = _session_for(org_a._client)
    organization = await session.get(Organization, uuid.UUID(org_a.organization_id))
    organization.sales_prompt = "Residential and commercial remodeling in Miami / South Florida."
    await session.flush()

    async def reads_it(history, message, stage):
        return {**analyzer.heuristic_analysis(message, stage), "wants_person": True}

    async def scope_says(prompt, timeout):
        return {"job": "dog grooming", "service_fits": False}

    prompts = []

    async def groq(prompt):
        prompts.append(prompt)
        return "We don't do dog grooming, I'm afraid."

    monkeypatch.setattr("app.api.operations.analyzer.analyse", reads_it)
    monkeypatch.setattr(understanding, "structured", scope_says)
    monkeypatch.setattr(llm_service, "_call_groq", groq)

    body = (
        await org_a.post(
            "/api/v1/agent/simulate", json={"message": "Can you groom my dog this week?"}
        )
    ).json()
    assert body.get("provider") != "handover", body
    assert prompts, "no reply was generated"
    assert "NOT something this business does" in prompts[-1], prompts[-1]


@pytest.mark.asyncio
async def test_a_question_the_documents_do_not_answer_is_still_answered(org_a, monkeypatch):
    """The path the live run actually took.

    "Can you groom my dog this week?" came back from the model as a question
    it could not answer - truthfully, because the documents say nothing about
    dog grooming - and a colleague was alerted. The documents not mentioning
    it is the answer, not a reason to spend somebody's attention.
    """
    from app.services import understanding

    session = _session_for(org_a._client)
    organization = await session.get(Organization, uuid.UUID(org_a.organization_id))
    organization.sales_prompt = "Residential and commercial remodeling in Miami / South Florida."
    await session.flush()

    async def scope_says(prompt, timeout):
        return {"job": "dog grooming", "service_fits": False}

    async def groq(prompt):
        # What the model returns when it has no answer in the documents.
        return "NEEDS_TEAM: whether we groom dogs"

    monkeypatch.setattr(understanding, "structured", scope_says)
    monkeypatch.setattr(llm_service, "_call_groq", groq)

    body = (
        await org_a.post(
            "/api/v1/agent/simulate", json={"message": "Can you groom my dog this week?"}
        )
    ).json()
    assert body["needs_team"] is None, body
    assert "not something we do" in body["reply"], body["reply"]
