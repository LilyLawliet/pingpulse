"""Orders taken in the chat: worked out, confirmed by the customer, written down.

What went wrong, in a real chat with a stationery shop: the customer chose a
notebook, gave Lahore, an address and "online" for payment, and the agent said
"Great! I've noted the pink dotted Mochi Bunny Notebook". Nothing had been
noted anywhere. The payment methods the shop accepts were never said, the
shop was never told, and "when will I receive it?" was answered with the
delivery charges.

The shape of the fix is the one appointments already have:

* **The model reads, code decides.** The model reads the conversation into
  products (by id from the price list), a city, an address and a payment
  method. Code checks each against what the customer actually wrote and what
  the business actually sells and accepts, and prices it with the same
  arithmetic every quote uses.
* **Nothing is placed without a yes.** When everything is known, the customer
  is shown the summary - lines, delivery, total, address, payment - and asked
  to reply YES. Only that yes writes an Order row.
* **Every confirmation is rendered from the row**, never written by the model:
  the order number, the figures and the payment terms come from the database
  and the business's own sentences.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import ORDER_PLACED, Order
from app.services import offers

logger = logging.getLogger(__name__)

DRAFT_KEY = "order_draft"
# An order being put together is forgotten after this long without a word.
DRAFT_VALID_MINUTES = 180
FIRST_NUMBER = 1001

COLLECTING = "collecting"
CONFIRMING = "confirming"

# Ways to pay, as customers and businesses write them. A method counts only
# when the business's own documents name it: the agent never offers a way to
# pay the shop has not said it takes.
PAYMENT_METHODS: dict[str, str] = {
    "Cash on delivery": r"cash on delivery|\bcod\b|pay(?:ment)? on delivery|\bcash\b",
    "Bank transfer": r"bank transfer|bank deposit|online transfer|\bibft\b|\braast\b|\biban\b",
    "JazzCash": r"jazz ?cash",
    "Easypaisa": r"easy ?paisa",
    "SadaPay": r"sada ?pay",
    "NayaPay": r"naya ?pay",
    "Card": r"\bcards?\b|\bvisa\b|mastercard|debit|credit card",
    "PayPal": r"pay ?pal",
    "Zelle": r"\bzelle\b",
    "Venmo": r"\bvenmo\b",
    "UPI": r"\bupi\b",
    "Apple Pay": r"apple ?pay",
}
# Said as "online" by a customer who hasn't named one.
ONLINE_METHODS = {
    "Bank transfer", "JazzCash", "Easypaisa", "SadaPay", "NayaPay", "Card", "PayPal", "Zelle",
    "Venmo", "UPI", "Apple Pay",
}

_WANTS_ORDER = re.compile(
    r"\b(order|buy|purchase|i(?:'ll| will)? take|i want|i'd like|i would like|add (?:it|this|that|one)|"
    r"place (?:the|my|an|it)|check ?out|deliver (?:it|them|to)|send (?:it|them) to|my address|"
    r"cash on delivery|\bcod\b|pay (?:by|with|via|through|online)|payment method|jazz ?cash|easy ?paisa|"
    r"bank transfer|mujhe chahiye|chahiye|lena hai|order karna)\b",
    re.IGNORECASE,
)
_YES = re.compile(
    r"^\W*(yes|yes please|yeah|yep|yup|ok|okay|sure|confirm(?:ed)?|place it|place the order|"
    r"place my order|go ahead|please do|do it|done|perfect|correct|that's right|thats right|"
    r"haan|han|ji|ji haan|theek hai|thik hai|sahi hai|kar do|kardo|si|oui|ja)\b[\s\W]*"
    r"(please|thanks|thank you|go ahead|place it|confirm(?:ed)?|kar do|kardo)?[\s\W]*$",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\W*(no|nope|nah|cancel|cancel it|don'?t|do not|stop|not now|nahi|nahin|mat karo)\b",
    re.IGNORECASE,
)


def wants_to_order(text: str) -> bool:
    return bool(_WANTS_ORDER.search(text or ""))


# ------------------------------------------------------------------ payment
_ABOUT_PAYING = re.compile(
    r"\b(pay|paid|payment|payments|accept|accepted|transfer|iban|account|send to|cash on delivery|cod)\b",
    re.IGNORECASE,
)
_REFUSED = r"\b(no|not|don'?t|do not|never|without)\b[^.,;]{0,20}"


def _payment_sentences(prepared) -> list[str]:
    """Every sentence the business wrote about paying."""
    found: list[str] = []
    for _, text in prepared.texts:
        for sentence in offers._sentences(text):
            if _ABOUT_PAYING.search(sentence) and sentence not in found:
                found.append(sentence)
    return found


def accepted_methods(prepared) -> list[str]:
    """The ways to pay the business's own documents name, in a steady order.

    Only from sentences about paying, and not where it is refused: "no card
    needed" for a free trial is not an offer to take cards.
    """
    found: list[str] = []
    for sentence in _payment_sentences(prepared):
        low = sentence.lower()
        for method, pattern in PAYMENT_METHODS.items():
            if method in found:
                continue
            hits = [m for m in re.finditer(pattern, low) if not re.search(_REFUSED + r"$", low[: m.start()])]
            if hits:
                found.append(method)
    return found


def how_to_pay(prepared, method: str) -> list[str]:
    """The business's own words for paying this way: account details first."""
    pattern = PAYMENT_METHODS.get(method)
    if not pattern:
        return []
    mentions = [s for s in _payment_sentences(prepared) if re.search(pattern, s.lower())]
    details = [s for s in mentions if re.search(r"\d{6,}|\d{4}-\d{5,}|\biban\b", s.lower())]
    return details[:2] if details else mentions[:1]


