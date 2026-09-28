"""Working out what to quote, before the model is asked to say it.

The agent used to be handed the retrieved passages of a price list and left to
do the rest: find the product, notice that cable is sold by the 100 m coil and
not by the metre, multiply by the quantity, add delivery. It could not do that
reliably, and the price guard - rightly - refused any figure it could not find
written in the document. So "10 panels at PKR 31,800" was refused because
PKR 318,000 is not in the price list, the retry was refused for the same
reason, and the customer got "could you tell me a little more?" after twenty
seconds. The guard was also blind to most prices in the first place: a table
whose header says "Unit Price (PKR)" writes each row as a bare "31,800", and
only amounts with a currency written in front of them were ever counted.

So the arithmetic moves here, where it is deterministic:

  1. `read_items`   Every priced product the business has written down - the
                    rows of a price table, and priced lines of plain text - as
                    structured items: name, SKU, specification, the unit it is
                    sold in, what one of those units contains, and its price.
                    Columns are recognised by what their headers mean, not by
                    position, so any shop's table works.
  2. `read_request` What the customer asked for: the quantities, the units
                    they asked in, and the specifications that pick a product
                    ("4mm", "550W"), told apart from the quantities.
  3. `quote`        The matching items and the sums: packs needed when they
                    asked in metres and it is sold in coils, quantity times
                    price, a subtotal. Where the unit they asked in is not one
                    the business sells in, that is said, with what it does sell.

The result is handed to the model as figures it may quote, handed to the guard
as figures it may accept, and - if both providers are down - sent as the reply
itself. Nothing here is specific to one business or one trade.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

# ------------------------------------------------------------------ money
CURRENCIES = {
    "pkr": "PKR", "rs": "PKR", "rs.": "PKR", "₨": "PKR", "rupees": "PKR",
    "usd": "USD", "$": "USD", "us$": "USD",
    "aed": "AED", "dhs": "AED", "sar": "SAR", "qar": "QAR", "omr": "OMR",
    "kwd": "KWD", "bhd": "BHD", "gbp": "GBP", "£": "GBP", "eur": "EUR", "€": "EUR",
    "inr": "INR", "₹": "INR", "try": "TRY", "ngn": "NGN", "zar": "ZAR",
}
_CURRENCY_WORD = r"(?:PKR|Rs\.?|₨|USD|US\$|\$|AED|Dhs|SAR|QAR|OMR|KWD|BHD|GBP|£|EUR|€|INR|₹|TRY|NGN|ZAR)"
_NUMBER = r"\d[\d,]*(?:\.\d+)?"
MONEY_BEFORE = re.compile(rf"({_CURRENCY_WORD})\s*({_NUMBER})", re.IGNORECASE)
MONEY_AFTER = re.compile(rf"({_NUMBER})\s*({_CURRENCY_WORD})(?![a-z])", re.IGNORECASE)


def to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal((raw or "").replace(",", "").strip().rstrip("."))
    except (InvalidOperation, AttributeError):
        return None


def currency_of(text: str) -> str | None:
    """The currency a piece of text names, if it names exactly one kind."""
    found = {
        CURRENCIES.get(m.group(0).lower().rstrip("."), m.group(0).upper())
        for m in re.finditer(_CURRENCY_WORD, text or "", re.IGNORECASE)
    }
    return found.pop() if len(found) == 1 else None


def money(amount: Decimal, currency: str | None) -> str:
    """PKR 318,000 / USD 12.50 - whole amounts without decimals."""
    value = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{value:,.0f}" if value == value.to_integral() else f"{value:,.2f}"
    return f"{currency} {text}" if currency else text


# ------------------------------------------------------------------ units
# What a customer can ask in, folded to one name per family, with the factor
# that turns it into the base unit of that family.
MEASURES: dict[str, tuple[str, Decimal]] = {
    "m": ("m", Decimal(1)), "meter": ("m", Decimal(1)), "meters": ("m", Decimal(1)),
    "metre": ("m", Decimal(1)), "metres": ("m", Decimal(1)), "mtr": ("m", Decimal(1)),
    "mtrs": ("m", Decimal(1)), "ft": ("m", Decimal("0.3048")), "feet": ("m", Decimal("0.3048")),
    "foot": ("m", Decimal("0.3048")), "yard": ("m", Decimal("0.9144")), "yards": ("m", Decimal("0.9144")),
    "kg": ("kg", Decimal(1)), "kgs": ("kg", Decimal(1)), "kilo": ("kg", Decimal(1)),
    "kilos": ("kg", Decimal(1)), "kilogram": ("kg", Decimal(1)), "kilograms": ("kg", Decimal(1)),
    "ton": ("kg", Decimal(1000)), "tons": ("kg", Decimal(1000)), "tonne": ("kg", Decimal(1000)),
    "tonnes": ("kg", Decimal(1000)),
    "l": ("l", Decimal(1)), "ltr": ("l", Decimal(1)), "litre": ("l", Decimal(1)),
    "litres": ("l", Decimal(1)), "liter": ("l", Decimal(1)), "liters": ("l", Decimal(1)),
    "sqm": ("m2", Decimal(1)), "sq m": ("m2", Decimal(1)), "m2": ("m2", Decimal(1)),
    "square meters": ("m2", Decimal(1)), "square metres": ("m2", Decimal(1)),
}
MEASURE_NAME = {"m": "metre", "kg": "kg", "l": "litre", "m2": "square metre"}

# How a count is asked for when it is not the product's own noun.
COUNT_WORDS = (
    "x", "×", "pcs", "pc", "piece", "pieces", "nos", "no", "units", "unit", "qty",
    "items", "item", "numbers",
)

# Words that describe the unit something is sold in.
SALE_UNITS = (
    "coil", "roll", "box", "bag", "pack", "packet", "kit", "carton", "case", "set",
    "pair", "panel", "length", "sheet", "bundle", "drum", "tin", "bottle", "can",
    "unit", "piece", "dozen", "tray", "reel", "bar", "tube", "jar", "sack", "pallet",
    "session", "hour", "day", "month", "year", "visit", "service", "seat", "licence",
    "license", "user", "plan", "night", "person", "item", "each",
)

WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "fifty": 50, "hundred": 100, "dozen": 12,
}

# A number followed by one of these is a specification, not an amount: 4 mm²
# cable, a 550 W panel, a 32 A breaker, a 20 kg bag of adhesive.
SPEC_UNITS = {
    "mm2": "mm", "mm²": "mm", "sqmm": "mm", "sq mm": "mm", "mm": "mm", "cm": "cm",
    "in": "in", "inch": "in", "inches": "in", '"': "in", "w": "w", "watt": "w",
    "watts": "w", "kw": "kw", "kva": "kva", "a": "a", "amp": "a", "amps": "a",
    "ah": "ah", "mah": "mah", "v": "v", "volt": "v", "volts": "v", "kv": "kv",
    "gb": "gb", "tb": "tb", "mb": "mb", "hp": "hp", "lm": "lm", "k": "k", "rpm": "rpm",
    "ton": "ton", "kg": "kg", "g": "g", "ml": "ml", "l": "l", "m": "m", "ft": "ft",
    "mp": "mp", "hz": "hz", "gsm": "gsm", "seater": "seater", "way": "way", "pole": "pole",
    "core": "core", "bar": "bar", "psi": "psi", "mbps": "mbps",
}
_SPEC = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mm²|mm2|sq\s?mm|mm|cm|inches|inch|in\b|\"|kw|kva|watts|watt|w\b|"
    r"amps|amp|a\b|mah|ah\b|volts|volt|kv\b|v\b|gb\b|tb\b|mb\b|hp\b|lm\b|rpm|k\b|mp\b|hz\b|gsm|"
    r"seater|way|pole|core)",
    re.IGNORECASE,
)


def spec_tokens(text: str) -> set[tuple[str, str]]:
    """The measured specifications in a piece of text, as (number, unit) pairs.

    A fraction on its own - "3/8 copper pipe" - is a size as well: nobody
    orders three-eighths of a thing.
    """
    found = {(fraction, "fraction") for fraction in re.findall(r"(?<![\d/])(\d+/\d+)(?![\d/])", text or "")}
    for number, unit in _SPEC.findall(text or ""):
        key = SPEC_UNITS.get(unit.lower().replace(" ", ""), unit.lower())
        value = to_decimal(number)
        if value is not None:
            found.add((str(value.normalize()), key))
    return found


STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "are", "have", "has", "how",
    "what", "does", "can", "want", "need", "would", "like", "please", "this", "that",
    "them", "they", "not", "any", "all", "from", "get", "was", "there", "about",
    "much", "many", "price", "prices", "cost", "costs", "give", "tell", "quote",
    "including", "include", "incl", "delivery", "deliver", "only", "just", "some",
    "also", "per", "each", "total", "order", "buy", "sell", "sold", "these", "those",
    "which", "one", "ones", "available", "stock", "hello", "hi", "thanks", "thank",
    "rate", "rates", "offer", "send", "details", "info", "information", "kindly",
}


def _stem(word: str) -> str:
    for suffix in ("ies", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)]
            return base + "y" if suffix == "ies" else base
    return word


def words(text: str) -> set[str]:
    return {
        _stem(w)
        for w in re.findall(r"[a-z][a-z0-9\-]{2,}", (text or "").lower())
        if w not in STOPWORDS
    }


# ------------------------------------------------------------------ items
@dataclass
class Item:
    """One thing the business sells, at one price."""

    name: str
    price: Decimal
    currency: str | None = None
    sku: str | None = None
    spec: str = ""
    sale_unit: str | None = None
    # What one sale unit contains, when that is a measure: a 100 m coil is
    # (100, "m"); a 50 kg bag is (50, "kg"). None when it is simply one thing.
    content: tuple[Decimal, str] | None = None
    # A count or size stated for the sale unit without saying of what.
    pack: Decimal | None = None
    source: str = ""

    @property
    def label(self) -> str:
        detail = self.spec.split(",")[0].strip() if self.spec else ""
        name = self.name if not detail or detail.lower() in self.name.lower() else f"{self.name} {detail}"
        return f"{name} ({self.sku})" if self.sku else name

    @property
    def unit_word(self) -> str:
        return self.sale_unit or "unit"

    def sold_as(self) -> str:
        """"a 100 m coil" / "a 50 kg bag" / "a panel"."""
        if self.content:
            amount, unit = self.content
            return f"{_article(amount)}{_num(amount)} {unit} {self.unit_word}"
        return f"{_article_word(self.unit_word)} {self.unit_word}"

    def haystack_words(self) -> set[str]:
        return words(f"{self.name} {self.spec} {self.sale_unit or ''}")


def _num(value: Decimal) -> str:
    return f"{value.normalize():f}".rstrip("0").rstrip(".") if "." in f"{value:f}" else f"{value:f}"


def _article(amount: Decimal) -> str:
    text = _num(amount)
    return "an " if text.startswith("8") or text in {"11", "18"} else "a "


def _article_word(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _plural(word: str, count: Decimal | int) -> str:
    if Decimal(count) == 1:
        return word
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


# Which role a column plays, by the words in its header. Checked in this order,
# so "Unit Price (PKR)" is a price and not a unit.
COLUMN_ROLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("price", ("price", "rate", "cost", "amount", "mrp", "tariff", "fee", "charge", "rrp")),
    ("sku", ("sku", "code", "item no", "item #", "part", "ref", "article", "model no", "id")),
    ("unit", ("sale unit", "sold as", "sold by", "sold per", "uom", "unit", "per")),
    ("pack", ("pack", "length", "size", "qty per", "contents", "quantity", "pcs", "pieces", "volume", "weight")),
    ("spec", ("spec", "specification", "detail", "variant", "feature", "description", "size/spec")),
    ("name", ("product", "item", "name", "title", "service", "model", "description", "article")),
    ("stock", ("stock", "availability", "available", "in stock")),
)


def _roles(header: list[str]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for index, cell in enumerate(header):
        lowered = cell.lower()
        for role, keys in COLUMN_ROLES:
            if role in roles:
                continue
            if any(key in lowered for key in keys):
                roles[role] = index
                break
    # A lone "description" column is the name; with a name column, it is detail.
    if "name" not in roles and "spec" in roles:
        roles["name"] = roles.pop("spec")
    return roles


def _content_from(pack_cell: str, spec: str, unit: str | None) -> tuple[tuple[Decimal, str] | None, Decimal | None]:
    """What one sale unit contains: "100" + "…100 m coil" is (100, "m")."""
    pack = None
    measured = re.search(r"(\d+(?:\.\d+)?)\s*([a-zA-Z²]+)", pack_cell or "")
    if measured and measured.group(2).lower() in MEASURES:
        family, factor = MEASURES[measured.group(2).lower()]
        return (to_decimal(measured.group(1)) * factor, family), None
    number = re.search(r"\d+(?:\.\d+)?", pack_cell or "")
    if number:
        pack = to_decimal(number.group(0))

    # The specification often says what the pack number is of: "100 m coil",
    # "4 m length", "50 kg bag". Prefer the one that agrees with the pack cell.
    candidates = []
    for amount, measure in re.findall(
        r"(\d+(?:\.\d+)?)\s*(m|metres?|meters?|mtrs?|ft|feet|kg|kgs|l|ltrs?|litres?|liters?)\b",
        spec or "",
        re.IGNORECASE,
    ):
        family, factor = MEASURES[measure.lower()]
        candidates.append((to_decimal(amount) * factor, family))
    for candidate in candidates:
        if pack is not None and candidate[0] == pack:
            return candidate, None
    # One measure beside the sale unit word ("20 kg bag") is what the unit holds.
    if unit and candidates and len(candidates) == 1:
        if re.search(rf"\d+(?:\.\d+)?\s*\w+\s+{re.escape(unit)}\b", spec or "", re.IGNORECASE):
            return candidates[0], None
    if pack is not None and pack == 1:
        pack = None
    return None, pack


def _is_header(line: str) -> bool:
    """A row of column names: no figures in it, and a price and a name column.

    Figures are the tell. "Charger included" in a product row contains the
    word "charge", and read as a header it cut the rest of the table off.
    """
    if re.search(r"\d", line):
        return False
    roles = _roles([cell.strip() for cell in line.split("|")])
    return "price" in roles and "name" in roles


def _table_items(lines: list[str], currency: str | None, source: str) -> list[Item]:
    header = [cell.strip() for cell in lines[0].split("|")]
    roles = _roles(header)
    if "price" not in roles or "name" not in roles:
        return []
    header_currency = currency_of(header[roles["price"]]) or currency
    items = []
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != len(header):
            continue
        cell = lambda role: cells[roles[role]] if role in roles else ""  # noqa: E731
        amount = re.search(_NUMBER, cell("price"))
        price = to_decimal(amount.group(0)) if amount else None
        if price is None or price <= 0 or not cell("name"):
            continue
        unit = (cell("unit") or "").strip().lower() or None
        if unit in {"each", "ea", "pc", "pcs", "nos", "no"}:
            unit = "unit"
        spec = cell("spec")
        content, pack = _content_from(cell("pack"), spec, unit)
        items.append(
            Item(
                name=cell("name"),
                price=price,
                currency=currency_of(cell("price")) or header_currency,
                sku=cell("sku") or None,
                spec=spec,
                sale_unit=unit,
                content=content,
                pack=pack,
                source=source,
            )
        )
    return items


# A priced phrase in running text: "Classic manicure: AED 90", "Haircut AED 120",
# "Colour from AED 350 per session".
_PRICED_PHRASE = re.compile(
    rf"(?P<name>[A-Za-z][^:\n;|]{{1,80}}?)\s*(?:[:\-–—=]|\bis\b|\bat\b|\bfor\b|\bfrom\b)?\s*"
    rf"(?P<money>{_CURRENCY_WORD}\s*{_NUMBER}|{_NUMBER}\s*{_CURRENCY_WORD})"
    rf"(?:\s*(?:/|per|a|an)\s*(?P<unit>[a-z]{{2,12}}))?",
    re.IGNORECASE,
)


def _text_items(text: str, currency: str | None, source: str) -> list[Item]:
    items = []
    for segment in re.split(r"[\n;•]|(?<=[.!?])\s+", text or ""):
        segment = segment.strip(" -*\t")
        if not segment or "|" in segment:
            continue
        found = list(_PRICED_PHRASE.finditer(segment))
        # Several prices in one sentence are usually a rule ("PKR 2,500 below
        # PKR 100,000"), not a product each.
        if len(found) != 1:
            continue
        match = found[0]
        name = re.sub(r"^(?:and|or|the|our|a)\s+", "", match.group("name").strip(" :-–—,."), flags=re.I)
        if len(words(name)) == 0 or re.search(r"\b(order|orders|above|below|over|under|minimum|fee|charge|discount|delivery)\b", name, re.I):
            continue
        amount = re.search(_NUMBER, match.group("money"))
        price = to_decimal(amount.group(0)) if amount else None
        if not price or price <= 0:
            continue
        unit = (match.group("unit") or "").lower() or None
        content = None
        if unit in MEASURES:
            family, factor = MEASURES[unit]
            content, unit = (Decimal(1) * factor, family), MEASURE_NAME.get(family, family)
        items.append(
            Item(
                name=name,
                price=price,
                currency=currency_of(match.group("money")) or currency,
                sale_unit=unit,
                content=content,
                source=source,
            )
        )
    return items


def read_items(documents: Iterable[tuple[str, str]], default_currency: str | None = None) -> list[Item]:
    """Every priced item in these (source, text) pairs, de-duplicated.

    Tables are read by their header; a block of " | " rows without a header
    with a price column is left to the text reader, which only takes lines
    with exactly one price in them.
    """
    items: list[Item] = []
    for source, text in documents:
        currency = currency_of(text) or default_currency
        blocks = re.split(r"\n\s*\n", text or "")
        for block in blocks:
            lines = [line for line in block.splitlines() if line.strip()]
            table = [line for line in lines if line.count("|") >= 1]
            if len(table) >= 2 and len(table) >= len(lines) // 2:
                # Tables can repeat their header part-way down; start fresh
                # at every row that reads like one.
                start = 0
                for index, line in enumerate(table):
                    if index > start and _is_header(line):
                        items += _table_items(table[start:index], currency, source)
                        start = index
                items += _table_items(table[start:], currency, source)
            else:
                items += _text_items(block, currency, source)

    unique: dict[tuple, Item] = {}
    for item in items:
        key = (
            (item.sku or "").lower() or (item.name.lower(), item.spec.lower()),
            item.price,
        )
        unique.setdefault(key, item)
    return list(unique.values())


# ------------------------------------------------------------------ requests
@dataclass
class Wanted:
    """One thing the customer asked about, and how much of it."""

    text: str
    quantity: Decimal | None = None
    # A measure they asked in - "20 meters" - as (amount in base unit, family).
    measure: tuple[Decimal, str] | None = None
    # The word they counted in - "panels", "boxes" - when they counted.
    counted_as: str | None = None


_QUANTITY = re.compile(
    rf"(?<![\w.,])(?P<n>\d+(?:\.\d+)?|{'|'.join(WORD_NUMBERS)})\s*"
    rf"(?:(?P<measure>{'|'.join(sorted((re.escape(k) for k in MEASURES), key=len, reverse=True))})\b"
    rf"|(?P<times>x|×|pcs|pc|pieces|piece|nos|units|qty)\b"
    rf"|(?P<noun>(?:of\s+(?:the|these|those|your)\s+)?(?:[a-z0-9\-²]+\s+){{0,3}}[a-z]+))",
    re.IGNORECASE,
)


def read_request(message: str, nouns: set[str]) -> list[Wanted]:
    """The things asked for in one message, each with its quantity if given.

    `nouns` are the words the catalogue's products and sale units are called,
    so "10 solar panels" counts panels while "10 years warranty" counts nothing.
    A specification ("4mm", "550W") is never read as an amount.
    """
    text = (message or "").replace("×", " x ")
    clauses = [c.strip() for c in re.split(r"[;\n]|,\s*(?=\d|a\b|an\b|one\b)|\band\s+(?=\d)", text) if c.strip()]
    wanted: list[Wanted] = []
    for clause in clauses or [text]:
        found = None
        for match in _QUANTITY.finditer(clause):
            raw = match.group("n").lower()
            number = Decimal(WORD_NUMBERS[raw]) if raw in WORD_NUMBERS else to_decimal(raw)
            if number is None or number <= 0:
                continue
            start, end = match.span()
            # Part of a fraction or a size like 600 × 600 is not an amount.
            if clause[max(0, start - 1):start] == "/" or clause[end - len(match.group(0)) + len(match.group("n")):][:1] == "/":
                continue
            # A number glued to a spec unit is a specification: 4mm, 550W, 32A.
            if _SPEC.match(clause[start:]) and not match.group("measure"):
                continue
            if raw in {"a", "an", "one"} and not (match.group("noun") or match.group("times")):
                continue
            # Money is not a quantity.
            before = clause[max(0, start - 5):start]
            if re.search(_CURRENCY_WORD + r"\s*$", before, re.IGNORECASE):
                continue
            if match.group("measure"):
                family, factor = MEASURES[match.group("measure").lower()]
                # "4 m" right after a spec word like "cable" is still a length.
                found = Wanted(clause, number, (number * factor, family))
                break
            if match.group("times"):
                found = Wanted(clause, number)
                break
            noun_words = [w for w in re.findall(r"[a-z]+", (match.group("noun") or "").lower())]
            counted = next((w for w in reversed(noun_words) if _stem(w) in nouns), None)
            if counted and raw not in {"a", "an"} or (counted and raw in {"a", "an"} and _stem(counted) in set(SALE_UNITS)):
                found = Wanted(clause, number, counted_as=counted)
                break
        if found is None:
            # "how much for 3?", "I need 12", "3 of those please": a number
            # with no noun, where the words around it say it is how many.
            bare = re.search(
                r"\b(?:for|need|want|take|order|get|buy|qty|quantity|make it)\s+(\d+)\b"
                r"(?!\s*(?:am|pm|o'?clock|days?|hours?|hrs?|weeks?|months?|years?|%|:|th\b|st\b|nd\b|rd\b))"
                r"|\b(\d+)\s+(?:of (?:them|those|these)|please|pls)\b",
                clause,
                re.IGNORECASE,
            )
            if bare:
                number = to_decimal(bare.group(1) or bare.group(2))
                if number and number > 0:
                    found = Wanted(clause, number)
        wanted.append(found or Wanted(clause))
    return wanted


# ------------------------------------------------------------------ matching
def score(item: Item, text: str) -> float:
    """How well one item answers one piece of what the customer wrote.

    A specification the customer named is decisive both ways: asking for
    550 W and finding 550 W is strong, finding 585 W instead rules it out.
    """
    asked_specs = spec_tokens(text)
    item_specs = spec_tokens(f"{item.name} {item.spec}")
    total = 0.0
    if item.sku and re.search(rf"\b{re.escape(item.sku.lower())}\b", text.lower()):
        total += 10
    for number, unit in asked_specs:
        same_unit = {n for n, u in item_specs if u == unit}
        if number in same_unit:
            total += 3
        elif same_unit:
            return 0.0
    asked = words(text)
    name_hits = asked & words(item.name)
    detail_hits = (asked & item.haystack_words()) - name_hits
    total += 1.5 * len(name_hits) + 0.5 * len(detail_hits)
    if not name_hits and total < 10:
        return 0.0
    return total


def best(items: list[Item], text: str) -> list[Item]:
    """The items this text is about: one when it is clear, several when not."""
    scored = sorted(((score(item, text), item) for item in items), key=lambda p: p[0], reverse=True)
    scored = [(s, item) for s, item in scored if s > 0]
    if not scored:
        return []
    top = scored[0][0]
    return [item for s, item in scored if s >= top - 0.01][:6]


# ------------------------------------------------------------------ rules
# What a customer asks about besides the goods, and the words a business uses
# when it writes the rule down.
RULE_TOPICS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "delivery": (
        ("deliver", "delivery", "shipping", "ship", "courier", "freight", "dispatch", "postage"),
        ("deliver", "shipping", "courier", "freight", "postage"),
    ),
    "discount": (
        ("discount", "offer", "deal", "bulk", "wholesale", "trade price", "cheaper", "% off"),
        ("discount", "% off", "percent off"),
    ),
    "tax": (("tax", "gst", "vat", "inclusive", "exclusive"), ("tax", "gst", "vat")),
    "minimum": (("minimum", "moq", "min order"), ("minimum order", "minimum", "moq")),
}


def _sentences(text: str) -> list[str]:
    return [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+|\n+", text or "")
        if len(s.strip()) > 12 and "|" not in s
    ]


# A sentence about another subject that happens to mention this one - "original
# delivery charges are non-refundable" is a returns rule, not a delivery charge.
RULE_ELSEWHERE = ("refund", "return", "restocking", "warranty", "exchange", "cancel")


def _is_rule(topic: str, sentence: str, low: str) -> bool:
    """Whether this sentence states the rule, rather than mentioning the subject."""
    if any(word in low for word in RULE_ELSEWHERE):
        return False
    if topic == "delivery":
        return bool(amounts(sentence)) or re.search(r"\bfree\b", low) is not None
    if topic == "discount":
        return "%" in sentence or "percent" in low
    return bool(amounts(sentence)) or "%" in sentence


def rules_for(message: str, texts: Iterable[tuple[str, str]], limit: int = 2) -> list[str]:
    """The business's own sentences about what the customer asked besides price.

    "Including delivery?" is answered from the sentence that states the
    delivery charge - quoted, not paraphrased, so a threshold is never
    rounded into a different one. Only sentences with a figure or a
    percentage in them: the rule, not the paragraph about it.
    """
    lowered = (message or "").lower()
    wanted = [topic for topic, (asked, _) in RULE_TOPICS.items() if any(w in lowered for w in asked)]
    found: list[str] = []
    for topic in wanted:
        written = RULE_TOPICS[topic][1]
        count = 0
        for _, text in texts:
            for sentence in _sentences(text):
                low = sentence.lower()
                if any(w in low for w in written) and _is_rule(topic, sentence, low):
                    if sentence not in found:
                        found.append(sentence)
                        count += 1
                if count >= limit:
                    break
            if count >= limit:
                break
    return found


# ------------------------------------------------------------------ quoting
@dataclass
class Line:
    item: Item
    quantity: Decimal | None = None
    units: Decimal | None = None
    total: Decimal | None = None
    note: str = ""


@dataclass
class Quote:
    """What the price list says about what was asked, worked out."""

    asked: str
    lines: list[Line] = field(default_factory=list)
    # Several products answer the same request; the customer has to pick.
    options: list[tuple[Wanted, list[Item]]] = field(default_factory=list)
    unmatched: list[Wanted] = field(default_factory=list)
    catalogue_size: int = 0
    # The business's own sentences about delivery, discounts or tax, when the
    # customer asked about them.
    rules: list[str] = field(default_factory=list)

    @property
    def currency(self) -> str | None:
        found = {line.item.currency for line in self.lines if line.item.currency}
        return found.pop() if len(found) == 1 else None

    @property
    def subtotal(self) -> Decimal | None:
        totals = [line.total for line in self.lines if line.total is not None]
        if len(totals) < 2 or len(totals) != len(self.lines) or self.options:
            return None
        return sum(totals, Decimal(0))

    def empty(self) -> bool:
        return not self.lines and not self.options

    @property
    def order_value(self) -> Decimal | None:
        """What the goods come to: the subtotal, or the one line's total."""
        if self.subtotal is not None:
            return self.subtotal
        if len(self.lines) == 1 and not self.options:
            return self.lines[0].total
        return None

    def figures(self) -> set[Decimal]:
        """Every amount this quote states, for the guard to accept."""
        found: set[Decimal] = set()
        for line in self.lines:
            found.add(line.item.price)
            if line.total is not None:
                found.add(line.total)
        for _, items in self.options:
            for item in items:
                found.add(item.price)
        if self.subtotal is not None:
            found.add(self.subtotal)
        return found

    def quantities(self) -> set[Decimal]:
        found = {line.quantity for line in self.lines if line.quantity}
        found |= {line.units for line in self.lines if line.units}
        return found

    # ----------------------------------------------------------- for the model
    def prompt_block(self) -> str:
        if self.empty():
            return ""
        out = ["=== PRICE FACTS (worked out from this business's own price list) ==="]
        for line in self.lines:
            out.append("- " + _describe_line(line))
        for wanted, items in self.options:
            asked = f" for {_num(wanted.quantity)} {wanted.counted_as or ''}".rstrip() if wanted.quantity else ""
            out.append(f"- Several products match \"{wanted.text}\"{asked}; list them and ask which one:")
            for item in items:
                each = f"{money(item.price, item.currency)} per {item.unit_word}"
                extra = ""
                if wanted.quantity and not wanted.measure:
                    extra = f"; {_num(wanted.quantity)} × {money(item.price, item.currency)} = {money(item.price * wanted.quantity, item.currency)}"
                out.append(f"    * {item.label}{' — ' + item.spec if item.spec else ''}: {each}{extra}")
        if self.subtotal is not None:
            out.append(
                f"- Subtotal of the lines above: {money(self.subtotal, self.currency)}, before any "
                "tax, delivery charge or discount."
            )
        if self.rules:
            out.append("- What this business's own documents say about what they asked (quoted):")
            out += [f'    "{rule}"' for rule in self.rules]
            if self.order_value is not None:
                out.append(
                    f"  Apply these to an order value of {money(self.order_value, self.currency or self.lines[0].item.currency)} "
                    "and show the sum."
                )
        out.append(
            "How to use these facts:\n"
            "- Quote these figures exactly as written. Do not work out any other product price.\n"
            "- Sell only in the units listed. Never divide a pack, coil, box or bag price into a "
            "price per metre, per piece or per kg that is not listed.\n"
            "- If the customer asked in a unit that is not how it is sold, say plainly how it IS "
            "sold and what that costs, and how many of those cover what they asked for. Do not "
            "ask them to be more specific instead.\n"
            "- Delivery charges, discounts and taxes come only from the rules in the knowledge "
            "above. If you apply one, show the sum (for example subtotal + delivery = total) so "
            "every figure can be checked."
        )
        return "\n".join(out)

    # ----------------------------------------------------------- when nobody can
    def reply(self) -> str:
        """The answer itself, for when neither model is reachable."""
        if self.empty():
            return ""
        out: list[str] = []
        for line in self.lines:
            out.append(_describe_line(line, for_customer=True))
        for wanted, items in self.options:
            out.append("We have these:")
            for item in items:
                bit = f"• {item.label}: {money(item.price, item.currency)} per {item.unit_word}"
                if wanted.quantity and not wanted.measure:
                    bit += f" ({_num(wanted.quantity)} = {money(item.price * wanted.quantity, item.currency)})"
                out.append(bit)
        if self.subtotal is not None:
            out.append(f"Together that is {money(self.subtotal, self.currency)} before tax and delivery.")
        # Quoted as written rather than applied: with no model there is nobody
        # to read "within Lahore" and decide whether it covers this order.
        for rule in self.rules:
            out.append(rule)
        out.append("Which would you like?" if self.options else "Shall I put that together for you?")
        return "\n".join(out)


