"""Step 1 of the pipeline: understand before answering.

The model is asked for a small, strictly-shaped JSON object — intent, stage,
objection, colours, budget, whether they want pictures — and nothing else. That
result then drives retrieval and the sales policy, so the response step is told
what to do rather than deciding for itself.

Every field is optional and every failure degrades to a deterministic
keyword-based reading, so a provider outage costs accuracy, never a reply.
"""

from __future__ import annotations

import asyncio

import json
import logging
import re
from typing import Any, Iterable, Sequence

from app.config import settings
from app.services import llm_service
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
    "NEW": "NEW_LEAD",
    "DISCOVERY": "CONTACTED",
    "QUALIFIED": "QUALIFIED",
    "PRESENTATION": "QUALIFIED",
    # Still in play, not lost - an objection is a conversation to answer.
    "OBJECTION": "QUALIFIED",
    "NEGOTIATION": "ESTIMATE_SENT",
    "READY_TO_BUY": "ESTIMATE_SENT",
    "CLOSED": "WON",
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

# An explicit request to get on a call, as opposed to merely mentioning one.
# MEETING_MARKERS is deliberately broad — it only decides whether to put a
# booking link in the prompt — but the `book_call` intent suppresses the
# catalogue entirely, so it needs phrases that can only mean "book me in".
# "What do you call this?" contains "call" and must not qualify.
BOOK_CALL_MARKERS = (
    "book a call", "book a demo", "book a meeting", "book a slot", "book a time",
    "schedule a call", "schedule a demo", "schedule a meeting", "schedule time",
    "set up a call", "set up a demo", "set up a meeting", "setup a call",
    "arrange a call", "arrange a meeting", "hop on a call", "get on a call",
    "jump on a call", "have a call", "quick call", "demo call",
    "consultation call", "talk to sales", "speak to sales", "talk to your team",
    "speak to your team", "talk to someone", "speak to someone",
    "book an appointment", "schedule an appointment",
    "call book", "meeting book", "demo dikha",
)

# "Can we book a call?", "I'd like to schedule a demo" — the verb and the
# noun separated by a few words, which the flat phrase list above misses.
BOOK_CALL_PATTERN = re.compile(
    r"\b(book|schedule|arrange|set\s?up|organis[ez]|have)\b[^.?!]{0,24}"
    r"\b(call|demo|meeting|appointment|consultation)\b",
    re.IGNORECASE,
)


def wants_to_book(text: str) -> bool:
    """Is this an explicit request to get on a call, demo or meeting?"""
    lowered = (text or "").lower()
    return _contains(lowered, BOOK_CALL_MARKERS) or bool(BOOK_CALL_PATTERN.search(lowered))


# "pricing" does not contain "price" as a substring ("prici" + "ng"), and
# "what's the pricing?" is how B2B buyers ask, so it needs its own entry.
PRICE_MARKERS = (
    "price", "pricing", "cost", "how much", "kitne", "kitna", "rate",
    "budget", "quote", "fees", "charges", "per month", "per seat",
)
DELIVERY_MARKERS = ("deliver", "delivery", "shipping", "ship", "courier", "days", "arrive")
PAYMENT_MARKERS = ("payment", "pay", "cod", "cash on delivery", "card", "tabby", "installment")
BUY_MARKERS = ("order", "buy", "purchase", "i'll take", "confirm", "checkout")

