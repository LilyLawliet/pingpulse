"""Dynamic prompt assembly and Groq -> Gemini generation with fallback.

Final Prompt = Organization Prompt + Customer Metadata + Chat History + Latest Message
"""

from __future__ import annotations

import asyncio

import json
import re
import logging
import time
from typing import Any, Iterable, Sequence

import httpx

from app.config import settings
from app.models import SENDER_CUSTOMER, SENDER_OPERATOR, Contact, Message, Organization
from app.services import agent_config
from app.schemas import GenerationResult
from app.services import booking
from app.services import languages
from app.services import offers
from app.services import orders
from app.services import sales_policy
from app.services.understanding import groq_reasoning

logger = logging.getLogger(__name__)

# Reasoning models spend part of this budget thinking before emitting text, so
# it must comfortably exceed the length of the reply we actually want.
MAX_OUTPUT_TOKENS = 2048
# Gemini counts its thinking against the same budget, so it needs more room.
GEMINI_OUTPUT_TOKENS = 8192

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

DEFAULT_SALES_PROMPT = (
    "You are a professional WhatsApp sales agent. Be concise, friendly and helpful. "
    "Qualify the lead and move the conversation toward a booked demo."
)

# Openers that count as a greeting, so the agent never greets the same person
# twice. Matched against the start of a message, lowercased.
GREETING_MARKERS = (
    "hi ",
    "hi,",
    "hi!",
    "hello",
    "hey",
    "salam",
    "assalam",
    "as-salam",
    "walaikum",
    "waalaikum",
    "wa alaikum",
    "good morning",
    "good afternoon",
    "good evening",
    "welcome",
)


def _opens_with_greeting(text: str) -> bool:
    stripped = (text or "").strip().lower()
    return any(stripped.startswith(marker) for marker in GREETING_MARKERS)


def _contains_greeting(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return any(marker.strip() in lowered for marker in GREETING_MARKERS)


# --------------------------------------------------------------------------
# Catalogue scoping
# --------------------------------------------------------------------------
# Sections that always travel with the catalogue, whatever is being discussed.
ALWAYS_INCLUDE = ("general", "delivery", "shipping", "policy", "policies", "returns")

STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "are", "have", "has",
    "how", "what", "does", "can", "want", "need", "would", "like", "please",
    "this", "that", "them", "they", "not", "any", "all", "from", "get", "one",
    "pair", "size", "much", "price", "prices", "cost", "list",
}


def split_catalogue(product_rules: str) -> dict[str, list[str]]:
    """Split a price list into sections keyed by their heading.

    A heading is a line with no leading bullet that is mostly upper-case —
    the shape people naturally write price lists in. Anything before the first
    heading lands under "" and is always kept.
    """
    sections: dict[str, list[str]] = {"": []}
    current = ""
    for raw in (product_rules or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        letters = [c for c in stripped if c.isalpha()]
        is_heading = (
            not stripped.startswith("-")
            and len(letters) >= 3
            and sum(c.isupper() for c in letters) / len(letters) > 0.7
        )
        if is_heading:
            current = stripped.rstrip(":")
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return sections


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]{3,}", (text or "").lower())
    return {w for w in words if w not in STOPWORDS}


def scope_catalogue(product_rules: str, focus: str, max_sections: int = 1) -> str:
    """Return the catalogue trimmed to what this conversation is about.

    The full list is returned whenever the focus is unclear or the list has no
    headings — being complete matters more than being short.
    """
    if not product_rules:
        return ""

    sections = split_catalogue(product_rules)
    named = {k: v for k, v in sections.items() if k}
    if len(named) < 2:
        return product_rules

    focus_tokens = _tokens(focus)
    if not focus_tokens:
        return product_rules

    scored: list[tuple[int, str]] = []
    for heading, lines in named.items():
        if heading.lower() in ALWAYS_INCLUDE or any(
            word in heading.lower() for word in ALWAYS_INCLUDE
        ):
            continue
        overlap = len(focus_tokens & _tokens(heading + " " + " ".join(lines)))
        scored.append((overlap, heading))

    scored.sort(reverse=True)
    if not scored or scored[0][0] == 0:
        return product_rules

    keep = {heading for _, heading in scored[:max_sections]}

    out: list[str] = list(sections.get("", []))
    for heading, lines in named.items():
        always = heading.lower() in ALWAYS_INCLUDE or any(
            word in heading.lower() for word in ALWAYS_INCLUDE
        )
        if heading in keep or always:
            out.append(heading)
            out += lines
        else:
            # Named so the agent knows it exists and can offer to switch.
            out.append(f"{heading} (also stocked — ask if they want details)")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Price guardrail
# --------------------------------------------------------------------------
MONEY = re.compile(
    r"(?:PKR|Rs\.?|₨|USD|AED|SAR|\$|£|€)\s*([\d][\d,\.]*)", re.IGNORECASE
)


def _normalise_amount(raw: str) -> str:
    """1,596 / 1596.00 / 1596. all become 1596.

    Feeds quote prices as "1596.00" while people write "Rs. 1,596"; without
    folding those together the guard rejects a perfectly correct price.
    """
    amount = raw.replace(",", "").rstrip(".")
    if "." in amount:
        amount = amount.rstrip("0").rstrip(".")
    return amount or "0"


def money_figures(text: str) -> set[str]:
    """Currency amounts in a piece of text, normalised for comparison."""
    return {_normalise_amount(match.group(1)) for match in MONEY.finditer(text or "")}


def unsupported_prices(reply: str, product_rules: str) -> set[str]:
    """Amounts the reply quotes that do not appear in the price list."""
    if not product_rules:
        return set()
    catalogue = money_figures(product_rules)
    if not catalogue:
        return set()
    return money_figures(reply) - catalogue


# ---------------------------------------------------------- promises
# Commitments a business can only make if it has said it makes them. Each is
# a pattern to find in a reply and the word to look for in the shop's own
# configuration - so a shop that offers free estimates may say so, and a shop
# that has never mentioned one may not start.
#
# The price guard covers invented figures. These are the claims with no digits
# in them, which commit the business just as firmly and were unguarded: a free
# visit, a guarantee, a discount, an area served.
_PROMISES: tuple[tuple[str, str, str], ...] = (
    (r"\bfree\s+(consultation|estimate|quote|survey|visit|assessment|inspection)\b",
     "free", "a free visit or quote"),
    (r"\b(guarantee|guaranteed|warranty|warrantied)\b",
     "guarantee", "a guarantee or warranty"),
    (r"\b(discount|%\s*off|money[- ]back|refund guarantee)\b",
     "discount", "a discount"),
    (r"\bno[- ]obligation\b", "obligation", "a no-obligation offer"),
    (r"\b(same[- ]day|next[- ]day|24[- ]hour)\s+(service|visit|response|turnaround)\b",
     "same-day", "a same-day or next-day promise"),
    (r"\b(fully\s+)?(licensed|insured|bonded|certified|accredited)\b",
     "licensed", "a licensing or insurance claim"),
    (r"\b(price\s+match|beat any (price|quote))\b", "price match", "a price match"),
)

