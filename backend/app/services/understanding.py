"""Understanding, done by the model; checking, done by code.

Two things here used to be a growing set of pattern rules - reading an owner's
price list, and reading what a customer asked for - and every new client's
wording found a hole in them. Both are language, which is what the model is
for. So the model does them, in structured form, and the code does what code
is good at: checking every answer against the record before it is used.

* **Reading a document** (once, at upload): the model returns the products and
  the rules as JSON. Every price must be a number written in the file, every
  name must be made of words in the file, every rule must be a sentence in the
  file with its figures in it. Anything else is dropped, and said so.

* **Reading a message** (every turn): the model returns what was asked for as
  order lines pointing at catalogue ids. An id must exist, a quantity must be
  a number the customer actually wrote, a unit must be one the product can be
  counted in. The arithmetic after that is `offers`' - the model never adds up.

When the model is not reachable, or its answer does not survive the checks,
the old pattern reader in `offers` is the fallback - no longer the main path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import httpx

from app.config import settings
from app.services import offers

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


# ------------------------------------------------------------------ the call
def groq_reasoning(model: str) -> dict:
    """Extra request fields for a reasoning model on Groq, or none.

    gpt-oss thinks before it writes, and the thinking counts against
    max_tokens and against the reply deadline. A sales reply needs little of
    it; left at the default, a busy prompt can think its way past both.
    """
    return {"reasoning_effort": "low"} if "gpt-oss" in (model or "") else {}


# How long a refusal for being too busy (429/503) may be waited out before the
# same key is asked again, and how many times.
#
# Each key used to get exactly one try. A document upload sends its reading and
# its study at once, several requests in the same second, and a free Groq key
# answers a burst like that with 429 - so the first refusal ended the call, the
# page said "the AI was not available", and the file was read by the pattern
# reader alone. The limit resets within seconds; waiting for it is the fix.
BUSY_RETRIES = 2
BUSY_WAIT_CAP_SECONDS = 20.0


def _busy_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before asking again: the server's own figure if given."""
    header = response.headers.get("retry-after") or ""
    try:
        wait = float(header)
    except ValueError:
        wait = 2.0 * 2**attempt
    return max(0.5, min(wait, BUSY_WAIT_CAP_SECONDS))


async def _post_json(
    provider: str,
    keys: list[str],
    send,
    read,
    timeout: float,
) -> dict | None:
    """Ask each key in turn, waiting out "too busy", until one gives an object.

    A failure with one key - busy, refused, timed out, garbled - moves on to
    the next rather than ending the call: one key over its limit is not the
    AI being unavailable. Returns None only when every key has failed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    for index, key in enumerate(keys, start=1):
        for attempt in range(BUSY_RETRIES + 1):
            left = deadline - loop.time()
            if left <= 0:
                logger.warning("structured %s call ran out of time", provider)
                return None
            try:
                async with httpx.AsyncClient(timeout=left) as client:
                    response = await send(client, key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("structured %s call failed (key %d): %s", provider, index, exc)
                break
            if response.status_code in (429, 503):
                wait = _busy_wait(response, attempt)
                if attempt < BUSY_RETRIES and wait < deadline - loop.time():
                    logger.info(
                        "structured %s call busy (key %d, %s); asking again in %.1fs",
                        provider, index, response.status_code, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning("structured %s call still busy (key %d)", provider, index)
                break
            if response.status_code >= 400:
                # Worth reading in the log: a wrong model name or a dead key
                # looks exactly like "not available" on the page.
                logger.warning(
                    "structured %s call refused (key %d): %s %s",
                    provider, index, response.status_code, response.text[:300],
                )
                break
            try:
                found = _as_object(read(response.json()))
            except Exception as exc:  # noqa: BLE001
                logger.warning("structured %s answer unreadable (key %d): %s", provider, index, exc)
                break
            if found is not None:
                return found
            logger.warning("structured %s answer was not a JSON object (key %d)", provider, index)
            break
    return None


async def _groq_json(prompt: str, timeout: float) -> dict | None:
    model = settings.understanding_model or settings.groq_model

    def send(client, key):
        return client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "max_tokens": 4096,
                **groq_reasoning(model),
            },
        )

    return await _post_json(
        "Groq",
        settings.groq_api_keys,
        send,
        lambda body: body["choices"][0]["message"]["content"],
        timeout,
    )


async def _gemini_json(prompt: str, timeout: float) -> dict | None:
    def send(client, key):
        return client.post(
            GEMINI_URL.format(model=settings.gemini_model),
            params={"key": key},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "maxOutputTokens": 8192,
                },
            },
        )

    return await _post_json(
        "Gemini",
        settings.gemini_api_keys,
        send,
        lambda body: "".join(p.get("text", "") for p in body["candidates"][0]["content"]["parts"]),
        timeout,
    )


def _as_object(text: str) -> dict | None:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


async def structured(prompt: str, timeout: float) -> dict | None:
    """A JSON object from the first provider that gives one in time, or None."""
    try:
        return await asyncio.wait_for(_structured(prompt, timeout), timeout)
    except asyncio.TimeoutError:
        logger.info("structured call ran out of time after %.1fs", timeout)
        return None


async def _structured(prompt: str, timeout: float) -> dict | None:
    # With Gemini behind it, Groq waiting out its limit must leave Gemini time
    # to answer: both share the one timeout the caller gave.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    if settings.groq_api_keys:
        share = timeout * 0.75 if settings.gemini_api_keys else timeout
        found = await _groq_json(prompt, share)
        if found is not None:
            return found
    if settings.gemini_api_keys:
        return await _gemini_json(prompt, max(0.5, deadline - loop.time()))
    return None


# ------------------------------------------------------------------ helpers
def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _numbers_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for raw in re.findall(offers._NUMBER, text or ""):
        value = offers.to_decimal(raw)
        if value is not None:
            found.add(value)
    return found


def _plain_words(text: str) -> set[str]:
    return offers.words(text)


def _clean(value: Any, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", offers._plain(text or "")).strip().lower()


# ------------------------------------------------------------ reading a file
EXTRACT_PROMPT = """You are reading a business's own document to build its price list.
Return ONLY a JSON object:

