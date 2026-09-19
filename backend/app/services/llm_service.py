"""Dynamic prompt assembly and Groq -> Gemini generation with fallback.

Final Prompt = Organization Prompt + Customer Metadata + Chat History + Latest Message
"""

from __future__ import annotations

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
from app.services import sales_policy

logger = logging.getLogger(__name__)

# Reasoning models spend part of this budget thinking before emitting text, so
# it must comfortably exceed the length of the reply we actually want.
MAX_OUTPUT_TOKENS = 2048

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
        "One to three sentences. Under 45 words. Crisp, not chatty.",
        "Answer the question they actually asked in the FIRST sentence. Propose the next "
        "step after that, never before.",
        "No preamble and no filler — skip \"great question\", \"I'd be happy to help\", "
        "\"thanks for reaching out\" and restating their question back at them.",
        "Never paste catalogue or website copy. Name the product, give its price, and say "
        "one useful thing about it in your own words.",
        "Sound like a real person who works here, not a script or a chatbot.",
        "Never invent a product, price, size or promise that is not in the business rules.",
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
            f"Write the reply in {name}. If the customer writes in another "
            f"language, answer in theirs, but default to {name}."
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

    sections.append(f"=== LATEST CUSTOMER MESSAGE ===\n{latest_message}")

    # Roman Urdu is detected per message, not per organization: the same shop
    # gets English and Roman Urdu customers within the same hour.
    recent = " ".join(getattr(m, "content", "") for m in list(history)[-3:])
    if is_roman_urdu(latest_message) or is_roman_urdu(recent):
        sections.append(ROMAN_URDU_GUIDE)

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
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": MAX_OUTPUT_TOKENS},
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
) -> GenerationResult:
    """Build the prompt, try Groq, fall back to Gemini, and time both attempts."""
    prompt = build_prompt(
        organization, contact, history, latest_message, knowledge, memory_block, policy_block
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
    expects_english = (
        (getattr(organization, "default_language", None) or "en").split("-")[0].lower() == "en"
        and not is_roman_urdu(customer_said)
    )

    async def guard(text: str, call) -> str:
        """Reject a reply quoting a price the business does not actually list.

        One corrective retry, then give up rather than send a wrong number to
        a customer — a made-up price is a commitment the shop has to honour.
        """
        problems: list[str] = []

        # Only the price check is behind the price-guard flag. The handoff and
        # language checks are about what the agent is allowed to say at all,
        # not about price accuracy, and turning off price checking must not
        # quietly turn those off too.
        if settings.price_guard_enabled:
            bad = unsupported_prices(text, price_corpus)
            if bad:
                problems.append(
                    "you quoted " + ", ".join(sorted(bad)) + " which is NOT in the price "
                    "list; quote only exact figures from the price list above, or omit it"
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

        # A promised human callback is never acceptable — the agent answers now.
        handoff = sales_policy.contains_handoff(text)
        if handoff:
            problems.append(
                f'you wrote "{handoff}"; never promise that a person will follow up, '
                "answer using the facts above or say plainly what you do not know and "
                "offer the closest thing you do have"
            )

        # An English-speaking tenant whose customer wrote in English must not
        # be answered in Roman Urdu. The instruction to write in English is in
        # the prompt, but on a booking turn the catalogue is stripped out and
        # the model has drifted anyway, so the output is checked rather than
        # trusted.
        if expects_english and is_roman_urdu(text):
            problems.append(
                "you replied in Roman Urdu, but this customer wrote in English; "
                "rewrite the same reply in English"
            )

        if not problems:
            return text

        logger.warning("reply rejected (%s) — regenerating", "; ".join(problems))
        corrected = await call(
            prompt + "\n\nCORRECTION: " + "; also ".join(problems) + ". Rewrite the reply."
        )

        if unsupported_prices(corrected, price_corpus):
            raise RuntimeError("reply still quoted an unlisted price")
        if sales_policy.contains_handoff(corrected):
            raise RuntimeError("reply still promised a human follow-up")
        if booking.unverified_claims(
            corrected, appointment=appointment, cancelled=did_cancel, moved=did_move
        ):
            raise RuntimeError("reply still claimed an appointment that does not exist")
        if expects_english and is_roman_urdu(corrected):
            raise RuntimeError("reply still came back in Roman Urdu")
        return corrected

    started = time.perf_counter()
    try:
        text = await guard(await _call_groq(prompt), _call_groq)
        return GenerationResult(
            provider="groq",
            text=text,
            prompt_used=prompt,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fallback_used=False,
        )
    except Exception as groq_error:  # noqa: BLE001 - any Groq failure triggers fallback
        logger.warning("Groq generation failed, falling back to Gemini: %s", groq_error)

        fallback_started = time.perf_counter()
        try:
            text = await guard(await _call_gemini(prompt), _call_gemini)
            return GenerationResult(
                provider="gemini",
                text=text,
                prompt_used=prompt,
                latency_ms=int((time.perf_counter() - fallback_started) * 1000),
                fallback_used=True,
                error=f"groq: {groq_error}",
            )
        except Exception as gemini_error:  # noqa: BLE001 - both providers down
            logger.error("Gemini fallback also failed: %s", gemini_error)
            return GenerationResult(
                provider="none",
                # Never a handoff promise: the caller composes this from the
                # knowledge actually retrieved for this question.
                text=last_resort or sales_policy.deterministic_reply({}, [], organization),
                prompt_used=prompt,
                latency_ms=int((time.perf_counter() - started) * 1000),
                fallback_used=True,
                error=f"groq: {groq_error} | gemini: {gemini_error}",
            )


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
