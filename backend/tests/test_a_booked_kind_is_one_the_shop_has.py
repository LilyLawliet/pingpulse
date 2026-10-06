"""Booking by hand with a kind nobody recognises is refused, not reinterpreted.

`POST /appointments` with kind "call" came back as an appointment of kind
"onsite", described as "site visit on Wednesday 7 October at 12:00 pm". The
caller asked for a phone call. Nothing in the response said it had been
changed, so the operator believes they booked a call and the diary says
somebody is driving out.

`book()` substituting the shop's default is right where it sits: a reading
of a customer's message that comes back with an odd word must not end the
turn with nothing booked. A deliberate value sent to an endpoint is the
other case, and this holds the difference.
"""

from __future__ import annotations

import pytest

from app.models import APPOINTMENT_KINDS, Organization
from tests.conftest import make_tenant

EVERY_DAY = {
    day: {"open": "00:00", "close": "23:59"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


async def _shop_with_a_contact(client):
    import uuid as _uuid

    from tests.conftest import _session_for

    tenant = await make_tenant(client, "owner-kind@example.com", "Kind Shop")
    session = _session_for(client)
    organization = await session.get(Organization, _uuid.UUID(tenant.organization_id))
    organization.timezone = "UTC"
    organization.agent_config = {
        "business_hours": EVERY_DAY,
        "appointments": {"min_notice_minutes": 0, "duration_minutes": 60, "require_address": False},
    }
    await session.flush()
    made = await tenant.post(
        "/api/v1/crm/contacts",
        json={"phone_number": "+923009990002", "name": "Test"},
    )
    assert made.status_code == 201, made.text
    return tenant, made.json()["id"]


def _tomorrow_at_ten():
    from datetime import datetime, timedelta, timezone

    day = datetime.now(timezone.utc) + timedelta(days=1)
    return day.replace(hour=10, minute=0, second=0, microsecond=0).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["call", "Call", "telephone", "zoom", "site visit", "coffee"])
async def test_a_kind_the_shop_does_not_have_is_refused(client, said):
    tenant, contact_id = await _shop_with_a_contact(client)
    answer = await tenant.post(
        "/api/v1/appointments",
        json={"contact_id": contact_id, "starts_at": _tomorrow_at_ten(), "kind": said},
    )
    assert answer.status_code == 422, answer.text
    assert "kind must be one of" in answer.text
    for known in APPOINTMENT_KINDS:
        assert known in answer.text


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["phone", "PHONE", " onsite ", "video", "other"])
async def test_a_kind_the_shop_has_is_booked_as_asked(client, said):
    tenant, contact_id = await _shop_with_a_contact(client)
    answer = await tenant.post(
        "/api/v1/appointments",
        json={"contact_id": contact_id, "starts_at": _tomorrow_at_ten(), "kind": said},
    )
    assert answer.status_code == 201, answer.text
    assert answer.json()["appointment"]["kind"] == said.strip().lower()


@pytest.mark.asyncio
async def test_no_kind_at_all_still_takes_the_shops_default(client):
    """Leaving it out is not the same as sending something unknown."""
    tenant, contact_id = await _shop_with_a_contact(client)
    answer = await tenant.post(
        "/api/v1/appointments",
        json={"contact_id": contact_id, "starts_at": _tomorrow_at_ten()},
    )
    assert answer.status_code == 201, answer.text
    assert answer.json()["appointment"]["kind"] in APPOINTMENT_KINDS