{{"items": [
   {{"name": "product or service name, as written",
     "sku": "code if written, else null",
     "details": "size / variant / specification as written, else \\"\\"",
     "price": number,
     "currency": "PKR/USD/AED/... if written, else null",
     "sold_as": "the unit one price buys, as written: coil, bag, pack, box, kg, month, head, session, item...",
     "holds": {{"amount": number, "unit": "m|kg|g|l|ml"}} or null,
     "pack_count": number or null,
     "priced_per_measure": true/false,
     "starting_price": true/false}}
 ],
 "rules": [
   {{"topic": "delivery|discount|payment|returns|minimum|tax|other",
     "sentence": "the sentence copied EXACTLY from the document",
     "min_order": number or null,
     "max_order": number or null,
     "percent": number or null,
     "fee": number or null,
     "free": true/false,
     "place": "where it applies, as written (e.g. within Karachi, rest of Pakistan), else \\"\\""}}
 ]}}

How to read it:
- One item per product or service that has its own price. Copy names and prices exactly;
  never compute, round or convert a price. Never invent an item.
- "holds": what one sale unit contains when that is a measure ("100 m coil" -> 100 m,
  "50 kg bag" -> 50 kg). "pack_count": how many pieces one sale unit contains
  ("pack of 10 pens" -> 10, "ream of 500 sheets" -> 500). Leave both null when neither is written.
- "priced_per_measure": true only when the price is a rate per kg / per metre / per litre,
  so any amount can be bought ("Rs 180/kg").
- "starting_price": true when written as "from", "starting at", "starts at".
- Rules: every sentence that states delivery charges, discounts, payment terms, returns,
  minimum orders or taxes. For a sentence with several bands, one rule per band, each with
  the same sentence. min_order / max_order are the order values the band applies between.
- A price in a rule sentence ("delivery is PKR 250 below PKR 3,000") is a rule, not an item.

