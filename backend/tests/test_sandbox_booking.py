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

    first = (await org_a.post("/api/v1/agent/simulate", json={"message": "Can I book an appointment tomorrow?"})).json()
    assert first["booking"]["offered"], first
    assert "APPOINTMENTS" in prompts[-1], "the offered times did not reach the prompt"

    async def confirms(prompt):
        prompts.append(prompt)
        return "You're booked in."

    monkeypatch.setattr(llm_service, "_call_groq", confirms)
    second = (
        await org_a.post(
            "/api/v1/agent/simulate",
            json={
                "message": "The first one please",
                "history": [
                    {"sender": "user", "content": "Can I book an appointment tomorrow?"},
                    {"sender": "agent", "content": first["reply"]},
                ],
                "booking_state": first["booking_state"],
            },
        )
    ).json()
    assert second["booking"]["performed"] == "booked", second
    assert "Nothing was saved" in second["booking"]["note"]
    assert second["reply"] == "You're booked in.", "a real booking may be announced"

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

    first = await say("Can I book an appointment?", {})
    booked = await say("the first one", first["booking_state"])
    assert booked["booking"]["performed"] == "booked", booked
    assert "sandbox_appointment" in booked["booking_state"]

    offer = await say("can I reschedule?", booked["booking_state"])
    assert offer["booking"]["offered"], offer
    moved = await say("the last one", offer["booking_state"])
    assert moved["booking"]["performed"] == "moved", moved

    gone = await say("please cancel my appointment", moved["booking_state"])
    assert gone["booking"]["performed"] == "cancelled", gone
    assert "sandbox_appointment" not in gone["booking_state"]

    kept = await session.scalar(
        select(func.count(Appointment.id)).where(Appointment.organization_id == organization.id)
    )
    assert kept == 0
