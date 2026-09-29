"""An order taken in the chat is a row, and only a yes to a summary writes one.

Replays the real conversation: the Mochi Bunny notebook, "pink dotted, and I
want it delivered in lahore", then "Preferred payment method is online and
delivery address is lahore bahria town" - which got "Great! I've noted..."
with nothing noted anywhere, no payment methods named, and the delivery
charges given in answer to "when will I receive it?".
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import CRMContact, Order, Organization
from app.services import offers, orders, understanding

TERMS = (
    "Delivery within Karachi is PKR 250 for orders below PKR 3,000, and free for orders of PKR 3,000 or more. "
    "Delivery to the rest of Pakistan is PKR 350 for orders below PKR 5,000, and free for orders of PKR 5,000 or more. "
    "Karachi orders arrive in 1–2 working days; other cities in 3–5 working days. We do not offer same-day delivery. "
    "Cash on Delivery is available for orders up to PKR 15,000. "
    "Orders above PKR 15,000 need 50% advance payment by bank transfer, JazzCash or Easypaisa before dispatch, "
    "and the rest on delivery."
)


def _prepared():
    items = [
        offers.Item("Mochi Bunny Notebook A5", Decimal(1250), "PKR", spec="Dotted, pink cover"),
        offers.Item("Mochi Bunny Notebook A5", Decimal(1250), "PKR", spec="Lined, mint cover"),
        offers.Item("Kitty Cloud Mini Notebook A6", Decimal(650), "PKR"),
    ]
    texts = [("Mikus_Stationery_Catalogue.docx", TERMS)]
    return offers.Prepared(items=items, texts=texts, tiers=offers.read_tiers(texts), written=set())


def _said(sender, content):
    return SimpleNamespace(sender=sender, content=content)


HISTORY = [
    _said("user", "I want the mochi bunny notebook"),
    _said("agent", "The Mochi Bunny Notebook is PKR 1,250. Pink dotted or mint lined?"),
    _said("user", "pink dotted, and I want it delivered in lahore"),
    _said("agent", "Delivery to Lahore is PKR 350, total PKR 1,600. Address and payment method?"),
]


@pytest.fixture
def model(monkeypatch):
    """The model's reading, as it would answer. Tests set what it says."""
    answers = {}

    async def structured(prompt, timeout):
        return answers.get("next")

    monkeypatch.setattr(understanding, "structured", structured)
    monkeypatch.setattr(settings, "groq_api_key", "test")
    return answers


@pytest.fixture
async def shop(db_session):
    organization = Organization(name="Miku's Stationery", sales_prompt="Stationery.")
    db_session.add(organization)
    await db_session.flush()
    contact = CRMContact(organization_id=organization.id, phone_number="923001234567", name="Rimeun")
    db_session.add(contact)
    await db_session.flush()
    return organization, contact


def _reading(payment="online", address="lahore bahria town", place="lahore"):
    return {
        "lines": [{"product": "P1", "quantity": 1, "counted_in": "unit", "as_written": "pink dotted"}],
        "choose_between": [],
        "place": place,
        "address": address,
        "payment": payment,
        "note": "",
    }


def test_the_methods_come_from_the_business_s_own_words():
    assert orders.accepted_methods(_prepared()) == [
        "Cash on delivery", "Bank transfer", "JazzCash", "Easypaisa",
    ]
    method, could_be = orders.method_in("online", orders.accepted_methods(_prepared()))
    assert method is None and could_be == ["Bank transfer", "JazzCash", "Easypaisa"]
    assert orders.method_in("jazz cash please", orders.accepted_methods(_prepared()))[0] == "JazzCash"
    assert orders.method_in("paypal", orders.accepted_methods(_prepared())) == (None, [])