def _describe_line(line: Line, for_customer: bool = False) -> str:
    item = line.item
    each = money(item.price, item.currency)
    head = f"{item.label}: {each} per {item.unit_word}"
    if item.content:
        head = f"{item.label}: sold as {item.sold_as()} at {each}"
    if line.note and for_customer:
        return f"{line.note}"
    if line.note:
        return f"{head}. {line.note}"
    if line.quantity and line.total is not None:
        count = _num(line.units or line.quantity)
        return f"{head}. {count} {_plural(item.unit_word, line.units or line.quantity)} × {each} = {money(line.total, item.currency)}."
    return f"{head}."


def _line_for(item: Item, wanted: Wanted) -> Line:
    each = money(item.price, item.currency)
    if wanted.measure:
        asked_amount, family = wanted.measure
        asked_text = f"{_num(wanted.quantity)} {_unit_as_typed(wanted)}"
        if item.content and item.content[1] == family:
            per_unit = item.content[0]
            units = Decimal(math.ceil(asked_amount / per_unit))
            total = units * item.price
            name = MEASURE_NAME.get(family, family)
            if asked_amount % per_unit == 0:
                note = (
                    f"{item.label} is sold as {item.sold_as()} at {each}. {asked_text} is "
                    f"{_num(units)} {_plural(item.unit_word, units)}: {_num(units)} × {each} = "
                    f"{money(total, item.currency)}."
                )
            else:
                note = (
                    f"We don't sell {item.label} by the {name}; it comes as {item.sold_as()} at "
                    f"{each}. For {asked_text} you would need {_num(units)} "
                    f"{_plural(item.unit_word, units)}"
                    + (f" ({_num(units)} × {each} = {money(total, item.currency)})" if units > 1 else "")
                    + f", which is {money(total, item.currency)}."
                )
            return Line(item, wanted.quantity, units, total, note)
        # Asked in a measure the listing does not state it in: say how it is sold.
        return Line(
            item,
            wanted.quantity,
            None,
            None,
            f"{item.label} is sold per {item.unit_word} at {each}; there is no per-"
            f"{MEASURE_NAME.get(family, family)} price in the price list, so {asked_text} "
            f"cannot be priced as asked.",
        )
    if wanted.quantity:
        total = wanted.quantity * item.price
        return Line(item, wanted.quantity, wanted.quantity, total)
    return Line(item)