ANALYZER_PROMPT = """You are the analysis step of a sales agent. Read the conversation and
return STRICT JSON describing the customer's latest message. Do not write anything else.

Return exactly these keys:
{{
  "intent": one of ["greeting","product_question","price_question","delivery_question",
                    "payment_question","image_request","objection","purchase","book_call",
                    "smalltalk","other"],
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
  "wants_person": true if their latest message asks to be put through to a real person -
                  the team, staff, the owner, a human - in any language or spelling
                  ("put me with team", "insaan se baat karao", "can i talk 2 sm1 real"),
                  else false. Booking a demo or a call is NOT this.
  "appointment": what their latest message asks to do about an appointment:
     {{"action": one of ["book","move","cancel","none"],
       "not_yet": true if they say not to do it yet - to wait, or that they must ask
                  someone first - else false,
       "asks_about_existing": true if they ask about an appointment they say they
                  already have ("is my visit still on?"), else false}}
     Read every "not": "Cancel it, I don't want to reschedule or book anything else"
     -> cancel. "I don't want to cancel, just move it to Friday" -> move. "Don't book
     anything" -> none. Any language: "Cancela mi cita" -> cancel. "book" is a new
     appointment; "move" changes the time of one they have; "none" when they ask for
     neither. Picking a time from ones just offered is "book".
  "next_action": one of ["answer_question","show_products","handle_objection",
                         "qualify","confirm_order","book_call","greet"],
  "meaning": their latest message as one clear, complete question or sentence in
             English - typos and shorthand fixed, any language translated, and words
             like "it", "that one" or "the 2nd" replaced with what they refer to in the
             conversation ("hw mch yrly 4 it" after the Growth plan was discussed ->
             "How much is the Growth plan per year?"). Add nothing they did not say,
             and drop nothing they did: every "not", "don't", "never", "stop", "not yet"
             and condition stays ("DO NOT book it yet" -> "Do not book it yet").
}}

Rules:
- Report ONLY what the customer actually said. Never guess. Use null when unsure.
- "dropped_requirements" is only for explicit reversals ("not red, I want blue").
- "rejected_items" is for dislikes and refusals: "I don't like blue", "not silk",
  "too flashy", "not this one". Record the attribute, not the whole sentence.
- Use intent "book_call" (and next_action "book_call") whenever they ask to book or
  schedule a call, demo, meeting or consultation, or to talk to sales — even if they
  also mention a product or a price in the same message. Booking wins.
- Return raw JSON with no code fences.

CONVERSATION SO FAR:
{history}

LATEST CUSTOMER MESSAGE:
{message}
"""


def _contains(text: str, markers: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


# Where one clause stops and the next begins. "I don't like red, show me blue"
# is a dislike followed by a request, and the comma is what separates them.
CLAUSE_BREAK = re.compile(r"[,.;!?]|\bbut\b|\blekin\b|\bmagar\b|\bhowever\b")

# How far from "don't like" a colour can sit and still be the thing disliked.
REJECTION_REACH = 24


def read_colours(text: str) -> tuple[str | None, list[str]]:
    """Split the colours in a message into the wanted one and the rejected ones.

    "I don't like red, show me blue" names two colours with opposite meanings,
    so position decides which is which. A colour counts as rejected when it sits
    beside a rejection marker inside the same clause - just behind it, as Roman
    Urdu puts it ("red pasand nahi"), or just after it ("don't like red").
    Whatever is left over is what the customer actually wants.

    Reading in message order is the point. Taking the first colour that appears
    in COLOUR_WORDS instead makes the outcome depend on the order of that tuple,
    so "I don't like blue, show me red" would reject red - the very colour that
    was just asked for.
    """
    colours = sorted(
        (m.start(), m.end(), word)
        for word in COLOUR_WORDS
        for m in re.finditer(rf"\b{re.escape(word)}\b", text)
    )
    if not colours:
        return None, []

    markers = [
        (m.start(), m.end())
        for marker in REJECTION_MARKERS
        for m in re.finditer(re.escape(marker), text)
    ]

    def unbroken(left: int, right: int) -> bool:
        return left <= right and not CLAUSE_BREAK.search(text[left:right])

    rejected: list[str] = []
    for marker_start, marker_end in markers:
        behind = [
            c for c in colours
            if c[1] <= marker_start
            and marker_start - c[1] <= REJECTION_REACH
            and unbroken(c[1], marker_start)
        ]
        ahead = [
            c for c in colours
            if c[0] >= marker_end
            and c[0] - marker_end <= REJECTION_REACH
            and unbroken(marker_end, c[0])
        ]
        hit = behind[-1] if behind else (ahead[0] if ahead else None)
        if hit and hit[2] not in rejected:
            rejected.append(hit[2])

    wanted = next((c[2] for c in colours if c[2] not in rejected), None)
    return wanted, rejected


def heuristic_analysis(message: str, current_stage: str = "NEW") -> dict[str, Any]:
    """Deterministic reading used as the fallback and as a safety net.

    Cheap, predictable, and good enough to keep the agent behaving sensibly
    when the analyzer call fails.
    """
    text = (message or "").lower()

    colour, rejected = read_colours(text)
    wants_images = _contains(text, IMAGE_REQUEST_MARKERS)

    # Booking outranks every other reading. Someone asking for a call has
    # stopped browsing, and answering with a product FAQ loses the meeting.
    if wants_to_book(text):
        intent, action = "book_call", "book_call"
    elif _contains(text, PAYMENT_MARKERS):
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
    elif intent == "book_call":
        # Asking for a call is a qualified lead by definition.
        stage = max(stage, "QUALIFIED", key=SALES_STAGES.index)
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
        # Asking to book one is wanting one. MEETING_MARKERS is a looser net
        # cast over mentions of calls, and misses some phrasings that
        # BOOK_CALL_MARKERS catches outright ("talk to sales").
        "wants_meeting": intent == "book_call" or _contains(text, MEETING_MARKERS),
        "next_action": action,
        "meaning": None,
        "source": "heuristic",
    }


