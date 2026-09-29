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

import dataclasses
import itertools
import math
import re
import unicodedata
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
# Thousands written with commas, western or lakh style ("1,000,000",
# "1,04,999"), or no commas at all. "104999,1 year" in a comma-separated list
# is 104,999 followed by the next cell, not one million.
_NUMBER = r"(?:\d{1,3}(?:,\d{2,3})+(?!\d)|\d+)(?:\.\d+)?"
MONEY_BEFORE = re.compile(rf"({_CURRENCY_WORD})\s*({_NUMBER})", re.IGNORECASE)
MONEY_AFTER = re.compile(rf"({_NUMBER})\s*({_CURRENCY_WORD})(?![a-z])", re.IGNORECASE)


# "Rs. 2,500" is one price, not the end of a sentence and the start of the
# next. Every splitter keeps these together.
NOT_A_STOP = (
    r"(?<![Rr]s\.)(?<![Nn]o\.)(?<![Dd]r\.)(?<![Ss]t\.)(?<![Mm]r\.)(?<![Mm]s\.)(?<!vs\.)"
    r"(?<!approx\.)(?<!incl\.)(?<!excl\.)(?<!e\.g\.)(?<!i\.e\.)(?<!Dhs\.)"
    # Leader dots, "Keratin ........ Rs 18,000", do not end anything either.
    r"(?<!\.\.)"
)
SENTENCE_END = NOT_A_STOP + r"(?<=[.!?])\s+"


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
    "g": ("kg", Decimal("0.001")), "gm": ("kg", Decimal("0.001")), "gms": ("kg", Decimal("0.001")),
    "gram": ("kg", Decimal("0.001")), "grams": ("kg", Decimal("0.001")),
    "ml": ("l", Decimal("0.001")),
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
    "half": Decimal("0.5"), "quarter": Decimal("0.25"),
    # Roman Urdu, as customers type it: "do kg", "teen suits", "das packets".
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "panch": 5, "paanch": 5,
    "chay": 6, "chhe": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    "bees": 20, "pachas": 50, "sau": 100,
}
# Those that are also English words or too short to trust alone: they count
# only with the unit or the product straight after them, so "do kg" is two
# kilos and "do you have" is a question.
_LOOSE_NUMBERS = {"ek", "do", "teen", "char", "chaar", "panch", "paanch", "chay", "chhe",
                  "che", "saat", "aath", "nau", "das", "bees", "pachas", "sau"}

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


# Words customers use for the same thing a business wrote differently. Kept to
# the ones that are the same in every trade: who it is for, and people as a
# count. Folded before stemming, so "ladies" finds "Haircut (women)" and
# "120 guests" is counted against a price per head.
SAME_WORD = {
    "ladies": "woman", "lady": "woman", "women": "woman", "female": "woman", "females": "woman",
    "girls": "woman", "gents": "man", "men": "man", "male": "man", "males": "man",
    "boys": "man", "gentlemen": "man", "kids": "child", "kid": "child", "children": "child",
    "people": "person", "persons": "person", "guests": "person", "guest": "person",
    "pax": "person", "heads": "person", "head": "person", "attendees": "person",
    "ppl": "person", "log": "person", "bande": "person", "afraad": "person",
    "teeth": "tooth", "feet": "foot", "mice": "mouse",
}


def _stem(word: str) -> str:
    if word in SAME_WORD:
        return SAME_WORD[word]
    # "wrapped", "wrapping" and "wrap" are one word to a customer.
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            base = word[: -len(suffix)]
            return base[:-1] if len(base) > 4 and base[-1] == base[-2] else base
    if word.endswith("ies") and len(word) >= 6:
        return word[:-3] + "y"
    # "boxes", "dishes", "tomatoes" drop "es"; "cakes", "services", "sponges"
    # only drop the "s" - stripping "es" from those made "cak" and "servic",
    # which matched nothing the business wrote in the singular.
    # "-oes" needs a word in front of it: "tomatoes" is "tomato", but "shoes"
    # is "shoe", and stripping it left "sho", which matched no shoe anyone
    # sells.
    if word.endswith("es") and len(word) >= 5 and (
        re.search(r"(s|x|z|ch|sh)es$", word) or re.search(r"\w{3}oes$", word)
    ):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) >= 4:
        return word[:-1]
    return word