@pytest.mark.asyncio
async def test_the_real_conversation_asks_how_to_pay_then_confirms_then_places(shop, db_session, model):
    organization, contact = shop
    prepared = _prepared()

    model["next"] = _reading()
    first = await orders.handle_turn(
        db_session, organization, contact,
        "Preferred payment method is online and delivery address is lahore bahria town.",
        HISTORY, prepared,
    )
    assert first.performed is None and first.reply is None
    assert "NOTHING has been placed" in first.prompt_block
    assert "Bank transfer, JazzCash, Easypaisa" in first.prompt_block
    assert (await db_session.execute(select(Order))).scalars().all() == []

    model["next"] = _reading(payment="JazzCash")
    history = [*HISTORY, _said("user", "Preferred payment method is online and delivery address is lahore bahria town."),
               _said("agent", "Which one: bank transfer, JazzCash or Easypaisa?")]
    ready = await orders.handle_turn(db_session, organization, contact, "JazzCash", history, prepared)
    assert ready.performed is None
    assert ready.reply is not None
    for line in ("Mochi Bunny Notebook A5 Dotted × 1 — PKR 1,250", "Delivery (lahore): PKR 350",
                 "Total: PKR 1,600", "Deliver to: lahore bahria town", "Payment: JazzCash", "Reply YES"):
        assert line in ready.reply, (line, ready.reply)

    placed = await orders.handle_turn(db_session, organization, contact, "yes", history, prepared)
    assert placed.placed
    row = placed.order
    assert row.number == 1001 and row.total == Decimal("1600") and row.payment_method == "JazzCash"
    assert row.delivery_fee == Decimal("350") and row.address == "lahore bahria town"
    assert "#1001 is placed" in placed.reply and "PKR 1,600" in placed.reply
    assert "3–5 working days" in placed.reply
    assert "JazzCash details" in placed.reply
    assert orders.remembered(contact) is None

    again = await orders.handle_turn(db_session, organization, contact, "yes", history, prepared)
    assert not again.placed, "a second yes placed a second order"


@pytest.mark.asyncio
async def test_no_at_the_summary_places_nothing(shop, db_session, model):
    organization, contact = shop
    model["next"] = _reading(payment="cash on delivery")
    ready = await orders.handle_turn(
        db_session, organization, contact, "cash on delivery, address lahore bahria town", HISTORY, _prepared()
    )
    assert ready.reply and "Payment: Cash on delivery" in ready.reply
    no = await orders.handle_turn(db_session, organization, contact, "no", HISTORY, _prepared())
    assert not no.placed and "NOTHING was placed" in no.prompt_block
    assert (await db_session.execute(select(Order))).scalars().all() == []


@pytest.mark.asyncio
async def test_an_address_the_customer_never_wrote_is_asked_for(shop, db_session, model):
    organization, contact = shop
    model["next"] = _reading(payment="jazzcash", address="House 12, Street 4, DHA Phase 6")
    turn = await orders.handle_turn(db_session, organization, contact, "jazzcash", HISTORY, _prepared())
    assert turn.reply is None
    assert "full delivery address" in turn.prompt_block


@pytest.mark.asyncio
async def test_a_product_the_list_does_not_have_is_never_ordered(shop, db_session, model):
    organization, contact = shop
    model["next"] = {**_reading(payment="jazzcash"), "lines": [{"product": "P99", "quantity": 1}]}
    turn = await orders.handle_turn(db_session, organization, contact, "i want to order", HISTORY, _prepared())
    assert turn.reply is None and not turn.placed


@pytest.mark.asyncio
async def test_orders_are_numbered_per_shop(shop, db_session, model):
    organization, contact = shop
    state = {"lines": [{"name": "X", "quantity": "1", "total": "10"}], "goods_total": "10", "total": "10",
             "address": "somewhere"}
    first = await orders.place(db_session, organization, contact, state)
    second = await orders.place(db_session, organization, contact, state)
    assert (first.number, second.number) == (1001, 1002)


@pytest.mark.asyncio
async def test_without_a_model_the_order_goes_to_a_person(shop, db_session, monkeypatch):
    organization, contact = shop
    # Every key, not just the first. groq_api_keys is a property over
    # groq_api_key and _2.._5, so clearing the singular one leaves a working
    # .env reachable - the test then called the real providers and decided a
    # model was available, which is the opposite of what it is here to check.
    for base in ("groq_api_key", "gemini_api_key"):
        for suffix in ("", "_2", "_3", "_4", "_5"):
            monkeypatch.setattr(settings, base + suffix, "", raising=False)
    assert not settings.groq_api_keys and not settings.gemini_api_keys
    turn = await orders.handle_turn(db_session, organization, contact, "I want to order the notebook", [], _prepared())
    assert turn.needs_person and not turn.placed


