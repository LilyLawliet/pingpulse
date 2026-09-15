"""Vision, rejection memory, Roman Urdu, scheduling and follow-up queuing."""

from types import SimpleNamespace

import pytest

from app.services import customer_memory, llm_service, scheduling, vision
from app.services.analyzer import heuristic_analysis
from app.services.llm_service import ROMAN_URDU_GUIDE, build_prompt, is_roman_urdu

ORG = SimpleNamespace(
    name="Nishat Linen",
    sales_prompt="Sell clothes.",
    target_tone="Warm",
    product_rules="Basic Shirt PKR 1596.",
    default_currency="PKR",
    default_language="en",
)


def contact(**overrides):
    base = dict(
        name="Ayesha",
        phone_number="+923001234567",
        pipeline_stage="LEAD",
        sales_stage="QUALIFIED",
        city=None,
        shoe_size=None,
        category_interest=None,
        colour_preference=None,
        budget_note=None,
        contact_metadata={},
        organization_id="org-1",
        id="contact-1",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ================================ vision ==================================
def test_vision_block_tells_the_agent_what_it_can_see():
    block = vision.as_prompt_block(
        {"colour": "red", "pattern": "printed", "category": "kurta",
         "description": "A red printed kurta."}
    )

    assert "red" in block and "printed" in block and "kurta" in block
    assert "Do NOT ask them what colour" in block


def test_no_analysis_adds_no_block():
    assert vision.as_prompt_block({}) == ""


def test_vision_result_becomes_search_terms():
    terms = vision.search_terms(
        {"colour": "red", "pattern": "printed", "fabric": "lawn", "category": "suit"}
    )

    assert "red" in terms and "printed" in terms and "lawn" in terms


def test_vision_parse_survives_code_fences():
    parsed = vision._parse('```json\n{"colour": "red", "category": "kurta"}\n```')

    assert parsed == {"colour": "red", "category": "kurta"}


def test_vision_parse_drops_nulls_and_placeholders():
    parsed = vision._parse('{"colour": "red", "fabric": "null", "pattern": "unknown"}')

    assert parsed == {"colour": "red"}


def test_vision_parse_of_garbage_is_empty():
    assert vision._parse("I could not see the image") == {}


@pytest.mark.asyncio
async def test_vision_disabled_returns_nothing(monkeypatch):
    monkeypatch.setattr(llm_service.settings, "vision_enabled", False)
    from app.services.vision import analyse_image  # bound to the real function

    assert await analyse_image("https://example.com/a.jpg") == {}


# =========================== rejection memory =============================
def test_a_dislike_is_recorded_as_a_rejection_not_a_preference():
    result = heuristic_analysis("I don't like blue")

    assert result["rejected_items"] == ["blue"]
    assert result["colour_preference"] is None


def test_a_plain_colour_request_is_still_a_preference():
    result = heuristic_analysis("show me blue suits")

    assert result["colour_preference"] == "blue"
    assert result["rejected_items"] == []


def test_roman_urdu_dislike_is_caught():
    assert heuristic_analysis("blue pasand nahi")["rejected_items"] == ["blue"]


def test_rejections_are_deduplicated_case_insensitively():
    memory = customer_memory.reject_item({}, "Blue")
    memory = customer_memory.reject_item(memory, "blue")

    assert customer_memory.rejected_items(memory) == ["Blue"]


def test_a_rejected_colour_matches_a_product_name():
    memory = customer_memory.reject_item({}, "blue")

    assert customer_memory.is_rejected(memory, "Blue Printed Lawn Suit")
    assert not customer_memory.is_rejected(memory, "Red Printed Lawn Suit")


def test_the_prompt_forbids_re_recommending_rejections():
    memory = customer_memory.reject_item({}, "blue")
    block = customer_memory.as_prompt_block(memory)

    assert "never recommend" in block.lower()
    assert "blue" in block


def test_analysis_folds_rejections_into_memory():
    memory = customer_memory.apply_analysis({}, {"rejected_items": ["blue", "silk"]})

    assert customer_memory.rejected_items(memory) == ["blue", "silk"]


# ============================== Roman Urdu =================================
@pytest.mark.parametrize(
    "message",
    [
        "Iski delivery kitne din mein hogi?",
        "COD hai kya? mujhe order karna hai",
        "Red wala dikhao, kitne ka hai",
    ],
)
def test_roman_urdu_is_detected(message):
    assert is_roman_urdu(message)


@pytest.mark.parametrize(
    "message",
    [
        "How long does delivery take?",
        "Do you accept card payments?",
        "Show me red suits",
    ],
)
def test_plain_english_is_not_flagged(message):
    assert not is_roman_urdu(message)


def test_one_stray_urdu_word_does_not_flip_the_language():
    """Two markers are required, so "hai" inside English does not trigger it."""
    assert not is_roman_urdu("Is this the price hai")


def test_roman_urdu_guidance_reaches_the_prompt():
    prompt = build_prompt(ORG, contact(), [], "Iski delivery kitne din mein hogi?")

    assert "ROMAN URDU" in prompt
    assert "Delivery poore Pakistan mein 5 se 7 working days" in prompt


def test_english_messages_get_no_urdu_guidance():
    prompt = build_prompt(ORG, contact(), [], "How long does delivery take?")

    assert "ROMAN URDU" not in prompt


def test_the_guide_forbids_urdu_script():
    assert "Never reply in Urdu script" in ROMAN_URDU_GUIDE


# =============================== scheduling ================================
def test_b2b_language_is_recognised():
    assert scheduling.looks_like_b2b("we need a bulk order for our store")
    assert scheduling.looks_like_b2b("interested in becoming a reseller")
    assert not scheduling.looks_like_b2b("can I buy one red kurta")


def test_a_booking_link_is_produced():
    link = scheduling.booking_link("Nishat Linen", "Ayesha", "+923001234567")

    assert link and link.startswith("https://")


def test_calcom_is_preferred_when_configured(monkeypatch):
    monkeypatch.setattr(scheduling.settings, "calcom_link", "https://cal.com/nishat/30min")
    link = scheduling.booking_link("Nishat Linen", "Ayesha")

    assert link.startswith("https://cal.com/nishat/30min")
    assert "name=Ayesha" in link


def test_scheduling_off_produces_no_link(monkeypatch):
    monkeypatch.setattr(scheduling.settings, "scheduling_enabled", False)

    assert scheduling.booking_link("Nishat Linen") is None


def test_without_a_link_the_agent_is_told_not_to_promise_a_callback(monkeypatch):
    monkeypatch.setattr(scheduling.settings, "scheduling_enabled", False)
    block = scheduling.as_prompt_block("Nishat Linen")

    assert "Do NOT promise a callback" in block


def test_the_link_block_tells_the_agent_to_send_it_verbatim():
    block = scheduling.as_prompt_block("Nishat Linen", "Ayesha", "+923001234567")

    assert "book a call here" in block
    assert "https://" in block


# ============================== follow-ups =================================
def test_warm_stages_are_the_only_ones_nudged(monkeypatch):
    from app import tasks

    monkeypatch.setattr(tasks.settings, "followups_enabled", True)
    queued = []
    monkeypatch.setattr(
        tasks.schedule_customer_followup,
        "apply_async",
        lambda **kwargs: queued.append(kwargs),
    )

    assert tasks.schedule_followups(contact(sales_stage="QUALIFIED")) is not None
    # Three: at 4, 24 and 72 hours. The third was added with the opt-out work
    # and is the last anybody gets - see MAX_FOLLOWUPS for why there is no
    # fourth.
    assert len(queued) == tasks.MAX_FOLLOWUPS

    queued.clear()
    assert tasks.schedule_followups(contact(sales_stage="NEW")) is None
    assert tasks.schedule_followups(contact(sales_stage="CLOSED")) is None
    assert queued == []


def test_follow_ups_can_be_switched_off(monkeypatch):
    from app import tasks

    monkeypatch.setattr(tasks.settings, "followups_enabled", False)

    assert tasks.schedule_followups(contact(sales_stage="QUALIFIED")) is None


def test_a_broker_outage_does_not_break_the_reply(monkeypatch):
    """Queuing happens in the request the customer is waiting on."""
    from app import tasks

    monkeypatch.setattr(tasks.settings, "followups_enabled", True)

    def explode(**kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(tasks.schedule_customer_followup, "apply_async", explode)

    assert tasks.schedule_followups(contact(sales_stage="QUALIFIED")) is None


def test_each_scheduling_run_gets_its_own_token(monkeypatch):
    from app import tasks

    monkeypatch.setattr(tasks.settings, "followups_enabled", True)
    monkeypatch.setattr(tasks.schedule_customer_followup, "apply_async", lambda **kw: None)

    first = tasks.schedule_followups(contact(sales_stage="QUALIFIED"))
    second = tasks.schedule_followups(contact(sales_stage="QUALIFIED"))

    assert first and second and first != second


def test_an_unreadable_photo_is_still_acknowledged():
    """A vision outage must not produce a reply that ignores the picture."""
    block = vision.as_prompt_block({}, image_received=True)

    assert "received their picture" in block
    assert "never claim you cannot receive images" in block.lower()


def test_no_photo_still_adds_nothing():
    assert vision.as_prompt_block({}, image_received=False) == ""


# ====================== per-tenant Twilio credentials ======================
def test_a_tenant_with_its_own_credentials_is_isolated():
    from app.services.twilio_service import Sender

    channel = SimpleNamespace(
        account_sid="ACtenant", auth_token="tenanttoken", phone_number="+14155550001"
    )
    sender = Sender.for_channel(channel)

    assert sender.account_sid == "ACtenant"
    assert sender.auth_token == "tenanttoken"
    assert sender.whatsapp_from == "whatsapp:+14155550001"


def test_a_tenant_number_on_the_platform_account():
    """Supplying only a number still sends on the platform credentials."""
    from app.services.twilio_service import Sender

    channel = SimpleNamespace(account_sid=None, auth_token=None, phone_number="+14155550002")
    sender = Sender.for_channel(channel)

    assert sender.account_sid == Sender.platform().account_sid
    assert sender.whatsapp_from == "whatsapp:+14155550002"


def test_half_supplied_credentials_fall_back_rather_than_mixing():
    """A tenant SID with the platform token would authenticate as nobody."""
    from app.services.twilio_service import Sender

    channel = SimpleNamespace(account_sid="ACtenant", auth_token=None, phone_number="+1415")
    sender = Sender.for_channel(channel)

    assert sender.account_sid == Sender.platform().account_sid
    assert sender.auth_token == Sender.platform().auth_token


def test_no_channel_uses_the_platform_sender():
    from app.services.twilio_service import Sender

    assert Sender.for_channel(None) == Sender.platform()


def test_the_whatsapp_prefix_is_added_once():
    from app.services.twilio_service import Sender

    already = Sender("AC", "tok", "whatsapp:+1415")
    plain = Sender("AC", "tok", "+1415")

    assert already.whatsapp_from == "whatsapp:+1415"
    assert plain.whatsapp_from == "whatsapp:+1415"


# ===================== local colour extraction (no AI) =====================
def _solid_image(rgb, size=(40, 40)) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, rgb).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "rgb,expected",
    [
        ((220, 20, 20), "red"),
        ((20, 40, 200), "blue"),
        ((20, 140, 40), "green"),
        ((240, 240, 240), ("white", "off white")),
        ((10, 10, 10), "black"),
    ],
)
def test_dominant_colour_is_read_from_pixels(rgb, expected):
    from app.services import colour

    found = colour.dominant_colours(_solid_image(rgb))

    assert found, "a solid image must yield a colour"
    if isinstance(expected, tuple):
        assert found[0] in expected
    else:
        assert found[0] == expected