def _unit_as_typed(wanted: Wanted) -> str:
    match = re.search(
        rf"{re.escape(_num(wanted.quantity))}\s*([a-z]+(?:\s[a-z]+)?)", wanted.text.lower()
    )
    if match and match.group(1).split()[0] in MEASURES:
        return match.group(1).split()[0]
    return MEASURE_NAME.get(wanted.measure[1], "") if wanted.measure else ""


def quote(message: str, items: list[Item], context: str = "") -> Quote:
    """What these items say about this message.

    `context` is the conversation just before it, used only when the message
    names nothing on its own - "how much for 10?" after a question about
    panels is about panels.
    """
    result = Quote(asked=message, catalogue_size=len(items))
    if not items:
        return result
    nouns = set()
    for item in items:
        nouns |= words(item.name)
        if item.sale_unit:
            nouns.add(_stem(item.sale_unit))
    nouns |= {_stem(unit) for unit in SALE_UNITS}

    for wanted in read_request(message, nouns):
        matches = best(items, wanted.text)
        if not matches and context:
            matches = best(items, f"{context} {wanted.text}")
        if not matches:
            if wanted.quantity:
                result.unmatched.append(wanted)
            continue
        if len(matches) == 1:
            result.lines.append(_line_for(matches[0], wanted))
        else:
            result.options.append((wanted, matches))
    return result