def _plain(text: str) -> str:
    """"Café" and "cafe" are the same word to a customer typing on a phone."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")


def words(text: str) -> set[str]:
    return {
        _stem(w)
        # Three letters and up, or a short code with a digit in it: "A4",
        # "A5", "4K" name the product as surely as a word does.
        for w in re.findall(r"[a-z][a-z0-9\-]{2,}|\b[a-z]\d{1,2}\b|\b\d[a-z]\b", _plain(text).lower())
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
    # Priced by the measure - "Rs 180/kg", "Rs 2,400 per kg" - so any amount
    # can be bought: half a kilo is half the price, not a whole kilo.
    by_measure: bool = False
    # "From $9,500", "starting at Rs 8,000": the lowest it costs, not what it
    # costs. Said that way, never as a fixed price or a total.
    starting: bool = False

    @property
    def label(self) -> str:
        detail = self.spec.split(",")[0].strip() if self.spec else ""
        name = self.name if not detail or detail.lower() in self.name.lower() else f"{self.name} {detail}"
        return f"{name} ({self.sku})" if self.sku else name

    @property
    def plain_label(self) -> str:
        """The label without the stock code, for a customer to read."""
        detail = self.spec.split(",")[0].strip() if self.spec else ""
        return self.name if not detail or detail.lower() in self.name.lower() else f"{self.name} {detail}"

    @property
    def unit_word(self) -> str:
        return self.sale_unit or "unit"

    def sold_as(self) -> str:
        """"a 100 m coil" / "a 50 kg bag" / "a panel"."""
        if self.content:
            amount, unit = self.content
            return f"{_article(amount)}{_num(amount)} {unit} {self.unit_word}"
        if self.pack and self.pack > 1:
            return f"{_article_word(self.unit_word)} {self.unit_word} of {_num(self.pack)}"
        return f"{_article_word(self.unit_word)} {self.unit_word}"

    def haystack_words(self) -> set[str]:
        return words(f"{self.name} {self.spec} {self.sale_unit or ''}")

    def priced(self) -> str:
        """"PKR 2,500", "PKR 180 per kg", "from USD 9,500": the price as written.

        "Per unit" is said only when the business named a unit; a haircut
        is not sold "per unit". A starting price is always said as one.
        """
        text = money(self.price, self.currency)
        if self.sale_unit:
            text += f" per {self.sale_unit}"
        return f"from {text}" if self.starting else text


def _num(value: Decimal) -> str:
    return f"{value.normalize():f}".rstrip("0").rstrip(".") if "." in f"{value:f}" else f"{value:f}"


def _article(amount: Decimal) -> str:
    text = _num(amount)
    return "an " if text.startswith("8") or text in {"11", "18"} else "a "


def _article_word(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


_IRREGULAR = {"tooth": "teeth", "person": "people", "child": "children", "man": "men",
              "woman": "women", "foot": "feet"}


def _plural(word: str, count: Decimal | int) -> str:
    if Decimal(count) == 1:
        return word
    if word in _IRREGULAR:
        return _IRREGULAR[word]
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
    for amount, measure, per in re.findall(
        r"(\d+(?:\.\d+)?)\s*(m|metres?|meters?|mtrs?|ft|feet|kg|kgs|l|ltrs?|litres?|liters?)\b"
        r"(\s+(?:per|a|each)\s+\w+|\s+each)?",
        spec or "",
        re.IGNORECASE,
    ):
        # "5 m per roll" in a set of 5 rolls is one roll, not the set: the
        # 5 beside it in the pack column counts rolls, not metres.
        if per and not (unit and re.search(rf"\b{re.escape(unit)}", per, re.IGNORECASE)):
            continue
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
    rf"(?P<name>[A-Za-z][^:\n;|]{{1,80}}?)"
    rf"(?P<lead>(?:\s*(?:[:\-–—=.…]+|\bis\b|\bat\b|\bfor\b|\bfrom\b|\bstarting\b|\bstarts\b|\bonly\b|\bjust\b))*)\s*"
    rf"(?P<money>{_CURRENCY_WORD}\s*{_NUMBER}|{_NUMBER}\s*{_CURRENCY_WORD})"
    rf"(?:\s*(?:/|per|a|an)\s*(?P<unit>[a-z]{{2,12}}))?",
    re.IGNORECASE,
)


# Words a rule is written in, which a product's name is not: "Home service is
# available in DHA for an extra Rs 2,000", "Otherwise Rs 150".
_RULE_WORDS = re.compile(
    r"\b(order|orders|above|below|over|under|minimum|discount|delivery|shipping|"
    r"extra|additional|otherwise|surcharge|available|advance|deposit|refund|cancell?ation|"
    r"penalty|late|if|when|unless)\b",
    re.IGNORECASE,
)
# "Hair colour — starting from", "Full bathroom remodel starts at".
_STARTING_TAIL = re.compile(
    r"[\s—–\-:.,]*\b(?:starting|starts|start|from|at|only|just|approx|approximately|price|rate|cost)\b[\s.]*$",
    re.IGNORECASE,
)
# "Basmati rice (5 kg bag)", "Milk 1 litre": what one of it holds.
_SIZE_IN_NAME = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|kgs|g|gm|gms|grams?|l|ltrs?|litres?|liters?|ml)\b"
    r"\s*(bag|pack|packet|box|bottle|tin|jar|can|sack|pouch|carton|tub)?",
    re.IGNORECASE,
)


def _text_items(text: str, currency: str | None, source: str) -> list[Item]:
    items = []
    # A line can hold two services side by side: "Manicure - Rs 1,500 |
    # Pedicure - Rs 2,000". Tables are read before this, so a bar here
    # separates items.
    for segment in re.split(r"[\n;•|]|" + SENTENCE_END, text or ""):
        segment = segment.strip(" -*\t")
        if not segment:
            continue
        found = list(_PRICED_PHRASE.finditer(segment))
        # Several prices in one sentence are usually a rule ("PKR 2,500 below
        # PKR 100,000"), not a product each.
        if len(found) != 1:
            continue
        match = found[0]
        name = re.sub(r"^(?:and|or|the|our|a)\s+", "", match.group("name").strip(" :-–—,.…"), flags=re.I)
        starting = bool(re.search(r"\b(from|starting|starts|start)\b", match.group("lead") or "", re.I))
        while (tail := _STARTING_TAIL.search(name)):
            starting = starting or bool(re.search(r"\b(starting|starts|start|from)\b", tail.group(0), re.I))
            name = name[: tail.start()].strip(" :-–—,.…")
        # A sentence is not a product name; a product name is a few words.
        if len(words(name)) == 0 or _RULE_WORDS.search(name) or len(name.split()) > 7:
            continue
        # Nor is the tail of one. "...free for orders of PKR 5,000 or more"
        # left "ders of", which was listed to customers as something sold.
        # Nothing is sold under a name ending in a joining word.
        if re.search(r"\b(?:of|for|to|and|or|with|from|in|on|at|by|per|than|is|are)$", name, re.I):
            continue
        amount = re.search(_NUMBER, match.group("money"))
        price = to_decimal(amount.group(0)) if amount else None
        if not price or price <= 0:
            continue
        unit = (match.group("unit") or "").lower() or None
        content = None
        by_measure = False
        if unit in MEASURES:
            family, factor = MEASURES[unit]
            content, unit = (Decimal(1) * factor, family), MEASURE_NAME.get(family, family)
            by_measure = True
        elif unit is None:
            size = _SIZE_IN_NAME.search(name)
            if size and size.group(2).lower() in MEASURES:
                family, factor = MEASURES[size.group(2).lower()]
                content = (to_decimal(size.group(1)) * factor, family)
                unit = (size.group(3) or "").lower() or "pack"
        items.append(
            Item(
                name=name,
                price=price,
                currency=currency_of(match.group("money")) or currency,
                sale_unit=unit,
                content=content,
                source=source,
                by_measure=by_measure,
                starting=starting,
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

    # The same thing read twice - a table row, and a sentence that mentions its
    # price ("Gift wrapping is PKR 150 per item") - is the table row.
    tabled = {(i.name.lower(), i.price) for i in items if i.sku or i.spec}
    items = [i for i in items if i.sku or i.spec or (i.name.lower(), i.price) not in tabled]
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
    # They counted the pieces inside a pack ("20 pens" of a pack of 10), as
    # read by the model rather than guessed from the word.
    pieces: bool = False


_QUANTITY = re.compile(
    rf"(?<![\w.,])(?P<n>\d+(?:\.\d+)?|(?:{'|'.join(WORD_NUMBERS)})\b)\s*"
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
    # Sentences first: "...and 10 MCBs. Also, 20m of the 4mm cable" is two
    # requests, and read as one the MCBs were matched to the cable.
    clauses = [
        c.strip()
        for c in re.split(
            NOT_A_STOP + r"(?<=[a-z0-9)][.!?])\s+"
            r"|[;\n]|,\s*(?=\d|a\b|an\b|one\b)"
            r"|\b(?:and|aur|or|plus|&)\s+(?=\d|a\b|an\b|the\b|one\b|ek\b|do\b|teen\b|char\b|panch\b|das\b)", text, flags=re.I
        )
        if c and c.strip()
    ]
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
            # "Delivered 10 days ago" is when, not how many days of anything.
            if re.match(
                r"\s*(?:days?|weeks?|months?|years?|hours?|nights?)\s+(?:ago|back|before|earlier|later|old)\b",
                clause[start + len(match.group("n")):],
                re.IGNORECASE,
            ):
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
            if raw in _LOOSE_NUMBERS and not match.group("measure") and not match.group("times"):
                first = noun_words[0] if noun_words else ""
                if not (_stem(first) in nouns or any(_near(_stem(first), n) for n in nouns)):
                    continue
                noun_words = [first]
            # "3 packs of gel pens" counts packs; "20 gel pens" counts pens.
            leading = [w for w in noun_words if w not in {"of", "the", "these", "those", "your"}]
            if leading and _stem(leading[0]) in set(SALE_UNITS) and _stem(leading[0]) in nouns:
                counted = leading[0]
            else:
                counted = next((w for w in reversed(noun_words) if _stem(w) in nouns), None)
                # "teen kurte": an Urdu plural, one letter from the product's word.
                if counted is None:
                    counted = next(
                        (w for w in noun_words if len(w) >= 5 and any(_near(_stem(w), n) for n in nouns)),
                        None,
                    )
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
def _asked_forms(text: str) -> tuple[set[str], set[str], set[str]]:
    """What the customer typed, three ways: its words, words run together, initials.

    "hair cut" is "Haircut"; "mani pedi" is short for "Manicure" and
    "Pedicure"; "PT" is "Personal training". People type the way they talk.
    """
    plain = _plain(text or "")
    raw = [w for w in re.findall(r"[a-z][a-z0-9\-]*", plain.lower())]
    joined = {_stem(a + b) for a, b in zip(raw, raw[1:])}
    short = {w for w in raw if len(w) >= 4 and w not in STOPWORDS}
    initials = {w.lower() for w in re.findall(r"\b[A-Z]{2,4}\b", plain)}
    return joined, short, initials


def _near(a: str, b: str) -> bool:
    """One slip of the thumb apart: "chocolat", "keratine", "vanila"."""
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 5 or a[0] != b[0]:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        # One letter wrong, or two neighbours swapped.
        return len(diffs) == 1 or (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                                   and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def score(item: Item, text: str, rarity: dict[str, float] | None = None) -> float:
    """How well one item answers one piece of what the customer wrote.

    A specification the customer named is decisive both ways: asking for
    550 W and finding 550 W is strong, finding 585 W instead rules it out.
    A word few products share counts for more than one they all do: in
    "half kg vanilla cake", "vanilla" says which cake.
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
    name_words = words(item.name)
    joined, short, initials = _asked_forms(text)
    name_hits = (asked | joined) & name_words
    # "mani" for "manicure": a start of a name word, four letters or more.
    name_hits |= {w for w in name_words if w not in name_hits and any(w.startswith(p) and w != p for p in short)}
    # A typo still names the thing, when nothing else in the name was hit.
    if not name_hits:
        name_hits |= {w for w in name_words if any(_near(w, a) for a in asked)}
    detail_hits = (asked & item.haystack_words()) - name_hits
    weight = lambda w: (rarity or {}).get(w, 1.0)  # noqa: E731
    total += 1.5 * sum(weight(w) for w in name_hits) + 0.5 * len(detail_hits)
    acronym = "".join(w[0] for w in re.findall(r"[a-z]+", _plain(item.name).lower()) if w not in STOPWORDS)
    if initials and any(len(i) >= 2 and acronym.startswith(i) for i in initials):
        total += 2.0
        name_hits.add("acronym")
    if not name_hits and total < 10:
        return 0.0
    return total