def test_colour_analysis_is_shaped_like_a_vision_result():
    from app.services import colour

    result = colour.analyse(_solid_image((200, 30, 30)))

    assert result["colour"] == "red"
    assert result["colour_source"] == "pixels"
    assert "description" in result


def test_a_corrupt_image_yields_nothing_rather_than_raising():
    from app.services import colour

    assert colour.analyse(b"not an image") == {}
    assert colour.dominant_colours(b"") == []


def test_a_studio_background_does_not_beat_the_garment():
    """Product shots are mostly white; the garment is still the subject."""
    import io

    from PIL import Image

    from app.services import colour

    canvas = Image.new("RGB", (60, 60), (255, 255, 255))
    for x in range(20, 40):
        for y in range(10, 50):
            canvas.putpixel((x, y), (200, 30, 30))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")

    assert colour.dominant_colours(buffer.getvalue())[0] == "red"


def test_a_pixel_read_is_marked_uncertain():
    """Skin and warm backdrops can outvote a garment, so pixels only hint."""
    from app.services import colour

    result = colour.analyse(_solid_image((200, 30, 30)))

    assert result["confidence"] == "low"
    assert "Possibly" in result["description"]


def test_a_low_confidence_read_asks_the_agent_to_confirm():
    block = vision.as_prompt_block({"colour": "red", "confidence": "low"})

    assert "CONFIRM the colour" in block
    assert "may be wrong" in block


def test_a_vision_read_is_stated_not_questioned():
    block = vision.as_prompt_block({"colour": "red", "category": "kurta"})

    assert "Do NOT ask them what colour" in block