def _meaning(value: Any, message: str = "") -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:300]
    if len(text) < 3 or text.lower() in ("null", "none", "n/a"):
        return None
    if not keeps_what_they_ruled_out(message, text):
        # The reply is told to "answer that", so a reading that lost the
        # customer's "DO NOT" would be answered as the opposite request
        # (October 6 retest). Their own words are used instead.
        logger.warning("discarded a reading that dropped a negation: %r -> %r", message[:120], text[:120])
        return None
    return text


# What a customer rules out, in the languages customers here write: English,
# Spanish, Roman Urdu. Counted, not parsed - a reading with fewer of these than
# the message has lost one of them.
_RULED_OUT = re.compile(
    r"\b(not|no|don'?t|dont|doesn'?t|didn'?t|never|nothing|stop|without|cancel|wait|"
    r"hold off|nunca|nada|tampoco|nahi|nahin|nai|mat|na)\b|n't\b",
    re.IGNORECASE,
)


def keeps_what_they_ruled_out(message: str, meaning: str) -> bool:
    """Whether a rewording keeps every "not", "don't", "never", "stop" the customer wrote."""
    # Strict on purpose: a reading discarded costs nothing - the reply works
    # from the customer's own words - and one that lost "not" costs the answer.
    return len(_RULED_OUT.findall(meaning or "")) >= len(_RULED_OUT.findall(message or ""))


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

    # Booking is a floor too, and a hard one. If the customer explicitly asked
    # for a call, no reading by the model may downgrade that to a product or
    # price question — doing so answers with a catalogue and loses the meeting.
    booking = fallback["intent"] == "book_call"
    intent = "book_call" if booking else (raw.get("intent") or fallback["intent"])
    next_action = (
        "book_call" if booking else (raw.get("next_action") or fallback["next_action"])
    )
    # A booking request also means "show me nothing else" — the response step
    # keys off this to suppress the catalogue.
    if booking:
        wants_images = False

    return {
        "intent": intent,
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
        "wants_meeting": (
            booking or bool(raw.get("wants_meeting")) or fallback["wants_meeting"]
        ),
        "next_action": next_action,
        "wants_person": raw.get("wants_person") is True,
        "appointment": _appointment(raw.get("appointment")),
        # A reading used to search the business's documents and to help the
        # reply make sense of a garbled message. Never quoted, never a fact.
        "meaning": _meaning(raw.get("meaning"), message),
        "source": "llm",
    }


APPOINTMENT_ACTIONS = ("book", "move", "cancel", "none")


def _appointment(raw) -> dict | None:
    """The model's reading of what they want done about an appointment, or None.

    None whenever it is not a clean answer, so the booking rules decide
    instead - never a guess filled in here.
    """
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action not in APPOINTMENT_ACTIONS:
        return None
    return {
        "action": action,
        "not_yet": raw.get("not_yet") is True,
        "asks_about_existing": raw.get("asks_about_existing") is True,
    }


async def analyse(
    history: Sequence[Any], message: str, current_stage: str = "NEW"
) -> dict[str, Any]:
    """Understand one customer message. Always returns a usable analysis."""
    prompt = ANALYZER_PROMPT.format(history=format_history(history), message=message)

    try:
        # Read at temperature 0: the same message, the same reading.
        with llm_service.exact():
            raw_text = await asyncio.wait_for(_call_groq(prompt), settings.analyzer_timeout_seconds)
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