def best(items: list[Item], text: str) -> list[Item]:
    """The items this text is about: one when it is clear, several when not."""
    # How rare each word is among these items' names: 1 for a word only one
    # product has, less the more of them share it.
    counts: dict[str, int] = {}
    for item in items:
        for w in words(item.name):
            counts[w] = counts.get(w, 0) + 1
    rarity = {w: 1.0 + 1.0 / n for w, n in counts.items()}
    scored = sorted(
        ((score(item, text, rarity), item) for item in items), key=lambda p: p[0], reverse=True
    )
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
        ("deliver", "delivery", "shipping", "ship", "courier", "freight", "dispatch", "postage",
         # When it comes, which is a delivery question too.
         "receive", "arrive", "reach me", "how long", "how many days", "eta", "get it by"),
        ("deliver", "shipping", "courier", "freight", "postage", "arrive", "dispatch",
         "working days", "business days"),
    ),
    "discount": (
        ("discount", "offer", "deal", "bulk", "wholesale", "trade price", "cheaper", "% off"),
        ("discount", "% off", "percent off"),
    ),
    "tax": (("tax", "gst", "vat", "inclusive", "exclusive"), ("tax", "gst", "vat")),
    "minimum": (("minimum", "moq", "min order"), ("minimum order", "minimum", "moq")),
    "payment": (
        ("pay", "payment", "paid", "credit", "advance", "cash on delivery", "cod", "deposit",
         "installment", "instalment", "upfront", "up front", "next month", "net 15", "net 30",
         "cash", "cheque", "bank transfer", "insurance", "card", "visa", "mastercard",
         "jazzcash", "easypaisa", "installments", "emi"),
        ("payment", "advance", "credit", "deposit", "cash", "installment", "instalment",
         "upfront", "net 15", "net 30", "net 7", "net 60", "cheque", "bank transfer",
         "insurance", "accept", "card", "jazzcash", "easypaisa"),
    ),
    "returns": (
        ("return", "refund", "exchange", "damaged", "defective", "faulty", "broken",
         "wrong item", "warranty", "replace"),
        ("return", "refund", "exchange", "damaged", "defective", "warranty", "restocking",
         "replaced"),
    ),
}

# Topics whose words are short enough to sit inside other words - "cod" in
# "code", "pay" in "display" - are matched as whole words.
_WHOLE_WORD_TOPICS = {"payment", "returns"}


# "I'll pay after delivery", "it was delivered 10 days ago": delivery named as
# a moment, not asked about.
_DELIVERY_AS_A_MOMENT = re.compile(
    r"\b(?:after|on|upon|before|at|until|till|cash on|pay(?:ment)? on)\s+delivery\b"
    r"|\b(?:was|were|been|got|it's|its|is)\s+delivered\b|\bdelivered\s+(?:\w+\s+){0,2}ago\b"
)


_PRICE_AS_PAYING = re.compile(
    r"\b(?:how much|what)\s+(?:do|will|would|should|shall|must)\s+(?:i|we)\s+(?:have to\s+|need to\s+)?pay\b"
)


def _asks(topic: str, lowered: str) -> bool:
    asked = RULE_TOPICS[topic][0]
    if topic == "delivery":
        lowered = _DELIVERY_AS_A_MOMENT.sub(" ", lowered)
    elif topic == "payment":
        # "How much will I pay for 10?" asks a price, not the payment terms.
        lowered = _PRICE_AS_PAYING.sub(" ", lowered)
    elif topic == "returns":
        lowered = lowered.replace("returning customer", " ")
    if topic in _WHOLE_WORD_TOPICS:
        return any(re.search(rf"\b{re.escape(w)}(?:s|es|ed|ing)?\b", lowered) for w in asked)
    return any(w in lowered for w in asked)


def _stems(text: str, drop_common: bool = False) -> set[str]:
    return {
        w[:5]
        for w in re.findall(r"[a-z]{3,}", (text or "").lower())
        if not (drop_common and w in _STOP)
    }


def _shares(stem: str, stems: set[str]) -> bool:
    """"pay" is in "payment"; "insta" is in "installing" and "installation"."""
    return any(s.startswith(stem) for s in stems)


_STOP = {
    "the", "and", "for", "you", "your", "can", "what", "how", "much", "this", "that", "with",
    "have", "need", "want", "give", "will", "would", "about", "are", "did", "does", "any",
    "all", "but", "not", "then", "them", "they", "its", "it's", "i'm", "i'll", "our", "from",
    "just", "also", "please", "tell", "make", "any", "anything", "everything", "was", "were",
    "only", "today", "now", "may", "must", "unless", "where", "which", "there", "their",
    "after", "before", "into", "over", "than", "when", "been", "being", "some", "more",
}


def _sentences(text: str) -> list[str]:
    return [
        s.strip()
        for s in re.split(SENTENCE_END + r"|\n+", text or "")
        if len(s.strip()) > 12 and "|" not in s
    ]


# A sentence about another subject that happens to mention this one - "original
# delivery charges are non-refundable" is a returns rule, not a delivery charge.
RULE_ELSEWHERE = ("refund", "return", "restocking", "warranty", "exchange", "cancel")


# A sentence that says how long delivery takes, and a question asking it.
_TAKES_TIME = re.compile(
    r"\b\d+\s*(?:[-\u2013\u2014]|to)?\s*\d*\s*(?:working |business )?(?:days?|hours?|weeks?)\b"
    r"|\bsame[- ]day\b|\bnext[- ]day\b|\bovernight\b"
)
_ASKS_WHEN = re.compile(
    r"\b(when|how long|how many days|how soon|receive|arrive|reach|eta|get it by|by (?:when|monday|"
    r"tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today)|same[- ]day|today|tomorrow)\b"
)


def _is_rule(topic: str, sentence: str, low: str) -> bool:
    """Whether this sentence states the rule, rather than mentioning the subject."""
    if topic == "returns":
        return True
    if any(word in low for word in RULE_ELSEWHERE):
        return False
    if topic == "payment":
        return True
    if topic == "delivery":
        return (
            bool(amounts(sentence))
            or re.search(r"\bfree\b", low) is not None
            # "Karachi orders arrive in 1-2 working days" is the rule for when.
            or _TAKES_TIME.search(low) is not None
        )
    if topic == "discount":
        return "%" in sentence or "percent" in low
    return bool(amounts(sentence)) or "%" in sentence


def rules_for(
    message: str, texts: Iterable[tuple[str, str]], limit: int = 2, ignore: set[str] = frozenset()
) -> list[str]:
    """The business's own sentences about what the customer asked besides price.

    "Including delivery?" is answered from the sentence that states the
    delivery charge - quoted, not paraphrased, so a threshold is never
    rounded into a different one. Only sentences with a figure or a
    percentage in them: the rule, not the paragraph about it.
    """
    wanted = [topic for topic in RULE_TOPICS if topic in topics_in(message)]
    texts = list(texts)
    found: list[str] = []
    for topic in wanted:
        written = RULE_TOPICS[topic][1]
        candidates: list[str] = []
        for _, text in texts:
            for sentence in _sentences(text):
                low = sentence.lower()
                if (
                    any(w in low for w in written)
                    and _is_rule(topic, sentence, low)
                    and sentence not in found
                    and sentence not in candidates
                    # A heading - "Returns, Exchanges & Warranty" - is not a rule.
                    and len(sentence.split()) >= 5
                ):
                    candidates.append(sentence)
        # The sentences closest to what they said, in the order written:
        # "I'm a new customer" is answered by "New customers: 50% advance",
        # not by whichever payment sentence came first.
        # Their words, weighted by how few of these sentences use them: every
        # returns sentence says "return", only one says "installing".
        # Product names are about the goods, not the terms: "cable" in an
        # order is no reason to prefer the sentence about custom-cut cable.
        asked = _stems(message, drop_common=True) - {w[:5] for w in ignore}
        stems = {c: _stems(c, drop_common=True) for c in candidates}
        rare = {
            stem: 1 / count
            for stem in asked
            if (count := sum(1 for c in candidates if _shares(stem, stems[c])))
        }

        asks_when = topic == "delivery" and bool(_ASKS_WHEN.search(message.lower()))

        def closeness(sentence: str) -> tuple[float, int]:
            low = sentence.lower()
            said = sum(weight for stem, weight in rare.items() if _shares(stem, stems[sentence]))
            # "When will I get it?" is answered by how long it takes, not what it costs.
            if asks_when and _TAKES_TIME.search(low):
                said += 10
            states = 1 if re.search(r"\d", sentence) else 0
            return (round(said, 3), sum(1 for w in written if w in low) + states)

        ranked = sorted(candidates, key=closeness, reverse=True)[:limit]
        found += [c for c in candidates if c in ranked]
    return found


