"""The price list, read and worked out before the model is asked to speak.

Written against a trade supplier's catalogue because it has every awkward
case in one place - cable sold by the coil, cement by the bag, two panels a
customer might mean - but nothing here is about that supplier. A salon, a
clothing shop and a subscription service are checked the same way below.
"""

from __future__ import annotations

import io
from decimal import Decimal

import docx
import pytest

from app.models import Organization
from app.schemas import GenerationResult
from app.services import llm_service, offers

TABLE = """NORTHSTAR TRADE SUPPLY
Price List Version: NS-2026-09 | Currency: PKR

Prices are quoted in Pakistani Rupees (PKR). Standard delivery fee is PKR 2,500 for orders below PKR 100,000; PKR 1,500 for orders from PKR 100,000 to PKR 249,999; and free standard delivery for orders of PKR 250,000 or more within Lahore. Trade discount: orders of PKR 500,000 or more receive 2% off eligible product lines.

SKU | Product | Specification | Sale Unit | Pack / Length | Unit Price (PKR)
NS-CBL-201 | CopperCore XLPE Cable | 4 mm², 2-core copper cable, 90°C XLPE insulation, 100 m coil | coil | 100 | 18,750
NS-CBL-202 | CopperCore XLPE Cable | 6 mm², 2-core copper cable, 90°C XLPE insulation, 100 m coil | coil | 100 | 27,900
NS-SOL-110 | HelioMax Solar Panel | 550 W monocrystalline, 144 half-cell, 2278 × 1134 mm | panel | 1 | 31,800
NS-SOL-125 | HelioMax Solar Panel | 585 W monocrystalline, N-type, 2278 × 1134 mm | panel | 1 | 34,650
NS-HVAC-302 | AeroFlow Copper Pipe | 3/8 in OD × 0.8 mm wall, refrigeration grade | coil | 15 | 12,400
NS-HVAC-303 | AeroFlow Copper Pipe | 5/8 in OD × 1.0 mm wall, refrigeration grade | coil | 15 | 19,850
NS-FST-410 | ProBuild Cement | Ordinary Portland Cement, 50 kg bag, Grade 42.5 | bag | 1 | 1,685
NS-ELC-610 | VoltSafe MCB | 2-pole, C-curve, 32 A, 6 kA breaking capacity | unit | 1 | 1,475
NS-ELC-620 | BrightLine LED Panel | 48 W, 600 × 600 mm, 4000 K, 4800 lm | unit | 1 | 3,250
NS-TOOL-710 | TorquePro Drill | 18 V cordless drill, 2 × 2.0 Ah batteries, charger included | kit | 1 | 18,900
NS-SAF-810 | SiteGuard Safety Helmet | ABS shell, adjustable ratchet, EN 397 compliant | unit | 1 | 1,150
"""


@pytest.fixture
def items():
    return offers.read_items([("price list", TABLE)], "PKR")


# ---------------------------------------------------------------- reading
def test_every_row_of_a_price_table_is_read(items):
    assert len(items) == 11
    cable = next(i for i in items if i.sku == "NS-CBL-201")
    assert cable.price == Decimal("18750")
    assert cable.currency == "PKR", "the currency is in the header, not the row"
    assert cable.sale_unit == "coil"
    assert cable.content == (Decimal("100"), "m")


def test_a_row_mentioning_a_charger_is_not_mistaken_for_a_header(items):
    """"Charger included" contains "charge"; it once cut the table short."""
    assert any(i.sku == "NS-TOOL-710" for i in items)
    assert any(i.sku == "NS-SAF-810" for i in items), "rows after it were lost"


def test_what_a_bag_holds_is_read_from_the_specification(items):
    cement = next(i for i in items if i.sku == "NS-FST-410")
    assert cement.content == (Decimal("50"), "kg")


def test_columns_are_found_by_meaning_not_position():
    text = "Price | Service | Duration\nAED 120 | Haircut | 45 min\nAED 350 | Colour | 2 hours\n"
    found = offers.read_items([("salon", text)])
    assert {(i.name, i.price, i.currency) for i in found} == {
        ("Haircut", Decimal("120"), "AED"),
        ("Colour", Decimal("350"), "AED"),
    }