# ------------------------------------------------------------------ the guard
def amounts(text: str) -> set[Decimal]:
    """Every amount written with a currency next to it, either side."""
    found = set()
    for match in MONEY_BEFORE.finditer(text or ""):
        value = to_decimal(match.group(2))
        if value is not None:
            found.add(value)
    for match in MONEY_AFTER.finditer(text or ""):
        value = to_decimal(match.group(1))
        if value is not None:
            found.add(value)
    return found


def percentages(text: str) -> set[Decimal]:
    return {to_decimal(p) for p in re.findall(r"(\d+(?:\.\d+)?)\s*%", text or "")} - {None}


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= Decimal("0.5")


def unexplained(
    reply: str,
    listed: set[Decimal],
    quantities: set[Decimal],
    rates: set[Decimal],
) -> set[Decimal]:
    """Amounts in the reply that nothing the business wrote accounts for.

    An amount is accounted for when it is listed or was worked out above, or
    when it follows by one step from those: a listed price times a quantity
    the customer asked for, a listed percentage of an amount in the same
    reply, or the sum or difference of two amounts in the same reply. So a
    total can be shown with its working, and an invented figure standing on
    its own cannot.
    """
    stated = amounts(reply)
    accepted = {value for value in stated if any(_close(value, known) for known in listed)}
    pending = stated - accepted
    changed = True
    while pending and changed:
        changed = False
        pool = listed | accepted
        for value in list(pending):
            ok = any(_close(value, a * q) for a in pool for q in quantities if q > 1)
            if not ok:
                ok = any(
                    _close(value, a * r / 100)
                    or _close(value, a - a * r / 100)
                    or _close(value, a + a * r / 100)
                    for a in pool
                    for r in rates
                )
            if not ok:
                in_reply = accepted | (stated & listed)
                ok = any(
                    _close(value, a + b) or _close(value, abs(a - b))
                    for a in in_reply
                    for b in in_reply
                )
            if ok:
                accepted.add(value)
                pending.discard(value)
                changed = True
    return pending


