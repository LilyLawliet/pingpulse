"""Step 1 of the pipeline: understand before answering.

The model is asked for a small, strictly-shaped JSON object — intent, stage,
objection, colours, budget, whether they want pictures — and nothing else. That
result then drives retrieval and the sales policy, so the response step is told
what to do rather than deciding for itself.

Every field is optional and every failure degrades to a deterministic
keyword-based reading, so a provider outage costs accuracy, never a reply.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Sequence

from app.services.llm_service import _call_groq, format_history

logger = logging.getLogger(__name__)

# The PDF's state machine. `pipeline_stage` on the CRM is a rollup of this.
SALES_STAGES = (
    "NEW",
    "DISCOVERY",
    "QUALIFIED",
    "PRESENTATION",
    "OBJECTION",
    "NEGOTIATION",
    "READY_TO_BUY",
    "CLOSED",
)

# How the agent's own state machine rolls up to the operator's CRM board.
STAGE_TO_PIPELINE = {
    "NEW": "LEAD",
    "DISCOVERY": "LEAD",
    "QUALIFIED": "QUALIFIED",
    "PRESENTATION": "QUALIFIED",
    "OBJECTION": "QUALIFIED",
    "NEGOTIATION": "DEMO_BOOKED",
    "READY_TO_BUY": "DEMO_BOOKED",
    "CLOSED": "CLOSED",
}

OBJECTION_TYPES = ("price", "quality", "delivery", "trust", "timing", "none")

COLOUR_WORDS = (
    "red", "blue", "green", "black", "white", "pink", "yellow", "purple",
    "orange", "brown", "grey", "gray", "navy", "maroon", "beige", "cream",
    "gold", "silver", "teal", "olive", "peach", "lilac", "mustard", "rust",
)

IMAGE_REQUEST_MARKERS = (
    "picture", "pictures", "pic", "pics", "photo", "photos", "image", "images",
    "show me", "dikhao", "dikhayen", "send me", "catalogue", "catalog",
    "design", "designs", "look like", "see it", "koi aur",
)

REJECTION_MARKERS = (
    "don't like", "dont like", "do not like", "not a fan", "hate",
    "don't want", "dont want", "do not want", "no thanks", "not interested in",
    "pasand nahi", "nahi chahiye", "nai chahiye", "acha nahi", "something else",
    "anything else", "other than", "not this", "not that", "instead of",
)

MEETING_MARKERS = (
    "call", "meeting", "demo", "schedule", "appointment", "book a time",
    "talk to someone", "speak to", "consultation", "discuss", "zoom",
    "wholesale", "bulk order", "b2b", "partnership", "reseller",
)

PRICE_MARKERS = ("price", "cost", "how much", "kitne", "kitna", "rate", "budget")
DELIVERY_MARKERS = ("deliver", "delivery", "shipping", "ship", "courier", "days", "arrive")
PAYMENT_MARKERS = ("payment", "pay", "cod", "cash on delivery", "card", "tabby", "installment")
BUY_MARKERS = ("order", "buy", "purchase", "i'll take", "confirm", "checkout")

ANALYZER_PROMPT = """You are the analysis step of a sales agent. Read the conversation and
return STRICT JSON describing the customer's latest message. Do not write anything else.

Return exactly these keys:
{{
  "intent": one of ["greeting","product_question","price_question","delivery_question",
                    "payment_question","image_request","objection","purchase","smalltalk","other"],
  "stage": one of ["NEW","DISCOVERY","QUALIFIED","PRESENTATION","OBJECTION","NEGOTIATION","READY_TO_BUY","CLOSED"],
  "wants_images": true or false,
  "colour_preference": a colour they asked for, or null,
  "category_interest": what kind of product they want (e.g. "unstitched lawn"), or null,
  "fabric_preference": fabric named, or null,
  "budget": a number or range they stated, or null,
  "size": a size they stated, or null,
  "city": a city they stated, or null,
  "objection": one of ["price","quality","delivery","trust","timing","none"],
  "objection_text": their words for the objection, or null,
  "new_requirements": list of things they now want,
  "dropped_requirements": list of things they explicitly no longer want,
  "commitments": list of things they committed to,
  "rejected_items": list of things they said they do NOT want — colours, fabrics,
                    styles or specific products ("I don't like blue" -> ["blue"]),
  "wants_meeting": true if they asked for a call, meeting, demo or to speak to someone,
  "next_action": one of ["answer_question","show_products","handle_objection",
                         "qualify","confirm_order","greet"]
}}

Rules:
- Report ONLY what the customer actually said. Never guess. Use null when unsure.
- "dropped_requirements" is only for explicit reversals ("not red, I want blue").
- "rejected_items" is for dislikes and refusals: "I don't like blue", "not silk",
  "too flashy", "not this one". Record the attribute, not the whole sentence.
- Return raw JSON with no code fences.

CONVERSATION SO FAR:
{history}