def payment_terms(prepared, limit: int = 3) -> list[str]:
    """The business's own sentences about paying, as written."""
    return offers.rules_for("payment options", prepared.texts, limit=limit)


def method_in(text: str, accepted: list[str]) -> tuple[str | None, list[str]]:
    """The accepted method named here, or the accepted ones it could mean."""
    low = (text or "").lower()
    spans = {m: [x.span() for x in re.finditer(PAYMENT_METHODS[m], low)] for m in accepted}
    spans = {m: found for m, found in spans.items() if found}

    def inside_another(method: str) -> bool:
        # "cash" in "jazzcash" is JazzCash, not cash on delivery.
        return all(
            any(o != method and a <= s0 and e1 <= b for o, others in spans.items() for a, b in others)
            for s0, e1 in spans[method]
        )

    named = [m for m in spans if not inside_another(m)]
    if len(named) == 1:
        return named[0], []
    if len(named) > 1:
        return None, named
    if re.search(r"\bonline\b|\btransfer\b|\bwallet\b", low):
        return None, [m for m in accepted if m in ONLINE_METHODS]
    return None, []


# ------------------------------------------------------------------ reading
ORDER_PROMPT = """You read a WhatsApp chat between a shop and a customer and say what the
customer wants to ORDER. Return ONLY a JSON object:

{{"lines": [{{"product": "id from the list", "quantity": number or null,
              "counted_in": "unit" | "pieces" | "kg" | "g" | "m" | "l" | "ml",
              "as_written": "the customer's words for it"}}],
  "choose_between": [{{"as_written": "...", "products": ["id", "id"]}}],
  "place": "the city or area it is to be delivered to, else \\"\\"",
  "address": "the delivery address exactly as the customer wrote it, else \\"\\"",
  "payment": "how the customer said they will pay, in their words, else \\"\\"",
  "note": "an instruction for the order such as gift wrapping, in their words, else \\"\\""}}

Rules:
- Only products the customer said they want to buy. A product they only asked the price of is
  not ordered. If they changed their mind, use what they decided last.
- A choice they made later ("pink dotted") decides between products they named earlier.
- quantity is only a number the customer wrote; a product ordered with no number is 1.
- Use only ids from the product list. If it could still be more than one product, put it in
  choose_between.

Products:
{products}

The chat, oldest first:
{chat}"""


def _customer_lines(history, message: str, turns: int = 12) -> list[str]:
    said = [
        (getattr(m, "content", "") or "")
        for m in list(history)[-turns:]
        if str(getattr(m, "sender", "")).lower() in ("user", "customer")
    ]
    return [*said, message]


def _chat(history, message: str, turns: int = 12) -> str:
    lines = []
    for m in list(history)[-turns:]:
        who = "Customer" if str(getattr(m, "sender", "")).lower() in ("user", "customer") else "Shop"
        lines.append(f"{who}: {(getattr(m, 'content', '') or '')[:400]}")
    lines.append(f"Customer: {message[:400]}")
    return "\n".join(lines)


