"""What somebody trying to break this would actually try.

Not error handling. The question here is narrower and meaner: can one tenant
reach another's data, can a customer reach the business's devices, can a
client write state the server is supposed to own, and can any of it be made
to fall over with input rather than volume.

The customer-controlled surfaces are the ones that matter most, because they
are the ones reached by somebody who was never given an account:

  * A WhatsApp profile name is chosen by the person messaging in. It reaches
    the business owner's *calendar*, through the feed, onto their phone.
  * A message body reaches the escalation matcher and the notification body.
  * A document is uploaded by the tenant, but its contents reach the prompt.

And the calendar feed is served with no authentication at all, because a
subscribing client cannot authenticate. The secret in the URL is the entire
control, which makes it worth attacking directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    APPOINTMENT_CONFIRMED,
    Appointment,
    CRMContact,
    Organization,
)
from app.services import agent_config, booking, calendar_feed


async def _org(db_session, tenant):
    return await db_session.get(Organization, uuid.UUID(tenant.organization_id))


async def _bookable(db_session, tenant):
    organization = await _org(db_session, tenant)
    organization.timezone = "America/New_York"
    organization.agent_config = {
        "business_hours": {
            day: {"open": "09:00", "close": "17:00"}
            for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
        "appointments": {"enabled": True, "min_notice_minutes": 0},
    }
    await db_session.flush()
    return organization


# ==================================================== the calendar feed
# Served to anyone who has the URL, because a subscribing client cannot be
# prompted for a login. The secret is the whole control.
@pytest.mark.asyncio
async def test_a_customers_own_name_cannot_forge_an_event(org_a, db_session):
    """The sharpest surface in the product.

    A WhatsApp profile name is chosen by the person messaging in, and it ends
    up in an iCalendar document on the business owner's phone. A name carrying
    a line break could close one event and open another - an appointment the
    owner never agreed to, in their real diary, looking exactly like the rest.
    """
    organization = await _bookable(db_session, org_a)
    hostile = (
        "Irsa\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:PAY INVOICE 4471\r\nDTSTART:20260101T090000Z"
    )
    contact = CRMContact(
        organization_id=organization.id, phone_number="+13055550101", name=hostile
    )
    db_session.add(contact)
    await db_session.flush()

    starts = datetime.now(timezone.utc) + timedelta(days=3)
    db_session.add(
        Appointment(
            organization_id=organization.id,
            contact_id=contact.id,
            starts_at=starts,
            ends_at=starts + timedelta(minutes=60),
            timezone_name="America/New_York",
            kind="onsite",
            status=APPOINTMENT_CONFIRMED,
            location="12 Ocean Drive\r\nBEGIN:VEVENT\r\nSUMMARY:ALSO INJECTED",
            notes="see you\r\nEND:VCALENDAR",
        )
    )
    await db_session.flush()

    rows = await calendar_feed.appointments_for(db_session, organization.id)
    ics = calendar_feed.render(organization.name, rows)

    # Counted structurally. The literal text "BEGIN:VEVENT" does appear inside
    # the values, escaped, and that is harmless - what would matter is a
    # *line* that is the marker, because that is what a parser acts on.
    lines = ics.split("\r\n")
    assert lines.count("BEGIN:VEVENT") == 1, lines
    assert lines.count("END:VEVENT") == 1
    assert lines.count("END:VCALENDAR") == 1

    # The payload survives as text, which is right: it is somebody's name.
    assert "PAY INVOICE 4471" in ics
    # But no line of it is ever read as a field of its own.
    for line in lines:
        assert not line.startswith("SUMMARY:PAY")
        assert not line.startswith("DTSTART:20260101")
        assert not line.startswith("BEGIN:")  or line == "BEGIN:VCALENDAR" or line == "BEGIN:VEVENT"

    # And the escape that makes all of it safe is present rather than assumed.
    assert "\\nEND:VEVENT" in ics


@pytest.mark.asyncio
async def test_every_line_of_a_hostile_calendar_is_still_well_formed(
    org_a, db_session
):
    """A feed that parses as invalid is a feed that silently shows nothing."""
    organization = await _bookable(db_session, org_a)
    contact = CRMContact(
        organization_id=organization.id,
        phone_number="+13055550101",
        name="x" * 400 + "\r\n" + "é" * 200 + ";,\\",
    )
    db_session.add(contact)
    await db_session.flush()
    starts = datetime.now(timezone.utc) + timedelta(days=2)
    db_session.add(
        Appointment(
            organization_id=organization.id,
            contact_id=contact.id,
            starts_at=starts,
            ends_at=starts + timedelta(minutes=60),
            timezone_name="America/New_York",
            kind="onsite",
            status=APPOINTMENT_CONFIRMED,
        )
    )
    await db_session.flush()

    rows = await calendar_feed.appointments_for(db_session, organization.id)
    ics = calendar_feed.render(organization.name, rows)

    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, len(line.encode("utf-8"))
        line.encode("utf-8").decode("utf-8")  # never a split character


@pytest.mark.asyncio
async def test_a_guessed_token_never_serves_anybody(org_a, db_session, client):
    """The URL is the only control, so the ways of guessing it are the attack."""
    await org_a.post("/api/v1/calendar/subscription")
    await db_session.commit()

    for guess in (
        "",
        "a",
        "null",
        "undefined",
        "%20",
        "x" * 15,
        "x" * 64,
        "../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "' OR '1'='1",
        "*",
    ):
        response = await client.get(f"/api/v1/calendar/{guess}.ics")
        assert response.status_code in (404, 400, 405, 422), (guess, response.status_code)
        assert "BEGIN:VCALENDAR" not in response.text


@pytest.mark.asyncio
async def test_one_tenants_feed_url_never_returns_anothers_diary(
    org_a, org_b, db_session, client
):
    organization = await _bookable(db_session, org_a)
    mine = CRMContact(
        organization_id=organization.id, phone_number="+13055550101", name="AlphaCustomer"
    )
    db_session.add(mine)
    await db_session.flush()
    starts = datetime.now(timezone.utc) + timedelta(days=2)
    db_session.add(
        Appointment(
            organization_id=organization.id,
            contact_id=mine.id,
            starts_at=starts,
            ends_at=starts + timedelta(minutes=60),
            timezone_name="America/New_York",
            kind="onsite",
            status=APPOINTMENT_CONFIRMED,
        )
    )
    await db_session.flush()

    theirs = (await org_b.post("/api/v1/calendar/subscription")).json()
    await db_session.commit()

    feed = await client.get("/api/v1" + theirs["urls"]["https"].split("/api/v1")[1])
    assert feed.status_code == 200
    assert "AlphaCustomer" not in feed.text


# ============================================== reaching across tenants
@pytest.mark.asyncio
async def test_no_new_endpoint_leaks_across_tenants(org_a, org_b, db_session):
    """Every endpoint added recently, checked the same way: what B sees must
    never be what A stored."""
    await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"never_promise": "ALPHA-SECRET"}, "timezone": None},
    )
    await db_session.commit()

    config = (await org_b.get("/api/v1/agent-config")).json()
    assert "ALPHA-SECRET" not in str(config)

    suggestions = (await org_b.get("/api/v1/agent-config/suggestions")).json()
    assert "ALPHA-SECRET" not in str(suggestions)

    preview = (
        await org_b.post(
            "/api/v1/agent-config/preview", json={"agent_config": {}}
        )
    ).json()
    assert "ALPHA-SECRET" not in str(preview)

    undo = await org_b.post("/api/v1/agent-config/undo")
    assert undo.status_code == 404
    assert "ALPHA-SECRET" not in undo.text


@pytest.mark.asyncio
async def test_a_client_cannot_write_its_own_undo_history(org_a, db_session):
    """The snapshot is the server's. A page that could write it could offer
    somebody an undo button that restores something they never had."""
    forged = {
        "never_promise": "mine",
        agent_config.PREVIOUS_KEY: {
            "config": {"never_promise": "PLANTED"},
            "at": "2020-01-01T00:00:00+00:00",
            "was_undo": False,
        },
    }
    await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": forged, "timezone": None},
    )
    await db_session.commit()

    undone = await org_a.post("/api/v1/agent-config/undo")
    assert "PLANTED" not in undone.text


@pytest.mark.asyncio
async def test_the_undo_snapshot_cannot_grow_without_bound(org_a, db_session):
    """A snapshot that contained a snapshot would double on every save, and
    this rides in a column read on every inbound message."""
    for index in range(12):
        await org_a._client.put(
            "/api/v1/agent-config",
            headers=org_a.headers,
            json={"agent_config": {"notes": f"round {index}"}, "timezone": None},
        )
    await db_session.commit()

    organization = await _org(db_session, org_a)
    await db_session.refresh(organization)
    stored = organization.agent_config or {}
    snapshot = stored.get(agent_config.PREVIOUS_KEY) or {}
    assert agent_config.PREVIOUS_KEY not in (snapshot.get("config") or {})
    assert len(str(stored)) < 4000, len(str(stored))


# ================================================ input meant to break it
@pytest.mark.asyncio
async def test_a_config_bomb_is_refused_rather_than_stored(org_a):
    """Everything here is folded into a prompt. A megabyte of text in
    `never_promise` is a megabyte on every reply."""
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={
            "agent_config": {
                "escalate_on": ["x" * 500] * 400,
                "never_promise": "y" * 200_000,
            },
            "timezone": None,
        },
    )
    # Refused outright. This was accepted before, and 753,665 bytes went into
    # the row - the prompt survived, because every field folded into it is
    # truncated, so the model never saw it and nothing looked wrong. The row
    # is read on every inbound message.
    assert response.status_code == 422, response.status_code
    assert any("KB" in problem for problem in response.json()["detail"])

    stored = (await org_a.get("/api/v1/agent-config")).json()["agent_config"]
    assert stored == {}, "a refused save must store nothing"


@pytest.mark.asyncio
async def test_escalate_on_as_a_bare_string_is_refused(org_a):
    """The shape that iterates character by character and turns "refund" into
    six single-letter triggers, so every conversation escalates and nothing in
    the logs says why."""
    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"escalate_on": "refund"}, "timezone": None},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_deeply_nested_json_does_not_take_the_process_down(org_a):
    payload: dict = {"never_promise": "x"}
    for _ in range(200):
        payload = {"nested": payload}

    response = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": payload, "timezone": None},
    )
    assert response.status_code in (200, 413, 422)

    # And the service is still answering.
    assert (await org_a.get("/api/v1/agent-config")).status_code == 200


@pytest.mark.asyncio
async def test_a_hostile_timezone_is_refused(org_a):
    for zone in ("../../etc/passwd", "'; drop table organizations; --", "x" * 500, "UTC+25"):
        response = await org_a._client.put(
            "/api/v1/agent-config",
            headers=org_a.headers,
            json={"agent_config": {}, "timezone": zone},
        )
        assert response.status_code == 422, zone


# ======================================================= booking abuse
@pytest.mark.asyncio
async def test_a_time_in_the_past_cannot_be_booked(org_a, db_session):
    organization = await _bookable(db_session, org_a)
    contact = CRMContact(organization_id=organization.id, phone_number="+13055550101")
    db_session.add(contact)
    await db_session.flush()

    past = datetime.now(timezone.utc) - timedelta(days=2)
    result = await booking.book(db_session, organization, contact, past)
    assert getattr(result, "appointment", None) is None


@pytest.mark.asyncio
async def test_a_time_outside_opening_hours_cannot_be_booked(org_a, db_session):
    """The 1am booking, attempted directly against the service rather than
    through a conversation."""
    organization = await _bookable(db_session, org_a)
    contact = CRMContact(organization_id=organization.id, phone_number="+13055550101")
    db_session.add(contact)
    await db_session.flush()

    zone = agent_config.zone_of(organization)
    tomorrow = datetime.now(zone) + timedelta(days=1)
    while tomorrow.weekday() > 4:
        tomorrow += timedelta(days=1)
    one_am = tomorrow.replace(hour=1, minute=0, second=0, microsecond=0)

    result = await booking.book(
        db_session, organization, contact, one_am.astimezone(timezone.utc)
    )
    assert getattr(result, "appointment", None) is None


@pytest.mark.asyncio
async def test_an_absurd_appointment_length_is_refused(org_a):
    for minutes in (0, -30, 10_000):
        response = await org_a._client.put(
            "/api/v1/agent-config",
            headers=org_a.headers,
            json={
                "agent_config": {"appointments": {"duration_minutes": minutes}},
                "timezone": None,
            },
        )
        assert response.status_code == 422, minutes


# ============================================================== auth
@pytest.mark.asyncio
async def test_the_new_endpoints_all_refuse_an_anonymous_caller(client):
    for method, path in (
        ("GET", "/api/v1/agent-config"),
        ("GET", "/api/v1/agent-config/trades"),
        ("GET", "/api/v1/agent-config/suggestions"),
        ("POST", "/api/v1/agent-config/preview"),
        ("POST", "/api/v1/agent-config/undo"),
        ("GET", "/api/v1/calendar/subscription"),
        ("POST", "/api/v1/calendar/subscription"),
        ("DELETE", "/api/v1/calendar/subscription"),
    ):
        response = await client.request(method, path, json={})
        assert response.status_code in (401, 403), (method, path, response.status_code)


@pytest.mark.asyncio
async def test_a_made_up_token_is_refused(client):
    for token in ("", "Bearer", "pp_live_" + "x" * 40, "null", "admin"):
        response = await client.get(
            "/api/v1/agent-config", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code in (401, 403), token