_PROMISE_PATTERNS = tuple(
    (re.compile(pattern, re.IGNORECASE), needle, description)
    for pattern, needle, description in _PROMISES
)


def unsupported_promises(reply: str, corpus: str) -> list[str]:
    """Commitments in this reply that the shop's own data does not support.

    An empty corpus returns nothing. A shop that has configured no rules at
    all is not a shop making false promises; it is one we know nothing about,
    and refusing every sentence would leave it with an agent that cannot
    speak.
    """
    if not reply or not corpus:
        return []

    haystack = corpus.lower()
    found: list[str] = []
    for pattern, needle, description in _PROMISE_PATTERNS:
        if pattern.search(reply) and needle not in haystack:
            found.append(description)
    return found


# --------------------------------------------------------- claiming to be human
# A reply may say "we" all it likes: the agent answers for the business, which
# is the whole arrangement. What it may never say is that it is a person.
#
# Unconditional, and the only guard here that consults nothing. The others ask
# the record a question - is there an appointment, is that price listed. This
# one is false every time it is said, because of what is saying it.
_NOT_HUMAN = re.compile(
    r"(?:"
    # "I am a live team member", "I'm a real person", "I am human"
    r"\bI(?:'m|\s+am)\s+(?:a\s+|an\s+)?(?:real|live|actual|genuine|human)\s+"
    r"(?:person|human|team\s+member|agent|rep|representative|employee|staff)\b"
    r"|\bI(?:'m|\s+am)\s+(?:a\s+)?human\b"
    # "I am not a bot"
    r"|\bI(?:'m|\s+am)\s+not\s+(?:a\s+|an\s+)?(?:bot|robot|ai|a\.i\.|machine|"
    r"computer|automated|chatbot)\b"
    # "you are speaking with a real person"
    r"|\byou(?:'re|\s+are)\s+(?:speaking|talking|chatting|dealing)\s+(?:to|with)\s+"
    r"(?:a\s+|an\s+)?(?:real\s+|live\s+|actual\s+)?(?:person|human|team\s+member)\b"
    # "this is a real person"
    r"|\bthis\s+is\s+(?:a\s+)?(?:real|live|actual)\s+(?:person|human|team\s+member)\b"
    r")",
    re.IGNORECASE,
)


# "Here are the pictures", "I've sent you the photos", "see the attached
# image". Only a claim that a picture is going with this message; "do you
# have a photo of it?" or "we don't have pictures of that" are fine.
_PHOTO_SENT = re.compile(
    r"(?:"
    r"\bhere(?:'s|\s+is|\s+are)\s+(?:a\s+|the\s+|some\s+|our\s+)?(?:\w+\s+){0,2}"
    r"(?:photos?|pictures?|pics?|images?|snaps?)\b"
    r"|\b(?:I|we)(?:'ve|\s+have)?\s+(?:just\s+)?(?:sent|attached|shared)\s+(?:you\s+)?"
    r"(?:a\s+|the\s+|some\s+)?(?:\w+\s+){0,2}(?:photos?|pictures?|pics?|images?)\b"
    r"|\b(?:photos?|pictures?|pics?|images?)\s+(?:is|are)\s+(?:attached|above|below|included)\b"
    r"|\b(?:see|check)\s+(?:the\s+)?(?:attached|photos?|pictures?|images?)\b"
    r"|\battached\s+(?:photos?|pictures?|images?)\b"
    r")",
    re.IGNORECASE,
)


# "Shall I send you product images?", "I can share pictures of it" - an offer,
# which is only honest when this business has any pictures to send.
_PHOTO_OFFER = re.compile(
    r"(?:"
    r"\b(?:shall|should|can|may|could)\s+(?:I|we)\s+(?:also\s+)?(?:send|share|show|forward)\s+"
    r"(?:you\s+)?(?:\w+\s+){0,4}?(?:photos?|pictures?|pics?|images?)\b"
    r"|\b(?:I|we)(?:'ll|\s+will|\s+can|\s+could)\s+(?:also\s+)?(?:send|share|show|forward)\s+"
    r"(?:you\s+)?(?:\w+\s+){0,4}?(?:photos?|pictures?|pics?|images?)\b"
    r"|\bwould\s+you\s+like\s+(?:to\s+see\s+|me\s+to\s+send\s+(?:you\s+)?)?(?:\w+\s+){0,3}?"
    r"(?:photos?|pictures?|pics?|images?)\b"
    r"|\bwant\s+(?:to\s+see\s+)?(?:some\s+|the\s+)?(?:photos?|pictures?|pics?|images?)\b"
    r")",
    re.IGNORECASE,
)


def offers_photos(reply: str) -> str | None:
    """The phrase in which this reply offers to send a picture, if any."""
    found = _PHOTO_OFFER.search(reply or "")
    return found.group(0) if found else None


# Things only a person at the business can do, promised as if they will be
# done: a document sent, a quotation or a price changed. Nothing in this
# system issues an invoice or edits a quotation, so the sentence is untrue
# unless a person really was told to (see `handoff_allowed`). "Understood,
# the unit prices will be reduced" and "we will share a signed proforma
# invoice" both reached a customer.
_DOCUMENT = r"(?:pro[- ]?forma|invoices?|receipts?|contracts?|agreements?|pdfs?|revised\s+quot(?:e|ation)s?|quotation\s+documents?)"
_CHANGE = r"(?:reduce[sd]?|lower(?:ed)?|adjust(?:ed)?|revise[sd]?|change[sd]?|update[sd]?|cut|amend(?:ed)?|edit(?:ed)?|remove[sd]?)"
_UNBACKED = re.compile(
    r"(?:"
    rf"\b(?:I|we)(?:'ll|\s+will|\s+shall|\s+can|\s+would)\s+(?:\w+\s+){{0,2}}?"
    rf"(?:send|share|email|e-mail|issue|prepare|generate|draw\s+up|provide|forward|raise)\s+"
    rf"(?:you\s+)?(?:\w+\s+){{0,3}}?{_DOCUMENT}\b"
    rf"|\b(?:shall|should|can|may)\s+I\s+(?:send|share|email|issue|prepare|raise)\s+(?:you\s+)?"
    rf"(?:\w+\s+){{0,3}}?{_DOCUMENT}\b"
    rf"|\b(?:unit\s+)?(?:prices?|rates?|quot(?:e|ation)s?|invoices?)\s+(?:will|shall|would)\s+be\s+"
    rf"(?:\w+\s+)?{_CHANGE}\b"
    rf"|\b(?:I|we)(?:'ll|\s+will|\s+shall|\s+have|'ve)\s+(?:\w+\s+)?{_CHANGE}\s+"
    rf"(?:the\s+|your\s+|our\s+|each\s+)?(?:unit\s+)?(?:prices?|rates?|quot(?:e|ation)s?|invoices?)\b"
    r")",
    re.IGNORECASE,
)