def _written_by_customer(value: str, customer_text: str, share: float = 0.7) -> bool:
    """Most of these words are ones the customer actually typed."""
    said = set(re.findall(r"[a-z0-9]+", offers._plain(customer_text).lower()))
    words = re.findall(r"[a-z0-9]+", offers._plain(value).lower())
    if not words:
        return False
    return sum(1 for w in words if w in said) / len(words) >= share


async def read_order(prepared, history, message: str, timeout: float | None = None) -> dict | None:
    """What the model says the customer is ordering, checked. None if no model answered."""
    from app.config import settings
    from app.services import understanding

    if not prepared.items or not (settings.groq_api_keys or settings.gemini_api_keys):
        return None
    customer_text = " \n".join(_customer_lines(history, message))
    shown = understanding._candidates(prepared.items, customer_text, "")
    listing, ids = understanding.product_lines(shown)
    answer = await understanding.structured(
        ORDER_PROMPT.format(products=listing, chat=_chat(history, message)),
        timeout or settings.understanding_timeout_seconds + 2,
    )
    if answer is None:
        return None
    reading = understanding.check_reading(answer, ids, customer_text)
    lowered = offers._plain(customer_text).lower()
    place = str(answer.get("place") or "").strip()[:80]
    if place and place.lower() not in lowered:
        place = ""
    address = str(answer.get("address") or "").strip()[:300]
    if address and not _written_by_customer(address, customer_text):
        address = ""
    note = str(answer.get("note") or "").strip()[:200]
    if note and not _written_by_customer(note, customer_text, 0.6):
        note = ""
    payment = str(answer.get("payment") or "").strip()[:80]
    if payment and not _written_by_customer(payment, customer_text, 0.5):
        payment = ""
    reading.update({"place": place, "address": address, "payment": payment, "note": note})
    return reading


# ------------------------------------------------------------------ pricing
# Things nobody delivers: a monthly plan, a seat, an onboarding session. An
# order made only of these has no address to ask for and no delivery charge -
# asking somebody buying software where to send it is how a sale feels broken.
_NOT_DELIVERED_UNITS = {
    "day", "week", "month", "quarter", "year", "user", "seat", "licence", "license",
    "subscription", "plan", "session", "hour", "service", "class", "course", "consultation",
    "call", "visit", "setup", "installation", "training", "workspace", "account",
}
_NOT_DELIVERED_NAMES = re.compile(
    r"\b(subscription|licen[cs]e|onboarding|training|setup fee|set-up|installation|"
    r"per user|per seat|per month|per year|monthly|annual|yearly|plan|add-on|addon|support)\b",
    re.IGNORECASE,
)


def is_delivered(item) -> bool:
    """Whether this is a thing that is sent somewhere, rather than a service."""
    unit = offers._stem((item.sale_unit or "").lower())
    if unit in _NOT_DELIVERED_UNITS or (item.sale_unit or "").lower() in _NOT_DELIVERED_UNITS:
        return False
    return not _NOT_DELIVERED_NAMES.search(f"{item.name} {item.spec}")


