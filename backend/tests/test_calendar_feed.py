"""The diary, as a calendar a phone can subscribe to.

The feed is public by necessity: a subscribing calendar client fetches a URL
for years and can never be prompted for a login, so the secret is in the URL.
That makes these tests mostly about the secret and about the shape of the
document, because both are the kind of thing that fails silently - a feed with
an over-long line does not show one wrong event, it refuses to load at all,
and the business finds out by having an empty day.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    Appointment,
    CRMContact,
    Organization,
)
from app.services import calendar_feed


async def _contact(db_session, org, name="Irsa", phone="+13055550101"):
    contact = CRMContact(
        organization_id=uuid.UUID(org.organization_id),
        phone_number=phone,
        name=name,
        pipeline_stage="LEAD",
        sales_stage="NEW",
        tags=[],
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


async def _appointment(db_session, org, contact, *, days=2, status=APPOINTMENT_CONFIRMED, **kw):
    starts = datetime.now(timezone.utc) + timedelta(days=days)
    appointment = Appointment(
        organization_id=uuid.UUID(org.organization_id),
        contact_id=contact.id,
        starts_at=starts,
        ends_at=starts + timedelta(minutes=90),
        timezone_name="America/New_York",
        kind="onsite",
        status=status,
        **kw,
    )
    db_session.add(appointment)
    await db_session.flush()
    return appointment


# ============================================================ the document
def test_lines_are_folded_at_seventy_five_octets():
    """Outlook rejects a calendar with over-long lines, and the failure is the
    whole feed refusing to load - so one long address takes the day with it."""
    rendered = calendar_feed._fold("LOCATION:" + "x" * 400)
    for line in rendered.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
    assert rendered.split("\r\n")[1].startswith(" ")


def test_folding_never_splits_a_character_in_half():
    """Counted in octets but cut on characters. A multi-byte character split
    down the middle is an invalid document."""
    rendered = calendar_feed._fold("SUMMARY:" + "é" * 200)
    for line in rendered.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
        line.encode("utf-8").decode("utf-8")


def test_text_is_escaped():
    escaped = calendar_feed._escape("Smith, J; unit 4\nback door")
    assert escaped == "Smith\\, J\\; unit 4\\nback door"


def test_a_calendar_with_no_appointments_is_still_a_valid_calendar():
    body = calendar_feed.render("Beluga Group", [])
    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert body.rstrip().endswith("END:VCALENDAR")
    assert "Beluga Group appointments" in body


def test_the_document_uses_crlf_throughout():
    """The specification says so, and the clients that tolerate bare newlines
    are not the ones a client will be using."""
    body = calendar_feed.render("Beluga Group", [])
    assert "\n" in body
    assert body.replace("\r\n", "") .count("\n") == 0


# ============================================================== the events
@pytest.mark.asyncio
async def test_an_appointment_becomes_an_event(org_a, db_session):
    contact = await _contact(db_session, org_a)
    appointment = await _appointment(
        db_session, org_a, contact, location="12 Ocean Drive, Miami"
    )

    rows = await calendar_feed.appointments_for(db_session, uuid.UUID(org_a.organization_id))
    body = calendar_feed.render("Beluga Group", rows)

    assert f"UID:{appointment.id}@pingpulse" in body
    assert "SUMMARY:Irsa — onsite" in body
    assert "STATUS:CONFIRMED" in body
    assert "12 Ocean Drive" in body
    # The number, so calling back is one tap from the event.
    assert "+13055550101" in body


@pytest.mark.asyncio
async def test_a_cancelled_appointment_is_published_as_cancelled(org_a, db_session):
    """Not dropped. A row that simply vanishes leaves the phone showing it
    forever, which is the "is it cancelled or not" question the appointments
    table exists to answer."""
    contact = await _contact(db_session, org_a)
    await _appointment(db_session, org_a, contact, status=APPOINTMENT_CANCELLED)

    rows = await calendar_feed.appointments_for(db_session, uuid.UUID(org_a.organization_id))
    body = calendar_feed.render("Beluga Group", rows)

    assert "STATUS:CANCELLED" in body
    assert "TRANSP:TRANSPARENT" in body
    # Bumped, or a client that already cached the event keeps the old one.
    assert "SEQUENCE:1" in body


@pytest.mark.asyncio
async def test_the_uid_is_stable_across_fetches(org_a, db_session):
    """Otherwise every refresh adds a second copy of the same appointment to
    somebody's phone."""
    contact = await _contact(db_session, org_a)
    await _appointment(db_session, org_a, contact)

    rows = await calendar_feed.appointments_for(db_session, uuid.UUID(org_a.organization_id))
    first = calendar_feed.render("Beluga Group", rows)
    rows = await calendar_feed.appointments_for(db_session, uuid.UUID(org_a.organization_id))
    second = calendar_feed.render("Beluga Group", rows)

    uid = [line for line in first.split("\r\n") if line.startswith("UID:")]
    assert uid and uid == [line for line in second.split("\r\n") if line.startswith("UID:")]