The document:
<<<
{text}
>>>"""


def _item_from(raw: dict, index: int, text: str, numbers: set[Decimal], vocabulary: set[str],
               currency: str | None) -> tuple[dict | None, str | None]:
    """One item the model read, checked against the file. (item, or why not)."""
    name = _clean(raw.get("name"), 160)
    price = _decimal(raw.get("price"))
    if not name:
        return None, "no name"
    if price is None or price <= 0:
        return None, f"{name}: no price"
    if price not in numbers:
        return None, f"{name}: {price} is not a price written in the file"
    name_words = _plain_words(name)
    if name_words and len(name_words & vocabulary) < max(1, round(len(name_words) * 0.6)):
        return None, f"{name}: the name is not in the file"
    sku = _clean(raw.get("sku"), 60) or None
    if sku and _normal(sku) not in _normal(text):
        sku = None
    unit = _clean(raw.get("sold_as"), 30).lower() or None
    holds = raw.get("holds") if isinstance(raw.get("holds"), dict) else None
    content = None
    if holds:
        amount = _decimal(holds.get("amount"))
        measure = _clean(holds.get("unit"), 10).lower()
        if amount and amount > 0 and measure in offers.MEASURES and amount in numbers:
            family, factor = offers.MEASURES[measure]
            content = [str(amount * factor), family]
    pack = _decimal(raw.get("pack_count"))
    if pack is not None and (pack <= 1 or pack not in numbers):
        pack = None
    per_measure = bool(raw.get("priced_per_measure"))
    if per_measure and unit in offers.MEASURES:
        family, factor = offers.MEASURES[unit]
        content = [str(factor), family]
        unit = offers.MEASURE_NAME.get(family, family)
    elif per_measure:
        per_measure = False
    item_currency = _clean(raw.get("currency"), 8).upper() or None
    item_currency = offers.CURRENCIES.get((item_currency or "").lower(), item_currency) or currency
    return {
        "id": f"p{index}",
        "name": name,
        "sku": sku,
        "details": _clean(raw.get("details"), 200),
        "price": str(price),
        "currency": item_currency,
        "sold_as": unit,
        "holds": content,
        "pack": str(pack) if pack is not None else None,
        "per_measure": per_measure,
        "starting": bool(raw.get("starting_price")),
    }, None


def _find_sentence(sentence: str, text: str) -> str | None:
    """The sentence as the file writes it, or the file's closest one."""
    wanted = _normal(sentence)
    if not wanted:
        return None
    if wanted in _normal(text):
        return _clean(sentence, 600)
    best, best_score = None, 0.0
    asked = _plain_words(sentence)
    for candidate in offers._sentences(text):
        have = _plain_words(candidate)
        if not asked or not have:
            continue
        score = len(asked & have) / len(asked | have)
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 0.7 else None


RULE_TOPICS = {"delivery", "discount", "payment", "returns", "minimum", "tax", "other"}


def _rule_from(raw: dict, text: str) -> tuple[dict | None, str | None]:
    topic = _clean(raw.get("topic"), 20).lower()
    if topic not in RULE_TOPICS:
        topic = "other"
    sentence = _find_sentence(_clean(raw.get("sentence"), 600), text)
    if not sentence:
        return None, f"a {topic} rule whose sentence is not in the file"
    in_sentence = _numbers_in(sentence)
    rule = {"topic": topic, "sentence": sentence, "place": _clean(raw.get("place"), 60)}
    for key in ("min_order", "max_order", "percent", "fee"):
        value = _decimal(raw.get(key))
        if value is not None and value not in in_sentence:
            # A figure the sentence does not contain is the model's, not the
            # business's: the rule is kept as a sentence to quote, not applied.
            value = None
        rule[key] = str(value) if value is not None else None
    rule["free"] = bool(raw.get("free")) and "free" in sentence.lower()
    if rule["place"] and _normal(rule["place"]).replace("the ", "") not in _normal(sentence):
        rule["place"] = ""
    return rule, None


def _chunks(text: str, size: int = 12000) -> list[str]:
    blocks = re.split(r"\n\s*\n", text or "")
    out, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) > size:
            out.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        out.append(current)
    return out