def test_priced_lines_of_plain_text_are_read():
    found = offers.read_items(
        [("what you sell", "Classic manicure: AED 90\nGel manicure: AED 140\nWe are open daily.")]
    )
    assert {(i.name, i.price) for i in found} == {
        ("Classic manicure", Decimal("90")),
        ("Gel manicure", Decimal("140")),
    }


def test_a_sentence_with_several_prices_is_a_rule_not_a_product():
    found = offers.read_items(
        [("policy", "Delivery is PKR 2,500 below PKR 100,000 and free above PKR 250,000.")]
    )
    assert found == []


# ---------------------------------------------------------------- asking in the wrong unit
def test_metres_of_a_cable_sold_by_the_coil(items):
    """The case that started this: no per-metre price exists, so none is made up."""
    result = offers.quote("I only need 20 meters of the 4mm cable. How much?", items)

    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.item.sku == "NS-CBL-201", "4mm must pick the 4 mm² cable, not the 6 mm²"
    assert line.units == 1
    assert line.total == Decimal("18750")
    reply = result.reply()
    assert "100 m coil" in reply and "PKR 18,750" in reply
    assert "187.5" not in reply and "3,750" not in reply, "a per-metre price was invented"
    assert "tell me a little more" not in reply


def test_more_than_one_pack_is_counted_up(items):
    result = offers.quote("I want 100 kg of cement", items)
    assert result.lines[0].units == 2
    assert result.lines[0].total == Decimal("3370")


def test_asking_in_a_measure_it_is_not_listed_in_says_so(items):
    """A panel has no per-metre price, and saying so beats guessing."""
    result = offers.quote("5 metres of the 550W solar panel", items)
    line = result.lines[0]
    assert line.total is None
    assert "no per-metre price" in line.note


# ---------------------------------------------------------------- choosing and counting
def test_two_matching_products_are_both_offered(items):
    result = offers.quote("I need 10 solar panels. What do you have?", items)
    assert not result.lines
    [(wanted, options)] = result.options
    assert {o.sku for o in options} == {"NS-SOL-110", "NS-SOL-125"}
    assert wanted.quantity == 10
    reply = result.reply()
    assert "PKR 318,000" in reply and "PKR 346,500" in reply


def test_a_specification_picks_one_and_is_not_a_quantity(items):
    result = offers.quote("Give me the price for 10 of the 550W panels, including delivery.", items)
    [line] = result.lines
    assert line.item.sku == "NS-SOL-110"
    assert line.quantity == 10, "550 is the wattage, not how many"
    assert line.total == Decimal("318000")


def test_a_fraction_is_a_size(items):
    result = offers.quote("price of 3/8 copper pipe", items)
    [line] = result.lines
    assert line.item.sku == "NS-HVAC-302"
    assert line.quantity is None


def test_an_order_of_several_lines_has_a_subtotal(items):
    result = offers.quote(
        "12 x HelioMax Solar Panel 550 W, 6 x 100 m CopperCore 4 mm² cable coils, "
        "20 x BrightLine LED Panels and 10 x VoltSafe MCBs",
        items,
    )
    totals = {line.item.sku: line.total for line in result.lines}
    assert totals == {
        "NS-SOL-110": Decimal("381600"),
        "NS-CBL-201": Decimal("112500"),
        "NS-ELC-620": Decimal("65000"),
        "NS-ELC-610": Decimal("14750"),
    }
    assert result.subtotal == Decimal("573850")


def test_something_not_sold_produces_no_quote(items):
    assert offers.quote("Do you sell laptops?", items).empty()
    assert offers.quote("Hello", items).empty()


def test_a_follow_up_uses_what_they_asked_about_before(items):
    result = offers.quote("ok and how much for 3?", items, context="the 585W solar panel")
    [line] = result.lines
    assert line.item.sku == "NS-SOL-125" and line.total == Decimal("103950")


# ---------------------------------------------------------------- the guard
def _listed(items):
    return {i.price for i in items} | offers.amounts(TABLE)


def test_a_total_with_its_working_is_accepted(items):
    reply = "10 × PKR 31,800 = PKR 318,000, and delivery is free within Lahore."
    assert offers.unexplained(reply, _listed(items), {Decimal(10)}, set()) == set()