def asked_quantities(texts: Iterable[str]) -> set[Decimal]:
    """Plain numbers a customer wrote that are not specifications or money."""
    found: set[Decimal] = set()
    for text in texts:
        cleaned = _SPEC.sub(" ", text or "")
        cleaned = MONEY_BEFORE.sub(" ", cleaned)
        for raw in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", cleaned):
            value = to_decimal(raw)
            if value and value < 100000:
                found.add(value)
    return found


def as_dict(result: Quote) -> dict[str, Any]:
    """What the sandbox shows about how a question was read."""
    return {
        "lines": [
            {
                "item": line.item.label,
                "quantity": _num(line.quantity) if line.quantity else None,
                "units": _num(line.units) if line.units else None,
                "sold_as": line.item.sold_as(),
                "price": money(line.item.price, line.item.currency),
                "total": money(line.total, line.item.currency) if line.total is not None else None,
            }
            for line in result.lines
        ],
        "options": [[item.label for item in items] for _, items in result.options],
        "subtotal": money(result.subtotal, result.currency) if result.subtotal is not None else None,
        "catalogue_size": result.catalogue_size,
    }


# ------------------------------------------------------------------ one turn
@dataclass
class Turn:
    """Everything worked out for one customer message."""

    quote: Quote
    items: list[Item]
    # Every amount written anywhere in the business's own documents - the
    # delivery threshold three paragraphs from the table included, which is
    # not necessarily in the passages retrieved for this message.
    written: set[Decimal] = field(default_factory=set)

    @property
    def prices(self) -> set[Decimal]:
        """Every figure the guard may accept as written by the business."""
        return {item.price for item in self.items} | self.quote.figures() | self.written

    def prompt_block(self) -> str:
        return self.quote.prompt_block()

    def reply(self) -> str:
        return self.quote.reply()