# ------------------------------------------------------------------ tiers
@dataclass
class Tier:
    """One rung of a rule that depends on how big the order is.

    "Orders of PKR 500,000 or more receive 2% off" is a discount tier from
    500,000 up. "PKR 1,500 for orders from PKR 100,000 to PKR 249,999" is a
    delivery tier with both ends. Read from the business's own sentence, so
    the threshold and the rate are theirs, not the model's recollection.
    """

    topic: str  # "discount" or "delivery"
    low: Decimal | None  # applies from this amount...
    high: Decimal | None  # ...up to and including this one
    percent: Decimal | None = None
    fee: Decimal | None = None
    free: bool = False
    currency: str | None = None
    condition: str = ""  # "within Lahore": said, not decided here
    sentence: str = ""

    def covers(self, value: Decimal) -> bool:
        return (self.low is None or value >= self.low) and (self.high is None or value <= self.high)

    def describe(self) -> str:
        if self.percent is not None:
            what = f"{_num(self.percent)}% off"
        elif self.free:
            what = "free"
        else:
            what = money(self.fee, self.currency)
        span = (
            f"from {money(self.low, self.currency)} to {money(self.high, self.currency)}"
            if self.low is not None and self.high is not None
            else f"from {money(self.low, self.currency)}"
            if self.low is not None
            else f"up to {money(self.high, self.currency)}"
        )
        return f"{what} for orders {span}" + (f" {self.condition}" if self.condition else "")


_AMOUNT = rf"(?:{_CURRENCY_WORD}\s*{_NUMBER}|{_NUMBER}\s*{_CURRENCY_WORD})"


def _amount_in(text: str) -> Decimal | None:
    found = re.search(_NUMBER, text or "")
    return to_decimal(found.group(0)) if found else None


def _bounds(clause: str) -> tuple[Decimal | None, Decimal | None, list[tuple[int, int]]]:
    """The order-size range a clause states, and where its amounts sit."""
    low = high = None
    spans: list[tuple[int, int]] = []
    between = re.search(rf"(?:from|between)\s+({_AMOUNT})\s+(?:to|and|-|–)\s+({_AMOUNT})", clause, re.I)
    if between:
        low, high = _amount_in(between.group(1)), _amount_in(between.group(2))
        spans.append(between.span())
        return low, high, spans
    for match in re.finditer(
        rf"(?:(?P<up>of|over|above|exceeding|from|at least|minimum of)\s+(?P<a>{_AMOUNT})\s*(?P<more>or more|and above|or above|\+|and over|or over)?"
        rf"|(?P<down>below|under|less than|up to)\s+(?P<b>{_AMOUNT}))",
        clause,
        re.I,
    ):
        spans.append(match.span())
        if match.group("down"):
            value = _amount_in(match.group("b"))
            # "below 100,000" stops just short of it; "up to" includes it.
            high = value if match.group("down").lower() == "up to" else value - Decimal("0.01")
        else:
            value = _amount_in(match.group("a"))
            strict = match.group("up").lower() in {"over", "above", "exceeding"} and not match.group("more")
            low = value + Decimal("0.01") if strict else value
    return low, high, spans


_PLACE = re.compile(
    r"\b(within|inside|in|to|outside)\s+(?:the\s+)?((?:rest\s+of\s+|other\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
)


def _place(text: str) -> str:
    """"within Karachi", "to the rest of Pakistan": where a rule applies."""
    found = _PLACE.search(text or "")
    if not found:
        return ""
    preposition, where = found.group(1), found.group(2)
    if preposition == "to" and not where.lower().startswith(("rest", "other")):
        return ""  # "to Easypaisa" is not a place
    return f"{preposition} {'the ' if where.lower().startswith('rest') else ''}{where}"


def _places_named(condition: str) -> set[str]:
    return {
        w.lower() for w in re.findall(r"[A-Z][a-z]+", condition) if w.lower() not in {"rest", "other"}
    }


def read_tiers(texts: Iterable[tuple[str, str]]) -> list[Tier]:
    """Every order-size rule for discounts and delivery the business wrote.

    Only clauses that state both a range and exactly one rate, fee or "free"
    are read. Anything less definite is left as a sentence for the model to
    quote, because a rule read wrongly here would be applied with certainty.
    """
    tiers: list[Tier] = []
    for _, text in texts:
        currency = currency_of(text)
        for sentence in _sentences(text):
            low_sentence = sentence.lower()
            if any(word in low_sentence for word in RULE_ELSEWHERE):
                continue
            # "50% advance payment above PKR 15,000" is how to pay, not 50% off.
            about_paying = any(
                w in low_sentence for w in ("advance", "deposit", "payment", "instal", "upfront", "up front")
            ) and not ("discount" in low_sentence or re.search(r"%\s*off\b", low_sentence))
            if about_paying:
                continue
            topic = (
                "discount" if ("discount" in low_sentence or "% off" in low_sentence or re.search(r"\d\s*%", sentence))
                else "delivery" if any(w in low_sentence for w in RULE_TOPICS["delivery"][1])
                else None
            )
            if topic is None:
                continue
            # Where the whole sentence applies, when it says so before its
            # first amount: "Delivery within Karachi is PKR 250 ...".
            first_amount = re.search(_AMOUNT, sentence, re.I)
            sentence_place = _place(sentence[: first_amount.start()] if first_amount else "")
            for clause in re.split(r";|\.\s+|,\s*and\s+|\band\s+(?=free\b)", sentence):
                low, high, spans = _bounds(clause)
                if low is None and high is None:
                    continue
                # What is left once the range is taken out is the rate.
                rest = clause
                for start, end in sorted(spans, reverse=True):
                    rest = rest[:start] + " " + rest[end:]
                percents = re.findall(r"(\d+(?:\.\d+)?)\s*%", rest)
                fees = [m for m in re.finditer(_AMOUNT, rest, re.I)]
                free = re.search(r"\bfree\b", rest, re.I) is not None
                condition = _place(clause) or sentence_place
                if topic == "discount" and len(percents) == 1 and not fees:
                    tiers.append(Tier("discount", low, high, percent=to_decimal(percents[0]),
                                      currency=currency_of(clause) or currency, condition=condition, sentence=sentence))
                elif topic == "delivery" and (len(fees) == 1) != free and not percents:
                    tiers.append(Tier("delivery", low, high,
                                      fee=_amount_in(fees[0].group(0)) if fees else None, free=free,
                                      currency=currency_of(clause) or currency, condition=condition, sentence=sentence))
    return tiers


