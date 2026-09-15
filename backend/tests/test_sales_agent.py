"""The production sales-agent behaviours: policy, memory, analysis, products.

These cover the failures the architecture guide is written to prevent —
generic handoffs, lost requirements, colour mismatches — at the unit level, so
they hold without a live provider.
"""

from types import SimpleNamespace

import pytest

from app.services import analyzer, customer_memory, media_service, product_search, sales_policy
from app.services.llm_service import build_prompt


def product(title, colour=None, fabric=None, price=None, media=("https://cdn/x.jpg",), content=""):
    return SimpleNamespace(
        title=title,
        content=content or title,
        media_urls=list(media),
        attributes={"colour": colour, "fabric": fabric, "price": price, "currency": "PKR"},
    )


# ============================ no generic handoff ===========================
@pytest.mark.parametrize(
    "reply",
    [
        "Our team will get back to you shortly.",
        "We'll contact you soon with details.",
        "A representative will call you.",
        "Let me connect you with our support team.",
        "I am forwarding your query to our team.",
    ],
)
def test_handoff_phrases_are_caught(reply):
    assert sales_policy.contains_handoff(reply) is not None


@pytest.mark.parametrize(
    "reply",
    [
        "The Kitten Heel is PKR 8,900. Shall I reserve one?",
        "Delivery takes 5 to 7 working days across Pakistan.",
        "We don't stock that colour, but the maroon one is lovely.",
    ],
)
def test_good_replies_are_not_flagged(reply):
    assert sales_policy.contains_handoff(reply) is None


def test_policy_forbids_handoffs_in_the_prompt():
    org = SimpleNamespace(
        name="Nishat Linen",
        sales_prompt="Sell clothes.",
        product_rules="",
        default_currency="PKR",
        default_language="en",
    )
    prompt = build_prompt(org, None, [], "hello")

    assert "NEVER say a human will follow up" in prompt


def test_last_resort_lists_products_rather_than_promising_a_callback():
    reply = sales_policy.deterministic_reply(
        {"intent": "image_request"},
        [],
        SimpleNamespace(name="Nishat Linen"),
        [product("Printed Suit", price="2196.00")],
    )

    assert "Printed Suit" in reply
    assert "2196" in reply
    assert sales_policy.contains_handoff(reply) is None


def test_last_resort_without_anything_still_avoids_a_handoff():
    reply = sales_policy.deterministic_reply({}, [], SimpleNamespace(name="Nishat Linen"))

    assert sales_policy.contains_handoff(reply) is None
    assert reply.strip()


# ============================ product matching =============================
def test_red_does_not_match_embroidered():
    """"red" is a substring of "embroidered" — the classic false positive."""
    item = product("Embroidered Shirt", colour="blue", content="Embroidered Shirt in blue")

    assert product_search.score_product(item, product_search.wanted_attributes("red suit"), "red suit") == 0.0


def test_exact_colour_outranks_a_family_match():
    exact = product("Red Suit", colour="red")
    family = product("Maroon Suit", colour="maroon")
    wanted = product_search.wanted_attributes("red suit")

    assert product_search.score_product(exact, wanted, "red suit") > product_search.score_product(
        family, wanted, "red suit"
    )


def test_a_family_colour_still_matches():
    family = product("Maroon Suit", colour="maroon")

    assert product_search.colour_match(family, ["red"]) == 1.0


def test_a_different_colour_never_matches():
    other = product("Blue Suit", colour="blue")

    assert product_search.colour_match(other, ["red"]) == 0.0


def test_fabric_and_style_words_are_read_from_the_request():
    wanted = product_search.wanted_attributes("show me red printed lawn suits")

    assert "red" in wanted["colours"]
    assert "lawn" in wanted["fabrics"]
    assert "printed" in wanted["styles"]


def test_product_block_quotes_the_real_price():
    block = product_search.as_prompt_block([product("Printed Suit", colour="red", price="2196.00")])

    assert "Printed Suit" in block and "2196.00" in block