def _part(title: str) -> int:
    found = re.search(r"\((\d+)/\d+\)\s*$", title or "")
    return int(found.group(1)) if found else 0


async def for_turn(db, organization, message: str, history: Iterable[Any] = ()) -> Turn:
    """Read this business's priced items and work out what `message` asks for.

    Read from what is already stored - uploaded documents, imported products
    and the "What you sell" description - so a price list uploaded before this
    existed is understood without uploading it again. Passages of one file are
    put back together in order first: a table sits inside one passage, but the
    rules around it do not.
    """
    from sqlalchemy import select

    from app.models import KnowledgeDocument

    rows = (
        await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.organization_id == organization.id)
        )
    ).scalars().all()

    by_source: dict[str, list[Any]] = {}
    texts: list[tuple[str, str]] = []
    for row in rows:
        # An imported product (a WhatsApp or Shopify catalogue entry) carries
        # its price as a field. A price list uploaded as "product" does not,
        # and is read like any other document.
        price = (row.attributes or {}).get("price") if row.doc_type == "product" else None
        if price:
            currency = (row.attributes or {}).get("currency") or ""
            texts.append((row.source or row.title, f"{row.title}: {currency} {price}"))
            continue
        by_source.setdefault(row.source or row.title, []).append(row)
    for source, parts in by_source.items():
        parts.sort(key=lambda row: (_part(row.title), row.created_at or 0))
        texts.append((source, "\n\n".join(part.content or "" for part in parts)))

    rules = getattr(organization, "product_rules", None) or ""
    if rules.strip():
        texts.append(("What you sell", rules))

    currency = getattr(organization, "default_currency", None)
    items = read_items(texts, currency)

    # What they said just before, for "and how much for 10?" after naming one.
    earlier = " ".join(
        getattr(m, "content", "") or ""
        for m in list(history)[-4:]
        if str(getattr(m, "sender", "")).lower() == "user"
    )
    written: set[Decimal] = set()
    for _, text in texts:
        written |= amounts(text)
    result = quote(message, items, context=earlier)
    if not result.empty():
        result.rules = rules_for(message, texts)
    return Turn(result, items, written)