def unbacked_commitments(reply: str) -> str | None:
    """A promise to send a document or change a price, if the reply makes one."""
    found = _UNBACKED.search(reply or "")
    return found.group(0) if found else None


# "Ji, 1 coil means 100 meters": one Urdu word is below what `is_roman_urdu`
# counts, and still not English.
_URDU_OPENING = re.compile(r"^\s*(?:ji|jee|haan|han|bilkul|zaroor|acha|theek)(?=[\s,!.]|$)", re.IGNORECASE)


def opens_in_urdu(reply: str) -> bool:
    return bool(_URDU_OPENING.match(reply or ""))


def claims_photos(reply: str) -> str | None:
    """The phrase in which this reply says it is sending a picture, if any."""
    found = _PHOTO_SENT.search(reply or "")
    return found.group(0) if found else None


def claims_to_be_human(reply: str) -> str | None:
    """The phrase in which this reply says it is a person, if any."""
    if not reply:
        return None
    found = _NOT_HUMAN.search(reply)
    return found.group(0) if found else None


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------
def format_history(messages: Sequence[Message] | Iterable[Any]) -> str:
    """Render stored messages as a Customer / Shop / Agent transcript.

    Three roles rather than two. A message from `Shop` was typed by a person
    who took the conversation over, and saying so is worth the handful of
    tokens: it is the shop's own voice, in front of this very customer, and the
    model should follow it rather than treat it as more of its own output.
    """
    roles = {SENDER_CUSTOMER: "Customer", SENDER_OPERATOR: "Shop"}
    lines: list[str] = []
    for message in messages:
        sender = str(getattr(message, "sender", SENDER_CUSTOMER)).lower()
        lines.append(f"{roles.get(sender, 'Agent')}: {getattr(message, 'content', '')}")
    return "\n".join(lines) if lines else "(no prior messages - this is the first contact)"


def describe_state(
    history: Sequence[Message] | Iterable[Any],
    latest_message: str,
    contact_name: str | None,
) -> tuple[str, list[str]]:
    """Work out where the conversation stands, and the rules that follow from it.

    Without this the model re-greets on every turn and resolves phrases like
    "the black pair" against the whole price list instead of what was actually
    discussed.
    """
    messages = list(history)
    agent_turns = [m for m in messages if str(getattr(m, "sender", "")).lower() != "user"]
    already_greeted = any(_opens_with_greeting(getattr(m, "content", "")) for m in agent_turns)
    customer_greeted_now = _contains_greeting(latest_message)
    first_contact = len(messages) == 0

    facts = [
        f"Turn: {len(messages) + 1}"
        + (" (first ever message from this customer)" if first_contact else " (conversation already in progress)")
    ]

    if first_contact:
        facts.append("You have not spoken to this person before.")
    else:
        facts.append(
            "You have already greeted this customer."
            if already_greeted
            else "No greeting has been exchanged yet."
        )
        asked = [
            getattr(m, "content", "")
            for m in messages
            if str(getattr(m, "sender", "")).lower() == "user"
        ]
        if asked:
            facts.append("Already asked about: " + " | ".join(a.strip() for a in asked[-4:]))
        if agent_turns:
            facts.append(
                "You last told them: " + getattr(agent_turns[-1], "content", "").strip()
            )

    facts.append(
        "The customer greeted you in this message."
        if customer_greeted_now
        else "The customer did not greet you in this message."
    )

    # Rules that depend on where we are in the conversation.
    rules: list[str] = []
    if first_contact:
        rules.append(
            "Open with one short, warm greeting"
            + (f" using their name ({contact_name})." if contact_name else ".")
        )
    else:
        rules.append(
            "Do NOT greet. Do not open with Hi, Hello, Salam or Walaikum Assalam, and do "
            "not open with their name — you are mid-conversation, so continue naturally."
        )
    if not first_contact and customer_greeted_now:
        rules.append("They greeted you again, so a brief acknowledgement is fine, but keep it to a few words.")
    if not first_contact:
        rules.append(
            "Anything vague they refer to (\"the black pair\", \"that one\", \"it\", \"them\") "
            "means something named earlier in THIS conversation — re-read the transcript and "
            "resolve it there. If it is genuinely ambiguous, ask which one instead of guessing."
        )
        rules.append(
            "Stay in the product category they are shopping for. If nothing in that category "
            "matches, say so and offer the closest option from the SAME category — never "
            "suggest a product meant for someone else."
        )
        rules.append("Do not repeat information you have already given unless they ask again.")

    return "\n".join(facts), rules


def reply_rules(state_rules: list[str]) -> str:
    base = [
        "Write one WhatsApp message in plain text — no markdown, no bullet points, no headings.",
        "One to three sentences. Under 45 words. Crisp, not chatty. The one exception: when "
        "PRICE FACTS lists several products or order lines, give each its own short line with "
        "its figure, still with no markdown.",
        "Answer the question they actually asked in the FIRST sentence. Propose the next "
        "step after that, never before.",
        "No preamble and no filler — skip \"great question\", \"I'd be happy to help\", "
        "\"thanks for reaching out\" and restating their question back at them.",
        "Never paste catalogue or website copy. Name the product, give its price, and say "
        "one useful thing about it in your own words.",
        "Sound like a real person who works here, not a script or a chatbot.",
        "Never invent a product, price, size or promise that is not in the business rules, "
        "the knowledge or PRICE FACTS. Totals come from PRICE FACTS or from sums you show.",
        "End with one clear next step, unless they have already agreed to one.",
    ]
    lines = [f"- {rule}" for rule in state_rules + base]
    return "HOW TO REPLY\n" + "\n".join(lines)


# Currency symbol and the language's own name, so the instruction reads
# naturally to the model. Anything unlisted falls back to the bare code.
CURRENCY_NAMES = {
    "USD": "US dollars ($)",
    "EUR": "euros (€)",
    "GBP": "pounds sterling (£)",
    "PKR": "Pakistani rupees (PKR)",
    "AED": "UAE dirhams (AED)",
    "SAR": "Saudi riyals (SAR)",
    "INR": "Indian rupees (₹)",
    "TRY": "Turkish lira (₺)",
    "NGN": "Nigerian naira (₦)",
    "ZAR": "South African rand (R)",
    "CAD": "Canadian dollars (CA$)",
    "AUD": "Australian dollars (A$)",
}