@pytest.mark.parametrize(
    "reply",
    [
        "Great! I've noted the pink dotted Mochi Bunny Notebook for PKR 1,250.",
        "Your order has been placed and will reach you soon.",
        "Noted, your order is confirmed!",
        "I have recorded your order.",
    ],
)
def test_a_reply_may_not_claim_an_order_nobody_placed(reply):
    assert orders.claims_order(reply, placed=False)
    assert orders.claims_order(reply, placed=True) is None


@pytest.mark.parametrize(
    "reply",
    [
        "Shall I place the order for you?",
        "Reply YES and I can place it.",
        "The Mochi Bunny Notebook is PKR 1,250. Which colour would you like?",
    ],
)
def test_offering_to_place_an_order_is_not_a_claim(reply):
    assert orders.claims_order(reply, placed=False) is None


def test_when_will_i_receive_it_gets_the_delivery_time():
    found = offers.rules_for("Perfect, when will I receive it?", [("doc", TERMS)])
    assert found and "working days" in found[0]


# ------------------------------------------------------------- the live chat
@pytest.mark.asyncio
async def test_a_live_chat_places_the_order_and_tells_the_shop(db_session, monkeypatch, model):
    from app.api.webhook import process_inbound_message
    from app.models import Message
    from app.schemas import GenerationResult, TwilioWebhookPayload
    from app.services import llm_service, notifications
    from app.services.twilio_service import TwilioService

    organization = Organization(name="Miku's Stationery", sales_prompt="Stationery.")
    db_session.add(organization)
    await db_session.flush()

    async def prepare(db, org):
        return _prepared()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        return True, "SM_out"

    async def fake_generate(*_a, **_k):
        return GenerationResult(provider="groq", text="Which way would you like to pay?", prompt_used="p", latency_ms=5)

    raised = []

    async def record(db, org, event, title, body, contact_id=None):
        raised.append((event, title, body))

    async def no_read(*_a, **_k):
        return None

    monkeypatch.setattr(offers, "prepare", prepare)
    monkeypatch.setattr(understanding, "read_message", no_read)
    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)
    monkeypatch.setattr(notifications, "raise_and_send", record)

    async def say(text, sid):
        payload = TwilioWebhookPayload.model_validate(
            {"From": "whatsapp:+923001234567", "To": "whatsapp:+16602075318", "Body": text, "MessageSid": sid}
        )
        await process_inbound_message(db_session, payload)
        return (
            await db_session.execute(select(Message).where(Message.sender == "agent").order_by(Message.created_at))
        ).scalars().all()[-1].content

    model["next"] = _reading(payment="easypaisa")
    summary = await say("I want the pink dotted mochi notebook, lahore bahria town, easypaisa", "SMo1")
    assert "Reply YES" in summary and "Total: PKR 1,600" in summary
    confirmation = await say("yes", "SMo2")
    assert "#1001 is placed" in confirmation
    placed = [r for r in raised if r[0] == "order"]
    assert placed and "#1001" in placed[0][1] and "PKR 1,600" in placed[0][1]
    assert "Easypaisa details" in placed[0][2]



