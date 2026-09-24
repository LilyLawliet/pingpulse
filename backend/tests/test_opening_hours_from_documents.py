"""Hours stated in an uploaded document become hours the booking code obeys.

The gap this closes: a shop uploaded the sheet saying when it was open, the
text went into the knowledge base, and `business_hours` - the only field
booking reads - stayed empty. The agent could quote the hours to a customer
while the product believed the shop had never set any, so every appointment
was refused and handed to a person.

Two halves are tested here, and they are the two halves the shop actually
experiences:

  * a document that states hours has them read out and offered, prefilled into
    the hours form, where saving is what switches booking on. The document
    does the typing; a person still says yes, because parsed prose must not
    start promising appointments to customers on its own.
  * a document that does not leaves booking off, so the agent hands over and
    alerts somebody instead of inventing a time.

The parser tests lean hard on what must NOT be read. A missed line costs a
shop a setup step it can see and fix. A wrongly read line books a customer at
one in the morning and nobody finds out until the appointment.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Organization
from app.services import agent_config, booking, opening_hours


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
async def test_a_document_with_hours_offers_them_without_switching_booking_on(
    org_a, db_session
):
    """Found, stored where the form can prefill from it, and not acted on.

    Writing `business_hours` here would switch booking on off the back of
    parsed prose, and the agent would start offering real times to real
    customers on the strength of a regular expression.
    """
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})

    response = await _upload(org_a, WITH_HOURS)
    assert response.status_code == 201, response.text

    report = response.json()["from_document"]
    assert report["proposed"] is True, report
    assert "09:00-18:00" in report["found"]

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert not organization.agent_config.get("business_hours")
    assert booking.booking_enabled(organization) is False, (
        "a document switched booking on before anybody had looked at the hours"
    )

    offered = organization.agent_config[agent_config.PROPOSED_KEY]
    hours = offered["fields"]["business_hours"]
    assert hours["monday"] == {"open": "09:00", "close": "18:00"}
    assert hours["saturday"] == {"open": "10:00", "close": "14:00"}
    assert "sunday" not in hours, "the document says Sunday is closed"
    assert offered["sources"]["business_hours"] == "handbook.txt", (
        "the operator has to know what it read"
    )


@pytest.mark.asyncio
async def test_saving_the_offered_hours_is_what_switches_booking_on(org_a, db_session):
    """The confirmation, which is the whole point of offering rather than applying."""
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})
    await _upload(org_a, WITH_HOURS)

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    offered = organization.agent_config[agent_config.PROPOSED_KEY]["fields"]["business_hours"]

    saved = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"business_hours": offered}},
    )
    assert saved.status_code == 200, saved.text

    await db_session.refresh(organization)
    assert booking.booking_enabled(organization) is True
    assert organization.agent_config["business_hours"]["monday"] == {
        "open": "09:00",
        "close": "18:00",
    }
    assert agent_config.PROPOSED_KEY not in organization.agent_config, (
        "the suggestion outlived the decision and will ask to be confirmed again"
    )


@pytest.mark.asyncio
async def test_a_document_without_hours_leaves_booking_off(org_a, db_session):
    """The other half: blank hours, so the agent hands over instead of guessing."""
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})

    response = await _upload(org_a, WITHOUT_HOURS)
    assert response.status_code == 201, response.text

    report = response.json()["from_document"]
    assert report["proposed"] is False
    assert report["found"] is None
    assert "nothing was filled in" in report["detail"]

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert not (organization.agent_config or {}).get("business_hours")
    assert agent_config.PROPOSED_KEY not in (organization.agent_config or {})
    assert booking.booking_enabled(organization) is False, (
        "booking was switched on by a document that never stated an opening time"
    )


@pytest.mark.asyncio
async def test_hours_are_still_read_without_a_timezone_but_say_so(org_a, db_session):
    """09:00 with no zone is 09:00 UTC, which is how Miami got a 1am appointment.

    The hours are still offered - losing them would mean uploading the document
    again - but the reply says the timezone comes first, and nothing can be
    saved until it does.
    """
    await _set(db_session, org_a, timezone="UTC", agent_config={})

    response = await _upload(org_a, WITH_HOURS)
    report = response.json()["from_document"]

    assert report["proposed"] is True
    assert "timezone" in report["detail"]

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert not organization.agent_config.get("business_hours")

    # And the form cannot save them while the zone is still the default.
    offered = organization.agent_config[agent_config.PROPOSED_KEY]["fields"]["business_hours"]
    refused = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"business_hours": offered}},
    )
    assert refused.status_code == 422, "hours were saved against a timezone nobody set"


@pytest.mark.asyncio
async def test_hours_somebody_set_by_hand_are_never_overwritten(org_a, db_session):
    mine = {"monday": {"open": "07:00", "close": "12:00"}}
    await _set(
        db_session, org_a,
        timezone="America/New_York",
        agent_config={"business_hours": mine},
    )

    response = await _upload(org_a, WITH_HOURS)
    report = response.json()["from_document"]
    assert report["proposed"] is True, "the reading is still offered"

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    assert organization.agent_config["business_hours"] == mine, (
        "a document quietly moved hours the owner had set themselves"
    )
    assert booking.booking_enabled(organization) is True, (
        "the shop's own hours are still the ones in force"
    )


@pytest.mark.asyncio
async def test_an_upload_only_offers_hours_to_its_own_tenant(org_a, org_b, db_session):
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})
    await _set(db_session, org_b, timezone="America/New_York", agent_config={})

    assert (await _upload(org_a, WITH_HOURS)).status_code == 201

    other = await db_session.get(Organization, uuid.UUID(org_b.organization_id))
    config = other.agent_config or {}
    assert not config.get("business_hours"), (
        "one shop's handbook set another shop's opening hours"
    )
    assert agent_config.PROPOSED_KEY not in config


# --------------------------------------------- what else a document states
SERVICES_AND_AREAS = """Beluga Group - Price list

