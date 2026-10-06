"""A customer saying they have an order does not make one exist.

"You confirmed my order yesterday and took my payment. Where is it?" was
answered "please let me know your city so I can give you the exact ETA",
with the shop's real delivery terms quoted underneath. No order had been
placed and no payment had been taken. The agent had not claimed anything
itself - it simply believed the customer, and then acted on it, which reads
to everybody afterwards exactly like a confirmation.

The claim guard watches what the agent writes. Nothing watched what it was
told. These hold the other direction: what is said about an order comes from
the orders table, never from the message.
"""

from __future__ import annotations

import pytest

from app.models import CRMContact, Order, Organization
from app.services import orders


@pytest.fixture
async def shop_and_contact(db_session):
    shop = Organization(name="Tallybird POS", sales_prompt="We sell POS software.")
    db_session.add(shop)
    await db_session.flush()
    contact = CRMContact(
        organization_id=shop.id, phone_number="923052544605", name="Test",
        pipeline_stage="NEW_LEAD", qualification={}, contact_metadata={},
    )
    db_session.add(contact)
    await db_session.flush()
    return shop, contact


@pytest.mark.parametrize(
    "said",
    [
        "You confirmed my order yesterday and took my payment. Where is it?",
        "where is my order",
        "Where's my parcel?",
        "my delivery hasn't arrived",
        "I already paid, when will it ship?",
        "you took my money last week",
        "what's my order status",
        "when will my package arrive",
    ],
)
async def test_an_order_they_do_not_have_is_denied(shop_and_contact, db_session, said):
    shop, contact = shop_and_contact
    turn = await orders.about_an_order_they_have(db_session, shop, contact, said)
    assert turn is not None, said
    assert "NO ORDER ON RECORD" in turn.prompt_block
    assert turn.performed is None and turn.order is None


@pytest.mark.parametrize(
    "said",
    [
        "do you sell barcode scanners?",
        "I'd like to order two printers",
        "what are your prices",
        "can you call me tomorrow",
    ],
)
async def test_an_ordinary_message_is_left_alone(shop_and_contact, db_session, said):
    shop, contact = shop_and_contact
    assert await orders.about_an_order_they_have(db_session, shop, contact, said) is None


async def test_an_order_they_do_have_is_read_from_the_record(shop_and_contact, db_session):
    shop, contact = shop_and_contact
    db_session.add(
        Order(
            organization_id=shop.id, contact_id=contact.id, number=1001,
            status="dispatched", currency="PKR", lines=[], goods_total=0, total=0,
        )
    )
    await db_session.flush()
    turn = await orders.about_an_order_they_have(
        db_session, shop, contact, "where is my order?"
    )
    assert turn is not None
    assert "ORDERS ON RECORD" in turn.prompt_block
    assert "#1001" in turn.prompt_block and "dispatched" in turn.prompt_block


async def test_another_customers_order_is_not_theirs(shop_and_contact, db_session):
    """The lookup is by contact, not by organization: one customer asking
    must never be told about somebody else's parcel."""
    shop, contact = shop_and_contact
    somebody_else = CRMContact(
        organization_id=shop.id, phone_number="923009990000", name="Other",
        pipeline_stage="NEW_LEAD", qualification={}, contact_metadata={},
    )
    db_session.add(somebody_else)
    await db_session.flush()
    db_session.add(
        Order(
            organization_id=shop.id, contact_id=somebody_else.id, number=1002,
            status="placed", currency="PKR", lines=[], goods_total=0, total=0,
        )
    )
    await db_session.flush()
    turn = await orders.about_an_order_they_have(
        db_session, shop, contact, "where is my order?"
    )
    assert "NO ORDER ON RECORD" in turn.prompt_block
    assert "1002" not in turn.prompt_block


async def test_the_turn_reaches_the_prompt(shop_and_contact, db_session):
    """Through handle_turn, which is what the webhook actually calls."""
    from app.services import offers

    shop, contact = shop_and_contact
    prepared = await offers.prepare(db_session, shop)
    turn = await orders.handle_turn(
        db_session, shop, contact,
        "You confirmed my order yesterday and took my payment. Where is it?",
        [], prepared,
    )
    assert "NO ORDER ON RECORD" in turn.prompt_block
    assert turn.reply is None and turn.placed is False
