"""Hours stated in an uploaded document become hours the booking code obeys.

The gap this closes: a shop uploaded the sheet saying when it was open, the
text went into the knowledge base, and `business_hours` - the only field
booking reads - stayed empty. The agent could quote the hours to a customer
while the product believed the shop had never set any, so every appointment
was refused and handed to a person.

Two halves are tested here, and they are the two halves the shop actually
experiences:

  * a document that states hours switches booking on, with those hours;
  * a document that does not leaves it off, so the agent hands over and
    alerts somebody instead of inventing a time.

The parser tests lean hard on what must NOT be read. A missed line costs a
shop a setup step it can see and fix. A wrongly read line books a customer at
one in the morning and nobody finds out until the appointment.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Organization
from app.services import booking, opening_hours


# ------------------------------------------------------------------ the parser
@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Opening hours: Monday - Friday: 9:00 AM - 6:00 PM",
            {day: {"open": "09:00", "close": "18:00"} for day in
             ("monday", "tuesday", "wednesday", "thursday", "friday")},
        ),
        ("Saturday: 10:00 - 14:00", {"saturday": {"open": "10:00", "close": "14:00"}}),
        ("Sunday: Closed", {}),
        (
            "Tues, Thurs: 8:00 - 12:00",
            {"tuesday": {"open": "08:00", "close": "12:00"},
             "thursday": {"open": "08:00", "close": "12:00"}},
        ),
        ("Weekends 11:00 - 15:00", {"saturday": {"open": "11:00", "close": "15:00"},
                                    "sunday": {"open": "11:00", "close": "15:00"}}),
    ],
)
def test_the_shapes_a_real_document_uses(text, expected):
    assert opening_hours.parse(text) == expected


def test_an_en_dash_is_read_like_a_hyphen():
    """Word turns "Mon-Fri" into "Mon–Fri" on its own, without being asked."""
    assert opening_hours.parse("Mon–Fri 9am–6pm") == {
        day: {"open": "09:00", "close": "18:00"}
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
    }


def test_one_meridiem_covers_the_pair():
    """"9 - 6pm" is a nine-hour day, not a shop that opens at nine at night."""
    assert opening_hours.parse("Open Friday 9 - 6pm") == {
        "friday": {"open": "09:00", "close": "18:00"}
    }


def test_the_word_we_is_not_wednesday():
    """The two-letter day forms were dropped for exactly this sentence."""
    parsed = opening_hours.parse("We are open Saturday 9:00 - 17:00")
    assert parsed == {"saturday": {"open": "09:00", "close": "17:00"}}, (
        "'we' was read as Wednesday, so the shop takes bookings on a day it is shut"
    )


@pytest.mark.parametrize(
    "text, why",
    [
        ("Delivery takes 2-3 working days", "no day is named"),
        ("Monday orders ship in 2-3 batches", "a bare range with nothing calling it a time"),
        ("Wednesday 22:00 - 02:00", "it crosses midnight"),
        ("Friday 9:00 - 9:05", "a five-minute opening is a misread"),
        ("", "there is no document"),
    ],
)
def test_what_must_never_be_read_as_hours(text, why):
    assert opening_hours.parse(text) == {}, f"parsed hours out of something where {why}"


def test_a_document_that_contradicts_itself_keeps_the_first_statement():
    text = "Monday: 9:00 - 17:00\nSummer only - Monday: 11:00 - 15:00"
    assert opening_hours.parse(text)["monday"] == {"open": "09:00", "close": "17:00"}


def test_a_closed_line_removes_an_earlier_opening():
    text = "Daily 09:00 - 18:00\nSunday: closed"
    parsed = opening_hours.parse(text)
    assert "sunday" not in parsed, "the shop is shut on Sunday and the document says so"
    assert parsed["monday"] == {"open": "09:00", "close": "18:00"}


def test_parsed_hours_are_the_shape_booking_reads():
    """The parser and the booking code have to agree, not merely look similar."""
    from types import SimpleNamespace
    from datetime import datetime
    from zoneinfo import ZoneInfo

    parsed = opening_hours.parse("Monday - Friday 09:00 - 17:00")
    org = SimpleNamespace(agent_config={"business_hours": parsed}, timezone="America/New_York")

    assert booking.booking_enabled(org) is True
    zone = ZoneInfo("America/New_York")
    inside = datetime(2026, 9, 21, 10, 0, tzinfo=zone)
    assert booking.within_business_hours(org, inside, datetime(2026, 9, 21, 11, 0, tzinfo=zone))
    outside = datetime(2026, 9, 21, 19, 0, tzinfo=zone)
    assert not booking.within_business_hours(org, outside, datetime(2026, 9, 21, 20, 0, tzinfo=zone))


# ------------------------------------------------------------------ the upload
async def _set(db_session, tenant, **fields):
    organization = await db_session.get(Organization, uuid.UUID(tenant.organization_id))
    for key, value in fields.items():
        setattr(organization, key, value)
    await db_session.flush()
    return organization


async def _upload(tenant, body: str, name: str = "handbook.txt"):
    return await tenant._client.post(
        "/api/v1/knowledge/upload",
        headers=tenant.headers,
        files={"file": (name, body.encode("utf-8"), "text/plain")},
    )


WITH_HOURS = """Beluga Group - Service Handbook