def test_no_match_note_tells_the_agent_not_to_invent():
    note = product_search.no_match_note("red lawn", product_search.wanted_attributes("red lawn"))

    assert "do not invent" in note.lower()


def test_media_is_one_image_per_product_by_default():
    urls = product_search.media_for(
        [
            product("A", media=("https://cdn/a1.jpg", "https://cdn/a2.jpg")),
            product("B", media=("https://cdn/b1.jpg",)),
        ]
    )

    assert urls == ["https://cdn/a1.jpg", "https://cdn/b1.jpg"]


# ============================== attachments ================================
def test_inbound_media_is_read_from_the_twilio_form():
    found = media_service.extract_inbound(
        {
            "NumMedia": "2",
            "MediaUrl0": "https://api.twilio.com/a.jpg",
            "MediaContentType0": "image/jpeg",
            "MediaUrl1": "https://api.twilio.com/b.png",
            "MediaContentType1": "image/png",
        }
    )

    assert found == [
        ("https://api.twilio.com/a.jpg", "image/jpeg"),
        ("https://api.twilio.com/b.png", "image/png"),
    ]


def test_no_media_reads_as_empty():
    assert media_service.extract_inbound({"NumMedia": "0"}) == []
    assert media_service.extract_inbound({}) == []


def test_only_publicly_fetchable_urls_are_sent():
    """WhatsApp fetches media itself, so a relative path would silently fail."""
    usable = media_service.sendable(["/media/local.jpg", "https://cdn/ok.jpg", ""])

    assert usable == ["https://cdn/ok.jpg"]


def test_attachments_are_capped():
    many = [f"https://cdn/{i}.jpg" for i in range(10)]

    assert len(media_service.sendable(many, limit=3)) == 3


# ============================ customer memory ==============================
def test_a_stated_fact_outranks_an_inferred_one():
    memory = customer_memory.record_fact({}, "budget", "5000", source="customer")
    memory = customer_memory.record_fact(memory, "budget", "9000", source="inferred")

    assert memory["facts"]["budget"]["value"] == "5000"


def test_an_inference_fills_a_gap():
    memory = customer_memory.record_fact({}, "city", "Lahore", source="inferred")

    assert memory["facts"]["city"]["value"] == "Lahore"
    assert memory["facts"]["city"]["confidence"] < 1.0


def test_a_withdrawn_requirement_is_marked_not_deleted():
    memory = customer_memory.add_requirement({}, "red lawn")
    memory = customer_memory.add_requirement(memory, "blue silk")
    memory = customer_memory.drop_requirement(memory, "red lawn")

    assert customer_memory.active_requirements(memory) == ["blue silk"]
    assert customer_memory.dropped_requirements(memory) == ["red lawn"]


def test_dropped_requirements_are_shown_so_they_are_not_reoffered():
    memory = customer_memory.add_requirement({}, "red lawn")
    memory = customer_memory.drop_requirement(memory, "red lawn")

    assert "No longer wanted" in customer_memory.as_prompt_block(memory)


def test_an_objection_is_recorded_once_until_resolved():
    memory = customer_memory.add_objection({}, "price", "too expensive")
    memory = customer_memory.add_objection(memory, "price", "still pricey")

    assert len(customer_memory.open_objections(memory)) == 1


def test_resolving_an_objection_clears_it():
    memory = customer_memory.add_objection({}, "price")
    memory = customer_memory.resolve_objection(memory, "price")

    assert customer_memory.open_objections(memory) == []


def test_analysis_folds_into_memory():
    memory = customer_memory.apply_analysis(
        {},
        {
            "colour_preference": "red",
            "city": "Lahore",
            "new_requirements": ["lawn suit"],
            "objection": "price",
            "commitments": ["wants it before Eid"],
        },
    )

    assert memory["facts"]["colour_preference"]["value"] == "red"
    assert customer_memory.active_requirements(memory) == ["lawn suit"]
    assert len(customer_memory.open_objections(memory)) == 1
    assert memory["commitments"] == ["wants it before Eid"]


