"""The explicit sales policy, the stage machine's directives, and the
no-handoff guarantee.

Two things live here that the model is not trusted to decide on its own:

* the standing rules every reply must obey, and
* what to send when both providers are down — which must still be a real
  answer, never "our team will get back to you".
"""

from __future__ import annotations

import re
from typing import Any

# Phrases that hand the customer off to a human who is not coming. The agent
# IS the team; promising a callback is the failure this system exists to fix.
BANNED_PHRASES = (
    "get back to you",
    "team will contact",
    "team will reach",
    "we'll contact you",
    "we will contact you",
    "someone will contact",
    "someone will get in touch",
    "get in touch with you shortly",
    "our team will",
    "a representative will",
    "an agent will",
    "we'll be in touch",
    "we will be in touch",
    "please wait while we",
    "forwarding your query",
    "forwarded to our team",
    "connect you with",
    "transfer you to",
)

BANNED_PATTERN = re.compile("|".join(re.escape(p) for p in BANNED_PHRASES), re.IGNORECASE)


def contains_handoff(text: str) -> str | None:
    """Return the offending phrase, or None. Used to reject a reply."""
    match = BANNED_PATTERN.search(text or "")
    return match.group(0) if match else None


SALES_POLICY = """SALES POLICY (these override everything else):
1. Never invent a product, capability, price, discount or delivery promise. If it is not in
   the business rules or the knowledge below, you do not have it.
2. Answer the customer's actual question FIRST, before selling anything else.
3. Never ask for information the customer has already given you.
4. Never repeat a question, and never repeat information you have already sent.
5. Keep every requirement they have stated, unless they explicitly withdraw it.
6. If they raise an objection, address that objection before continuing to sell.
7. If you genuinely do not have a piece of information, say so plainly and offer the
   closest thing you DO have.
8. NEVER say a human will follow up. Do not say "our team will get back to you", "we'll
   contact you shortly", "a representative will call", or anything similar. You are the
   shop. You answer now, with what you have.
9. Never pressure the customer, and never promise something the business cannot fulfil.
10. If they are ready to buy, move to the next concrete step (confirm item, size, address,
   payment method).
11. Be direct. One to three sentences, maximum. No preamble, no restating their question
   back to them, no filler like "great question" or "I would be happy to help".
12. If they asked a specific question, the FIRST sentence answers it. Only then may you
   add one short line moving things forward."""


STAGE_DIRECTIVES = {
    "NEW": "First contact. Greet once, warmly, and find out what they are shopping for.",
    "DISCOVERY": "Find out what they want — item, colour, occasion, budget — one question at a time.",
    "QUALIFIED": "They have told you what they want. Give specifics: product, price, availability.",
    "PRESENTATION": "Present the matching items with names and exact prices, and offer pictures.",
    "OBJECTION": "Address their objection directly and honestly before selling anything further.",
    "NEGOTIATION": "Work out the practical details — sizes, quantity, delivery, payment method.",
    "READY_TO_BUY": "They want to order. Confirm the item, then collect size, address and payment method.",
    "CLOSED": "The order is agreed. Confirm what happens next and thank them.",
}

ACTION_DIRECTIVES = {
    "answer_question": "Answer their question directly, using the facts below.",
    "show_products": "Show matching products by name with exact prices. Pictures are attached automatically — refer to them naturally ('here are a few').",
    "handle_objection": "Acknowledge the objection, respond with real information, and do not discount unless the business rules allow it.",
    "qualify": "Ask the single most useful question to narrow down what they want.",
    "confirm_order": "Confirm the exact item and price, then ask for size, delivery address and payment method.",
    "book_call": (
        "They asked to book a call. Send the booking link from the BOOKING section "
        "verbatim and ask which time suits them. Nothing else."
    ),
    "greet": "Greet them once and ask what they are looking for.",
}


