"""Answering in the customer's language, whatever it is.

The model is told to reply in the language the customer wrote in. Two things
here make that hold beyond the instruction:

* **Scripts can be checked.** A customer who writes in Arabic, Urdu, Hindi,
  Bengali, Russian, Chinese... writes in a script, and a reply in a different
  one is wrong whatever it says. `script_of` names the script; the reply guard
  refuses a reply in the wrong one.
* **The backend's own sentences** - "I've passed it to the team", the list of
  what is sold, the answer worked out when no model is reachable - were only
  ever English. `in_customer_language` has the model rewrite them into the
  customer's language, and keeps the rewrite only if every number, price and
  link in it is exactly the original's.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from app.services import offers

logger = logging.getLogger(__name__)

# The first word of a character's Unicode name, folded to the script a person
# would name. Anything not listed counts as its own name's first word.
_SCRIPT_ALIASES = {
    "LATIN": "Latin",
    "ARABIC": "Arabic",
    "DEVANAGARI": "Devanagari",
    "BENGALI": "Bengali",
    "GURMUKHI": "Gurmukhi",
    "GUJARATI": "Gujarati",
    "TAMIL": "Tamil",
    "TELUGU": "Telugu",
    "KANNADA": "Kannada",
    "MALAYALAM": "Malayalam",
    "SINHALA": "Sinhala",
    "THAI": "Thai",
    "CYRILLIC": "Cyrillic",
    "GREEK": "Greek",
    "HEBREW": "Hebrew",
    "CJK": "Han",
    "HIRAGANA": "Japanese",
    "KATAKANA": "Japanese",
    "HANGUL": "Hangul",
    "ETHIOPIC": "Ethiopic",
    "ARMENIAN": "Armenian",
    "GEORGIAN": "Georgian",
    "KHMER": "Khmer",
    "MYANMAR": "Myanmar",
    "LAO": "Lao",
    "PERSIAN": "Arabic",
}


def scripts_in(text: str) -> dict[str, int]:
    """How many letters of each script the text has."""
    counts: dict[str, int] = {}
    for char in text or "":
        if not char.isalpha():
            continue
        try:
            first = unicodedata.name(char).split(" ")[0]
        except ValueError:
            continue
        script = _SCRIPT_ALIASES.get(first, first.title())
        counts[script] = counts.get(script, 0) + 1
    return counts


def script_of(text: str) -> str | None:
    """The script most of the letters are in, or None when there are none."""
    counts = scripts_in(text)
    if not counts:
        return None
    return max(counts.items(), key=lambda pair: pair[1])[0]


def wrong_script(customer_message: str, reply: str) -> str | None:
    """Why the reply is in the wrong script for this customer, or None.

    Only scripts are judged: Spanish and English share one, and telling them
    apart is the model's job, not this check's. A reply may carry a product
    name or a price in another script; it is the bulk of it that must match.
    """
    wrote = script_of(customer_message)
    counts = scripts_in(reply)
    total = sum(counts.values())
    if not wrote or not total:
        return None
    share = counts.get(wrote, 0) / total
    if wrote != "Latin" and share < 0.3:
        return (
            f"the customer wrote in {wrote} script and the reply is not; reply in the "
            "customer's own language and script"
        )
    if wrote == "Latin" and share < 0.5:
        return (
            "the customer wrote in Latin letters and the reply is in another script; reply "
            "in the customer's own language, in Latin letters"
        )
    return None


# Common words of English, for telling an English message from the rest.
# Everyday words of the Latin-script languages customers most often write in,
# for positive evidence that a message is not English. English is the default:
# a short or unclear message is left as it is, and a wrong "not English" only
# costs a rewrite into English, never a wrong figure.
_OTHER_LATIN = {
    # Spanish
    "el", "los", "las", "que", "y", "por", "para", "una", "uno", "quiero", "cuanto",
    "cuesta", "tienen", "hay", "hola", "gracias", "necesito", "puedo", "envio", "envios",
    # French
    "le", "les", "des", "une", "je", "vous", "est", "et", "pour", "avec", "combien",
    "bonjour", "merci", "voudrais", "coute", "livraison", "avez",
    # German
    "der", "das", "ich", "und", "ist", "nicht", "mochte", "haben", "wie", "viel", "zwei",
    "ein", "eine", "kostet", "danke", "bitte",
    # Italian
    "il", "gli", "di", "che", "voglio", "quanto", "costa", "ciao", "grazie", "vorrei",
    # Portuguese
    "eu", "quero", "custa", "obrigado", "voce", "uma", "tem", "preco",
    # Indonesian / Malay
    "saya", "mau", "berapa", "harga", "ada", "dan", "yang", "tidak", "bisa", "beli",
    "terima", "kasih",
    # Turkish
    "kadar", "bir", "istiyorum", "fiyat", "merhaba", "tesekkurler",
    # Dutch
    "ik", "wil", "hoeveel", "kost", "het", "een", "graag",
}
_ENGLISH = {
    "the", "an", "is", "are", "do", "does", "you", "your", "my", "we", "have", "i",
    "what", "how", "much", "can", "price", "and", "for", "to", "of", "it", "this", "that",
    "want", "need", "please", "hi", "hello", "thanks", "thank", "yes", "any", "show", "send",
    "cost", "deliver", "delivery", "when", "where", "which", "will", "would", "could",
    "there", "with", "from", "order", "buy", "get", "many", "some", "like", "am", "be",
    "was", "not", "at", "about", "sell", "available", "give", "me", "a", "in", "on",
}


def looks_english(message: str) -> bool:
    """English unless there is evidence otherwise: another script, Roman Urdu,
    or more everyday words of another language than of English."""
    if script_of(message) not in (None, "Latin"):
        return False
    from app.services.llm_service import is_roman_urdu

    if is_roman_urdu(message):
        return False
    tokens = re.findall(r"[a-z']+", offers._plain(message).lower())
    foreign = sum(1 for t in tokens if t in _OTHER_LATIN)
    english = sum(1 for t in tokens if t in _ENGLISH)
    return foreign == 0 or english > foreign


# A read-back, the hand-over question and the hand-over itself are fixed
# sentences the customer must be able to read, so they get longer than a
# passing reply to arrive in their language before falling back to English.
FIXED_REPLY_SECONDS = 8

TRANSLATE_PROMPT = """Rewrite the shop's message below in the same language and script the
customer wrote in. If the customer wrote Roman Urdu, write Roman Urdu. Keep every number,
price, currency, product name, link and emoji exactly as it is, and keep line breaks.
Do not add or remove anything. Return ONLY: {{"text": "..."}}