def test_memory_block_tells_the_agent_not_to_ask_again():
    memory = customer_memory.record_fact({}, "size", "M")

    assert "do NOT ask for any of these again" in customer_memory.as_prompt_block(memory)


def test_empty_memory_renders_as_nothing():
    assert customer_memory.as_prompt_block({}) == ""


def test_malformed_memory_is_repaired_rather_than_crashing():
    assert customer_memory.normalise("not a dict") == customer_memory.empty()
    assert customer_memory.normalise({"facts": "wrong type"})["facts"] == {}


# ============================== the analyzer ===============================
def test_keyword_reading_spots_an_image_request():
    result = analyzer.heuristic_analysis("show me red suits")

    assert result["wants_images"] is True
    assert result["colour_preference"] == "red"


def test_keyword_reading_spots_payment_and_delivery():
    assert analyzer.heuristic_analysis("do you take COD?")["intent"] == "payment_question"
    assert analyzer.heuristic_analysis("how long is delivery?")["intent"] == "delivery_question"


def test_a_purchase_moves_to_ready_to_buy():
    assert analyzer.heuristic_analysis("I want to order this")["stage"] == "READY_TO_BUY"


def test_stages_never_move_backwards():
    assert analyzer.advance_stage("READY_TO_BUY", "DISCOVERY") == "READY_TO_BUY"
    assert analyzer.advance_stage("NEW", "QUALIFIED") == "QUALIFIED"


def test_every_sales_stage_maps_to_a_crm_bucket():
    """Checked against the real board rather than a copy of it: this assertion
    held a duplicate of the stage list and went stale the moment the board
    changed, which is the failure it exists to catch."""
    from app.models import PIPELINE_STAGES

    for stage in analyzer.SALES_STAGES:
        assert analyzer.STAGE_TO_PIPELINE[stage] in PIPELINE_STAGES


@pytest.mark.asyncio
async def test_analyzer_falls_back_when_the_provider_fails(monkeypatch):
    async def boom(prompt):
        raise RuntimeError("provider down")

    monkeypatch.setattr(analyzer, "_call_groq", boom)
    result = await analyzer.analyse([], "show me red suits")

    assert result["source"] == "heuristic"
    assert result["wants_images"] is True


@pytest.mark.asyncio
async def test_analyzer_parses_a_good_response(monkeypatch):
    async def fake(prompt):
        return '{"intent":"price_question","stage":"QUALIFIED","wants_images":false,' \
               '"colour_preference":"blue","objection":"none","next_action":"answer_question"}'

    monkeypatch.setattr(analyzer, "_call_groq", fake)
    result = await analyzer.analyse([], "how much is the blue one?")

    assert result["intent"] == "price_question"
    assert result["colour_preference"] == "blue"
    assert result["source"] == "llm"


@pytest.mark.asyncio
async def test_keyword_pass_can_still_force_images_on(monkeypatch):
    """A missed picture request is very visible, so the keyword pass wins."""

    async def fake(prompt):
        return '{"intent":"other","stage":"DISCOVERY","wants_images":false,"objection":"none"}'

    monkeypatch.setattr(analyzer, "_call_groq", fake)
    result = await analyzer.analyse([], "show me pictures please")

    assert result["wants_images"] is True


# =============================== directives ================================
def test_directives_name_the_stage_and_action():
    block = sales_policy.as_prompt_block(
        {"stage": "OBJECTION", "next_action": "handle_objection", "objection": "price"}
    )

    assert "OBJECTION" in block
    assert "price objection" in block


def test_directives_mention_attached_photos_when_images_were_requested():
    block = sales_policy.as_prompt_block({"stage": "PRESENTATION", "wants_images": True})

    assert "photos" in block.lower()