@dataclass
class Applied:
    """What the order-size rules come to for one order value."""

    value: Decimal
    currency: str | None
    discount: Tier | None = None
    next_discount: Tier | None = None
    delivery: Tier | None = None
    had_discounts: bool = False
    had_delivery: bool = False
    # Delivery that depends on where it goes, when where was not said: every
    # place's charge, rather than one picked for them.
    delivery_by_place: list[Tier] = field(default_factory=list)

    @property
    def saving(self) -> Decimal | None:
        if not self.discount:
            return None
        return (self.value * self.discount.percent / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def topics(self) -> set[str]:
        return ({"discount"} if self.had_discounts else set()) | ({"delivery"} if self.had_delivery else set())

    def figures(self) -> set[Decimal]:
        found = {self.value}
        if self.saving is not None:
            found |= {self.saving, self.value - self.saving}
        if self.next_discount and self.next_discount.low is not None:
            found |= {self.next_discount.low, self.next_discount.low - self.value}
        if self.delivery and self.delivery.fee is not None:
            found.add(self.delivery.fee)
        found |= {t.fee for t in self.delivery_by_place if t.fee is not None}
        return found

    def lines(self) -> list[str]:
        out = []
        amount = money(self.value, self.currency)
        if self.had_discounts:
            if self.discount:
                after = self.value - self.saving
                out.append(
                    f"On {amount}, the {_num(self.discount.percent)}% discount applies: "
                    f"{money(self.saving, self.currency)} off, leaving {money(after, self.currency)}."
                )
            if self.next_discount and self.next_discount.low is not None:
                short = self.next_discount.low - self.value
                what = "No discount applies yet" if not self.discount else "The next rate is not reached"
                out.append(
                    f"{what}: {_num(self.next_discount.percent)}% off starts at "
                    f"{money(self.next_discount.low, self.currency)}, which is {money(short, self.currency)} more."
                )
        if self.had_delivery and self.delivery_by_place and not self.delivery:
            each = "; ".join(
                f"{'free' if t.free else money(t.fee, self.currency)} {t.condition}".strip()
                for t in self.delivery_by_place
            )
            out.append(f"Delivery on {amount} depends on where it goes: {each}. Ask which city it is for.")
        if self.had_delivery and self.delivery:
            charge = "free" if self.delivery.free else money(self.delivery.fee, self.currency)
            out.append(
                f"Delivery on {amount}: {charge}"
                + (f" ({self.delivery.condition})" if self.delivery.condition else "")
                + "."
            )
        return out


def apply_tiers(
    value: Decimal, tiers: list[Tier], currency: str | None, topics: set[str], where: str = ""
) -> Applied | None:
    """Which discount and delivery rung an order of `value` falls on.

    `where` is what the customer has said, for delivery that is charged by
    place: "I'm in Lahore" picks the rest-of-Pakistan rate over Karachi's.
    """
    discounts = [t for t in tiers if t.topic == "discount"] if "discount" in topics else []
    deliveries = [t for t in tiers if t.topic == "delivery"] if "delivery" in topics else []
    if not discounts and not deliveries:
        return None
    applying = [t for t in discounts if t.covers(value)]
    # Rates are not added together; the best one that applies is the one.
    discount = max(applying, key=lambda t: t.percent) if applying else None
    ahead = sorted(
        (t for t in discounts if t.low is not None and t.low > value
         and (discount is None or t.percent > discount.percent)),
        key=lambda t: t.low,
    )
    covering = [t for t in deliveries if t.covers(value)]
    by_place: list[Tier] = []
    places = {t.condition for t in covering}
    delivery = covering[0] if covering else None
    if len(places) > 1 and len({(t.fee, t.free) for t in covering}) == 1:
        # Free everywhere is free: no need to ask where.
        delivery = dataclasses.replace(covering[0], condition="")
    elif len(places) > 1:
        # Several schedules, one per place. The customer's own words decide
        # which; otherwise every place's charge is given and they are asked.
        said = _plain(where).lower()
        named = [t for t in covering if _places_named(t.condition) & set(re.findall(r"[a-z]+", said))]
        elsewhere = [t for t in covering if re.search(r"\b(rest|other|outside)\b", t.condition)]
        mentions_a_place = re.search(r"\b(?:in|to|at|from)\s+[A-Z][a-z]+", _plain(where))
        if named:
            delivery = named[0]
        elif elsewhere and mentions_a_place:
            delivery = elsewhere[0]
        else:
            delivery = None
            by_place = covering
    return Applied(
        value=value,
        currency=currency,
        discount=discount,
        next_discount=ahead[0] if ahead else None,
        delivery=delivery,
        had_discounts=bool(discounts),
        had_delivery=bool(deliveries),
        delivery_by_place=by_place,
    )


def stated_value(message: str) -> Decimal | None:
    """An order value the customer names: "an order worth PKR 499,999"."""
    found = sorted(amounts(message), reverse=True)
    if found:
        return found[0]
    bare = re.search(r"\b(?:worth|total(?:s|ling)?|value of|comes to|make it|of)\s+(\d{1,3}(?:,\d{3})+|\d{4,})\b", message or "", re.I)
    return to_decimal(bare.group(1)) if bare else None


def _rule_topic(sentence: str) -> str | None:
    low = sentence.lower()
    # "Within 7 days of delivery" is a returns rule, not the delivery charge,
    # and is not answered by working the delivery charge out.
    if any(word in low for word in RULE_ELSEWHERE):
        return "returns"
    if "discount" in low or "% off" in low:
        return "discount"
    if any(w in low for w in RULE_TOPICS["delivery"][1]):
        return "delivery"
    return None


def topics_in(text: str) -> set[str]:
    lowered = (text or "").lower()
    return {topic for topic in RULE_TOPICS if _asks(topic, lowered)}


# ------------------------------------------------------------------ quoting
@dataclass
class Line:
    item: Item
    quantity: Decimal | None = None
    units: Decimal | None = None
    total: Decimal | None = None
    note: str = ""
    # Said for information and not counted: "20 m of the cable" alongside
    # "6 coils of the cable" in one order is how it is sold, not a seventh coil.
    aside: bool = False
    # What they asked for that this item is not: "fountain" in "fountain
    # pens" when the list has gel pens. The closest thing, said as such.
    differs: str = ""


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
    # Those rules worked out for this order's value, where they could be read.
    applied: Applied | None = None
    # An order value the customer named themselves - "what if I make it PKR
    # 500,000?" - which the rules were applied to instead of the goods above.
    stated: Decimal | None = None
    # Things they asked for that the list has nothing like, in their words.
    not_stocked: list[str] = field(default_factory=list)
    # Who read the message: the model ("model") or the pattern reader.
    read_by: str = "reader"
    # "What items do you have?": nothing named, so a spread of what is sold.
    overview: list[Item] = field(default_factory=list)
    overview_total: int = 0
    # A place they asked about delivering to that nothing the business wrote
    # mentions: "do you ship to Dubai?" is for a person, not for the rules
    # about Karachi.
    unknown_place: str | None = None

    @property
    def currency(self) -> str | None:
        found = {line.item.currency for line in self.lines if line.item.currency}
        return found.pop() if len(found) == 1 else None

    @property
    def subtotal(self) -> Decimal | None:
        counted = [line for line in self.lines if not line.aside]
        totals = [line.total for line in counted if line.total is not None]
        if len(totals) < 2 or len(totals) != len(counted) or self.options:
            return None
        return sum(totals, Decimal(0))

    def empty(self) -> bool:
        return (
            not self.lines and not self.options and not self.applied and not self.rules
            and not self.unknown_place and not self.overview and not self.not_stocked
        )

    @property
    def order_value(self) -> Decimal | None:
        """What the goods come to: the subtotal, or the one line's total."""
        if self.subtotal is not None:
            return self.subtotal
        counted = [line for line in self.lines if not line.aside]
        if len(counted) == 1 and not self.options:
            return counted[0].total
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
        if self.applied:
            found |= self.applied.figures()
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
            if line.differs:
                out.append(
                    f'- They asked for "{line.differs}". The price list has no such item; the '
                    "closest one is below. Say plainly that you don't have what they named, then "
                    "offer this."
                )
            out.append("- " + _describe_line(line))
        for wanted, items in self.options:
            asked = f" for {_num(wanted.quantity)} {wanted.counted_as or ''}".rstrip() if wanted.quantity else ""
            out.append(f"- Several products match \"{wanted.text}\"{asked}; list them and ask which one:")
            for item in items:
                each = item.priced()
                extra = ""
                if wanted.quantity and not wanted.measure:
                    extra = f"; {_num(wanted.quantity)} × {money(item.price, item.currency)} = {money(item.price * wanted.quantity, item.currency)}"
                out.append(f"    * {item.label}{' — ' + item.spec if item.spec else ''}: {each}{extra}")
        if self.subtotal is not None:
            out.append(
                f"- Subtotal of the lines above: {money(self.subtotal, self.currency)}, before any "
                "tax, delivery charge or discount."
            )
        if self.applied and self.applied.lines():
            if self.stated is not None:
                out.append(
                    f"- The customer named an order value of {money(self.stated, self.applied.currency)}. "
                    "Answer for that value exactly as below, not for any earlier order in the "
                    "conversation."
                )
            out.append("- The order-size rules, already applied (use these exact figures):")
            out += [f"    {line}" for line in self.applied.lines()]
        for thing in self.not_stocked:
            out.append(
                f'- They asked for "{thing}". Nothing in the price list is that. Say plainly that '
                "you don't have it, then offer the closest thing listed, if anything is close."
            )
        if self.overview:
            more = self.overview_total - len(self.overview)
            out.append(
                f"- They asked what you sell. The price list has {self.overview_total} products; "
                "a spread of them:"
            )
            out += [f"    * {item.plain_label}: {item.priced()}" for item in self.overview]
            out.append(
                "  Answer with a short overview of the kinds of things sold, name a few of "
                "these with their prices, and ask what they are looking for."
                + (f" There are {more} more not listed here; do not invent them." if more > 0 else "")
            )
        if self.unknown_place:
            out.append(
                f"- They asked about {self.unknown_place}, which none of this business's "
                "documents mention. Do not say whether it is covered: reply NEEDS_TEAM: "
                f"delivery to {self.unknown_place}."
            )
        if self.rules:
            out.append("- What this business's own documents say about what they asked (quoted):")
            out += [f'    "{rule}"' for rule in self.rules]
            if self.order_value is not None and not self.applied and self.lines:
                out.append(
                    f"  Apply these to an order value of {money(self.order_value, self.currency or self.lines[0].item.currency)} "
                    "and show the sum."
                )
            out.append(
                "  Answer from these sentences only. Do not add terms, exceptions, documents or "
                "arrangements they do not state, and do not agree to anything they rule out."
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
        if self.empty() or (self.unknown_place and not self.lines):
            return ""
        out: list[str] = []
        for thing in self.not_stocked:
            out.append(f"We don't have {thing}.")
        if self.overview:
            out.append("Here's some of what we have:")
            out += [f"• {item.plain_label}: {item.priced()}" for item in self.overview]
            more = self.overview_total - len(self.overview)
            if more > 0:
                out.append(f"…and {more} more.")
            out.append("What are you looking for?")
            return "\n".join(out)
        for line in self.lines:
            if line.differs:
                out.append(f"We don't have {line.differs}. The closest we have:")
            out.append(_describe_line(line, for_customer=True))
        for wanted, items in self.options:
            out.append("We have these:")
            labels = [item.label.split(" (")[0] for item in items]
            for item in items:
                bit = f"• {item.label}: {item.priced()}"
                if labels.count(item.label.split(" (")[0]) > 1 and item.spec:
                    # Two "Mochi Bunny Notebook A5"s: what tells them apart.
                    bit = f"• {item.label} — {item.spec}: {item.priced()}"
                if wanted.quantity and not wanted.measure:
                    bit += f" ({_num(wanted.quantity)} = {money(item.price * wanted.quantity, item.currency)})"
                out.append(bit)
        if self.subtotal is not None:
            out.append(f"Together that is {money(self.subtotal, self.currency)} before tax and delivery.")
        if self.applied:
            if self.stated is not None and not self.lines:
                out.append(f"For an order of {money(self.stated, self.applied.currency)}:")
            out += self.applied.lines()
        # A rule that could not be applied is quoted as written: with no model
        # there is nobody to read it and decide whether it covers this order.
        for rule in self.rules:
            if self.applied and _rule_topic(rule) in self.applied.topics:
                continue
            out.append(rule)
        if self.options:
            out.append("Which would you like?")
        elif any(not line.aside for line in self.lines):
            out.append("Shall I put that together for you?")
        else:
            out.append("Anything else I can check for you?")
        return "\n".join(out)


def _describe_line(line: Line, for_customer: bool = False) -> str:
    item = line.item
    each = money(item.price, item.currency)
    head = f"{item.label}: {item.priced()}"
    if item.content and not item.by_measure:
        head = f"{item.label}: sold as {item.sold_as()} at {each}"
    if line.note and for_customer:
        return f"{line.note}"
    if line.note:
        return f"{head}. {line.note}"
    if line.quantity and line.total is not None:
        count = line.units or line.quantity
        what = _plural(item.sale_unit, count) if item.sale_unit else ""
        sum_ = f"{_num(count)} {what}".strip() + f" × {each} = {money(line.total, item.currency)}"
        if item.starting:
            # A starting price times a count is where the total starts too.
            return f"{head}. {sum_} at the least; the final price depends on the job."
        return f"{head}. {sum_}."
    if item.starting:
        return f"{head} (a starting price; the final price depends on the job)."
    return f"{head}."


# Periods a plan or membership can be priced by, in days. A year of a monthly
# plan is twelve months of it.
PERIODS = {"day": 1, "week": 7, "month": 30, "quarter": 90, "year": 365}
_PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12}


def _periods(asked: str, sold: str, quantity: Decimal) -> Decimal | None:
    """How many of `sold` make `quantity` of `asked`: 1 year of "month" is 12."""
    a, b = _stem(asked), _stem(sold)
    if a == b or a not in PERIODS or b not in PERIODS:
        return None
    if a in _PERIOD_MONTHS and b in _PERIOD_MONTHS:
        return quantity * _PERIOD_MONTHS[a] / _PERIOD_MONTHS[b]
    if {a, b} <= {"day", "week"}:
        return quantity * PERIODS[a] / PERIODS[b]
    return None


def _line_for(item: Item, wanted: Wanted) -> Line:
    each = money(item.price, item.currency)
    if wanted.measure and item.by_measure and item.content and item.content[1] == wanted.measure[1]:
        # Priced by the kilo, the litre, the metre: any amount, at the rate.
        asked_amount, family = wanted.measure
        units = asked_amount / item.content[0]
        total = (units * item.price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        name = MEASURE_NAME.get(family, family)
        asked_text = f"{_num(wanted.quantity)} {_unit_as_typed(wanted)}".strip()
        note = f"{item.label} is {each} per {name}: {asked_text} × {each} = {money(total, item.currency)}."
        if asked_amount != wanted.quantity:
            note = (
                f"{item.label} is {each} per {name}: {asked_text} is {_num(units)} {name}, "
                f"{_num(units)} × {each} = {money(total, item.currency)}."
            )
        return Line(item, wanted.quantity, units, total, note)
    if wanted.quantity and wanted.counted_as and item.sale_unit:
        periods = _periods(wanted.counted_as, item.sale_unit, wanted.quantity)
        if periods is not None and periods == periods.to_integral():
            total = periods * item.price
            asked = f"{_num(wanted.quantity)} {_plural(_stem(wanted.counted_as), wanted.quantity)}"
            note = (
                f"{item.label} is {each} per {item.sale_unit}; {asked} is {_num(periods)} "
                f"{_plural(item.sale_unit, periods)}: {_num(periods)} × {each} = {money(total, item.currency)}."
            )
            return Line(item, wanted.quantity, periods, total, note)
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
    if wanted.quantity and _counts_pieces(item, wanted):
        # "20 gel pens" of a pack of 10, "2 sticker sheets" of a pack of 6:
        # they counted what is inside, and packs are what is sold.
        units = Decimal(math.ceil(wanted.quantity / item.pack))
        total = units * item.price
        asked = f"{_num(wanted.quantity)} {wanted.counted_as}"
        if wanted.quantity % item.pack == 0:
            how = f"{asked} is {_num(units)} {_plural(item.unit_word, units)}"
        else:
            how = (
                f"we don't split a {item.unit_word}, so {asked} needs {_num(units)} "
                f"{_plural(item.unit_word, units)} ({_num(units * item.pack)} in all)"
            )
        note = (
            f"{item.label} is sold as {item.sold_as()} at {each}; {how}: "
            f"{_num(units)} × {each} = {money(total, item.currency)}."
        )
        return Line(item, wanted.quantity, units, total, note)
    if wanted.quantity:
        total = wanted.quantity * item.price
        return Line(item, wanted.quantity, wanted.quantity, total)
    return Line(item)


def _counts_pieces(item: Item, wanted: Wanted) -> bool:
    """Whether they counted the pieces in a pack rather than the packs.

    "20 gel pens" when a pack holds 10 pens: the word they counted is part
    of what the product is, not the unit it is sold in, and the pack holds
    more than one.
    """
    if wanted.pieces:
        return bool(item.pack and item.pack > 1)
    if not (wanted.counted_as and item.pack and item.pack > 1):
        return False
    counted = _stem(wanted.counted_as)
    if item.sale_unit and counted == _stem(item.sale_unit):
        return False
    # The pieces are what the name is a plural of ("Gel Pens", "Sticker
    # Sheets") or what the pack number counts in the details ("500 sheets").
    # "3 washi tapes" of a "Washi Tape" set of 5 rolls counts sets.
    plural = {_stem(w) for w in re.findall(r"[a-z]+s\b", _plain(item.name).lower())}
    counted_in_spec = re.search(
        rf"\b{_num(item.pack)}\s+(?:[a-z]+\s+)?{re.escape(counted)}", _plain(item.spec).lower()
    )
    return counted in plural or bool(counted_in_spec)


def _names_the_product(item: Item, wanted: Wanted) -> bool:
    """Whether the number they typed is part of the product's name, not a count.

    The number and the word after it, as they typed them: "2027 planner" is
    written into "Sakura Days 2027 Planner", so it names it. "4 coils" is not
    written into "CopperCore XLPE Cable 4 mm2", so the 4 counts coils - which
    a bare search for "4" in the name got wrong, and stopped pricing the order.
    """
    number = _num(wanted.quantity)
    typed = re.search(
        rf"(?<![\w.,]){re.escape(number)}\s+([a-z]+)", _plain(wanted.text).lower()
    )
    return bool(typed) and f"{number} {typed.group(1)}" in _plain(item.name).lower()


_EVERYTHING = re.compile(
    r"\bwhat\b.{0,30}\b(items?|products?|things|stuff|options|services|do you (?:have|sell|offer|do|make)"
    r"|(?:have|sell|offer) you got|you (?:have|sell|offer|got))\b"
    r"|\b(catalogu?e|catalog|menu|price ?list|rate ?list|full list|your collection|your range|all (?:your )?(?:items|products))\b"
    r"|\bkya kya\b|\bsab kuch\b|\bkya milta\b|\bkya bechte\b",
    re.IGNORECASE,
)


_BROWSE_WORDS = {
    "item", "product", "thing", "stuff", "option", "service", "catalogue", "catalog", "menu",
    "range", "collection", "full", "rate", "what", "which", "do", "you", "kya", "sab", "kuch",
    "bechte", "have", "sell", "offer", "got", "show", "see", "send", "list", "available", "right",
    "now", "today", "currently", "kya", "hai", "milta", "hain", "aap", "apke", "ke", "pas",
    "your", "shop", "store", "please", "everything", "all",
}


def _asks_for_everything(message: str) -> bool:
    return bool(_EVERYTHING.search(_plain(message or "")))


def _spread(items: list[Item], limit: int = 8) -> list[Item]:
    """A few of everything rather than the first rows of the table.

    Picked one per kind of thing, by the last word of the name ("Notebook",
    "Pens", "Cake"), in the order the business listed them, then filled up.
    """
    chosen: list[Item] = []
    kinds: set[str] = set()
    for item in items:
        kind = (re.findall(r"[a-z]+", _plain(item.name).lower()) or [""])[-1]
        if kind not in kinds:
            kinds.add(kind)
            chosen.append(item)
        if len(chosen) >= limit:
            return chosen
    for item in items:
        if item not in chosen:
            chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen


def _not_this(item: Item, text: str) -> str:
    """The kind they asked for, when this item is only the same sort of thing.

    "Fountain pens" matched "Pastel Dream Gel Pens" on the word "pens". The
    word before it, which the item says nothing of, is the difference.
    """
    plain = _plain(text).lower()
    name = words(item.name)
    known = item.haystack_words() | name
    for match in re.finditer(r"\b([a-z]{4,})\s+(?=([a-z]{3,})\b)", plain):
        before, noun = match.group(1), match.group(2)
        # Only when the noun ends what they named. "standard gel pens" is not
        # a request for "standard gel": the word after is part of the name
        # as well, and this item is a gel pen.
        after = re.match(rf"{re.escape(noun)}\s+([a-z]{{3,}})\b", plain[match.end():])
        if after and _stem(after.group(1)) in known:
            continue
        if (
            _stem(noun) in name
            and _stem(before) not in known
            and before not in STOPWORDS
            and before not in _NOT_A_KIND
            and before not in MEASURES
            and before not in WORD_NUMBERS
            and not any(_near(before, k) for k in known)
            and _stem(before) not in set(SALE_UNITS)
        ):
            return f"{before} {noun}"
    return ""


# Words that come before a product noun without saying what kind it is.
_NOT_A_KIND = {
    "your", "some", "those", "these", "that", "this", "more", "many", "much", "cheap",
    "cheapest", "best", "nice", "cute", "good", "other", "same", "same", "another", "about",
    "need", "want", "have", "like", "buy", "order", "price", "cost", "sell", "show", "with",
    "pink", "blue", "green", "white", "black", "red", "purple", "yellow", "mint", "lilac",
    "little", "small", "big", "large", "mini", "new", "latest",
    # Quality and size words. "regular pens" is not a kind of pen a shop could
    # fail to stock, so it is not something to tell a customer we do not have.
    "standard", "regular", "normal", "ordinary", "usual", "plain", "simple",
    "basic", "premium", "modern", "classic", "special", "custom", "extra",
    "thick", "thin", "heavy", "light", "quick", "fast", "urgent", "proper",
}


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

    # Earlier messages decide the product only when this one asks for an
    # amount or a price without naming it - "and how much for 3?". Otherwise
    # "what about delivery?" came back quoting the cable from three turns ago.
    asks_price = re.search(r"\b(how much|price|cost|total|each|per)\b", message or "", re.I)
    seen: set[str] = set()
    requests = read_request(message, nouns)
    # "I only need 20 meters of the 4mm cable. How much?" names its product;
    # the "How much?" is about that, not about the panels asked for earlier.
    names_something = any(best(items, wanted.text) for wanted in requests)
    for wanted in requests:
        matches = best(items, wanted.text)
        if not matches and context and not names_something and (wanted.quantity or asks_price):
            matches = best(items, f"{context} {wanted.text}")
        if not matches:
            if wanted.quantity:
                result.unmatched.append(wanted)
            continue
        key = "|".join(sorted(item.label for item in matches))
        if key in seen and not wanted.quantity:
            continue
        seen.add(key)
        if len(matches) == 1 and wanted.quantity and _names_the_product(matches[0], wanted):
            # "the 2027 planner" names the Sakura Days 2027 Planner; it does
            # not ask for two thousand of them.
            wanted = Wanted(wanted.text)
        if len(matches) == 1:
            line = _line_for(matches[0], wanted)
            line.differs = _not_this(matches[0], wanted.text)
            result.lines.append(line)
        else:
            result.options.append((wanted, matches))

    # "What items do you have?", "send me your menu", "kya kya milta hai":
    # nothing named, so show a spread of what there is rather than nothing.
    if not result.lines and not result.options and _asks_for_everything(message):
        # "What do you have in pink?" - the rest of what they said narrows it.
        narrowing = words(message) - {_stem(w) for w in _BROWSE_WORDS}
        pool = items
        if narrowing:
            pool = [i for i in items if narrowing & i.haystack_words()]
        if pool:
            result.overview = _spread(pool)
            result.overview_total = len(pool)

    # "Mani pedi dono ka kitna?", "price for both": each of the matches is
    # wanted, not a choice between them.
    if result.options and re.search(r"\b(both|dono|donon|all of them|all three|each of them|sab)\b", message or "", re.I):
        for wanted, matches in result.options:
            if len(matches) <= 3:
                for item in matches:
                    result.lines.append(_line_for(item, wanted))
        result.options = [(w, m) for w, m in result.options if len(m) > 3]

    # In an order that counts things, one named without a number is one of
    # it: "2 notebooks, 25 pens and The Little Cat Café" includes the book.
    ordering = len(result.lines) > 1 and re.search(
        r"\b(total|deliver\w*|order|buy|need|want|take|send|ship)\b", message or "", re.I
    )
    if any(line.quantity for line in result.lines) or ordering:
        for line in result.lines:
            if line.quantity is None and not line.note and not line.differs:
                line.quantity = line.units = Decimal(1)
                line.total = line.item.price

    _mark_asides(result)
    return result


def _mark_asides(result: "Quote") -> None:
    """The same item asked for twice in one message, once counted in what it is
    sold as and once in a measure it is not ("6 coils of the 4mm cable ... I
    want 20m of the 4mm cable rather than a full coil"): the count is the
    order, the measure is answered with how it is sold and not added again.
    """
    counted = {line.item.label for line in result.lines if line.quantity and not line.note}
    for line in result.lines:
        if line.note and line.item.label in counted:
            line.aside = True
            item = line.item
            family = item.content[1] if item.content else None
            by_the = f" by the {MEASURE_NAME.get(family, family)}" if family else " in that measure"
            line.note = (
                f"{item.label} is sold only as {item.sold_as()} at {money(item.price, item.currency)}, "
                f"not{by_the}, so a part of one cannot be supplied on its own. It is already "
                "counted in the order above and is not added again."
            )
            line.units = None
            line.total = None


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
                # An order of four lines is totalled in one figure, so a sum
                # of several amounts already in the reply is working too.
                if not ok and 3 <= len(in_reply) <= 14:
                    terms = sorted(in_reply)
                    ok = any(
                        _close(value, sum(group, Decimal(0)))
                        for size in range(3, min(6, len(terms)) + 1)
                        for group in itertools.combinations(terms, size)
                    )
            if ok:
                accepted.add(value)
                pending.discard(value)
                changed = True
    return pending


def customer_figures(texts: Iterable[str]) -> set[Decimal]:
    """Amounts the customer wrote themselves: "an order worth PKR 499,999".

    Repeating a customer's own number back to them is not quoting a price,
    and refusing it was what sent "can I get the discount on PKR 499,999?"
    to both providers and out the other side after thirty seconds.
    """
    found: set[Decimal] = set()
    for text in texts:
        found |= amounts(text)
        cleaned = _SPEC.sub(" ", text or "")
        for raw in re.findall(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?(?![\w.])", cleaned):
            value = to_decimal(raw)
            if value:
                found.add(value)
    return found


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
                "aside": line.aside,
            }
            for line in result.lines
        ],
        "options": [[item.label for item in items] for _, items in result.options],
        "subtotal": money(result.subtotal, result.currency) if result.subtotal is not None else None,
        # The discount and delivery rules as they came out for this order.
        "rules_applied": result.applied.lines() if result.applied else [],
        # Whether the model read the message, or the pattern reader had to.
        "read_by": result.read_by,
        "not_stocked": list(result.not_stocked),
        # Payment, returns and other terms, quoted as the business wrote them.
        "terms_quoted": list(result.rules),
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


@dataclass
class Prepared:
    """Everything stored about what this business sells, loaded once per turn."""

    items: list[Item]
    texts: list[tuple[str, str]]
    tiers: list[Tier]
    written: set[Decimal]
    # Whether the items came from catalogues read by the model at upload
    # ("model"), or from the pattern reader ("reader").
    read_by: str = "reader"


async def prepare(db, organization) -> Prepared:
    """Load this business's catalogue: the checked reading of each file where
    there is one, and the pattern reader for anything uploaded before that.

    Passages of one file are put back together in order first: a table sits
    inside one passage, but the rules around it do not.
    """
    from sqlalchemy import select

    from app.models import Catalogue, KnowledgeDocument
    from app.services import understanding

    rows = (
        await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.organization_id == organization.id)
        )
    ).scalars().all()
    catalogues = {
        row.source: row
        for row in (
            await db.execute(select(Catalogue).where(Catalogue.organization_id == organization.id))
        ).scalars().all()
    }

    by_source: dict[str, list[Any]] = {}
    texts: list[tuple[str, str]] = []
    for row in rows:
        # A photo read out of an uploaded table: its price is already in
        # that table, and reading it twice would list the product twice.
        if (row.attributes or {}).get("photo_from"):
            continue
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
    items: list[Item] = []
    tiers: list[Tier] = []
    unread: list[tuple[str, str]] = []
    read_by = "reader"
    for source, text in texts:
        catalogue = catalogues.get(source)
        if catalogue is not None:
            items += understanding.as_items(catalogue.items or [], source)
            if catalogue.read_by == "model":
                tiers += understanding.as_tiers(catalogue.rules or [], currency_of(text) or currency)
                read_by = "model"
            else:
                tiers += read_tiers([(source, text)])
        else:
            unread.append((source, text))
    if unread:
        items += read_items(unread, currency)
        tiers += read_tiers(unread)

    written: set[Decimal] = set()
    for _, text in texts:
        written |= amounts(text)
    return Prepared(items, texts, tiers, written, read_by)