The customer wrote:
{customer}

The shop's message:
{text}"""


def _figures(text: str) -> list[str]:
    """Every number, without the punctuation that happens to follow it.

    "PKR 350." at the end of a sentence is the same 350 as "PKR 350 hai" in
    the middle of one. Reading the full stop as part of the number made every
    rewrite that moved a price off the end of its sentence look like a changed
    price, and the customer got the English back.
    """
    return sorted(re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""))


def _links(text: str) -> list[str]:
    return sorted(link.rstrip(".,;:!?)") for link in re.findall(r"https?://\S+", text or ""))


async def in_customer_language(text: str, customer_message: str, timeout: float = 4) -> str:
    """`text` in the customer's language, or `text` unchanged.

    Unchanged when the customer wrote English, when no model answers in time,
    or when the rewrite changed a single figure or link - a translated reply
    that says a different price is worse than an English one that says the
    right one.
    """
    if not text or looks_english(customer_message):
        return text
    from app.services import understanding

    with understanding.output_budget(understanding.MESSAGE_OUTPUT_TOKENS):
        answer = await understanding.structured(
            TRANSLATE_PROMPT.format(customer=customer_message[:500], text=text), timeout
        )
    rewritten = str((answer or {}).get("text") or "").strip()
    # Each way back to English is logged with its reason: a Spanish customer
    # asked "shall I pass you to the team?" in English (October 3, R21) left
    # nothing to say which of these it was.
    if not rewritten:
        logger.warning("no translation within %.0fs; replying in English to a non-English message", timeout)
        return text
    if _figures(rewritten) != _figures(text) or _links(rewritten) != _links(text):
        logger.warning("translation changed a figure or a link; replying in English")
        return text
    if wrong_script(customer_message, rewritten):
        logger.warning("translation came back in the wrong script; replying in English")
        return text
    return rewritten