@pytest.mark.asyncio
async def test_appointments_far_outside_the_window_are_left_out(org_a, db_session):
    contact = await _contact(db_session, org_a)
    await _appointment(db_session, org_a, contact, days=-400)
    await _appointment(db_session, org_a, contact, days=900)

    rows = await calendar_feed.appointments_for(db_session, uuid.UUID(org_a.organization_id))
    assert rows == []


# ================================================================ the feed
@pytest.mark.asyncio
async def test_no_subscription_until_one_is_asked_for(org_a):
    found = (await org_a.get("/api/v1/calendar/subscription")).json()
    assert found["active"] is False
    assert found["urls"] is None


@pytest.mark.asyncio
async def test_creating_a_subscription_gives_a_webcal_link(org_a):
    """webcal:// is what makes a phone offer to subscribe rather than download
    a file, so it is the one that should be tapped."""
    body = (await org_a.post("/api/v1/calendar/subscription")).json()
    assert body["active"] is True
    assert body["replaced"] is False
    assert body["urls"]["webcal"].startswith("webcal://")
    assert body["urls"]["https"].startswith("http")
    assert body["urls"]["https"].endswith(".ics")


@pytest.mark.asyncio
async def test_the_feed_serves_this_tenants_appointments(org_a, db_session):
    contact = await _contact(db_session, org_a)
    await _appointment(db_session, org_a, contact)
    await db_session.commit()

    urls = (await org_a.post("/api/v1/calendar/subscription")).json()["urls"]
    await db_session.commit()

    path = urls["https"].split("/api/v1")[1]
    response = await org_a._client.get(f"/api/v1{path}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in response.text
    assert "Irsa" in response.text


@pytest.mark.asyncio
async def test_the_feed_needs_no_login(org_a, db_session, client):
    """The whole point. A subscribing client fetches for years and can never
    be prompted for anything."""
    contact = await _contact(db_session, org_a)
    await _appointment(db_session, org_a, contact)
    await db_session.commit()

    urls = (await org_a.post("/api/v1/calendar/subscription")).json()["urls"]
    await db_session.commit()

    path = urls["https"].split("/api/v1")[1]
    # No Authorization header at all.
    response = await client.get(f"/api/v1{path}")
    assert response.status_code == 200
    assert "BEGIN:VCALENDAR" in response.text


@pytest.mark.asyncio
async def test_a_wrong_token_is_a_flat_404(client):
    """No detail. Telling "no such feed" apart from "wrong secret" would make
    this somewhere to guess."""
    response = await client.get("/api/v1/calendar/" + "z" * 43 + ".ics")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_short_token_is_refused_without_a_lookup(client):
    response = await client.get("/api/v1/calendar/abc.ics")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rotating_stops_the_old_link(org_a, db_session, client):
    """How a forwarded link is taken back."""
    contact = await _contact(db_session, org_a)
    await _appointment(db_session, org_a, contact)
    await db_session.commit()

    old = (await org_a.post("/api/v1/calendar/subscription")).json()["urls"]["https"]
    await db_session.commit()
    new = (await org_a.post("/api/v1/calendar/subscription")).json()
    await db_session.commit()

    assert new["replaced"] is True
    assert new["urls"]["https"] != old

    assert (await client.get("/api/v1" + old.split("/api/v1")[1])).status_code == 404
    assert (
        await client.get("/api/v1" + new["urls"]["https"].split("/api/v1")[1])
    ).status_code == 200


@pytest.mark.asyncio
async def test_deleting_stops_the_feed(org_a, db_session, client):
    urls = (await org_a.post("/api/v1/calendar/subscription")).json()["urls"]
    await db_session.commit()
    await org_a.delete("/api/v1/calendar/subscription")
    await db_session.commit()

    response = await client.get("/api/v1" + urls["https"].split("/api/v1")[1])
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_one_tenants_diary_never_reaches_anothers_feed(org_a, org_b, db_session):
    mine = await _contact(db_session, org_a, name="Irsa")
    await _appointment(db_session, org_a, mine)
    theirs = await _contact(db_session, org_b, name="Someone Else", phone="+13055550999")
    await _appointment(db_session, org_b, theirs)
    await db_session.commit()

    urls = (await org_b.post("/api/v1/calendar/subscription")).json()["urls"]
    await db_session.commit()

    response = await org_b._client.get("/api/v1" + urls["https"].split("/api/v1")[1])
    assert "Someone Else" in response.text
    assert "Irsa" not in response.text


@pytest.mark.asyncio
async def test_the_token_is_not_written_into_the_audit_log(org_a, db_session):
    """An audit log is read by more people than the link is meant for."""
    from sqlalchemy import select

    from app.models import AuditLog

    body = (await org_a.post("/api/v1/calendar/subscription")).json()
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.organization_id == uuid.UUID(org_a.organization_id)
            )
        )
    ).scalars().all()

    token = body["urls"]["https"].rsplit("/", 1)[-1].removesuffix(".ics")
    for row in rows:
        assert token not in str(row.changes)