LANGUAGE_NAMES = {
    "en": "English",
    "ur": "Urdu",
    "ar": "Arabic",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "tr": "Turkish",
    "hi": "Hindi",
    "pt": "Portuguese",
    "id": "Indonesian",
    "ms": "Malay",
    "zh": "Chinese",
}


# Roman Urdu is how Pakistani customers actually type on WhatsApp — Urdu words
# in Latin script, mixed freely with English. Translating into formal Urdu
# script, or answering stiff textbook English, both read as a machine.
ROMAN_URDU_MARKERS = (
    "kitne", "kitna", "kitni", "hai", "hain", "kya", "kyun", "nahi", "nai",
    "chahiye", "chaiye", "mujhe", "mujhy", "aap", "ap ", "ka ", "ki ", "ke ",
    "acha", "accha", "theek", "thik", "bhej", "dikhao", "dikhaen", "batao",
    "bata", "din", "mein", "mai ", "hoga", "hogi", "karo", "kardo", "milega",
    "milegi", "salam", "assalam", "shukriya", "bhai", "behen", "yar", "yaar",
    "paisa", "paise", "rakh", "order karna", "lena hai",
)

ROMAN_URDU_GUIDE = """ROMAN URDU:
This customer is writing Roman Urdu (Urdu in English letters). Reply the same way —
Roman Urdu mixed with English exactly as Pakistani shopkeepers write on WhatsApp.

- Keep product names, prices and English business words in English (Cash on Delivery,
  delivery, order, size, colour). Do NOT translate them into formal Urdu.
- Never reply in Urdu script. Latin letters only.
- Write how people speak, not like a textbook translation.

Examples of the right voice:
Customer: "Iski delivery kitne din mein hogi?"
You: "Delivery poore Pakistan mein 5 se 7 working days leti hai. Aap ka city kaunsa hai?"

Customer: "COD hai?"
You: "Ji bilkul! Cash on Delivery poore Pakistan mein available hai. Rs. 15,000 se upar
order ho to advance payment lagti hai."

Customer: "Red wala dikhao"
You: "Zaroor! Basic Shirt red mein Rs. 1,596 ka hai. Tasveer bhej rahi hoon — size bata dein?"

Customer: "Ye mehnga hai"
You: "Samajh sakti hoon. Is range mein Basic Shirt Rs. 1,596 ka hai, wo dekhna chahenge?\""""


def is_roman_urdu(text: str) -> bool:
    """Roughly: does this read as Urdu typed in Latin script?

    Two markers rather than one, so a stray "hai" inside an English sentence
    does not flip the whole reply into Roman Urdu.
    """
    lowered = f" {(text or '').lower()} "
    hits = sum(1 for marker in ROMAN_URDU_MARKERS if marker in lowered)
    return hits >= 2


def regional_rules(currency: str | None, language: str | None) -> list[str]:
    """Reply rules implied by an organization's regional settings."""
    rules: list[str] = []

    code = (language or "en").strip()
    base = code.split("-")[0].lower()
    if base:
        # English is stated as explicitly as any other language. Leaving it
        # implicit — on the grounds that the model's default is English anyway
        # — is what let a booking reply for an English-speaking B2B tenant come
        # back in Roman Urdu: that turn strips the catalogue out of the prompt,
        # and with the surrounding English context gone there was nothing left
        # saying which language to write in.
        name = LANGUAGE_NAMES.get(base, code)
        rules.append(
            "LANGUAGE: reply in the language and script of the customer's latest message - "
            "Spanish to Spanish, Arabic to Arabic, Urdu script to Urdu script, Roman Urdu to "
            "Roman Urdu, and so on - even though everything above is written in English. "
            f"Only when you cannot tell what language they wrote in, use {name}. Keep product "
            "names and prices exactly as written above."
        )

    money = (currency or "").strip().upper()
    if money:
        described = CURRENCY_NAMES.get(money, money)
        rules.append(
            f"Quote every price in {described}, formatted the way a local "
            f"customer would expect. Never convert to another currency."
        )
    return rules


def voice_block(organization: Organization | None) -> str:
    """How this shop writes, when a person at the shop has approved a voice.

    Empty for every tenant that has not, which is what keeps this from changing
    the way a running client's agent sounds the moment it is deployed.

    The fence in the first lines is the load-bearing part. The examples are real
    messages written to real customers, and without being told otherwise a model
    will happily lift a detail out of one and answer a different customer with
    it. Form comes from here; facts come from above.
    """
    style = (getattr(organization, "voice_style", None) or "").strip()
    examples = [e for e in (getattr(organization, "voice_examples", None) or []) if e]
    if not style and not examples:
        return ""

    lines = [
        "=== HOW THIS SHOP WRITES ===",
        "Write the reply in this voice. This section describes FORM ONLY: length, "
        "greeting, language, formality, punctuation.",
        "Never take a fact, a price, a product name, a place or a promise from it. "
        "Those come only from the business rules and knowledge base above.",
    ]
    if style:
        lines.append(style)
    if examples:
        lines.append("Lines this shop has actually written, as examples of style:")
        lines += [f'- "{example}"' for example in examples]
    return "\n".join(lines)


