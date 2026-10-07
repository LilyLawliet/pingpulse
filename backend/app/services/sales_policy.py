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

from app.services import offers as offers_text

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


def directives(
    analysis: dict[str, Any],
    photos_available: bool = True,
    photos_attached: bool | None = None,
    about_booking: bool = True,
) -> list[str]:
    """Turn the analyzer's reading into instructions for the response step.

    A booking request short-circuits everything. Once someone has asked for a
    call, stage directives and product directives only compete with the link
    for the model's attention, and the failure mode that produced this rule was
    an agent answering "can we book a call?" with store policy and an FAQ.
    """
    lines: list[str] = []

    # Only while this message is about booking. The analyzer reads the whole
    # conversation, so once a demo was on the table every later question -
    # "what do you sell?", "show me pictures" - was read as booking too, and
    # answered with "we'll walk you through it at your meeting".
    wants_call = analysis.get("intent") == "book_call" or analysis.get("next_action") == "book_call"
    if wants_call and about_booking:
        return [
            ACTION_DIRECTIVES["book_call"],
            "Do NOT list products, prices, delivery terms, payment terms, store policies "
            "or FAQs on this turn. The only job is the booking link and the time slot.",
        ]

    stage = analysis.get("stage", "NEW")
    if stage in STAGE_DIRECTIVES:
        directive = STAGE_DIRECTIVES[stage]
        if not photos_available:
            directive = directive.replace(", and offer pictures", "")
        lines.append(f"Sales stage is {stage}. {directive}")

    action = analysis.get("next_action")
    if action in ACTION_DIRECTIVES:
        lines.append(f"Required next action: {ACTION_DIRECTIVES[action]}")

    objection = analysis.get("objection")
    if objection and objection != "none":
        lines.append(
            f"They have raised a {objection} objection — deal with it before anything else."
        )

    if analysis.get("wants_images"):
        if photos_attached or (photos_attached is None and photos_available):
            lines.append(
                "They asked to see products. Product photos are being attached to this reply, "
                "so introduce them briefly rather than describing every detail."
            )
        else:
            # What used to happen: "photos are being attached" was said with
            # none to attach, and the agent covered for it by deferring to a
            # meeting. The honest answer is short and still useful.
            lines.append(
                "They asked for pictures. "
                + (
                    "None match what they asked for. "
                    if photos_available
                    else "This business has no product photos. "
                )
                + "Say so in one short sentence, then answer what they wanted to see: "
                "name the products (and prices) from the details below. Never say a "
                "picture is attached, and never put it off to a call or meeting."
            )
    elif analysis.get("intent") in ("product_question", "price_question", "purchase"):
        lines.append(
            "Product details are listed below for reference. No photos are attached this "
            "time, so do not say you are sending any — offer to send them instead."
            if photos_available
            else "This business has no product photos. Never offer to send pictures, and "
            "never say one is attached."
        )

    return lines