def from_reader(source: str, text: str, currency: str | None) -> dict:
    """The fallback: what the pattern reader finds, in the same shape."""
    items = []
    for index, item in enumerate(offers.read_items([(source, text)], currency), start=1):
        items.append({
            "id": f"p{index}",
            "name": item.name,
            "sku": item.sku,
            "details": item.spec,
            "price": str(item.price),
            "currency": item.currency,
            "sold_as": item.sale_unit,
            "holds": [str(item.content[0]), item.content[1]] if item.content else None,
            "pack": str(item.pack) if item.pack else None,
            "per_measure": item.by_measure,
            "starting": item.starting,
        })
    rules = []
    for tier in offers.read_tiers([(source, text)]):
        rules.append({
            "topic": tier.topic,
            "sentence": tier.sentence,
            "place": tier.condition,
            "min_order": str(tier.low) if tier.low is not None else None,
            "max_order": str(tier.high) if tier.high is not None else None,
            "percent": str(tier.percent) if tier.percent is not None else None,
            "fee": str(tier.fee) if tier.fee is not None else None,
            "free": tier.free,
        })
    return {"items": items, "rules": rules, "dropped": [], "read_by": "reader"}


async def read_document(source: str, text: str, currency: str | None = None) -> dict:
    """The products and rules in one file: read by the model, checked by code.

    Returns {"items", "rules", "dropped", "read_by"}. Falls back to the
    pattern reader when no model answers, or when it found nothing the checks
    let through while the reader did.
    """
    currency = offers.currency_of(text) or currency
    fallback = from_reader(source, text, currency)
    if not (settings.groq_api_keys or settings.gemini_api_keys) or not (text or "").strip():
        return fallback

    parts = _chunks(text)
    answers = await asyncio.gather(
        *(structured(EXTRACT_PROMPT.format(text=part), settings.extraction_timeout_seconds) for part in parts)
    )
    if all(answer is None for answer in answers):
        return fallback

    numbers = _numbers_in(text)
    vocabulary = _plain_words(text)
    items: list[dict] = []
    rules: list[dict] = []
    dropped: list[dict] = []
    seen: set[tuple] = set()
    for answer in answers:
        for raw in (answer or {}).get("items") or []:
            if not isinstance(raw, dict):
                continue
            item, why = _item_from(raw, len(items) + 1, text, numbers, vocabulary, currency)
            if item is None:
                dropped.append({"kind": "item", "why": why})
                continue
            key = (item["name"].lower(), (item["sku"] or "").lower(), item["price"], item["details"].lower())
            if key not in seen:
                seen.add(key)
                items.append(item)
        for raw in (answer or {}).get("rules") or []:
            if not isinstance(raw, dict):
                continue
            rule, why = _rule_from(raw, text)
            if rule is None:
                dropped.append({"kind": "rule", "why": why})
            elif rule not in rules:
                rules.append(rule)

    if not items and fallback["items"]:
        # The model read nothing the checks accept, the reader read something:
        # the reader's reading is the better one to quote from.
        fallback["dropped"] = dropped
        return fallback
    return {"items": items, "rules": rules, "dropped": dropped, "read_by": "model"}


# ------------------------------------------------------------ into offers
def as_items(rows: Iterable[dict], source: str) -> list[offers.Item]:
    """Stored catalogue rows as the items the arithmetic works on."""
    out = []
    for row in rows:
        price = _decimal(row.get("price"))
        if price is None:
            continue
        holds = row.get("holds")
        content = None
        if isinstance(holds, (list, tuple)) and len(holds) == 2 and _decimal(holds[0]):
            content = (_decimal(holds[0]), str(holds[1]))
        item = offers.Item(
            name=row.get("name") or "",
            price=price,
            currency=row.get("currency"),
            sku=row.get("sku") or None,
            spec=row.get("details") or "",
            sale_unit=(row.get("sold_as") or None),
            content=content,
            pack=_decimal(row.get("pack")),
            source=source,
            by_measure=bool(row.get("per_measure")),
            starting=bool(row.get("starting")),
        )
        item.catalogue_id = f"{source}#{row.get('id')}"
        out.append(item)
    return out


def as_tiers(rows: Iterable[dict], currency: str | None) -> list[offers.Tier]:
    """Stored delivery and discount rules as the tiers `apply_tiers` works on."""
    tiers = []
    for row in rows:
        topic = row.get("topic")
        if topic not in ("delivery", "discount"):
            continue
        low, high = _decimal(row.get("min_order")), _decimal(row.get("max_order"))
        percent, fee = _decimal(row.get("percent")), _decimal(row.get("fee"))
        free = bool(row.get("free"))
        if low is None and high is None:
            continue
        if topic == "discount" and percent is None:
            continue
        if topic == "delivery" and fee is None and not free:
            continue
        tiers.append(offers.Tier(
            topic, low, high, percent=percent, fee=fee, free=free,
            currency=offers.currency_of(row.get("sentence") or "") or currency,
            condition=row.get("place") or "", sentence=row.get("sentence") or "",
        ))
    return tiers