def build_prompt(
    organization: Organization | None,
    contact: Contact | None,
    history: Sequence[Message] | Iterable[Any],
    latest_message: str,
    knowledge: str = "",
    memory_block: str = "",
    policy_block: str = "",
    meaning: str | None = None,
) -> str:
    """Stitch business rules, contact metadata, chat history and the new message.

    Pure function - no I/O - so the assembly order is directly unit-testable.
    """
    org_name = getattr(organization, "name", None) or "the business"
    sales_prompt = getattr(organization, "sales_prompt", None) or DEFAULT_SALES_PROMPT
    tone = getattr(organization, "target_tone", None)
    product_rules = getattr(organization, "product_rules", None)
    currency = getattr(organization, "default_currency", None)
    language = getattr(organization, "default_language", None)

    sections: list[str] = []

    business = [
        "=== BUSINESS RULES ===",
        f"Business name: {org_name}",
        f"Sales system prompt: {sales_prompt}",
    ]
    if currency:
        business.append(f"Currency: {CURRENCY_NAMES.get(currency.upper(), currency)}")
    if language:
        base = language.split("-")[0].lower()
        business.append(f"Language: {LANGUAGE_NAMES.get(base, language)}")
    if tone:
        business.append(f"Target audience tone: {tone}")
    if product_rules:
        # Send the part of the catalogue this conversation is actually about.
        # A long list dilutes attention and is where wrong prices come from.
        focus = " ".join(
            filter(
                None,
                [
                    latest_message,
                    getattr(contact, "category_interest", None),
                    " ".join(getattr(m, "content", "") for m in list(history)[-4:]),
                ],
            )
        )
        business.append(f"Product offerings and rules: {scope_catalogue(product_rules, focus)}")
    sections.append("\n".join(business))

    # How this particular business operates: hours, areas served, services,
    # what it will not promise. Empty for an organization that has configured
    # none of it, which is what keeps a running client's agent answering
    # exactly as it did before this shipped.
    operating = agent_config.as_prompt_block(organization)
    if operating:
        sections.append(operating)

    contact_name = getattr(contact, "name", None) or "Unknown"
    phone = getattr(contact, "phone_number", None) or "Unknown"
    stage = getattr(contact, "pipeline_stage", None) or "LEAD"

    metadata = [
        "=== CUSTOMER METADATA ===",
        f"Name: {contact_name}",
        f"Phone: {phone}",
        f"Current pipeline stage: {stage}",
    ]

    # Facts remembered from earlier — these outlive the chat-history window, so
    # the agent must never ask for something already listed here.
    remembered = {
        "City": getattr(contact, "city", None),
        "Shoe size": getattr(contact, "shoe_size", None),
        "Shopping for": getattr(contact, "category_interest", None),
        "Colour preference": getattr(contact, "colour_preference", None),
        "Budget": getattr(contact, "budget_note", None),
    }
    known = {label: value for label, value in remembered.items() if value}
    if known:
        metadata.append("Known from earlier (do NOT ask for any of these again):")
        metadata += [f"- {label}: {value}" for label, value in known.items()]

    # Structured memory: requirements, objections, commitments, with provenance.
    if memory_block:
        metadata.append(memory_block)

    sections.append("\n".join(metadata))

    if knowledge:
        sections.append(knowledge)

    sections.append("=== CHAT HISTORY ===\n" + format_history(history))

    state, state_rules = describe_state(history, latest_message, getattr(contact, "name", None))
    sections.append("=== CONVERSATION STATE ===\n" + state)

    latest = f"=== LATEST CUSTOMER MESSAGE ===\n{latest_message}"
    if meaning and " ".join(meaning.lower().split()) != " ".join(latest_message.lower().split()):
        # A reading of a hurried or garbled message, to answer from. The
        # customer's own words stay above it and win if the two disagree; the
        # reply is still in their language, not in this English.
        latest += (
            f"\n(Most likely meaning: {meaning} - answer that. If it does not fit their "
            "words, go by their words. Reply in the language they wrote in.)"
        )
    sections.append(latest)

    # Roman Urdu is detected per message, not per organization: the same shop
    # gets English and Roman Urdu customers within the same hour.
    recent = " ".join(getattr(m, "content", "") for m in list(history)[-3:])
    if is_roman_urdu(latest_message) or is_roman_urdu(recent):
        sections.append(ROMAN_URDU_GUIDE)

    sections.append(READING_CUSTOMERS)

    # What this specific turn must accomplish, then the standing rules.
    if policy_block:
        sections.append(policy_block)
    sections.append(sales_policy.SALES_POLICY)

    # Late, because an instruction about style competes with every other
    # instruction in the prompt and the ones nearest the end are followed best.
    voice = voice_block(organization)
    if voice:
        sections.append(voice)

    sections.append(reply_rules(state_rules + regional_rules(currency, language)))
    sections.append("=== YOUR REPLY (plain text only) ===")

    return "\n\n".join(sections)


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
def _is_exhausted(error: Exception) -> bool:
    """Rate limit or quota — worth retrying on a different key."""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in (429, 503)
    return False