@dataclass
class Draft:
    """An order worked out from the price list, with what is still missing."""

    lines: list[dict] = field(default_factory=list)
    currency: str | None = None
    goods_total: Decimal | None = None
    discount: Decimal | None = None
    delivery_fee: Decimal | None = None
    delivery_known: bool = False
    total: Decimal | None = None
    place: str = ""
    address: str = ""
    payment: str | None = None
    note: str = ""
    needs_delivery: bool = True
    missing: list[str] = field(default_factory=list)
    # Things to ask, in words for the prompt.
    asks: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing

    def as_state(self) -> dict:
        return {
            "lines": self.lines,
            "currency": self.currency,
            "goods_total": _s(self.goods_total),
            "discount": _s(self.discount),
            "delivery_fee": _s(self.delivery_fee),
            "delivery_known": self.delivery_known,
            "total": _s(self.total),
            "place": self.place,
            "address": self.address,
            "payment": self.payment,
            "note": self.note,
            "needs_delivery": self.needs_delivery,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _d(value) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def price(prepared, reading: dict, customer_text: str, accepted: list[str], latest: str) -> Draft:
    """The order, priced by the same arithmetic as every quote, and what is missing."""
    quote = offers.assemble(customer_text, prepared.items, reading)
    draft = Draft(place=reading.get("place") or "", address=reading.get("address") or "",
                  note=reading.get("note") or "")

    counted = [line for line in quote.lines if not line.aside]
    if quote.options:
        for wanted, choices in quote.options:
            names = " or ".join(item.plain_label for item in choices[:4])
            draft.asks.append(f"which one they want: {names}")
        draft.missing.append("choice")
    if not counted and not quote.options:
        draft.missing.append("items")
        draft.asks.append("what they would like to order")
    for line in counted:
        if line.total is None:
            draft.missing.append("quantity")
            draft.asks.append(f"how many of {line.item.plain_label} they want")
            continue
        draft.lines.append(
            {
                "name": line.item.plain_label,
                "quantity": offers._num(line.units if line.units is not None else (line.quantity or Decimal(1))),
                "sold_as": line.item.sold_as(),
                "unit_price": str(line.item.price),
                "total": str(line.total),
                "note": line.note or "",
                "delivered": is_delivered(line.item),
            }
        )
    currencies = {line.item.currency for line in counted if line.item.currency}
    draft.currency = currencies.pop() if len(currencies) == 1 else None

    draft.needs_delivery = any(line.get("delivered", True) for line in draft.lines) or not draft.lines
    if draft.lines and "quantity" not in draft.missing and "choice" not in draft.missing:
        goods = sum((Decimal(line["total"]) for line in draft.lines), Decimal(0))
        draft.goods_total = goods
        # Said as a place is said, so "lahore" in lower case still reads as
        # somewhere outside Karachi rather than as no place at all.
        place_said = draft.place or draft.address
        where = f"in {place_said.title()} " if place_said else ""
        where += f"{draft.address} {latest}"
        topics = {"delivery", "discount"} if draft.needs_delivery else {"discount"}
        applied = offers.apply_tiers(goods, prepared.tiers, draft.currency, topics, where=where)
        after = goods
        if applied and applied.discount is not None:
            draft.discount = applied.saving
            after = goods - applied.saving
        if applied and applied.delivery is not None:
            draft.delivery_known = True
            draft.delivery_fee = Decimal(0) if applied.delivery.free else applied.delivery.fee
        elif applied and applied.delivery_by_place:
            draft.missing.append("place")
            draft.asks.append("which city it is to be delivered to (delivery depends on it)")
        elif not draft.needs_delivery or not any(t.topic == "delivery" for t in prepared.tiers):
            # Nothing written about delivery charges: the total is the goods,
            # and delivery is for the shop to say.
            draft.delivery_known = False
        draft.total = after + (draft.delivery_fee or Decimal(0))

    if draft.needs_delivery and not draft.address:
        draft.missing.append("address")
        draft.asks.append("the full delivery address (house, street, area and city)")

    method, could_be = method_in(f"{reading.get('payment') or ''} {latest}", accepted)
    if accepted:
        if method:
            draft.payment = method
        else:
            draft.missing.append("payment")
            options = could_be or accepted
            draft.asks.append(
                "how they will pay - only these are accepted: " + ", ".join(options)
            )
    return draft


# ------------------------------------------------------------------ wording
def _money(value, currency) -> str:
    return offers.money(Decimal(str(value)), currency)


def summary(state: dict) -> str:
    """The order as the customer is asked to confirm it."""
    currency = state.get("currency")
    lines = ["Here's your order:"]
    for line in state["lines"]:
        lines.append(f"• {line['name']} × {line['quantity']} — {_money(line['total'], currency)}")
    goods = _d(state.get("goods_total"))
    lines.append(f"Items: {_money(goods, currency)}")
    if _d(state.get("discount")):
        lines.append(f"Discount: −{_money(state['discount'], currency)}")
    delivered = state.get("needs_delivery", True)
    if delivered and state.get("delivery_known"):
        fee = _d(state.get("delivery_fee")) or Decimal(0)
        where = f" ({state['place']})" if state.get("place") else ""
        lines.append(f"Delivery{where}: {'free' if fee == 0 else _money(fee, currency)}")
    lines.append(f"Total: {_money(state['total'], currency)}")
    if delivered and not state.get("delivery_known"):
        lines.append("Delivery charge: the shop will confirm it.")
    if delivered:
        lines.append(f"Deliver to: {state['address']}")
    if state.get("payment"):
        lines.append(f"Payment: {state['payment']}")
    if state.get("note"):
        lines.append(f"Note: {state['note']}")
    lines.append("")
    lines.append("Reply YES to place the order, or tell me what to change.")
    return "\n".join(lines)


def placed_text(order: Order, prepared) -> str:
    """The confirmation, from the row and the business's own sentences."""
    currency = order.currency
    out = [f"Your order #{order.number} is placed ✅"]
    for line in order.lines:
        out.append(f"• {line['name']} × {line['quantity']} — {_money(line['total'], currency)}")
    out.append(f"Total: {_money(order.total, currency)}")
    delivered = any(line.get("delivered", True) for line in order.lines)
    if delivered and order.address:
        out.append(f"Deliver to: {order.address}")
    if order.payment_method:
        out.append(f"Payment: {order.payment_method}")
        # The shop's own words about paying that way, when it wrote any.
        terms = how_to_pay(prepared, order.payment_method)
        out += terms
        if order.payment_method in ONLINE_METHODS and not any(
            re.search(r"\d{6,}|\d{4}-\d{5,}", t) for t in terms
        ):
            out.append(f"We'll send you the {order.payment_method} details to pay here.")
    timing = [
        t for t in offers.rules_for("when will it arrive delivery", prepared.texts, limit=2)
        if offers._TAKES_TIME.search(t.lower())
    ]
    if delivered:
        out += timing[:1]
    out.append("Thank you for your order!")
    return "\n".join(out)


# ------------------------------------------------------------------ the record
async def _next_number(db, organization_id) -> int:
    highest = await db.scalar(
        select(func.max(Order.number)).where(Order.organization_id == organization_id)
    )
    return (highest or FIRST_NUMBER - 1) + 1


async def place(db, organization, contact, state: dict, source: str = "agent") -> Order:
    """Write the order the customer said yes to. Retries a number taken meanwhile."""
    for _ in range(4):
        order = Order(
            organization_id=organization.id,
            contact_id=contact.id,
            number=await _next_number(db, organization.id),
            status=ORDER_PLACED,
            lines=state["lines"],
            currency=state.get("currency"),
            goods_total=_d(state["goods_total"]),
            discount=_d(state.get("discount")),
            delivery_fee=_d(state.get("delivery_fee")) if state.get("delivery_known") else None,
            total=_d(state["total"]),
            delivery_place=state.get("place") or None,
            address=state.get("address") or None,
            payment_method=state.get("payment") or None,
            customer_note=state.get("note") or None,
            source=source,
        )
        try:
            async with db.begin_nested():
                db.add(order)
                await db.flush()
            return order
        except IntegrityError:
            continue
    raise RuntimeError("could not number the order")


def remember(contact, stage: str, state: dict) -> None:
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    metadata[DRAFT_KEY] = {"stage": stage, "at": datetime.now(timezone.utc).isoformat(), **state}
    contact.contact_metadata = metadata


def remembered(contact) -> dict | None:
    draft = (getattr(contact, "contact_metadata", None) or {}).get(DRAFT_KEY)
    if not isinstance(draft, dict):
        return None
    try:
        at = datetime.fromisoformat(draft["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - at > timedelta(minutes=DRAFT_VALID_MINUTES):
        return None
    return draft


def forget(contact) -> None:
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    metadata.pop(DRAFT_KEY, None)
    contact.contact_metadata = metadata


# ------------------------------------------------------------------ the turn
@dataclass
class OrderTurn:
    """What this message did about an order, and what to tell the agent."""

    prompt_block: str = ""
    performed: str | None = None      # "placed"
    order: Order | None = None
    # A reply rendered here, sent as it is: the summary to confirm, or the
    # confirmation of a placed order. Never left to the model to reword.
    reply: str | None = None
    # The order couldn't be read (no model): a person has to take it.
    needs_person: bool = False

    @property
    def placed(self) -> bool:
        return self.performed == "placed"


def _asks_block(draft: Draft, accepted: list[str], terms: list[str]) -> str:
    have = []
    if draft.lines:
        have.append(
            "; ".join(f"{l['name']} × {l['quantity']} = {_money(l['total'], draft.currency)}" for l in draft.lines)
        )
    if draft.total is not None and draft.delivery_known:
        have.append(f"total with delivery {_money(draft.total, draft.currency)}")
    if draft.address:
        have.append(f"address: {draft.address}")
    if draft.payment:
        have.append(f"payment: {draft.payment}")
    block = (
        "=== ORDER IN PROGRESS ===\n"
        "The customer is ordering. NOTHING has been placed or noted yet - never say it has, "
        "and never say \"noted\" or \"confirmed\".\n"
        + (f"So far: {'; '.join(have)}.\n" if have else "")
        + "Ask them, in one short message, for: " + "; ".join(draft.asks) + "."
    )
    if "payment" in draft.missing and terms:
        block += "\nThe business's payment terms, as written: " + " ".join(terms)
    if not accepted:
        block += "\nThe business has not written which payment methods it takes: do not name any."
    return block


def may_read(contact, text: str) -> bool:
    """Whether this message is worth reading as an order at all."""
    state = remembered(contact)
    if state and state.get("stage") == CONFIRMING and (_YES.match(text or "") or _NO.match(text or "")):
        return False  # A yes or a no to the summary needs no reading.
    return bool(state) or wants_to_order(text)


async def handle_turn(
    db, organization, contact, text: str, history, prepared, *, reading: dict | None = None
) -> OrderTurn:
    """Do the order work for one message. Never raises.

    `reading` is the order reading when it was already made alongside the
    other readings of this message; otherwise it is made here.
    """
    try:
        return await _handle_turn(db, organization, contact, text, history, prepared, reading)
    except Exception as exc:  # noqa: BLE001 - never at the cost of a reply
        logger.warning("order handling failed for %s: %s", getattr(contact, "id", "?"), exc)
        return OrderTurn()


async def _handle_turn(db, organization, contact, text: str, history, prepared, reading=None) -> OrderTurn:
    state = remembered(contact)

    # ------------------------------------------------------------ the yes
    if state and state.get("stage") == CONFIRMING:
        if _YES.match(text or ""):
            order = await place(db, organization, contact, state)
            forget(contact)
            return OrderTurn(
                prompt_block=(
                    "=== ORDER ===\n"
                    f"Order #{order.number} was PLACED just now, successfully."
                ),
                performed="placed",
                order=order,
                reply=placed_text(order, prepared),
            )
        if _NO.match(text or "") and not wants_to_order(text):
            forget(contact)
            return OrderTurn(
                prompt_block=(
                    "=== ORDER ===\n"
                    "They did not confirm the order, so NOTHING was placed. Say so plainly and "
                    "ask whether they want to change anything."
                )
            )

    if not (state or wants_to_order(text)):
        return OrderTurn()
    if not prepared.items:
        return OrderTurn()

    if reading is None:
        reading = await read_order(prepared, history, text)
    if reading is None:
        return OrderTurn(
            prompt_block=(
                "=== ORDER ===\n"
                "The order can't be taken automatically right now. Do NOT say anything is "
                "placed, noted or confirmed."
            ),
            needs_person=True,
        )
    customer_text = " \n".join(_customer_lines(history, text))
    accepted = accepted_methods(prepared)
    draft = price(prepared, reading, customer_text, accepted, text)

    if not draft.lines and "choice" not in draft.missing:
        # Nothing orderable was named: an ordinary question, not an order.
        if not state:
            return OrderTurn()

    if draft.complete:
        snapshot = draft.as_state()
        remember(contact, CONFIRMING, snapshot)
        shown = summary(snapshot)
        return OrderTurn(
            prompt_block=(
                "=== ORDER READY TO CONFIRM ===\n"
                "Nothing is placed until they reply YES to this summary.\n" + shown
            ),
            reply=shown,
        )

    remember(contact, COLLECTING, draft.as_state())
    return OrderTurn(prompt_block=_asks_block(draft, accepted, payment_terms(prepared)))


# ------------------------------------------------------------------ claims
# "Great! I've noted the notebook" was the sentence. Checked against the
# record, like the booking claims: a reply may announce an order only on the
# turn one was placed.
_ORDER_CLAIMS = re.compile(
    r"\b("
    r"i(?:'ve| have)? ?(?:now )?(?:noted|placed|recorded|logged|booked|confirmed|processed|registered)"
    r"(?: down)? (?:your|the|that|this)\b"
    r"|(?:your|the) order (?:is|has been|was) (?:now )?(?:placed|confirmed|noted|booked|received|"
    r"recorded|processed|registered|on its way|being processed|dispatched)"
    r"|order (?:placed|confirmed|received)"
    r"|(?:noted|got) (?:it|that|your order)[,!. ]"
    r"|i(?:'ve| have) noted\b"
    r")",
    re.IGNORECASE,
)


def claims_order(text: str, placed: bool) -> str | None:
    """Why this reply claims an order that doesn't exist, or None."""
    if placed:
        return None
    from app.services import booking

    said = booking._asserted(_ORDER_CLAIMS, text)
    if said:
        return (
            f'you wrote "{said}", but no order has been placed; never say an order is noted, '
            "placed or confirmed - ask for what is missing instead"
        )
    return None