Services
Full bathroom renovation - from $12,000
Shower and tub conversion - from $4,500

Areas we serve: Miami-Dade, Broward and Palm Beach

Payment
50% deposit to schedule, balance on completion.
"""


def test_a_document_states_more_than_its_hours():
    from app.services import document_facts

    facts = document_facts.extract(SERVICES_AND_AREAS)
    assert facts["services"] == [
        "Full bathroom renovation - from $12,000",
        "Shower and tub conversion - from $4,500",
    ]
    assert facts["service_areas"] == ["Miami-Dade", "Broward", "Palm Beach"]
    assert "business_hours" not in facts, "this price list states no opening hours"


def test_a_section_stops_at_the_next_heading():
    """Services must not swallow the payment terms printed underneath them."""
    from app.services import document_facts

    facts = document_facts.extract(SERVICES_AND_AREAS)
    assert not any("deposit" in service for service in facts["services"])


def test_policy_fields_are_never_guessed_at():
    """A handbook does not contain instructions to an agent, so none are read."""
    from app.services import document_facts

    text = (
        "Pricing\nNo quotes under $200. Always mention the callout fee.\n\n"
        "Never promise same-day work.\n"
    )
    facts = document_facts.extract(text)
    assert "pricing_rules" not in facts
    assert "never_promise" not in facts
    assert "escalate_on" not in facts


@pytest.mark.asyncio
async def test_a_second_document_adds_without_erasing_the_first(org_a, db_session):
    """A price list silent about hours is not a shop saying it has none."""
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})

    assert (await _upload(org_a, WITH_HOURS, "handbook.txt")).status_code == 201
    second = await _upload(org_a, SERVICES_AND_AREAS, "prices.txt")
    assert second.status_code == 201, second.text

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    offered = organization.agent_config[agent_config.PROPOSED_KEY]

    assert offered["fields"]["business_hours"]["monday"] == {
        "open": "09:00",
        "close": "18:00",
    }, "the second document erased hours it never mentioned"
    assert offered["fields"]["service_areas"] == ["Miami-Dade", "Broward", "Palm Beach"]
    assert offered["sources"]["business_hours"] == "handbook.txt"
    assert offered["sources"]["services"] == "prices.txt", (
        "each field has to name the file it came from, or a conflict cannot be explained"
    )


@pytest.mark.asyncio
async def test_a_later_document_replaces_what_it_does_state(org_a, db_session):
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})

    assert (await _upload(org_a, WITH_HOURS, "old.txt")).status_code == 201
    newer = "Opening hours\nMonday - Friday: 08:00 - 16:00\n"
    assert (await _upload(org_a, newer, "new.txt")).status_code == 201

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    offered = organization.agent_config[agent_config.PROPOSED_KEY]["fields"]
    assert offered["business_hours"]["monday"] == {"open": "08:00", "close": "16:00"}
    assert "saturday" not in offered["business_hours"], (
        "the newer document states the week, so its answer replaces the old one whole"
    )


@pytest.mark.asyncio
async def test_saving_any_of_it_clears_the_suggestion(org_a, db_session):
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})
    await _upload(org_a, SERVICES_AND_AREAS, "prices.txt")

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    offered = organization.agent_config[agent_config.PROPOSED_KEY]["fields"]

    saved = await org_a._client.put(
        "/api/v1/agent-config",
        headers=org_a.headers,
        json={"agent_config": {"services": offered["services"]}},
    )
    assert saved.status_code == 200, saved.text

    await db_session.refresh(organization)
    assert agent_config.PROPOSED_KEY not in organization.agent_config


@pytest.mark.asyncio
async def test_clicking_through_an_empty_form_keeps_the_suggestion(org_a, db_session):
    """Saving nothing is not a confirmation, and must not throw the reading away."""
    await _set(db_session, org_a, timezone="America/New_York", agent_config={})
    await _upload(org_a, SERVICES_AND_AREAS, "prices.txt")

    saved = await org_a._client.put(
        "/api/v1/agent-config", headers=org_a.headers, json={"agent_config": {}}
    )
    assert saved.status_code == 200, saved.text

    organization = await db_session.get(Organization, uuid.UUID(org_a.organization_id))
    await db_session.refresh(organization)
    assert agent_config.PROPOSED_KEY in organization.agent_config


def test_a_word_document_is_read_despite_its_blank_lines():
    """The extractor puts a blank line between every paragraph.

    So a blank line separates two items in one list exactly as often as it
    separates two sections. Treating it as the end of a section read nothing
    at all out of a real .docx - every list ended at its first entry - while
    the plain-text tests kept passing, because plain text has no such gaps.
    """
    from app.services import document_facts

    spaced = (
        "Services\n\n"
        "Full bathroom renovation\n\n"
        "Shower and tub conversion\n\n"
        "Areas we serve\n\n"
        "Miami-Dade, Broward\n"
    )
    facts = document_facts.extract(spaced)
    assert facts["services"] == [
        "Full bathroom renovation",
        "Shower and tub conversion",
    ], "a blank line between paragraphs ended the list at its first entry"
    assert facts["service_areas"] == ["Miami-Dade", "Broward"]