def delivery_terms(prepared: "Prepared") -> str:
    """The business's own delivery sentences, for the reading to judge a place by."""
    found: list[str] = []
    for tier in prepared.tiers:
        if tier.topic == "delivery" and tier.sentence and tier.sentence not in found:
            found.append(tier.sentence)
    return "\n".join(found[:6])


def conversation(history: Iterable[Any], turns: int = 6) -> str:
    """The last few messages, both sides, for reading "the first one"."""
    out = []
    for m in list(history)[-turns:]:
        who = "Customer" if str(getattr(m, "sender", "")).lower() == "user" else "Shop"
        out.append(f"{who}: {(getattr(m, 'content', '') or '')[:400]}")
    return "\n".join(out)


async def for_turn(db, organization, message: str, history: Iterable[Any] = ()) -> Turn:
    """Load, read and work out one message: the three steps in order.

    The webhook runs the reading at the same time as the analysis; the
    sandbox and anything else that wants the whole thing calls this.
    """
    from app.services import understanding

    prepared = await prepare(db, organization)
    reading = await understanding.read_message(
        message, prepared.items, conversation(history), terms=delivery_terms(prepared)
    )
    return finish(prepared, organization, message, history, reading)


def assemble(message: str, items: list[Item], reading: dict) -> Quote:
    """The quote for what the model read, worked out by the arithmetic here."""
    result = Quote(asked=message, catalogue_size=len(items))
    for item, wanted in reading["lines"]:
        result.lines.append(_line_for(item, wanted))
    result.options = list(reading["choices"])
    result.not_stocked = list(reading["not_stocked"])
    if reading["wants_list"] and not result.lines and not result.options:
        pool = items
        narrowing = words(reading.get("list_filter") or "")
        if narrowing:
            pool = [i for i in items if narrowing & i.haystack_words()]
        if pool:
            result.overview = _spread(pool)
            result.overview_total = len(pool)
    _mark_asides(result)
    return result