@pytest.mark.asyncio
async def test_the_order_is_committed_before_the_customer_is_told(db_session, monkeypatch, model):
    """"Order #1001 is placed" may not go out over a row that could still vanish.

    Everything after the order is written can fail - the alert, the outbound
    row, the final commit. A failure there would take the order with it, after
    the customer had been told, and hand #1001 to whoever ordered next.
    """
    from app.api.webhook import process_inbound_message
    from app.schemas import GenerationResult, TwilioWebhookPayload
    from app.services import llm_service, notifications
    from app.services.twilio_service import TwilioService

    organization = Organization(name="Miku's Stationery", sales_prompt="Stationery.")
    db_session.add(organization)
    await db_session.flush()

    happened: list[str] = []

    async def prepare(db, org):
        return _prepared()

    async def fake_send(self, to_number, body, media_urls=None, sender=None):
        happened.append("told the customer: " + ("order placed" if "#1001" in body else "something else"))
        return True, "SM_out"

    async def fake_generate(*_a, **_k):
        return GenerationResult(provider="groq", text="Which way would you like to pay?", prompt_used="p", latency_ms=5)

    async def record(db, org, event, title, body, contact_id=None):
        happened.append(f"alerted the shop: {event}")

    async def no_read(*_a, **_k):
        return None

    real_commit = db_session.commit
    real_place = orders.place

    async def watched_commit():
        happened.append("committed")
        return await real_commit()

    async def watched_place(db, organization, contact, state, source="agent"):
        order = await real_place(db, organization, contact, state, source)
        happened.append("order written")
        return order

    monkeypatch.setattr(offers, "prepare", prepare)
    monkeypatch.setattr(understanding, "read_message", no_read)
    monkeypatch.setattr(TwilioService, "send_whatsapp", fake_send)
    monkeypatch.setattr("app.api.webhook.llm_service.generate_reply", fake_generate)
    monkeypatch.setattr(notifications, "raise_and_send", record)
    monkeypatch.setattr(db_session, "commit", watched_commit)
    monkeypatch.setattr(orders, "place", watched_place)

    async def say(text, sid):
        payload = TwilioWebhookPayload.model_validate(
            {"From": "whatsapp:+923001234567", "To": "whatsapp:+16602075318", "Body": text, "MessageSid": sid}
        )
        await process_inbound_message(db_session, payload)

    model["next"] = _reading(payment="easypaisa")
    await say("I want the pink dotted mochi notebook, lahore bahria town, easypaisa", "SMc1")
    happened.clear()
    await say("yes", "SMc2")

    written = happened.index("order written")
    told = next(i for i, step in enumerate(happened) if step.endswith("order placed"))
    assert "committed" in happened[written:told], (
        "nothing committed the order between writing it and telling the customer: "
        + str(happened)
    )