LATEST CUSTOMER MESSAGE:
{message}
"""


def _contains(text: str, markers: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def heuristic_analysis(message: str, current_stage: str = "NEW") -> dict[str, Any]:
    """Deterministic reading used as the fallback and as a safety net.

    Cheap, predictable, and good enough to keep the agent behaving sensibly
    when the analyzer call fails.
    """
    text = (message or "").lower()

    colour = next((c for c in COLOUR_WORDS if re.search(rf"\b{c}\b", text)), None)
    wants_images = _contains(text, IMAGE_REQUEST_MARKERS)

    # A dislike names a colour they do NOT want. Recording that as a colour
    # preference would be exactly backwards, so the rejection wins.
    rejecting = _contains(text, REJECTION_MARKERS)
    rejected = [colour] if (rejecting and colour) else []
    if rejecting:
        colour = None

    if _contains(text, PAYMENT_MARKERS):
        intent, action = "payment_question", "answer_question"
    elif _contains(text, DELIVERY_MARKERS):
        intent, action = "delivery_question", "answer_question"
    elif _contains(text, BUY_MARKERS):
        intent, action = "purchase", "confirm_order"
    elif _contains(text, PRICE_MARKERS):
        intent, action = "price_question", "answer_question"
    elif wants_images:
        intent, action = "image_request", "show_products"
    else:
        intent, action = "product_question", "answer_question"

    stage = current_stage if current_stage in SALES_STAGES else "NEW"
    if intent == "purchase":
        stage = "READY_TO_BUY"
    elif intent in ("price_question", "delivery_question", "payment_question"):
        stage = "QUALIFIED" if stage in ("NEW", "DISCOVERY") else stage
    elif stage == "NEW":
        stage = "DISCOVERY"

    return {
        "intent": intent,
        "stage": stage,
        "wants_images": wants_images,
        "colour_preference": colour,
        "category_interest": None,
        "fabric_preference": None,
        "budget": None,
        "size": None,
        "city": None,
        "objection": "none",
        "objection_text": None,
        "new_requirements": [],
        "dropped_requirements": [],
        "commitments": [],
        "rejected_items": rejected,
        "wants_meeting": _contains(text, MEETING_MARKERS),
        "next_action": action,
        "source": "heuristic",
    }


def _coerce(raw: dict[str, Any], message: str, current_stage: str) -> dict[str, Any]:
    """Force the model's output into the shape the rest of the code expects."""
    fallback = heuristic_analysis(message, current_stage)

    def text_or_none(key: str) -> str | None:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            value = str(value)
        if isinstance(value, str):
            value = value.strip()
            if value and value.lower() not in ("null", "none", "n/a", "unknown"):
                return value[:120]
        return None

    def string_list(key: str) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, list):
            return []
        return [str(v).strip()[:160] for v in value if str(v).strip()][:6]

    stage = raw.get("stage")
    stage = stage if stage in SALES_STAGES else fallback["stage"]
    # The keyword reading is a floor, never a ceiling: a price question is
    # qualification even when the analyzer calls it discovery.
    stage = advance_stage(fallback["stage"], stage)

    objection = raw.get("objection")
    objection = objection if objection in OBJECTION_TYPES else "none"

    # The keyword pass is authoritative for "did they ask for pictures" — it is
    # the trigger for sending media, and a missed request is very visible.
    wants_images = bool(raw.get("wants_images")) or fallback["wants_images"]

    return {
        "intent": raw.get("intent") or fallback["intent"],
        "stage": stage,
        "wants_images": wants_images,
        "colour_preference": text_or_none("colour_preference") or fallback["colour_preference"],
        "category_interest": text_or_none("category_interest"),
        "fabric_preference": text_or_none("fabric_preference"),
        "budget": text_or_none("budget"),
        "size": text_or_none("size"),
        "city": text_or_none("city"),
        "objection": objection,
        "objection_text": text_or_none("objection_text"),
        "new_requirements": string_list("new_requirements"),
        "dropped_requirements": string_list("dropped_requirements"),
        "commitments": string_list("commitments"),
        # Union with the keyword pass: a missed rejection means re-offering
        # something the customer just turned down, which is very visible.
        "rejected_items": list(
            dict.fromkeys(string_list("rejected_items") + fallback["rejected_items"])
        ),
        "wants_meeting": bool(raw.get("wants_meeting")) or fallback["wants_meeting"],
        "next_action": raw.get("next_action") or fallback["next_action"],
        "source": "llm",
    }


async def analyse(
    history: Sequence[Any], message: str, current_stage: str = "NEW"
) -> dict[str, Any]:
    """Understand one customer message. Always returns a usable analysis."""
    prompt = ANALYZER_PROMPT.format(history=format_history(history), message=message)

    try:
        raw_text = await _call_groq(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyzer call failed (%s); using the keyword reading", exc)
        return heuristic_analysis(message, current_stage)

    text = raw_text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        logger.warning("analyzer returned non-JSON; using the keyword reading")
        return heuristic_analysis(message, current_stage)

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("analyzer JSON did not parse; using the keyword reading")
        return heuristic_analysis(message, current_stage)

    if not isinstance(parsed, dict):
        return heuristic_analysis(message, current_stage)
    return _coerce(parsed, message, current_stage)


def advance_stage(current: str, proposed: str) -> str:
    """Stages move forward, or stay put — never backwards.

    A customer asking a casual question after agreeing to buy has not become
    a cold lead again.
    """
    if current not in SALES_STAGES:
        return proposed if proposed in SALES_STAGES else "NEW"
    if proposed not in SALES_STAGES:
        return current
    return max(current, proposed, key=SALES_STAGES.index)