def as_prompt_block(
    analysis: dict[str, Any],
    photos_available: bool = True,
    photos_attached: bool | None = None,
    about_booking: bool = True,
) -> str:
    lines = directives(analysis, photos_available, photos_attached, about_booking)
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
    message: str = "",
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

    # A greeting is answered with a greeting. With no model to phrase one,
    # this used to read out whichever passage scored highest - for "Hello",
    # half a returns policy starting in the middle of a word.
    if _only_greeting(message):
        name = getattr(organization, "name", None)
        return (
            f"Hello, and welcome{' to ' + name if name else ''}. What can I help you with today?"
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

    # Only where they asked something. A customer supplying their address or
    # their phone number is not asking a question, and reading out whichever
    # passage scored highest answered "the address is 1200 Brickell Ave,
    # Miami" with the company's own address in Dania Beach, and "my phone is
    # ..." with it too. Searching a message that was never a question finds
    # something every time, and it is never the reply.
    # Them handing over an address or a number is not a question, and it is
    # not something to alert a colleague about either. Blocking the document
    # search without answering sent "the address is 1200 Brickell Ave" to
    # "I don't have that to hand, I've passed it to the team".
    # They named work this business does. Say yes to it before anything else
    # here looks at the message, because a customer rarely names a job on its
    # own: "I want a kitchen remodel at 1200 Brickell Ave, Miami" is a job
    # and an address in one sentence, and it came back "Thanks, I've got
    # that" - an answer to the address, leaving the thing they actually asked
    # unanswered. A yes is one of the three answers a customer can be given
    # and this is where it is given with no model to phrase it.
    matched = _service_they_named(organization, message)
    if matched:
        return (
            f"Yes - we do {matched}. Tell me a bit more about what you need and "
            "I can get you booked in for a free consultation."
        )

    if _is_them_giving_details(message):
        return "Thanks, I've got that. What else can I help you with?"

    if knowledge_chunks:
        answer = relevant_sentences(message, knowledge_chunks)
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


def _service_they_named(organization, message: str) -> str | None:
    """A service of this business's own that the message asks for.

    Read off the message against the business's own list, with no model
    involved, so a customer who says what they want hears yes whether or not
    a model answered. Only where they are asking for it: "the first one
    please" during a booking for a kitchen remodel names no new work, and
    neither does a customer working through times.
    """
    from app.services import booking, scope

    try:
        if not booking.asks_for_work(message or ""):
            return None
        return scope.matched_service(organization, message or "")
    except Exception:  # noqa: BLE001 - a reply never depends on this working
        return None


def _is_them_giving_details(message: str) -> bool:
    """They are handing over a phone number, an email or an address.

    Searching the documents for a message like that finds something every
    time and it is never the reply: "the address is 1200 Brickell Ave,
    Miami" came back with the company's own address in Dania Beach, and so
    did "my name is Ali, phone ..., email ...". A statement that is really a
    question about terms - "I'll pay everything after delivery" - is not
    this, and is still answered from the documents.
    """
    from app.services import booking

    try:
        if booking.contact_details_in(message or ""):
            return True
        return bool(booking.address_in(message or "") or booking.place_in(message or ""))
    except Exception:  # noqa: BLE001 - a reply never depends on this working
        return False


_FILLER = (
    "Could you tell me a little more about what you're looking for",
    "Tell me a bit more about what you're after",
)


def without_filler(reply: str) -> str:
    """The last resort, unless all it does is ask them to say more.

    Asking a customer to rephrase a question nothing could answer only makes
    them wait twice. Empty means "nothing was known": the caller alerts a
    person and says so.
    """
    return "" if any((reply or "").startswith(f) for f in _FILLER) else reply


_COMMON = {
    "the", "and", "for", "you", "your", "can", "what", "how", "much", "this", "that", "with",
    "have", "need", "want", "give", "will", "would", "about", "are", "did", "does", "any",
    "all", "but", "not", "then", "them", "they", "its", "our", "from", "just", "also",
    "please", "tell", "make", "anything", "everything", "was", "were", "only", "today",
    "now", "after", "before", "into", "over", "than", "when", "been", "some", "more", "i'll",
    "i'm", "don't", "it's", "fine", "okay", "yes", "sure", "thanks",
    # Words every passage shares with every question: "What items do you
    # have?" was answered with the returns policy because it says "items".
    "item", "items", "thing", "things", "product", "products", "stuff", "you",
    "your", "which", "who", "why", "where", "there", "their", "have", "has", "had",
}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z']{3,}", (text or "").lower()) if w not in _COMMON}


_TO_THE_AGENT = re.compile(
    r"\bthe agent\b|\bagents?\s+(?:must|should|can|may)\b|^(?:never|do not|don'?t|only quote|quote only)\b",
    re.IGNORECASE,
)


def relevant_sentences(message: str, knowledge_chunks: list[Any], limit: int = 2) -> str:
    """The sentences of the retrieved passages that are about this message.

    The top passage read out from its first sentence answered "I'll pay after
    delivery" with the warranty terms, and a passage holding the price table
    with the whole table. Only sentences sharing a word with what the customer
    wrote are used, never a table row, and nothing at all is better than
    something unrelated.
    """
    from app.services import taught

    asked = _stems(message)
    if not asked:
        return ""

    def overlap(text: str) -> int:
        words = _stems(text)
        return sum(1 for stem in asked if any(word.startswith(stem) for word in words))

    scored: list[tuple[int, int, str]] = []
    order = 0
    for chunk in knowledge_chunks:
        content = getattr(chunk, "content", "") or ""
        # An answer somebody at the shop typed, against the question it was
        # taught for. The question is what the customer's words match - "how
        # much will the kitchen cost me?" shares nothing with "we don't
        # publish fixed prices" - and the answer is the only half to read
        # out. Both were being read out: a customer asking exactly that got
        # "When a customer asks: How much will my project cost? ...".
        question, answer = taught.parts(content)
        if question is not None:
            hits = overlap(question) + overlap(answer)
            if hits:
                scored.append((hits, -order, " ".join(answer.split())))
            order += 1
            continue

        # `answer` rather than `content`: a chunk holding only the second half
        # of a taught pair has no question to be found by, but its marker
        # still has to come off before anything in it is read out.
        body = _from_a_sentence_start(answer)
        for sentence in re.split(offers_text.SENTENCE_END + r"|\n+", body):
            sentence = sentence.strip()
            if len(sentence.split()) < 5 or "|" in sentence or not sentence[:1].isalnum():
                continue
            # Written to the agent, not to the customer: "Outside business
            # hours, the agent can still answer questions..." was read out
            # word for word as a reply.
            if _TO_THE_AGENT.search(sentence):
                continue
            hits = sum(1 for stem in asked if any(s.startswith(stem) for s in _stems(sentence)))
            if hits:
                scored.append((hits, -order, sentence))
            order += 1
    if not scored:
        return ""
    chosen = sorted(scored, reverse=True)[:limit]
    # Back in the order they were written.
    chosen.sort(key=lambda row: -row[1])
    return " ".join(sentence for _, _, sentence in chosen)


_GREETING = re.compile(
    r"^\s*(?:hi+|hello+|hey+|hiya|salam|salaam|assalam(?:u|o)?\s*o?\s*alaikum|aoa|"
    r"good\s+(?:morning|afternoon|evening|day)|greetings|yo|namaste|marhaba)"
    r"(?:\s+(?:there|team|all|everyone|sir|madam|bro))?[\s!.,?]*$",
    re.IGNORECASE,
)


def only_greeting(message: str) -> bool:
    """Whether the message says hello and nothing else."""
    return bool(message) and bool(_GREETING.match(message))


_only_greeting = only_greeting


# "Ok", "thanks", "great", "bye", "👍" - a customer acknowledging, not asking.
# These have no answer to look up, so they are never "a question the agent
# could not answer": "Ok" used to get "That's a good question, and I don't
# have the answer to hand. I've passed it to the team."
_ACK_WORD = (
    r"(?:ok(?:ay)?|k+|okie|alright|all right|fine|cool|great|nice|perfect|sure|noted|got it|"
    r"understood|thanks?|thank you|thx|ty|tysm|cheers|appreciate it|bye|goodbye|see you|"
    r"good night|gn|no worries|np|hmm+|ah+|oh+|yes|yeah|yep|no|nope|nah|haan|han|ji|"
    r"theek(?: hai)?|thik(?: hai)?|acha|achha|shukriya|shukria|jazakallah|jazak allah|"
    r"khuda hafiz|allah hafiz|gracias|merci|danke|shukran|so much|a lot|very much|bro|sir|"
    r"madam|dear|then|again|you|u)"
)
_ACKNOWLEDGING = re.compile(
    rf"^[\s\W]*{_ACK_WORD}(?:[\s,.!]+{_ACK_WORD})*[\s\W]*$", re.IGNORECASE
)
_ONLY_SYMBOLS = re.compile(r"^[\s\W\d]*$")


def just_acknowledging(message: str) -> bool:
    """Whether the message is an acknowledgement or a sign-off, with nothing to answer."""
    text = (message or "").strip()
    if not text or "?" in text:
        return False
    return bool(_ACKNOWLEDGING.match(text)) or (len(text) <= 8 and bool(_ONLY_SYMBOLS.match(text)))


def acknowledgement_reply(message: str) -> str:
    """A short, warm reply to an acknowledgement. Never a promise, never a question."""
    lowered = (message or "").lower()
    if re.search(r"\b(bye|goodbye|good night|gn|khuda hafiz|allah hafiz|see you|no|nope|nah)\b", lowered):
        return "Take care! Message us any time."
    if re.search(r"thank|thx|\bty\b|tysm|cheers|shukri|jazak|gracias|merci|danke|shukran", lowered):
        return "You're welcome! Anything else I can help with?"
    return "Great! Anything else I can help with?"


def _from_a_sentence_start(body: str) -> str:
    """Drop the tail of a sentence a passage begins inside.

    Passages overlap so that a fact on a boundary can be found from either
    side, which means one can start part-way through a word.
    """
    text = body.strip()
    if text and not (text[0].isupper() or text[0].isdigit()):
        cut = re.search(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z0-9])", text)
        if cut:
            text = text[cut.end():].strip()
    return text
