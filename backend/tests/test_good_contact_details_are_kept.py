"""Contact details that pass validation are written down, not just checked.

The agent refuses to book until it has a working email and a working phone,
and then kept neither: `note_details` recorded only the ones that were wrong,
so it could chase a correction. `CRMContact.email` has existed all along and
nothing in a conversation ever wrote to it. The shop's only copy of an email
its agent had insisted on was a line in the chat transcript.
"""

from __future__ import annotations

import pytest

from app.models import CRMContact, Organization
from app.services import booking


@pytest.fixture
async def contact(db_session):
    shop = Organization(name="Shop", sales_prompt="We sell things.")
    db_session.add(shop)
    await db_session.flush()
    row = CRMContact(
        organization_id=shop.id, phone_number="923009990005", name="Test",
        pipeline_stage="NEW_LEAD", qualification={}, contact_metadata={},
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def test_a_valid_email_reaches_the_contact_record(contact):
    booking.note_details(contact, "my email is ali@example.com")
    assert contact.email == "ali@example.com"


async def test_a_valid_phone_is_kept(contact):
    booking.note_details(contact, "my phone is +1 305 555 0144")
    assert booking.given_phone(contact) == "+1 305 555 0144"


async def test_both_at_once(contact):
    booking.note_details(
        contact, "phone +1 305 555 0144 and email ali@example.com, call me"
    )
    assert contact.email == "ali@example.com"
    assert booking.given_phone(contact) == "+1 305 555 0144"


async def test_a_bad_value_is_never_written(contact):
    booking.note_details(contact, "my email is not-an-email and my phone is 123")
    assert contact.email is None
    assert booking.given_phone(contact) is None
    assert booking.unusable_details(contact), "the problem was not recorded either"


async def test_a_later_bad_value_does_not_wipe_a_good_one(contact):
    """Rejected, not silently replaced - the shop keeps the address that works."""
    booking.note_details(contact, "my email is ali@example.com")
    booking.note_details(contact, "actually my email is ali@")
    assert contact.email == "ali@example.com"
    assert any("not a valid email" in why for why in booking.unusable_details(contact))


async def test_a_correction_replaces_it(contact):
    booking.note_details(contact, "my email is ali@example.com")
    booking.note_details(contact, "sorry, my email is ali.khan@example.com")
    assert contact.email == "ali.khan@example.com"


async def test_an_unrelated_message_changes_nothing(contact):
    booking.note_details(contact, "my email is ali@example.com")
    booking.note_details(contact, "what times do you have on Tuesday?")
    assert contact.email == "ali@example.com"