async def _groq_once(prompt: str, api_key: str) -> str:
    payload = {
        "model": settings.groq_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": MAX_OUTPUT_TOKENS,
        **groq_reasoning(settings.groq_model),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(GROQ_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    try:
        choice = data["choices"][0]
        text = choice["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError) as exc:
        raise RuntimeError(f"unexpected Groq response shape: {data}") from exc

    # A half-written sentence must never reach a customer — fail over instead.
    if choice.get("finish_reason") == "length" or not text:
        raise RuntimeError("Groq reply was truncated or empty (token budget exhausted)")
    return text


async def _call_groq(prompt: str) -> str:
    keys = settings.groq_api_keys
    if not keys:
        raise RuntimeError("GROQ_API_KEY is not configured")

    last: Exception | None = None
    for index, api_key in enumerate(keys, start=1):
        try:
            return await _groq_once(prompt, api_key)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _is_exhausted(exc) and index < len(keys):
                logger.info("Groq key %d/%d exhausted, rotating", index, len(keys))
                continue
            raise
    raise last  # type: ignore[misc]


async def _call_gemini(prompt: str) -> str:
    keys = settings.gemini_api_keys
    if not keys:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    last: Exception | None = None
    for index, api_key in enumerate(keys, start=1):
        try:
            return await _gemini_once(prompt, api_key)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _is_exhausted(exc) and index < len(keys):
                logger.info("Gemini key %d/%d exhausted, rotating", index, len(keys))
                continue
            raise
    raise last  # type: ignore[misc]


async def _gemini_once(prompt: str, api_key: str) -> str:
    url = GEMINI_URL_TEMPLATE.format(model=settings.gemini_model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        # Gemini 3.x spends several hundred "thinking" tokens before writing a
        # word, and they count against this budget. A tight cap here does not
        # produce a short reply — it produces a truncated one.
        #
        # 2048 still ran out: a busy prompt thought its way through the whole
        # budget and returned nothing, which is what took the agent to its
        # documents-only replies. Room for the thinking plus a full reply; how
        # long the reply is stays set by the prompt, not by this cap.
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": GEMINI_OUTPUT_TOKENS},
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": api_key},
        )
        response.raise_for_status()
        data = response.json()

    try:
        candidate = data["candidates"][0]
        text = candidate["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, AttributeError) as exc:
        raise RuntimeError(f"unexpected Gemini response shape: {data}") from exc

    if candidate.get("finishReason") == "MAX_TOKENS" or not text:
        raise RuntimeError("Gemini reply was truncated or empty (token budget exhausted)")
    return text


# What the model writes when the answer is not in anything it was given. The
# backend turns it into an alert to a person and a reply that says so, which
# is the only honest version of "our team will get back to you".
_NEEDS_TEAM = re.compile(r"^\W*NEEDS[_ ]TEAM\W*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)

NEEDS_TEAM_RULE = (
    "IF YOU DO NOT HAVE THE ANSWER: when what they ask is not in the business "
    "information above and nothing there comes close, do not guess and do not "
    "promise that anyone will follow up. Reply with exactly one line:\n"
    "NEEDS_TEAM: <their question in a few words>\n"
    "and nothing else. The system will alert the team and tell the customer. "
    "Use this only when you truly have nothing to answer with; a partial "
    "answer from the information above is always better. Never use it for a "
    "greeting, a thanks, an \"ok\", small talk or a question that has nothing to do "
    "with the business - answer those yourself, briefly and kindly."
)

# How to read people as they actually write. Customers type fast, misspell,
# drop words, mix languages and wander off topic, and none of that is theirs
# to fix: the agent works out what they mean, the way a good shop assistant
# would. Principles, not phrases - nothing here names a message to expect.
READING_CUSTOMERS = """=== HOW TO READ THE CUSTOMER ===
- People type fast. Work out what they mean through typos, missing words, shorthand
  ("pls", "u", "hw much"), voice-to-text mistakes and mixed languages. Never correct them
  and never ask them to rephrase something you can reasonably understand.
- Answer what they asked, first, from the information above. If they asked several
  things, answer each one. Do not answer a different question from an earlier message.
- If a message could mean two things, pick the likelier one and answer it; ask a short
  question only when a wrong guess would matter (an order, a booking, a price).
- Anything booked with the business - a demo, a call, a visit - is with the team, not
  with you. Never put off answering until then when the answer is here.
- Off-topic questions and small talk: a short, friendly line, then offer to help with
  what the business does. Do not lecture and do not refuse rudely.
- Never invent anything to fill a gap. Say what you do know, plainly."""


def needs_team(text: str) -> str | None:
    """The question the model could not answer, if it said so."""
    found = _NEEDS_TEAM.match(text or "")
    if not found:
        return None
    return found.group(1).strip().splitlines()[0][:200] if found.group(1).strip() else ""


# Said when nothing was known and nobody could be told. The caller replaces it
# with "I've passed this to the team" once an alert has actually gone out.
DONT_KNOW = "I'm sorry, I don't have that information."


class _Unanswerable(Exception):
    """The model asked for a person twice rather than answering."""


async def generate_reply(
    organization: Organization | None,
    contact: Contact | None,
    history: Sequence[Message] | Iterable[Any],
    latest_message: str,
    knowledge: str = "",
    memory_block: str = "",
    policy_block: str = "",
    last_resort: str = "",
    appointment=None,
    did_cancel: bool = False,
    did_move: bool = False,
    order_placed: bool = False,
    handoff_allowed: bool = False,
    known_prices: Iterable[Any] = (),
    known_quantities: Iterable[Any] = (),
    photos_attached: bool | None = None,
    photos_available: bool | None = None,
    meaning: str | None = None,
) -> GenerationResult:
    """Build the prompt, try Groq, fall back to Gemini, and time both attempts.

    `known_prices` are the business's own prices read out of its documents,
    and the sums worked out from them for this message (see `offers`);
    `known_quantities` are the amounts the customer asked for. Together they
    are what lets a reply show a total without the guard refusing it.
    """
    prompt = build_prompt(
        organization, contact, history, latest_message, knowledge, memory_block, policy_block,
        meaning=meaning,
    )
    if not handoff_allowed:
        prompt = prompt.replace(
            "=== YOUR REPLY (plain text only) ===",
            NEEDS_TEAM_RULE + "\n\n=== YOUR REPLY (plain text only) ===",
        )

    # Prices the agent is allowed to quote: the business rules AND whatever was
    # retrieved for this turn. Catalogue prices live in the knowledge base, so
    # checking against the rules alone would reject every real product price.
    # Prices already quoted to this customer count as supported: they were
    # validated when they were sent, and "how much was the first one?" must be
    # answerable without re-retrieving the product.
    already_quoted = " ".join(
        getattr(message, "content", "")
        for message in history
        if str(getattr(message, "sender", "")).lower() != "user"
    )
    price_corpus = "\n".join(
        filter(
            None,
            [getattr(organization, "product_rules", None) or "", knowledge, already_quoted],
        )
    )

    # Did the customer give us any reason to answer in Roman Urdu? Only their
    # own words count — what the agent said before does not, or one drifted
    # reply teaches the guard to accept every reply after it.
    customer_said = " ".join(
        [latest_message]
        + [
            getattr(message, "content", "")
            for message in history
            if str(getattr(message, "sender", "")).lower() == "user"
        ]
    )
    # What the guard accepts, and what it can work a figure out from.
    #
    # It used to accept only amounts written with a currency in front of them,
    # word for word. A price table whose header says "Unit Price (PKR)" writes
    # every row as a bare "31,800", so none of its prices counted - and no
    # total ever could, because 10 x PKR 31,800 is not written anywhere. Every
    # catalogue answer was refused, retried, refused again, and replaced with
    # "could you tell me a little more?".
    listed = offers.amounts(price_corpus) | {
        value for value in (offers.to_decimal(str(p)) for p in known_prices) if value
    }
    # Whether this business has written any prices down at all - decided
    # before the customer's figures are added, or a shop with no price list
    # would start having replies refused.
    has_prices = bool(listed)
    # The customer's own figures may be said back to them. They are not a
    # price this business is committing to; they are what was asked about.
    listed |= offers.customer_figures([customer_said])
    quantities = offers.asked_quantities([customer_said]) | {
        value for value in (offers.to_decimal(str(q)) for q in known_quantities) if value
    }
    rates = offers.percentages(price_corpus)

    def unexplained(text: str) -> set[str]:
        if not has_prices:
            # No price list at all: nothing to check against, as before.
            return set()
        return {
            _normalise_amount(str(value))
            for value in offers.unexplained(text, listed, quantities, rates)
        }

    expects_english = (
        (getattr(organization, "default_language", None) or "en").split("-")[0].lower() == "en"
        and not is_roman_urdu(customer_said)
    )

    def problems_in(text: str) -> list[str]:
        """Everything in this reply the record does not support, said as a correction."""
        problems: list[str] = []

        # Only the price check is behind the price-guard flag. The handoff and
        # language checks are about what the agent is allowed to say at all,
        # not about price accuracy, and turning off price checking must not
        # quietly turn those off too.
        if settings.price_guard_enabled:
            bad = unexplained(text)
            if bad:
                problems.append(
                    "you quoted " + ", ".join(sorted(bad)) + " which is NOT in the price "
                    "list and does not follow from it; quote only the figures in PRICE "
                    "FACTS and the knowledge above, and show the sum for any total"
                )

        # Promises with no digits in them. Same rule as the prices above: the
        # shop's own configuration decides, not the model's sense of what a
        # helpful business would offer.
        if settings.price_guard_enabled:
            promised = unsupported_promises(text, price_corpus)
            if promised:
                problems.append(
                    "you offered " + ", ".join(sorted(promised)) + " which this "
                    "business has not said it offers; say only what the rules and "
                    "knowledge above support"
                )

        # A booking, a cancellation or a move may only be announced if one
        # happened. This is the check that would have stopped "Your
        # appointment is confirmed for September 19, 2026 at 1:00 AM EST"
        # leaving the building: there was no appointment, and no amount of
        # instruction in the prompt had prevented the sentence.
        problems.extend(
            booking.unverified_claims(
                text,
                appointment=appointment,
                cancelled=did_cancel,
                moved=did_move,
            )
        )
        # The same for orders: "Great! I've noted the notebook" was said
        # about an order nothing had recorded.
        order_claim = orders.claims_order(text, order_placed)
        if order_claim:
            problems.append(order_claim)

        # Saying it is a person. Judged without consulting anything, because
        # it is false every time regardless of what the conversation was
        # about. "I am a live team member here" reached a real customer, and
        # the booking guard above only objected to the other half of that
        # sentence.
        # Pictures, like bookings, may only be announced if they are going
        # out. None means the caller did not say, which leaves this unchecked.
        if photos_attached is False:
            shown = claims_photos(text)
            if shown:
                problems.append(
                    f'you wrote "{shown}", but no picture is being sent with this reply; do '
                    "not mention photos being sent, and if they asked for one say there is "
                    "no photo of it to send"
                )

        # An offer of pictures from a business that has none to send.
        if photos_available is False:
            offered = offers_photos(text)
            if offered:
                problems.append(
                    f'you wrote "{offered}", but this business has no product photos to send; '
                    "do not offer pictures"
                )

        # A document sent or a price changed, which nothing here will do.
        if not handoff_allowed:
            committed = unbacked_commitments(text)
            if committed:
                problems.append(
                    f'you wrote "{committed}"; nothing will send that document or change '
                    "that price, so do not say it will happen. Say what the price list and "
                    "terms above state, and if they asked for something those do not allow, "
                    "say plainly that it is not offered"
                )

        pretending = claims_to_be_human(text)
        if pretending:
            problems.append(
                f'you wrote "{pretending}"; you are not a person and must never say '
                "you are. Answer as the business without claiming to be human"
            )

        # A promised human callback is acceptable only when one was really
        # arranged. `handoff_allowed` is set by the caller *after* the alert
        # has been raised and found somewhere to go - so the sentence is
        # backed by a delivered instruction to a person, not by the model
        # deciding it would be a comforting thing to say.
        handoff = None if handoff_allowed else sales_policy.contains_handoff(text)
        if handoff:
            problems.append(
                f'you wrote "{handoff}"; never promise that a person will follow up '
                "yourself. Answer using the facts above; if they are not there, reply "
                "only NEEDS_TEAM: <their question>"
            )

        # An English-speaking tenant whose customer wrote in English must not
        # be answered in Roman Urdu. The instruction to write in English is in
        # the prompt, but on a booking turn the catalogue is stripped out and
        # the model has drifted anyway, so the output is checked rather than
        # trusted.
        if expects_english and (is_roman_urdu(text) or opens_in_urdu(text)):
            problems.append(
                "you replied in Roman Urdu, but this customer wrote in English; "
                "rewrite the same reply in English"
            )

        # The script is checkable: Arabic answered in Latin letters, or English
        # answered in Devanagari, is wrong whatever the words say.
        script = languages.wrong_script(latest_message, text)
        if script:
            problems.append(f"{script}; rewrite the same reply")

        return problems

    def repaired(text: str) -> str | None:
        """The reply without the sentences that broke a rule, if the rest stands.

        Most refusals are one sentence - "Shall I send you pictures?", "our
        team will get back to you" - in an otherwise right answer. Asking the
        model again cost seconds and, failing twice, sent the customer the
        fallback instead of a good answer minus one line. A wrong language is
        not one sentence, and is not repaired.
        """
        if expects_english and (is_roman_urdu(text) or opens_in_urdu(text)):
            return None
        if languages.wrong_script(latest_message, text):
            return None
        bad_amounts = unexplained(text) if settings.price_guard_enabled else set()
        parts = [p for p in re.split(offers.SENTENCE_END + r"|\n+", text) if p.strip()]
        kept = []
        for part in parts:
            figures = {_normalise_amount(str(v)) for v in offers.amounts(part)}
            if figures & bad_amounts:
                continue
            others = [
                problem for problem in problems_in(part)
                if not problem.startswith("you quoted ")
            ]
            if others:
                continue
            kept.append(part.strip())
        dropped = len(parts) - len(kept)
        candidate = " ".join(kept).strip()
        if not kept or dropped > max(1, len(parts) // 2) or len(candidate) < 25:
            return None
        return candidate if not problems_in(candidate) else None

    async def guard(text: str, call) -> str:
        """Send a reply only if the record supports everything in it.

        A reply with one unsupported sentence goes out without that sentence.
        Otherwise one corrective retry, then give up rather than send a wrong
        number to a customer - a made-up price is a commitment the shop has to
        honour.
        """
        if needs_team(text) is not None:
            return text
        problems = problems_in(text)
        if not problems:
            return text
        fixed = repaired(text)
        if fixed:
            logger.info("reply repaired by dropping what broke a rule (%s)", "; ".join(problems))
            return fixed

        logger.warning("reply rejected (%s) — regenerating", "; ".join(problems))
        corrections.append("; also ".join(problems))
        corrected = await call(
            prompt + "\n\nCORRECTION: " + "; also ".join(problems) + ". Rewrite the reply."
        )
        if needs_team(corrected) is not None:
            return corrected
        if not problems_in(corrected):
            return corrected
        fixed = repaired(corrected)
        if fixed:
            return fixed
        still = unexplained(corrected) if settings.price_guard_enabled else set()
        if still:
            raise RuntimeError(
                "reply still quoted " + ", ".join(sorted(still)) + ", which the price list "
                "does not support"
            )
        if settings.price_guard_enabled and unsupported_promises(corrected, price_corpus):
            raise RuntimeError("reply still promised something the business has not offered")
        if claims_to_be_human(corrected):
            raise RuntimeError("reply still claimed to be a person")
        if photos_attached is False and claims_photos(corrected):
            raise RuntimeError("reply still said a picture was sent when none was")
        if photos_available is False and offers_photos(corrected):
            raise RuntimeError("reply still offered pictures this business does not have")
        if not handoff_allowed and unbacked_commitments(corrected):
            raise RuntimeError("reply still promised a document or a price change")
        if not handoff_allowed and sales_policy.contains_handoff(corrected):
            # Asked twice for a person rather than answering: it does not
            # have the answer. Handing to another provider for twenty more
            # seconds gets the same sentence; a person is what is needed.
            raise _Unanswerable(latest_message)
        if booking.unverified_claims(
            corrected, appointment=appointment, cancelled=did_cancel, moved=did_move
        ):
            raise RuntimeError("reply still claimed an appointment that does not exist")
        if orders.claims_order(corrected, order_placed):
            raise RuntimeError("reply still claimed an order that was not placed")
        if languages.wrong_script(latest_message, corrected):
            raise RuntimeError("reply still came back in the wrong script")
        if expects_english and (is_roman_urdu(corrected) or opens_in_urdu(corrected)):
            raise RuntimeError("reply still came back in Roman Urdu")
        return corrected

    # What the first provider was told to fix, so the second does not start
    # from nothing and make the same mistake at six times the latency.
    corrections: list[str] = []

    started = time.perf_counter()
    deadline = started + settings.reply_deadline_seconds

    def left() -> float:
        return deadline - time.perf_counter()

    acknowledging = sales_policy.just_acknowledging(latest_message)

    def finished(provider: str, text: str, fallback: bool, error: str | None = None) -> GenerationResult:
        asked = needs_team(text)
        if asked is not None and acknowledging:
            # "Ok" has nothing to pass to the team.
            asked = None
            text = sales_policy.acknowledgement_reply(latest_message)
        return GenerationResult(
            provider=provider,
            text=DONT_KNOW if asked is not None else text,
            prompt_used=prompt,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fallback_used=fallback,
            error=error,
            needs_team=(asked or latest_message[:200]) if asked is not None else None,
        )

    def gave_up(error: str) -> GenerationResult:
        # The worked-out answer if there is one; otherwise nothing was known,
        # and the caller alerts a person rather than sending a filler question.
        # An acknowledgement needed no AI in the first place - unless it was
        # a yes that did something (a booking made): that is said instead.
        if acknowledging and not last_resort:
            return GenerationResult(
                provider="none",
                text=sales_policy.acknowledgement_reply(latest_message),
                prompt_used=prompt,
                latency_ms=int((time.perf_counter() - started) * 1000),
                fallback_used=True,
                error=error,
                needs_team=None,
            )
        return GenerationResult(
            provider="none",
            text=last_resort or DONT_KNOW,
            prompt_used=prompt,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fallback_used=True,
            error=error,
            needs_team=None if last_resort else latest_message[:200],
        )

    async def attempt(call, text_prompt: str) -> str:
        return await guard(await call(text_prompt), call)

    try:
        text = await asyncio.wait_for(attempt(_call_groq, prompt), max(left(), 1))
        return finished("groq", text, False)
    except _Unanswerable:
        return finished("groq", "NEEDS_TEAM: " + latest_message[:200], False, "asked for a person twice")
    except Exception as groq_error:  # noqa: BLE001 - any Groq failure triggers fallback
        groq_reason = str(groq_error) or type(groq_error).__name__
        # The second provider only if there is time for it: a customer who
        # has waited the whole budget gets an answer now, not in thirty
        # seconds.
        if left() < 5:
            logger.warning("Groq failed (%s) with no time left for Gemini", groq_reason)
            return gave_up(f"groq: {groq_reason} | gemini: skipped, out of time")
        logger.warning("Groq generation failed, falling back to Gemini: %s", groq_reason)

        try:
            warned = (
                prompt + "\n\nBEFORE YOU WRITE: an earlier draft was rejected because "
                + corrections[-1] + "."
                if corrections
                else prompt
            )
            text = await asyncio.wait_for(attempt(_call_gemini, warned), left())
            return finished("gemini", text, True, f"groq: {groq_reason}")
        except _Unanswerable:
            return finished("gemini", "NEEDS_TEAM: " + latest_message[:200], True, "asked for a person twice")
        except Exception as gemini_error:  # noqa: BLE001 - both providers down
            reason = str(gemini_error) or type(gemini_error).__name__
            logger.error("Gemini fallback also failed: %s", reason)
            return gave_up(f"groq: {groq_reason} | gemini: {reason}")


# --------------------------------------------------------------------------
# Customer memory
# --------------------------------------------------------------------------
EXTRACTION_PROMPT = """From the WhatsApp conversation below, extract only facts the CUSTOMER
actually stated about themselves. Return strict JSON with exactly these keys:

{{"city": null, "shoe_size": null, "category_interest": null, "colour_preference": null, "budget_note": null}}

Rules:
- Use null for anything not clearly stated. Never guess or infer.
- shoe_size: just the number or size as given, e.g. "44" or "UK 8".
- category_interest: what they are shopping for in a few words, e.g. "men's formals".
- budget_note: only if they named a budget or price ceiling.
- Return JSON only — no explanation, no code fences.

CONVERSATION:
{conversation}

LATEST CUSTOMER MESSAGE:
{latest}
"""


async def extract_profile(
    history: Sequence[Message] | Iterable[Any], latest_message: str
) -> dict[str, str]:
    """Pull durable customer facts out of a conversation.

    Runs after the reply has been dispatched, so its latency never delays the
    customer. Any failure returns {} — memory is an enhancement, and must never
    break the reply path.
    """
    prompt = EXTRACTION_PROMPT.format(
        conversation=format_history(history), latest=latest_message
    )
    try:
        raw = await _call_groq(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profile extraction failed: %s", exc)
        return {}

    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("profile extraction returned non-JSON: %s", text[:120])
        return {}

    allowed = ("city", "shoe_size", "category_interest", "colour_preference", "budget_note")
    facts: dict[str, str] = {}
    for key in allowed:
        value = parsed.get(key)
        if isinstance(value, (int, float)):
            value = str(value)
        if isinstance(value, str):
            value = value.strip()
            if value and value.lower() not in ("null", "none", "unknown", "n/a"):
                facts[key] = value[:150]
    return facts


# --------------------------------------------------------------------------
# Provider reachability (used by /health)
# --------------------------------------------------------------------------
async def probe_groq() -> tuple[bool, str]:
    """Reachability AND availability of the configured model.

    Checking only that the endpoint answers is not enough: a valid key with a
    decommissioned model name still 404s on every generation.
    """
    keys = settings.groq_api_keys
    if not keys:
        return False, "GROQ_API_KEY not configured"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {keys[0]}"},
            )
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"

        available = {model["id"] for model in response.json().get("data", [])}
        if settings.groq_model not in available:
            return False, (
                f"model '{settings.groq_model}' is not available on this key "
                f"({len(available)} models offered)"
            )
        return True, f"model '{settings.groq_model}' available, {len(keys)} key(s)"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def probe_gemini() -> tuple[bool, str]:
    keys = settings.gemini_api_keys
    if not keys:
        return False, "GEMINI_API_KEY not configured"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": keys[0]},
            )
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"

        # Gemini reports names as "models/<id>".
        available = {
            model.get("name", "").removeprefix("models/")
            for model in response.json().get("models", [])
            if "generateContent" in model.get("supportedGenerationMethods", [])
        }
        if settings.gemini_model not in available:
            return False, (
                f"model '{settings.gemini_model}' is not available on this key "
                f"({len(available)} models offered)"
            )
        return True, f"model '{settings.gemini_model}' available, {len(keys)} key(s)"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