def finish(prepared: Prepared, organization, message: str, history: Iterable[Any] = (),
           reading: dict | None = None) -> Turn:
    """Work out what `message` asks for against the loaded catalogue.

    `reading` is the model's checked reading of the message; without one - no
    model reachable, or nothing survived the checks - the pattern reader
    reads it instead.
    """
    items, texts, tiers = prepared.items, prepared.texts, prepared.tiers

    # What they said just before, for "and how much for 10?" after naming one.
    earlier = " ".join(
        getattr(m, "content", "") or ""
        for m in list(history)[-4:]
        if str(getattr(m, "sender", "")).lower() == "user"
    )
    said_before = [
        getattr(m, "content", "") or ""
        for m in list(history)
        if str(getattr(m, "sender", "")).lower() == "user"
    ]
    earlier_one = said_before[-1] if said_before else ""

    if reading is not None:
        result = assemble(message, items, reading)
        asked_topics = set(reading["topics"])
        named_value = reading["order_value"] or stated_value(message)
        place = reading["place"]
    else:
        result = quote(message, items, context=earlier)
        asked_topics = topics_in(message)
        named_value = stated_value(message)
        place = ""
    result.read_by = "model" if reading is not None else "reader"

    # Discount and delivery tiers, applied to what this order is worth: the
    # goods worked out above, or an amount the customer names ("an order
    # worth PKR 499,999"). A follow-up like "what if I make it PKR 999,999?"
    # names no topic, so the ones asked about just before carry over.
    topics = asked_topics or (topics_in(earlier) if named_value else set())
    places = {p for t in tiers if t.topic == "delivery" for p in _places_named(t.condition)}
    said_words = set(re.findall(r"[a-z]+", _plain(f"{message} {place}").lower()))
    # "I'm in Karachi" after "how much with delivery?" answers which city.
    if not topics and places & said_words and "delivery" in topics_in(earlier):
        topics = {"delivery"}
    # "Do you ship to Dubai?" - a place none of the documents mention. Only
    # where delivery is charged by place and nothing covers the others: a
    # business whose rule is the same everywhere, or one that wrote a rate
    # for "the rest of Pakistan", has already answered, and handing that to a
    # person leaves the commonest question of all unanswered.
    delivery_rules = [t for t in tiers if t.topic == "delivery"]
    covers_elsewhere = any(
        re.search(r"\b(rest|other|others|outside|elsewhere|nationwide|anywhere)\b", t.condition, re.I)
        for t in delivery_rules if t.condition
    )
    if reading is not None and reading.get("place_covered") is False and place:
        # The model read the terms and the place: "rest of Pakistan" does not
        # reach Dubai. Only ever used to hand over, never to promise delivery.
        result.unknown_place = place
    elif delivery_rules and not covers_elsewhere and "delivery" in asked_topics:
        everything = _plain(" ".join(text for _, text in texts)).lower()
        named = [place] if place else re.findall(r"\b(?:to|in|into|at)\s+([A-Z][a-z]{2,})", _plain(message))
        for where in named:
            if where and where.lower() not in everything and where.lower() not in STOPWORDS:
                result.unknown_place = where
                break
    value = result.order_value or named_value
    if value is None and topics & {"discount", "delivery"}:
        # "What about delivery?" after an order was described: the order is
        # the one in their previous messages, newest first.
        for said in reversed(
            [getattr(m, "content", "") or "" for m in list(history)[-6:]
             if str(getattr(m, "sender", "")).lower() == "user"]
        ):
            value = quote(said, items).order_value or stated_value(said)
            if value:
                break
    # A discount the order qualifies for is said whether or not they asked:
    # a school buying PKR 15,000 of notebooks should hear it gets 10% off.
    unasked_discount = (
        "discount" not in topics
        and result.order_value is not None
        and any(t.topic == "discount" and t.covers(result.order_value) for t in tiers)
    )
    if unasked_discount:
        topics = topics | {"discount"}
    if topics & {"discount", "delivery"} and value:
        if result.order_value is None and named_value is not None:
            result.stated = value
        # The currency the rules and the price list are written in comes
        # before the business's default: a shop set to USD whose documents are
        # in PKR is quoting in PKR.
        currency = (
            result.currency
            or currency_of(message)
            or next((t.currency for t in tiers if t.currency), None)
            or next((i.currency for i in items if i.currency), None)
            or getattr(organization, "default_currency", None)
        )
        said = " ".join([*said_before[-6:], message, place])
        result.applied = apply_tiers(value, tiers, currency, topics, where=said)
        if unasked_discount and result.applied:
            # Said because it applies; the next rate up is for when they ask.
            result.applied.next_discount = None
    if result.empty() and not topics and earlier_one:
        # "I'm a new customer" straight after "I'll pay after delivery" is
        # still about paying; only the terms topics carry over, and only from
        # the message just before.
        topics = topics_in(earlier_one) & {"payment", "returns"}
        if topics:
            earlier = earlier_one
    # "Can I return the MCB?" is about returns, not the MCB's price.
    if asked_topics & {"returns", "payment"} and not re.search(
        r"\b(how much|price|cost|total|quote|quotation)\b", message or "", re.I
    ):
        result.lines = [line for line in result.lines if line.quantity]
        result.options = []
    if not result.empty() or topics:
        names = {w for item in items for w in words(item.name)}
        # Which sentences to quote: the topics as read, said in the words
        # rules_for looks for.
        asking = message if asked_topics else f"{message} {earlier}"
        if reading is not None and asked_topics:
            asking = f"{message} " + " ".join(sorted(asked_topics))
        result.rules = rules_for(asking, texts, ignore=names)
        if result.applied:
            result.rules = [r for r in result.rules if _rule_topic(r) not in result.applied.topics]
    return Turn(result, items, prepared.written)
