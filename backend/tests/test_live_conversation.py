"""The turns that went wrong in a real WhatsApp conversation with the agent.

Each test is one message from that conversation, and checks the thing that
decided the answer - the reading of the order, the rule quoted, the guard -
rather than the wording a model happened to produce. Nothing here is about the
trade supplier whose documents were used: the same reading serves any shop.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.schemas import GenerationResult  # noqa: F401 - keeps the import graph the app uses
from app.services import customer_memory, llm_service, offers, sales_policy

from .test_offers import TABLE, _docx

TERMS = """5. Payment Policy
New customers: 50% advance payment at order confirmation and 50% before dispatch, unless a written credit arrangement has been approved.
Customers with approved credit terms may receive Net 15 payment terms. Credit limits and terms are subject to written approval.
Accepted methods: bank transfer, company cheque subject to clearance, and approved digital payment methods listed on the invoice.
Cash payments above the company's internal transaction limit are not accepted.
6. Returns, Exchanges & Warranty
A return request must be submitted within 7 calendar days of delivery for visible shortages, wrong items, or transit damage.
The customer must photograph damaged packaging and goods and notify support before installing, cutting, modifying, or disposing of the affected item.
Custom-cut cable, cut pipe, configured equipment, special-order products, opened electrical components, and goods procured specifically for a customer are non-returnable unless defective or supplied incorrectly.
Used, installed, altered, damaged-through-misuse, or incomplete goods are not eligible for standard return.
Warranty claims are handled according to the manufacturer's warranty terms where a manufacturer warranty applies. Proof of purchase may be required.
Refunds for approved returns are issued to the original payment method within 10 business days after inspection and approval.
"""

FINAL_ORDER = (
    "I need 12 of the 550W panels, 6 coils of the 4mm cable, 20 LED panels and 10 MCBs. "
    "I'm in Lahore. Give me the final price including delivery and your maximum possible "
    "discount. Also, I want 20m of the 4mm cable rather than a full coil, and if anything "
    "arrives damaged I'll return it after installation."
)


@pytest.fixture
def items():
    return offers.read_items([("price list", TABLE)], "PKR")


# ---------------------------------------------------------------- the last order
def test_every_line_of_a_long_order_is_its_own_product(items):
    """"10 MCBs." ran on into the next sentence about 4mm cable and was quoted as 10 coils."""
    result = offers.quote(FINAL_ORDER, items)
    counted = {line.item.sku: line.quantity for line in result.lines if not line.aside}
    assert counted == {
        "NS-SOL-110": 12,
        "NS-CBL-201": 6,
        "NS-ELC-620": 20,
        "NS-ELC-610": 10,
    }
    assert result.subtotal == Decimal("573850")


def test_metres_of_an_item_already_counted_are_not_a_second_line(items):
    """"20m rather than a full coil" is answered with how it is sold, not a seventh coil."""
    result = offers.quote(FINAL_ORDER, items)
    aside = [line for line in result.lines if line.aside]
    assert len(aside) == 1 and aside[0].item.sku == "NS-CBL-201"
    assert aside[0].total is None
    assert "not by the metre" in aside[0].note
    reply = result.reply()
    assert "PKR 573,850" in reply
    assert reply.count("× PKR 18,750") == 1, reply


def test_a_total_of_several_lines_is_accepted_with_its_working():
    listed = {Decimal(p) for p in ("31800", "18750", "3250", "1475")}
    quantities = {Decimal(q) for q in ("12", "6", "20", "10")}
    reply = (
        "12 × PKR 31,800 = PKR 381,600; 6 × PKR 18,750 = PKR 112,500; 20 × PKR 3,250 = "
        "PKR 65,000; 10 × PKR 1,475 = PKR 14,750. Total PKR 573,850."
    )
    assert offers.unexplained(reply, listed, quantities, set()) == set()
    invented = reply.replace("573,850", "569,000")
    assert offers.unexplained(invented, listed, quantities, set()) == {Decimal("569000")}


# ---------------------------------------------------------------- payment and returns
@pytest.mark.parametrize(
    "message, expected",
    [
        ("Fine, I'll pay 10% now and the rest next month.", "New customers: 50% advance"),
        ("I'll pay everything after delivery.", "New customers: 50% advance"),
        ("Do you accept cash?", "Cash payments above"),
        ("Actually I opened the MCB and installed it. Can I return it?", "opened electrical components"),
        ("If anything arrives damaged I'll return it after installation.", "before installing"),
    ],
)
def test_terms_questions_are_answered_from_the_terms(message, expected):
    rules = offers.rules_for(message, [("terms", TERMS)])
    assert any(expected in rule for rule in rules), rules
    assert not any(rule.startswith("Warranty claims") for rule in rules if "pay" in message)


def test_a_heading_is_not_quoted_as_a_rule():
    rules = offers.rules_for("Can I return it?", [("terms", TERMS)], limit=5)
    assert "Returns, Exchanges & Warranty" not in rules


def test_short_topic_words_are_whole_words():
    assert "payment" not in offers.topics_in("What is the code on the display?")
    assert "payment" in offers.topics_in("Is COD available?")


def test_a_terms_only_answer_is_still_an_answer(items):
    result = offers.quote("I'll pay everything after delivery.", items)
    result.rules = offers.rules_for("I'll pay everything after delivery.", [("terms", TERMS)])
    assert not result.empty()
    assert "New customers: 50% advance" in result.prompt_block()
    assert "Do not add terms" in result.prompt_block()
    assert "New customers: 50% advance" in result.reply()


# ---------------------------------------------------------------- with no model at all
def test_the_last_resort_reads_out_what_was_asked_about_not_the_top_passage():
    """It answered "pay after delivery" with warranty terms, then with the whole price table."""

    class Chunk:
        def __init__(self, content):
            self.content = content

    table = "\n".join(line for line in TABLE.splitlines() if "|" in line)
    chunks = [Chunk(TERMS.split("6. Returns")[1]), Chunk(table), Chunk(TERMS.split("6. Returns")[0])]
    reply = sales_policy.deterministic_reply({}, chunks, None, message="I'll pay everything after delivery.")
    assert "|" not in reply and "Warranty claims" not in reply
    assert "payment" in reply.lower()

    nothing = sales_policy.deterministic_reply({}, [Chunk(table)], None, message="Is it raining?")
    assert "|" not in nothing


# ---------------------------------------------------------------- the guard
@pytest.mark.parametrize(
    "reply",
    [
        "Understood, the unit prices will be reduced directly without a separate discount line.",
        "We will share a signed proforma invoice with bank transfer details to build trust.",
        "Shall I send the invoice for your ten panels?",
        "We'll lower the unit price on the quotation for you.",
    ],
)
def test_a_document_or_price_change_nobody_will_make_is_refused(reply):
    assert llm_service.unbacked_commitments(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Unit prices are as listed: PKR 31,800 per panel.",
        "The 4% discount is shown as its own line; unit prices stay as listed.",
        "Prices are valid for 30 days from the effective date.",
        "Accepted methods are listed on the invoice.",
    ],
)
def test_stating_the_terms_is_not_a_commitment(reply):
    assert llm_service.unbacked_commitments(reply) is None


def test_offering_pictures_is_caught():
    assert llm_service.offers_photos("Shall I send over product images for your review?")
    assert llm_service.offers_photos("Would you like to see pictures?")
    assert llm_service.offers_photos("We don't have pictures of that.") is None


def test_a_one_word_urdu_opening_is_caught():
    assert llm_service.opens_in_urdu("Ji, 1 coil means 100 meters of cable.")
    assert not llm_service.opens_in_urdu("Jigsaws are not something we stock.")


@pytest.mark.asyncio
async def test_the_guard_refuses_an_image_offer_when_there_are_no_images(monkeypatch):
    replies = iter(
        [
            "10 panels total PKR 318,000. Shall I send over product images for your review?",
            "10 panels total PKR 318,000. Where should we deliver?",
        ]
    )

    async def groq(prompt):
        return next(replies)

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    result = await llm_service.generate_reply(
        None, None, [], "10 of the 550W panels", knowledge="PKR 31,800",
        known_quantities=[10], photos_available=False,
    )
    assert result.provider == "groq"
    assert "images" not in result.text


@pytest.mark.asyncio
async def test_the_guard_refuses_a_price_change_promise(monkeypatch):
    replies = iter(
        [
            "Understood, the unit prices will be reduced directly.",
            "The discount stays a separate line; unit prices are as listed.",
        ]
    )

    async def groq(prompt):
        return next(replies)

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    result = await llm_service.generate_reply(
        None, None, [], "Don't mention the discount. Just reduce the unit price."
    )
    assert "will be reduced" not in result.text


# ---------------------------------------------------------------- memory on hello
def test_a_hello_is_not_answered_with_an_old_purchase():
    memory = {
        "facts": {"category_interest": {"value": "wedding pair", "confidence": 1.0}},
        "commitments": ["wedding pair secured"],
    }
    assert customer_memory.as_prompt_block(memory, greeting=True) == ""
    block = customer_memory.as_prompt_block(memory)
    assert "wedding pair" in block and "Do not open a reply" in block


# ---------------------------------------------------------------- "what if I make it"
@pytest.mark.asyncio
async def test_a_value_the_customer_names_is_the_one_answered(org_a, monkeypatch):
    """"What if I make it PKR 500,000 exactly?" was answered about the earlier order."""
    lines = [line for line in TABLE.splitlines() if "|" in line and "Version" not in line]
    table = [[cell.strip() for cell in line.split("|")] for line in lines]
    rules = [block for block in TABLE.split("\n\n") if "delivery" in block.lower()]
    uploaded = await org_a._client.post(
        "/api/v1/knowledge/upload",
        headers=org_a.headers,
        files={"file": ("price-list.docx", _docx(rules, table), "application/octet-stream")},
    )
    assert uploaded.status_code == 201
    prompts = []

    async def groq(prompt):
        prompts.append(prompt)
        return "At PKR 500,000 the 2% discount applies: PKR 10,000 off, so PKR 490,000."

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    history = [
        {"sender": "user", "content": "I need 6 coils of the 4mm cable, 12 solar panels 550W, and 20 LED panels."},
        {"sender": "agent", "content": "Subtotal PKR 559,100."},
        {"sender": "user", "content": "I have an order worth PKR 499,999. Can you give me the 2% trade discount?"},
        {"sender": "agent", "content": "PKR 499,999 is PKR 1 short of PKR 500,000."},
    ]
    response = await org_a.post(
        "/api/v1/agent/simulate",
        json={"message": "What if I make it PKR 500,000 exactly?", "history": history},
    )
    body = response.json()
    assert body["provider"] == "groq", body.get("why")
    assert "named an order value of PKR 500,000" in prompts[0]


@pytest.mark.asyncio
async def test_pay_later_is_answered_from_the_payment_terms_with_no_model(org_a, monkeypatch):
    uploaded = await org_a._client.post(
        "/api/v1/knowledge/upload",
        headers=org_a.headers,
        files={"file": ("terms.docx", _docx(TERMS.splitlines(), [["SKU", "Product", "Unit Price (PKR)"], ["A-1", "Helmet", "1,150"]]), "application/octet-stream")},
    )
    assert uploaded.status_code == 201

    async def down(*_a, **_k):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_service, "_call_groq", down)
    monkeypatch.setattr(llm_service, "_call_gemini", down)
    response = await org_a.post(
        "/api/v1/agent/simulate", json={"message": "Fine, I'll pay 10% now and the rest next month."}
    )
    reply = response.json()["reply"]
    assert "50% advance" in reply, reply
    assert "|" not in reply


@pytest.mark.parametrize(
    "message, topics",
    [
        ("How much will I pay for 10 panels?", set()),
        ("I'm a returning customer", set()),
        ("It was delivered 10 days ago. Can I return it?", {"returns"}),
        ("I'll pay everything after delivery.", {"payment"}),
        ("What about delivery in Lahore?", {"delivery"}),
    ],
)
def test_a_topic_is_what_was_asked_not_a_word_that_appears(message, topics):
    assert offers.topics_in(message) == topics


def test_days_ago_is_not_a_quantity(items):
    assert offers.quote(
        "It was delivered 10 days ago but I only noticed the wrong item today.",
        items,
        context="I opened the MCB",
    ).lines == []
