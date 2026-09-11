"""WhatsApp does not always tell us who someone is, and used to cost us the person.

WhatsApp increasingly addresses a chat by LID — a privacy identifier like
153231615328393@lid — rather than by phone number, and attaches the real number
only sometimes. Two separate faults came out of that, and both were live:

  * the LID was stored as the contact's phone number, so the shop saw
    "+15323 161 532 8393" — fifteen digits that cannot be dialled — and the
    same person became a second contact the moment they switched the account on
    their handset;

  * the exact chat address was carried all the way through the payload and then
    never written down, so every reply was addressed to a rebuilt
    <digits>@s.whatsapp.net. WhatsApp accepts that for a number that does not
    exist, reports success, and delivers it to nobody. The dashboard showed two
    ticks against messages the customer never received.

These cover the identity rules that make one person one conversation, and the
one write that makes a reply reach them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api.webhook import _resolve_contact
from app.models import CRMContact, Message, Organization

LID = "153231615328393"
NUMBER = "923052544605"


@pytest.fixture
async def organization(db_session):
    org = Organization(name="LID Shop", sales_prompt="Sell things.")
    db_session.add(org)
    await db_session.flush()
    return org


async def _contacts(db_session, organization) -> list[CRMContact]:
    return list(
        (
            await db_session.execute(
                select(CRMContact).where(CRMContact.organization_id == organization.id)
            )
        )
        .scalars()
        .all()
    )


# ------------------------------------------------------- one person, one record
async def test_a_lid_only_sender_is_recognised_the_second_time(db_session, organization):
    """The duplicate-contact bug, at its simplest.

    Nothing but a LID, twice. Matching on the phone number alone could not do
    this, because the "number" is the LID and a second message re-derives it.
    """
    first, created_first = await _resolve_contact(
        db_session, organization, LID, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )
    second, created_second = await _resolve_contact(
        db_session, organization, LID, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )

    assert created_first is True
    assert created_second is False, "the same person arrived twice and became two contacts"
    assert first.id == second.id
    assert len(await _contacts(db_session, organization)) == 1


async def test_the_lid_is_recorded_rather_than_only_stored_as_a_number(
    db_session, organization
):
    """Without this the identifier is indistinguishable from a phone number."""
    contact, _ = await _resolve_contact(
        db_session, organization, LID, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )

    assert contact.wa_lid == LID
    # It is still the only identity we have, so it stands in as the number —
    # but now it is labelled, which is what lets the dashboard say so.
    assert contact.phone_number == LID


async def test_a_number_arriving_later_replaces_the_placeholder(db_session, organization):
    """WhatsApp often reveals the number on a later message. Take it.

    The shop should never be left looking at an identifier where a dialable
    number belongs, once a real one is known.
    """
    contact, _ = await _resolve_contact(
        db_session, organization, LID, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )
    assert contact.phone_number == LID

    same, created = await _resolve_contact(
        db_session, organization, NUMBER, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )

    assert created is False
    assert same.id == contact.id
    assert same.phone_number == NUMBER, "still showing an undialable identifier"
    assert same.wa_lid == LID, "and the LID is still how we recognise them"


async def test_switching_accounts_does_not_rewrite_a_known_number(
    db_session, organization
):
    """A real number is never overwritten by a LID.

    The placeholder swap has to be one-directional. If a later LID-only message
    could rewrite phone_number back to the identifier, the fix would undo itself
    on the next message.
    """
    contact, _ = await _resolve_contact(
        db_session, organization, NUMBER, "Irsa", wa_lid=LID, wa_jid=f"{LID}@lid"
    )
    assert contact.phone_number == NUMBER

    again, _ = await _resolve_contact(
        db_session, organization, LID, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )

    assert again.id == contact.id
    assert again.phone_number == NUMBER


# ------------------------------------------------------------------ merging
async def test_two_records_for_one_person_are_merged_with_their_messages(
    db_session, organization
):
    """The state the dashboard was actually in: the same person, listed twice.

    One record from before WhatsApp switched to LID addressing, one from after.
    When WhatsApp finally sends both identifiers together, they are one person
    and the transcript has to end up in one place — a merge that left the
    messages behind would still show a split conversation.
    """
    by_number, _ = await _resolve_contact(db_session, organization, NUMBER, "Irsa")
    db_session.add(
        Message(
            organization_id=organization.id,
            contact_id=by_number.id,
            sender="user",
            content="first message, under the number",
            media_urls=[],
        )
    )
    by_lid, _ = await _resolve_contact(
        db_session, organization, LID, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )
    db_session.add(
        Message(
            organization_id=organization.id,
            contact_id=by_lid.id,
            sender="user",
            content="later message, under the LID",
            media_urls=[],
        )
    )
    await db_session.flush()
    assert len(await _contacts(db_session, organization)) == 2

    # WhatsApp finally hands over both at once.
    survivor, created = await _resolve_contact(
        db_session, organization, NUMBER, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )

    assert created is False
    remaining = await _contacts(db_session, organization)
    assert len(remaining) == 1, "the same person is still listed twice"
    assert survivor.id == by_number.id, "the record with the real number survives"
    assert survivor.wa_lid == LID
    assert survivor.name == "Irsa", "the name must not be lost in the merge"

    moved = (
        (
            await db_session.execute(
                select(Message).where(Message.contact_id == survivor.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(moved) == 2, "half the conversation was left on the deleted contact"


# --------------------------------------------------------- replying correctly
async def test_the_exact_chat_address_is_stored_for_the_reply(db_session, organization):
    """The fault that made replies vanish.

    A reply has to go back to the JID the message arrived on. Rebuilding one
    from the digits of a LID produces a valid address for a number nobody owns:
    WhatsApp accepts it and reports success, so the dashboard shows two ticks
    and the customer receives nothing.
    """
    contact, _ = await _resolve_contact(
        db_session, organization, LID, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )

    assert (contact.contact_metadata or {}).get("wa_jid") == f"{LID}@lid", (
        "no chat address stored, so the reply is sent to a rebuilt number"
    )


async def test_the_chat_address_follows_a_moving_conversation(db_session, organization):
    """WhatsApp can re-address an existing chat; the stored JID has to keep up."""
    contact, _ = await _resolve_contact(
        db_session, organization, NUMBER, None, wa_jid=f"{NUMBER}@s.whatsapp.net"
    )
    assert contact.contact_metadata["wa_jid"] == f"{NUMBER}@s.whatsapp.net"

    same, _ = await _resolve_contact(
        db_session, organization, NUMBER, None, wa_lid=LID, wa_jid=f"{LID}@lid"
    )

    assert same.id == contact.id
    assert same.contact_metadata["wa_jid"] == f"{LID}@lid"


# ------------------------------------------------------------------- Twilio
async def test_twilio_contacts_are_untouched_by_any_of_this(db_session, organization):
    """Twilio addresses everyone by phone number and sends no LID at all."""
    contact, created = await _resolve_contact(db_session, organization, "+14155552671", "Sam")

    assert created is True
    assert contact.wa_lid is None
    assert contact.phone_number == "+14155552671"
    assert "wa_jid" not in (contact.contact_metadata or {})


async def test_tenancy_still_separates_the_same_lid(db_session):
    """One person messaging two shops is two contacts, LID or not."""
    first = Organization(name="Shop One", sales_prompt="Sell.")
    second = Organization(name="Shop Two", sales_prompt="Sell.")
    db_session.add_all([first, second])
    await db_session.flush()

    a, _ = await _resolve_contact(db_session, first, LID, None, wa_lid=LID)
    b, _ = await _resolve_contact(db_session, second, LID, None, wa_lid=LID)

    assert a.id != b.id
    assert a.organization_id != b.organization_id
