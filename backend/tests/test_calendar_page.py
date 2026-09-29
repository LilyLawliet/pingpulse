"""The calendar on the dashboard: the same diary, the same rules, by hand."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import APPOINTMENT_CANCELLED, Appointment, CRMContact, Message, Organization
from app.services import outbox

from .conftest import _session_for

KARACHI = ZoneInfo("Asia/Karachi")
HOURS = {
    day: {"open": "11:00", "close": "20:00"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
}


async def _setup(tenant):
    session = _session_for(tenant._client)
    organization = await session.get(Organization, uuid.UUID(tenant.organization_id))
    organization.timezone = "Asia/Karachi"
    organization.agent_config = {
        "business_hours": HOURS,
        "appointments": {"min_notice_minutes": 120, "duration_minutes": 30, "default_kind": "other"},
    }
    contact = CRMContact(
        organization_id=organization.id, phone_number="923001234567", name="Aiko", qualification={}
    )
    session.add(contact)
    await session.commit()
    return session, organization, contact


def _next(weekday: int, hour: int, minute: int = 0) -> datetime:
    today = datetime.now(KARACHI).date()
    day = today + timedelta(days=(weekday - today.weekday()) % 7 + 7)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KARACHI)


@pytest.mark.asyncio
async def test_book_move_cancel_by_hand(org_a):
    session, organization, contact = await _setup(org_a)
    friday = _next(4, 15)

    booked = await org_a.post(
        "/api/v1/appointments",
        json={"contact_id": str(contact.id), "starts_at": friday.isoformat()},
    )
    assert booked.status_code == 201, booked.text
    row = booked.json()["appointment"]
    assert row["contact"]["name"] == "Aiko"
    assert "3:00 pm" in row["description"]

    week = (await org_a.get(f"/api/v1/appointments?start={friday.date().isoformat()}&days=1")).json()
    assert [a["id"] for a in week["appointments"]] == [row["id"]]
    assert week["hours"]["friday"] == {"open": "11:00", "close": "20:00"}
    assert week["hours"]["sunday"] is None

    free = (await org_a.get(f"/api/v1/appointments/free?day={friday.date().isoformat()}")).json()
    assert friday.astimezone(timezone.utc).isoformat() not in free["slots"]
    assert len(free["slots"]) == 17  # 11:00..19:30 in half hours, less the one taken

    moved = await org_a.post(
        f"/api/v1/appointments/{row['id']}/move",
        json={"starts_at": _next(4, 17).isoformat()},
    )
    assert moved.status_code == 200, moved.text
    new = moved.json()["appointment"]
    assert new["replaces_id"] == row["id"] and "5:00 pm" in new["description"]

    gone = await org_a.post(f"/api/v1/appointments/{new['id']}/cancel", json={})
    assert gone.json()["appointment"]["status"] == APPOINTMENT_CANCELLED

    week = (await org_a.get(f"/api/v1/appointments?start={friday.date().isoformat()}&days=1")).json()
    assert {a["status"] for a in week["appointments"]} == {"cancelled"}


@pytest.mark.asyncio
async def test_the_hours_and_the_diary_hold_for_people_too(org_a):
    _, _, contact = await _setup(org_a)
    closed = await org_a.post(
        "/api/v1/appointments",
        json={"contact_id": str(contact.id), "starts_at": _next(6, 13).isoformat()},
    )
    assert closed.status_code == 409
    assert closed.json()["detail"]["reason"] == "closed"

    late = await org_a.post(
        "/api/v1/appointments",
        json={"contact_id": str(contact.id), "starts_at": _next(4, 19, 45).isoformat()},
    )
    assert late.json()["detail"]["reason"] == "closed", "ran past closing"

    first = await org_a.post(
        "/api/v1/appointments",
        json={"contact_id": str(contact.id), "starts_at": _next(4, 12).isoformat()},
    )
    assert first.status_code == 201
    clash = await org_a.post(
        "/api/v1/appointments",
        json={"contact_id": str(contact.id), "starts_at": _next(4, 12, 15).isoformat()},
    )
    assert clash.status_code == 409
    assert clash.json()["detail"]["reason"] == "taken"

    past = await org_a.post(
        "/api/v1/appointments",
        json={
            "contact_id": str(contact.id),
            "starts_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        },
    )
    assert past.status_code == 409


@pytest.mark.asyncio
async def test_the_customer_is_told_from_the_row(org_a, monkeypatch):
    session, organization, contact = await _setup(org_a)
    sent = []

    async def deliver(channel, to, text, **_kwargs):
        sent.append(text)
        return outbox.Delivery(outbox.SENT, "x", "delivered")

    monkeypatch.setattr(outbox, "deliver", deliver)
    booked = await org_a.post(
        "/api/v1/appointments",
        json={
            "contact_id": str(contact.id),
            "starts_at": _next(2, 14, 30).isoformat(),
            "tell_customer": True,
        },
    )
    assert booked.status_code == 201, booked.text
    assert sent and "2:30 pm" in sent[-1] and "Wednesday" in sent[-1]
    kept = (
        await session.execute(select(Message).where(Message.contact_id == contact.id))
    ).scalars().all()
    assert kept and kept[-1].content == sent[-1]


@pytest.mark.asyncio
async def test_another_shops_appointment_cannot_be_touched(org_a, org_b):
    _, _, contact = await _setup(org_a)
    booked = (
        await org_a.post(
            "/api/v1/appointments",
            json={"contact_id": str(contact.id), "starts_at": _next(4, 15).isoformat()},
        )
    ).json()["appointment"]
    assert (await org_b.post(f"/api/v1/appointments/{booked['id']}/cancel", json={})).status_code == 404
    assert (await org_b.get("/api/v1/appointments")).json()["appointments"] == []
    other = await org_b.post(
        "/api/v1/appointments",
        json={"contact_id": str(contact.id), "starts_at": _next(4, 16).isoformat()},
    )
    assert other.status_code == 404


# ------------------------------------------------------------- blocking out time
@pytest.mark.asyncio
async def test_blocked_out_time_is_never_offered_and_can_be_given_back(org_a, db_session):
    from app.services import booking, calendar_feed

    session, organization, contact = await _setup(org_a)
    thursday = _next(3, 14)
    blocked = await org_a.post(
        "/api/v1/appointments/block",
        json={
            "starts_at": thursday.isoformat(),
            "ends_at": _next(3, 18).isoformat(),
            "note": "Dentist",
        },
    )
    assert blocked.status_code == 201, blocked.text
    row = blocked.json()["appointment"]
    assert row["kind"] == "blocked" and row["contact"] is None
    assert "from 2:00 pm to 6:00 pm" in row["description"]

    free = (await org_a.get(f"/api/v1/appointments/free?day={thursday.date().isoformat()}")).json()
    local = [datetime.fromisoformat(s).astimezone(KARACHI) for s in free["slots"]]
    assert local and all(t.hour < 14 or t.hour >= 18 for t in local), local

    # A customer can't book inside it, however they ask.
    await session.refresh(organization)
    refusal = await booking.is_free(
        session, organization, _next(3, 15).astimezone(timezone.utc),
        _next(3, 15, 30).astimezone(timezone.utc),
    )
    assert refusal is not None and refusal.reason == "taken"
    by_hand = await org_a.post(
        "/api/v1/appointments",
        json={"contact_id": str(contact.id), "starts_at": _next(3, 16).isoformat()},
    )
    assert by_hand.status_code == 409

    feed = calendar_feed.render("Shop", await calendar_feed.appointments_for(session, organization.id))
    assert "SUMMARY:Blocked: Dentist" in feed

    # Unblocking frees it.
    freed = await org_a.post(f"/api/v1/appointments/{row['id']}/cancel", json={"tell_customer": True})
    assert freed.status_code == 200, freed.text
    again = (await org_a.get(f"/api/v1/appointments/free?day={thursday.date().isoformat()}")).json()
    assert len(again["slots"]) == 18


@pytest.mark.asyncio
async def test_a_block_over_a_booked_customer_is_refused(org_a):
    _, _, contact = await _setup(org_a)
    booked = await org_a.post(
        "/api/v1/appointments",
        json={"contact_id": str(contact.id), "starts_at": _next(4, 15).isoformat()},
    )
    assert booked.status_code == 201
    refused = await org_a.post(
        "/api/v1/appointments/block",
        json={"starts_at": _next(4, 11).isoformat(), "ends_at": _next(4, 20).isoformat()},
    )
    assert refused.status_code == 409
    assert "customer is booked" in refused.json()["detail"]["message"]

    backwards = await org_a.post(
        "/api/v1/appointments/block",
        json={"starts_at": _next(5, 15).isoformat(), "ends_at": _next(5, 12).isoformat()},
    )
    assert backwards.status_code == 409


@pytest.mark.asyncio
async def test_the_agent_offers_nothing_inside_a_block(org_a):
    from app.services import booking

    session, organization, contact = await _setup(org_a)
    friday = _next(4, 11)
    await org_a.post(
        "/api/v1/appointments/block",
        json={"starts_at": friday.isoformat(), "ends_at": _next(4, 20).isoformat(), "note": "Stock take"},
    )
    await session.refresh(organization)
    turn = await booking.handle_turn(
        session, organization, contact, f"any appointments on {friday.day} {friday:%B}?"
    )
    assert turn.offered == [] or all(s.astimezone(KARACHI).date() != friday.date() for s in turn.offered)
    named = await booking.handle_turn(
        session, organization, contact, f"book me {friday.day} {friday:%B} at 3pm"
    )
    assert named.performed is None