# ------------------------------------------------------------ reading a message
READ_PROMPT = """You read one customer message for a shop and say what it asks for.
Return ONLY a JSON object:

{{"lines": [{{"product": "id from the list", "quantity": number or null,
              "counted_in": "unit" | "pieces" | "kg" | "g" | "m" | "l" | "ml" | "day" | "week" | "month" | "year",
              "as_written": "the words the customer used for this"}}],
  "choose_between": [{{"as_written": "...", "products": ["id", "id"]}}],
  "not_stocked": ["something they asked for that is not in the list, in their words"],
  "wants_list": true/false,
  "list_filter": "what narrows it, e.g. books, pink, else \\"\\"",
  "topics": ["delivery", "discount", "payment", "returns", "minimum", "tax"],
  "place": "a city or place they named for delivery, else \\"\\"",
  "place_covered": true if the delivery terms below cover that place, false if they do not,
                   null if no place was named or the terms do not say,
  "order_value": number or null}}

Rules:
- counted_in: "unit" when they counted what one price buys (coils, bags, packs, panels, heads,
  sessions...); "pieces" when they counted the pieces INSIDE a pack or set ("20 pens" of a pack
  of 10, "1000 sheets" of a ream of 500); a measure when they asked for an amount (20 m, half kg);
  a period for a length of time (a year of a monthly plan).
- Use only ids from the product list. If what they wrote could be more than one product and
  nothing in the message decides it, put it in choose_between instead of guessing.
- quantity is only a number the customer wrote (in any language or spelling: "do kg" is 2 kg,
  "a dozen" is 12, "half kg" is 0.5 kg). A number that is part of a product name or a
  specification ("the 2027 planner", "4mm cable", "550W") is not a quantity.
- A product named with no number in an order of several things is quantity 1; asking about a
  product without ordering it is quantity null.
- wants_list: they asked what you sell / have in general, naming no product.
- order_value: an order value they named themselves ("an order worth PKR 499,999"), else null.
- topics: only what they asked about. "After delivery" as a moment to pay is payment, not delivery.

Earlier in the conversation (for "the first one", "that one", "how much for 3?"):
{context}

Delivery terms, as the business wrote them:
{terms}

Products:
{products}

The message:
{message}"""


def product_lines(items: list[offers.Item], limit: int = 120) -> tuple[str, dict[str, offers.Item]]:
    """The catalogue as the model sees it, with short ids that map back."""
    ids: dict[str, offers.Item] = {}
    lines = []
    for index, item in enumerate(items[:limit], start=1):
        key = f"P{index}"
        ids[key] = item
        bits = [item.plain_label]
        if item.spec and item.spec.lower() not in item.plain_label.lower():
            bits.append(item.spec)
        sold = f"sold as {item.sold_as()}" if (item.content or item.pack or item.sale_unit) else ""
        lines.append(f"{key} | " + " | ".join(filter(None, bits + [sold, item.priced()])))
    return "\n".join(lines), ids


def _candidates(items: list[offers.Item], message: str, context: str, limit: int = 120) -> list[offers.Item]:
    """The products worth showing the model: all of them, or the likeliest few hundred."""
    if len(items) <= limit:
        return items
    scored = sorted(
        items,
        key=lambda item: offers.score(item, f"{context} {message}"),
        reverse=True,
    )
    return scored[:limit]


def _written_numbers(message: str) -> set[Decimal]:
    """Every amount the customer could have meant, in digits or words."""
    found = _numbers_in(message)
    lowered = f" {offers._plain(message).lower()} "
    for word, value in offers.WORD_NUMBERS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            found.add(Decimal(value))
    if re.search(r"\bdozens?\b", lowered):
        found |= {Decimal(12) * n for n in (found or {Decimal(1)})} | {Decimal(12)}
    return found