We remodel bathrooms across Miami-Dade.

Opening hours
Monday - Friday: 9:00 AM - 6:00 PM
Saturday: 10:00 AM - 2:00 PM
Sunday: Closed
"""

WITHOUT_HOURS = """Beluga Group - Service Handbook

We remodel bathrooms across Miami-Dade. Full renovations start at $12,000 and
take three to four weeks. Delivery of fittings takes 2-3 working days.
"""


@pytest.mark.asyncio
async def test_a_document_with_hours_switches_booking_on(org_a, db_session):
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})

    response = await _upload(org_a, WITH_HOURS)
    assert response.status_code == 201, response.text

    report = response.json()["opening_hours"]
    assert report["applied"] is True, report
    assert "09:00-18:00" in report["found"]

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    hours = organization.agent_config["business_hours"]
    assert hours["monday"] == {"open": "09:00", "close": "18:00"}
    assert hours["saturday"] == {"open": "10:00", "close": "14:00"}
    assert "sunday" not in hours, "the document says Sunday is closed"
    assert booking.booking_enabled(organization) is True


@pytest.mark.asyncio
async def test_a_document_without_hours_leaves_booking_off(org_a, db_session):
    """The other half: blank hours, so the agent hands over instead of guessing."""
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})

    response = await _upload(org_a, WITHOUT_HOURS)
    assert response.status_code == 201, response.text

    report = response.json()["opening_hours"]
    assert report["applied"] is False
    assert report["found"] is None
    assert "booking stays off" in report["detail"]

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert not (organization.agent_config or {}).get("business_hours")
    assert booking.booking_enabled(organization) is False, (
        "booking was switched on by a document that never stated an opening time"
    )


@pytest.mark.asyncio
async def test_hours_are_not_applied_before_a_timezone_is_set(org_a, db_session):
    """09:00 with no zone is 09:00 UTC, which is how Miami got a 1am appointment."""
    await _set(db_session, org_a, timezone="UTC", agent_config={})

    response = await _upload(org_a, WITH_HOURS)
    report = response.json()["opening_hours"]

    assert report["applied"] is False
    assert report["found"], "the hours were read, they were just not trusted yet"
    assert "timezone" in report["detail"]

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert not (organization.agent_config or {}).get("business_hours")


@pytest.mark.asyncio
async def test_hours_somebody_set_by_hand_are_never_overwritten(org_a, db_session):
    mine = {"monday": {"open": "07:00", "close": "12:00"}}
    await _set(
        db_session, org_a,
        timezone="America/New_York",
        agent_config={"business_hours": mine},
    )

    response = await _upload(org_a, WITH_HOURS)
    report = response.json()["opening_hours"]
    assert report["applied"] is False
    assert "already have" in report["detail"]

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert organization.agent_config["business_hours"] == mine, (
        "a document quietly moved hours the owner had set themselves"
    )


@pytest.mark.asyncio
async def test_an_upload_only_sets_its_own_tenants_hours(org_a, org_b, db_session):
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})
    await _set(db_session, org_b, timezone="America/New_York", agent_config={})

    assert (await _upload(org_a, WITH_HOURS)).status_code == 201

    other = await db_session.get(Organization, uuid.UUID(org_b.organization_id))
    assert not (other.agent_config or {}).get("business_hours"), (
        "one shop's handbook set another shop's opening hours"
    )