def test_delivery_added_to_a_subtotal_is_accepted(items):
    reply = "Subtotal PKR 37,500 + delivery PKR 2,500 = PKR 40,000."
    assert offers.unexplained(reply, _listed(items), {Decimal(2)}, set()) == set()


def test_a_listed_percentage_off_is_accepted(items):
    reply = "Your subtotal is PKR 573,850; 2% off is PKR 11,477, so PKR 562,373."
    rates = offers.percentages(TABLE)
    assert offers.unexplained(reply, _listed(items) | {Decimal("573850")}, set(), rates) == set()


def test_an_invented_price_is_still_refused(items):
    assert offers.unexplained(
        "The 4 mm cable is PKR 187 per metre.", _listed(items), {Decimal(20)}, set()
    ) == {Decimal("187")}
    assert offers.unexplained("That panel is PKR 29,999.", _listed(items), set(), set()) == {
        Decimal("29999")
    }


def test_a_bare_table_price_counts_as_listed(items):
    """Rows say "31,800"; the currency is only in the header."""
    assert offers.unexplained("It is PKR 31,800 per panel.", _listed(items), set(), set()) == set()


# ---------------------------------------------------------------- end to end
def _docx(paragraphs, table):
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    grid = document.add_table(rows=len(table), cols=len(table[0]))
    for r, row in enumerate(table):
        for c, value in enumerate(row):
            grid.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def _upload_catalogue(org):
    lines = [line for line in TABLE.splitlines() if "|" in line and "Version" not in line]
    table = [[cell.strip() for cell in line.split("|")] for line in lines]
    rules = [line for line in TABLE.split("\n\n") if "delivery" in line.lower()]
    data = _docx(["Prices are quoted in Pakistani Rupees (PKR).", *rules], table)
    response = await org._client.post(
        "/api/v1/knowledge/upload",
        headers=org.headers,
        files={"file": ("price-list.docx", data, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_the_upload_says_how_many_products_it_read(org_a):
    body = await _upload_catalogue(org_a)
    assert body["products_found"] == 11


@pytest.mark.asyncio
async def test_with_both_providers_down_the_sandbox_still_quotes_the_coil(org_a, monkeypatch):
    """The failure in the screenshot: "tell me a little more" instead of the answer."""
    await _upload_catalogue(org_a)

    async def down(*_a, **_k):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_service, "_call_groq", down)
    monkeypatch.setattr(llm_service, "_call_gemini", down)

    response = await org_a.post(
        "/api/v1/agent/simulate",
        json={"message": "I only need 20 meters of the 4mm cable. How much?"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "none"
    assert "100 m coil" in body["reply"] and "PKR 18,750" in body["reply"]
    assert "tell me a little more" not in body["reply"]
    assert body["quote"]["lines"][0]["item"].startswith("CopperCore XLPE Cable 4 mm²")


@pytest.mark.asyncio
async def test_a_reply_that_shows_its_total_is_not_refused(org_a, monkeypatch):
    """The other half of the screenshot: correct totals were being thrown away.

    The delivery threshold is in the document but not necessarily in the
    passages retrieved for this message; written anywhere by the business is
    enough.
    """
    await _upload_catalogue(org_a)
    prompts = []

    async def groq(prompt):
        prompts.append(prompt)
        return (
            "The HelioMax 550 W panel is PKR 31,800 each, so 10 × PKR 31,800 = PKR 318,000. "
            "Standard delivery is free within Lahore for orders of PKR 250,000 or more."
        )

    monkeypatch.setattr(llm_service, "_call_groq", groq)

    response = await org_a.post(
        "/api/v1/agent/simulate",
        json={"message": "Give me the price for 10 of the 550W panels, including delivery."},
    )
    body = response.json()
    assert body["provider"] == "groq", "a correct total was refused by the guard"
    assert len(prompts) == 1, "a correct reply should not need a retry"
    assert "PRICE FACTS" in prompts[0] and "PKR 318,000" in prompts[0]


@pytest.mark.asyncio
async def test_an_invented_per_metre_price_is_still_refused_end_to_end(org_a, monkeypatch):
    await _upload_catalogue(org_a)

    async def wrong(prompt):
        return "The 4 mm cable is PKR 190 per metre, so 20 m is PKR 3,800."

    monkeypatch.setattr(llm_service, "_call_groq", wrong)
    monkeypatch.setattr(llm_service, "_call_gemini", wrong)

    response = await org_a.post(
        "/api/v1/agent/simulate",
        json={"message": "I only need 20 meters of the 4mm cable. How much?"},
    )
    body = response.json()
    assert "190" not in body["reply"] and "3,800" not in body["reply"]
    assert "100 m coil" in body["reply"], "the fallback should be the real answer"


def test_a_delivery_question_brings_the_business_s_own_delivery_rule(items):
    texts = [("price list", TABLE)]
    rules = offers.rules_for("price for 10 of the 550W panels, including delivery", texts)
    assert rules and "PKR 250,000" in rules[0], "the delivery rule was not found"
    assert offers.rules_for("price for 10 of the 550W panels", texts) == []
    returns = [("policy", "A 15% restocking fee applies, and original delivery charges are non-refundable.")]
    assert offers.rules_for("including delivery?", returns) == [], "a returns rule is not a delivery charge"


def test_a_greeting_is_answered_with_a_greeting_when_no_model_can_answer():
    from app.services import sales_policy

    reply = sales_policy.deterministic_reply({}, [], Organization(name="Northstar"), message="Hello")
    assert reply.startswith("Hello") and "Northstar" in reply


def test_a_passage_starting_mid_word_is_not_read_out_from_the_middle():
    from app.services import sales_policy

    class Chunk:
        content = "ns, Exchanges & Warranty\n\nA return request must be submitted within 7 days."

    reply = sales_policy.deterministic_reply({}, [Chunk()], None, message="returns?")
    assert reply.startswith("A return request"), reply


# ---------------------------------------------------------------- order-size rules
def test_discount_and_delivery_tiers_are_read_from_the_business_s_sentences():
    tiers = offers.read_tiers([("price list", TABLE)])
    discount = [t for t in tiers if t.topic == "discount"]
    delivery = [t for t in tiers if t.topic == "delivery"]
    assert [(t.low, t.percent) for t in discount] == [(Decimal("500000"), Decimal("2"))]
    assert [(t.low, t.high, t.fee, t.free) for t in delivery] == [
        (None, Decimal("99999.99"), Decimal("2500"), False),
        (Decimal("100000"), Decimal("249999"), Decimal("1500"), False),
        (Decimal("250000"), None, None, True),
    ]
    assert delivery[-1].condition == "within Lahore"


def _applied(value, topics=("discount", "delivery")):
    tiers = offers.read_tiers(
        [("price list", TABLE + "\nOrders of PKR 1,000,000 or more receive 4%.")]
    )
    return offers.apply_tiers(Decimal(value), tiers, "PKR", set(topics))


def test_one_rupee_short_of_the_discount_says_so():
    applied = _applied("499999", ("discount",))
    assert applied.discount is None
    assert "PKR 1 more" in " ".join(applied.lines())


def test_the_best_rate_that_applies_is_used_and_the_next_one_named():
    applied = _applied("999999", ("discount",))
    assert applied.discount.percent == 2
    assert applied.saving == Decimal("19999.98")
    assert applied.next_discount.percent == 4
    assert _applied("1000000", ("discount",)).discount.percent == 4


def test_delivery_follows_the_order_value():
    assert _applied("40000", ("delivery",)).delivery.fee == Decimal("2500")
    assert _applied("150000", ("delivery",)).delivery.fee == Decimal("1500")
    assert _applied("318000", ("delivery",)).delivery.free


def test_a_customer_s_own_figure_can_be_said_back():
    listed = {Decimal("500000")}
    said = offers.customer_figures(["I have an order worth PKR 499,999, or 499999 anyway"])
    reply = "Your PKR 499,999 order is PKR 1 short of PKR 500,000."
    assert offers.unexplained(reply, listed | said, set(), set()) == set()


def test_an_earlier_product_is_not_dragged_into_an_unrelated_question(items):
    assert offers.quote(
        "What about delivery in Lahore?", items, context="6 coils of the 4mm cable"
    ).lines == []


def test_the_same_product_is_not_listed_twice(items):
    result = offers.quote("4mm cable price? The 4mm cable, how much?", items)
    assert len(result.lines) == 1


# ---------------------------------------------------------------- the conversation that broke
CONVERSATION = [
    ("Hello", "Hello Test customer. We are open Monday to Saturday, 9:00 AM–6:00 PM PKT. "
     "What trade materials are you looking for today?"),
    ("I need 10 solar panels. What do you have?",
     "We have two options for your 10-panel order:\nNS-SOL-110 (550 W) at PKR 31,800 per panel, "
     "totaling PKR 318,000.\nNS-SOL-125 (585 W) at PKR 34,650 per panel, totaling PKR 346,500.\n"
     "Which model fits your project better?"),
    ("Give me the price for 10 of the 550W panels, including delivery.",
     "10 x HelioMax 550 W panels at PKR 318,000, with free delivery within Lahore for orders "
     "above PKR 250,000. Total is PKR 318,000. Where should we deliver the order?"),
    ("I only need 20 meters of the 4mm cable. How much?",
     "The 4 mm² CopperCore cable is sold as a full 100 m coil for PKR 18,750, so 20 meters "
     "requires one coil. Shall I add this to your solar panel order?"),
    ("Fine, give me 1 coil. How many meters is that?",
     "That coil is 100 meters. With your 10 solar panels, your total is PKR 336,750 "
     "(PKR 318,000 + PKR 18,750). What is your delivery address in Lahore?"),
    ("I need 6 coils of the 4mm cable, 12 solar panels 550W, and 20 LED panels. What's my total?",
     "6 coils of 4 mm² cable: PKR 112,500.\n12 panels 550 W: PKR 381,600.\n20 LED panels: "
     "PKR 65,000.\nSubtotal: PKR 559,100.\nWith 2% trade discount: PKR 547,918.\n"
     "Free delivery in Lahore. What is your site address?"),
    ("What about delivery in Lahore?",
     "Standard delivery within Lahore is free for orders of PKR 250,000 or more, so your "
     "PKR 547,918 total incurs no delivery charge."),
    ("I have an order worth PKR 499,999. Can you give me the 2% trade discount?",
     "At PKR 499,999 you are PKR 1 short of the PKR 500,000 needed for the 2% trade discount, "
     "so it does not apply yet. Add PKR 1 more and it will."),
    ("What if I make it PKR 999,999?",
     "At PKR 999,999 the 2% discount applies: PKR 19,999.98 off, so PKR 979,999.02. "
     "The 4% rate starts at PKR 1,000,000, just PKR 1 more."),
]


@pytest.mark.asyncio
async def test_the_whole_conversation_that_broke_is_answered_first_time(org_a, monkeypatch):
    """Every reply here was right, or is what right looks like; none may be refused.

    The last two were refused before because the customer's own figure was
    treated as an invented price, and both providers timed out trying.
    """
    lines = [line for line in TABLE.splitlines() if "|" in line and "Version" not in line]
    table = [[cell.strip() for cell in line.split("|")] for line in lines]
    rules = [block for block in TABLE.split("\n\n") if "delivery" in block.lower()]
    data = _docx(
        ["Prices are quoted in Pakistani Rupees (PKR).", *rules,
         "Orders of PKR 1,000,000 or more receive 4%. Discounts are not cumulative."],
        table,
    )
    uploaded = await org_a._client.post(
        "/api/v1/knowledge/upload",
        headers=org_a.headers,
        files={"file": ("price-list.docx", data, "application/octet-stream")},
    )
    assert uploaded.status_code == 201

    history: list[dict] = []
    for question, answer in CONVERSATION:
        calls = []

        async def groq(prompt, answer=answer):
            calls.append(prompt)
            return answer

        monkeypatch.setattr(llm_service, "_call_groq", groq)
        response = await org_a.post(
            "/api/v1/agent/simulate", json={"message": question, "history": history}
        )
        body = response.json()
        assert body["provider"] == "groq", (question, body.get("why"))
        assert len(calls) == 1, f"{question!r} needed a retry: {body.get('why')}"
        history += [{"sender": "user", "content": question}, {"sender": "agent", "content": answer}]


@pytest.mark.asyncio
async def test_the_discount_question_is_worked_out_before_the_model_answers(org_a, monkeypatch):
    await _upload_catalogue(org_a)
    prompts = []

    async def groq(prompt):
        prompts.append(prompt)
        return "At PKR 499,999 you are PKR 1 short of PKR 500,000, so no discount yet."

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    await org_a.post(
        "/api/v1/agent/simulate",
        json={"message": "I have an order worth PKR 499,999. Can you give me the 2% trade discount?"},
    )
    assert "No discount applies yet" in prompts[0] and "PKR 1 more" in prompts[0]


@pytest.mark.asyncio
async def test_a_refusal_is_explained_in_the_sandbox(org_a, monkeypatch):
    await _upload_catalogue(org_a)
    seen = []

    async def wrong(prompt):
        seen.append(prompt)
        return "The 4 mm cable is PKR 190 per metre."

    monkeypatch.setattr(llm_service, "_call_groq", wrong)
    monkeypatch.setattr(llm_service, "_call_gemini", wrong)
    response = await org_a.post(
        "/api/v1/agent/simulate", json={"message": "I only need 20 meters of the 4mm cable. How much?"}
    )
    body = response.json()
    assert body["provider"] == "none"
    assert "190" in body["why"], "the reason should name the refused figure"
    assert "BEFORE YOU WRITE" in seen[2], "the second provider was not told what went wrong"


@pytest.mark.asyncio
async def test_rules_are_quoted_in_the_currency_they_are_written_in(org_a, monkeypatch):
    """A business left on the USD default whose price list is in PKR."""
    await _upload_catalogue(org_a)

    async def down(*_a, **_k):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_service, "_call_groq", down)
    monkeypatch.setattr(llm_service, "_call_gemini", down)
    response = await org_a.post(
        "/api/v1/agent/simulate",
        json={
            "message": "What about delivery in Lahore?",
            "history": [{"sender": "user", "content": "I need 12 solar panels 550W. What's my total?"}],
        },
    )
    reply = response.json()["reply"]
    assert "PKR 381,600" in reply and "free" in reply and "USD" not in reply


# ---------------------------------------------------------------- pictures
def test_the_product_list_says_whether_photos_are_really_going():
    from app.models import KnowledgeDocument
    from app.services import product_search

    product = KnowledgeDocument(title="Brogue", content="", attributes={"price": "15500"}, media_urls=[])
    assert "NO photos" in product_search.as_prompt_block([product], photos_attached=False)
    assert "ARE attached" in product_search.as_prompt_block([product], photos_attached=True)


@pytest.mark.asyncio
async def test_a_reply_may_not_say_it_is_sending_pictures_it_is_not(monkeypatch):
    replies = iter(["Here are the photos of the 550 W panel.", "The 550 W panel is PKR 31,800."])

    async def groq(prompt):
        return next(replies)

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    result = await llm_service.generate_reply(
        Organization(name="Northstar", sales_prompt="Sell well."), None, [], "show me the panel",
        photos_attached=False,
    )
    assert result.text == "The 550 W panel is PKR 31,800."


@pytest.mark.asyncio
async def test_a_reply_may_mention_pictures_that_are_going(monkeypatch):
    async def groq(prompt):
        return "Here are the photos of the brogue."

    monkeypatch.setattr(llm_service, "_call_groq", groq)
    result = await llm_service.generate_reply(
        Organization(name="Shoes", sales_prompt="Sell well."), None, [], "show me", photos_attached=True
    )
    assert result.provider == "groq" and "photos" in result.text


@pytest.mark.asyncio
async def test_an_uploaded_price_list_is_not_offered_as_matching_products(org_a, db_session):
    from app.services import product_search

    await _upload_catalogue(org_a)
    # Uploaded as "product" on purpose: that is what used to leak through.
    data = _docx(["Solar panels in stock."], [["Product", "Price"], ["HelioMax Solar Panel", "PKR 31,800"]])
    await org_a._client.post(
        "/api/v1/knowledge/upload",
        headers=org_a.headers,
        files={"file": ("panels.docx", data, "application/octet-stream")},
        data={"doc_type": "product"},
    )
    import uuid as _uuid
    found = await product_search.find_products(db_session, _uuid.UUID(org_a.organization_id), "solar panel price")
    assert found == [], [p.title for p in found]