def directives(analysis: dict[str, Any]) -> list[str]:
    """Turn the analyzer's reading into instructions for the response step.

    A booking request short-circuits everything. Once someone has asked for a
    call, stage directives and product directives only compete with the link
    for the model's attention, and the failure mode that produced this rule was
    an agent answering "can we book a call?" with store policy and an FAQ.
    """
    lines: list[str] = []

    if analysis.get("intent") == "book_call" or analysis.get("next_action") == "book_call":
        return [
            ACTION_DIRECTIVES["book_call"],
            "Do NOT list products, prices, delivery terms, payment terms, store policies "
            "or FAQs on this turn. The only job is the booking link and the time slot.",
        ]

    stage = analysis.get("stage", "NEW")
    if stage in STAGE_DIRECTIVES:
        lines.append(f"Sales stage is {stage}. {STAGE_DIRECTIVES[stage]}")

    action = analysis.get("next_action")
    if action in ACTION_DIRECTIVES:
        lines.append(f"Required next action: {ACTION_DIRECTIVES[action]}")

    objection = analysis.get("objection")
    if objection and objection != "none":
        lines.append(
            f"They have raised a {objection} objection — deal with it before anything else."
        )

    if analysis.get("wants_images"):
        lines.append(
            "They asked to see products. Product photos are being attached to this reply, "
            "so introduce them briefly rather than describing every detail."
        )
    elif analysis.get("intent") in ("product_question", "price_question", "purchase"):
        lines.append(
            "Product details are listed below for reference. No photos are attached this "
            "time, so do not say you are sending any — offer to send them instead."
        )

    return lines


def as_prompt_block(analysis: dict[str, Any]) -> str:
    lines = directives(analysis)
    if not lines:
        return ""
    return "=== WHAT TO DO ON THIS TURN ===\n" + "\n".join(f"- {line}" for line in lines)


# --------------------------------------------------------------------------
# Last resort
# --------------------------------------------------------------------------
def deterministic_reply(
    analysis: dict[str, Any],
    knowledge_chunks: list[Any],
    organization: Any = None,
    products: list[Any] | None = None,
    booking_url: str | None = None,
) -> str:
    """What to send when both providers are down.

    Still a real answer: matched products are listed by name and price, or the
    retrieved fact is read out. Only if nothing at all was found do we ask them
    to say more — and even then, without promising a human.
    """
    # Booking first, and before anything is retrieved. A provider outage is no
    # reason to answer "can we book a call?" with a catalogue — this path once
    # replied to a booking request with "tell me the item and colour", which is
    # the exact failure the book_call intent exists to prevent.
    if analysis.get("intent") == "book_call" or analysis.get("next_action") == "book_call":
        if booking_url:
            return f"You can book a time here: {booking_url}\n\nWhat time suits you best?"
        return (
            "Happy to set up a call. What day and time suit you, and I'll get it in "
            "the diary?"
        )

    # A product question is answered with products, not with whatever policy
    # happened to score highest.
    if products:
        lines = []
        for product in products[:3]:
            attributes = getattr(product, "attributes", None) or {}
            price = attributes.get("price")
            currency = attributes.get(
                "currency", getattr(organization, "default_currency", None) or ""
            )
            title = getattr(product, "title", "")
            lines.append(f"{title} — {currency} {price}" if price else title)
        listing = "\n".join(lines)
        return (
            f"Here's what we have:\n{listing}\n\n"
            "Would you like me to reserve one, or show you something else?"
        )

    if knowledge_chunks:
        best = knowledge_chunks[0]
        body = getattr(best, "content", "") or ""
        # Keep it to a WhatsApp-sized answer.
        sentences = re.split(r"(?<=[.!?])\s+", body.strip())
        answer = " ".join(sentences[:3]).strip()
        if answer:
            return f"{answer}\n\nAnything else you'd like to know?"

    name = getattr(organization, "name", None) or "us"
    intent = analysis.get("intent", "")
    # Deliberately industry-neutral: the same agent serves an electronics shop
    # and a B2B SaaS company, and "what outfit are you after" is nonsense to one.
    if intent == "image_request":
        return (
            f"Tell me a bit more about what you're after and I'll pull up what "
            f"{name} has right now."
        )
    return (
        "Could you tell me a little more about what you're looking for, and I'll "
        "check exactly what we have?"
    )