# ------------------------------------------------------------- the dashboard
@pytest.mark.asyncio
async def test_the_shop_works_through_an_order_and_the_customer_is_told(org_a, monkeypatch):
    import uuid as _uuid

    from app.services import outbox

    from .conftest import _session_for

    session = _session_for(org_a._client)
    organization = await session.get(Organization, _uuid.UUID(org_a.organization_id))
    contact = CRMContact(organization_id=organization.id, phone_number="923001234567", name="Rimeun")
    session.add(contact)
    await session.flush()
    order = await orders.place(
        session, organization, contact,
        {"lines": [{"name": "Mochi Bunny Notebook A5 Dotted", "quantity": "1", "total": "1250"}],
         "currency": "PKR", "goods_total": "1250", "delivery_fee": "350", "delivery_known": True,
         "total": "1600", "address": "lahore bahria town", "payment": "JazzCash"},
    )
    await session.commit()

    listed = (await org_a.get("/api/v1/orders")).json()["orders"]
    assert [o["number"] for o in listed] == [1001]
    assert listed[0]["total_text"] == "PKR 1,600" and listed[0]["contact"]["name"] == "Rimeun"

    sent = []

    async def deliver(channel, to, text, **_kwargs):
        sent.append(text)
        return outbox.Delivery(outbox.SENT, "x", "delivered")

    monkeypatch.setattr(outbox, "deliver", deliver)
    paid = await org_a.patch(
        f"/api/v1/orders/{order.id}", json={"payment_status": "paid", "status": "dispatched", "tell_customer": True}
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["order"]["status"] == "dispatched"
    assert "order #1001 is on its way" in sent[-1].lower() and "PKR 1,600" in sent[-1]

    cancelled = await org_a.patch(f"/api/v1/orders/{order.id}", json={"status": "cancelled"})
    assert cancelled.json()["order"]["status"] == "cancelled"
    stuck = await org_a.patch(f"/api/v1/orders/{order.id}", json={"status": "delivered"})
    assert stuck.status_code == 409


@pytest.mark.asyncio
async def test_another_shop_sees_none_of_it(org_a, org_b):
    import uuid as _uuid

    from .conftest import _session_for

    session = _session_for(org_a._client)
    organization = await session.get(Organization, _uuid.UUID(org_a.organization_id))
    contact = CRMContact(organization_id=organization.id, phone_number="923001234568")
    session.add(contact)
    await session.flush()
    order = await orders.place(
        session, organization, contact,
        {"lines": [], "goods_total": "10", "total": "10", "address": "x"},
    )
    await session.commit()
    assert (await org_b.get("/api/v1/orders")).json()["orders"] == []
    assert (await org_b.patch(f"/api/v1/orders/{order.id}", json={"status": "confirmed"})).status_code == 404


@pytest.mark.asyncio
async def test_a_software_plan_is_never_asked_where_to_deliver(shop, db_session, model):
    organization, contact = shop
    items = [
        offers.Item("Growth plan", Decimal(4500), "PKR", sale_unit="month", spec="up to 5 users"),
        offers.Item("Thermal receipt printer", Decimal(18500), "PKR", sale_unit="unit"),
    ]
    texts = [("plans.docx", "We accept bank transfer and JazzCash. " + TERMS)]
    prepared = offers.Prepared(items=items, texts=texts, tiers=offers.read_tiers(texts), written=set())
    assert orders.is_delivered(items[1]) and not orders.is_delivered(items[0])

    model["next"] = {"lines": [{"product": "P1", "quantity": 1, "counted_in": "unit", "as_written": "growth plan"}],
                     "choose_between": [], "place": "", "address": "", "payment": "jazzcash", "note": ""}
    ready = await orders.handle_turn(
        db_session, organization, contact, "I want the growth plan, I'll pay by jazzcash", [], prepared
    )
    assert ready.reply, ready.prompt_block
    assert "Deliver to" not in ready.reply and "Delivery" not in ready.reply
    assert "Total: PKR 4,500" in ready.reply
    placed = await orders.handle_turn(db_session, organization, contact, "yes", [], prepared)
    assert placed.placed and "Deliver to" not in placed.reply and "working days" not in placed.reply


@pytest.mark.parametrize(
    "name",
    [
        "Annual Planner 2027",
        "Monthly Planner Pad",
        "Yearly Wall Calendar",
        "Cable Support Bracket",
        "Wall Plan Holder",
        "Training Whiteboard",
        "Desk Setup Organiser",
    ],
)
def test_a_thing_in_a_box_is_still_delivered_whatever_it_is_called(name):
    """A stationer sells Annual Planners; a supplier sells Support Brackets.

    Classed as software, nobody is asked where to send them and no delivery is
    charged - the shop finds out when it has nowhere to post the parcel.
    """
    assert orders.is_delivered(offers.Item(name, Decimal(500), "PKR", sale_unit="piece"))


@pytest.mark.parametrize(
    ("name", "unit"),
    [
        ("Starter plan", "month"),
        ("Extra user", "user"),
        ("Onboarding session", "session"),
        ("Data import service", "service"),
        ("Pro Subscription", "piece"),
        ("Extra User Licence", "seat"),
        ("Installation service", "piece"),
    ],
)
def test_nothing_is_posted_to_you_for_a_service(name, unit):
    assert not orders.is_delivered(offers.Item(name, Decimal(500), "PKR", sale_unit=unit))


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("Cash on delivery is available in Lahore. We also take card.", ["Cash on delivery", "Card"]),
        ("We take card and JazzCash.", ["Card", "JazzCash"]),
        ("We accept JazzCash and bank transfer.", ["JazzCash", "Bank transfer"]),
        # still only where it is offered, and only where a method is named
        ("Free trial for 14 days, no card needed.", []),
        ("We do not accept cash on delivery.", []),
        ("We take orders on WhatsApp until 6pm.", []),
        ("We take returns within 7 days.", []),
        ("Our staff carry cash floats for the till.", []),
    ],
)
def test_the_ways_to_pay_are_the_ways_the_business_says_it_takes(written, expected):
    """"We also take card" is how half of these documents put it."""
    prepared = offers.Prepared(items=[], texts=[("terms.docx", written)], tiers=[], written=set())
    assert sorted(orders.accepted_methods(prepared)) == sorted(expected)