PERIOD_WORDS = {"day", "week", "month", "quarter", "year"}


def _wanted(line: dict, item: offers.Item, message: str, allowed: set[Decimal]) -> offers.Wanted | None:
    quantity = _decimal(line.get("quantity"))
    words_used = _clean(line.get("as_written"), 120) or item.name
    # A number the customer did not write is the model's, not theirs. One is
    # the exception: "and the cat cafe book" in an order is one of it.
    if quantity is not None and (
        quantity <= 0 or quantity > 100000 or (quantity not in allowed and quantity != 1)
    ):
        return None
    counted = _clean(line.get("counted_in"), 20).lower()
    if quantity is None:
        return offers.Wanted(words_used)
    if counted in offers.MEASURES:
        family, factor = offers.MEASURES[counted]
        return offers.Wanted(words_used, quantity, (quantity * factor, family))
    stem = offers._stem(counted) if counted else ""
    if stem in PERIOD_WORDS:
        return offers.Wanted(words_used, quantity, counted_as=stem)
    if counted == "pieces" and item.pack and item.pack > 1:
        # Pieces inside a pack: priced in whole packs by `offers`.
        return offers.Wanted(words_used, quantity, counted_as="pieces", pieces=True)
    return offers.Wanted(words_used, quantity, counted_as=item.sale_unit)


async def read_message(
    message: str, items: list[offers.Item], context: str = "", timeout: float | None = None,
    terms: str = "",
) -> dict | None:
    """What the model says the message asks for, checked. None if nobody answered."""
    if not items or not (settings.groq_api_keys or settings.gemini_api_keys):
        return None
    from app.services import sales_policy

    # "Hi" asks for nothing: no call, and nothing to wait for.
    if sales_policy.only_greeting(message):
        return None
    shown = _candidates(items, message, context)
    listing, ids = product_lines(shown)
    answer = await structured(
        READ_PROMPT.format(
            context=context.strip() or "(nothing earlier)",
            terms=terms.strip() or "(none written)",
            products=listing,
            message=message,
        ),
        timeout or settings.understanding_timeout_seconds,
    )
    if answer is None:
        return None
    return check_reading(answer, ids, message)


def check_reading(answer: dict, ids: dict[str, offers.Item], message: str) -> dict:
    """The model's reading, with everything the record does not support removed."""
    allowed = _written_numbers(message)
    lines: list[tuple[offers.Item, offers.Wanted]] = []
    rejected: list[str] = []
    for raw in answer.get("lines") or []:
        if not isinstance(raw, dict):
            continue
        item = ids.get(str(raw.get("product") or "").strip().upper())
        if item is None:
            rejected.append(f"unknown product {raw.get('product')!r}")
            continue
        wanted = _wanted(raw, item, message, allowed)
        if wanted is None:
            rejected.append(f"a quantity for {item.name} the customer did not write")
            wanted = offers.Wanted(_clean(raw.get("as_written"), 120) or item.name)
        lines.append((item, wanted))
    choices: list[tuple[offers.Wanted, list[offers.Item]]] = []
    for raw in answer.get("choose_between") or []:
        if not isinstance(raw, dict):
            continue
        options = [ids[k] for k in (str(p).strip().upper() for p in raw.get("products") or []) if k in ids]
        if len(options) >= 2:
            choices.append((offers.Wanted(_clean(raw.get("as_written"), 120)), options[:6]))
    topics = {t for t in (answer.get("topics") or []) if t in offers.RULE_TOPICS}
    value = _decimal(answer.get("order_value"))
    if value is not None and value not in _numbers_in(message):
        value = None
    return {
        "lines": lines,
        "choices": choices,
        "not_stocked": [_clean(n, 80) for n in (answer.get("not_stocked") or []) if _clean(n, 80)][:3],
        "wants_list": bool(answer.get("wants_list")),
        "list_filter": _clean(answer.get("list_filter"), 60),
        "topics": topics,
        "place": _clean(answer.get("place"), 60),
        # Whether the written delivery terms reach that place, as the model
        # reads them: "rest of Pakistan" covers Lahore and not Dubai.
        "place_covered": answer.get("place_covered") if isinstance(answer.get("place_covered"), bool) else None,
        "order_value": value,
        "rejected": rejected,
    }
