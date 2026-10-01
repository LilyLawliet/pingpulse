"""What a shop already taught its customers, turned into what the agent knows.

Two different things fall out of a shop's own past replies, and the whole
design turns on not confusing them.

*What is true.* "Delivery inside Lahore is next day." "We take bank transfer,
not cash on delivery." Facts about the business, which belong in the knowledge
base where retrieval can find them when a customer asks.

*How the shop sounds.* Short sentences. No emoji. Opens in Roman Urdu, answers
a price without being asked twice. Style, which belongs in the prompt as an
instruction about form.

Mixing them is the failure mode, and it is not hypothetical: a style example
reading "the black boots are $189" teaches the model both how this shop writes
and a price, and the price comes back out later attached to whatever product is
under discussion at the time. So examples are filtered for figures, and the
prompt block says in as many words that facts do not come from it.

One rule governs both halves: only words a *person at the shop* actually wrote.
That is harder than it sounds, and `human_cutoff` below is where it is enforced.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import SENDER_AGENT, SENDER_CUSTOMER, SENDER_OPERATOR, Message

logger = logging.getLogger(__name__)

# Where imported facts are filed, so a shop can throw all of them away in one
# action if the extraction turns out to have been wrong.
LEARNED_SOURCE = "Past conversations"

# Below this there is no voice to learn — just a handful of replies, which
# would produce a confident description of nothing.
MIN_REPLIES_FOR_VOICE = 5

# What gets sent to the model. Both are sampled across the whole period rather
# than taken from the end, so one busy week does not define the shop.
MAX_REPLIES_SAMPLED = 60
MAX_EXCHANGES_SAMPLED = 40
MAX_FACTS = 25
MAX_EXAMPLES = 4

# A conversation older than this describes a shop that may no longer exist in
# that form. Prices move and ranges change, and a stale fact is worse than no
# fact because the agent states it with exactly the same confidence as a true
# one.
DEFAULT_WINDOW_DAYS = 180

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
PHONE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")
LONG_DIGITS = re.compile(r"\b\d{6,}\b")
FIGURES = re.compile(r"\d{3,}")
WHITESPACE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# Material
# --------------------------------------------------------------------------
@dataclass
class ShopReply:
    """One message a person at the shop wrote, and where it came from."""

    text: str
    at: datetime
    origin: str  # "phone" (typed on the handset) | "dashboard" (typed here)


@dataclass
class Exchange:
    """A customer's question, and the answer a person at the shop gave it."""

    asked: str
    answered: str
    at: datetime


@dataclass
class Material:
    """Everything safe to learn from, and the accounting behind it."""

    replies: list[ShopReply] = field(default_factory=list)
    exchanges: list[Exchange] = field(default_factory=list)
    cutoff: datetime | None = None
    from_phone: int = 0
    from_dashboard: int = 0
    chats_seen: int = 0
    skipped_after_cutoff: int = 0


def _aware(value) -> datetime | None:
    """Naive timestamps come back from SQLite; treat them as UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def human_cutoff(db, organization_id, channel) -> datetime | None:
    """The moment after which a message from this number might not be a person's.

    WhatsApp history records that a message came *from this number*, never who
    typed it. Once the agent is answering on that number its own replies sit in
    the same history, indistinguishable from the owner's. Learning a shop's
    voice from those is the model learning from itself, and each round of it
    drifts further from the person who wrote the originals — the same reason
    operator replies were given their own label before any of this was built.

    So the line is drawn at the earliest moment the agent could have sent
    anything at all, and only what the handset sent before it counts as human.
    Erring early costs some material; erring late costs the voice itself.

    None means the agent has never sent a message for this tenant, so the whole
    history is the shop's own writing.
    """
    candidates: list[datetime] = []

    connected = _aware(getattr(channel, "session_connected_at", None))
    if connected:
        candidates.append(connected)

    first_agent = _aware(
        await db.scalar(
            select(func.min(Message.created_at)).where(
                Message.organization_id == organization_id,
                Message.sender == SENDER_AGENT,
            )
        )
    )
    if first_agent:
        candidates.append(first_agent)

    return min(candidates) if candidates else None


def scrub(text: str) -> str:
    """Take other people's identifiers out before anything is stored or sent.

    These replies were written to one customer and may be read back in front of
    a different one. An email address, a phone number or an order reference
    carried across is a leak from one of a shop's customers to another, so they
    are removed here rather than relied upon not to show up.
    """
    cleaned = EMAIL.sub("[email]", text or "")
    cleaned = PHONE.sub("[number]", cleaned)
    cleaned = LONG_DIGITS.sub("[ref]", cleaned)
    return WHITESPACE.sub(" ", cleaned).strip()


async def gather(
    db,
    organization_id,
    channel,
    chats: list[dict] | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Material:
    """Collect the shop's own human-written replies, from both places they live.

    The handset's history is the bulk of it, and the part that needs the
    cutoff. Messages typed in the dashboard need no such test: they were
    recorded as an operator's at the moment they were written, which is
    precisely why that label had to exist before this feature could.
    """
    from app.services import prospects

    if chats is None:
        chats = await prospects.read_history(channel)

    cutoff = await human_cutoff(db, organization_id, channel)
    horizon = datetime.now(timezone.utc) - timedelta(days=window_days)
    material = Material(cutoff=cutoff, chats_seen=len(chats or []))

    for chat in chats or []:
        messages = sorted(chat.get("messages") or [], key=lambda m: int(m.get("at") or 0))
        material.exchanges += _chat_exchanges(messages, material, cutoff, horizon)

    material.exchanges += await _dashboard_exchanges(db, organization_id, horizon)
    for reply in await _dashboard_replies(db, organization_id, horizon):
        material.replies.append(reply)
        material.from_dashboard += 1

    material.replies.sort(key=lambda r: r.at)
    material.exchanges.sort(key=lambda e: e.at)
    return material


def _chat_exchanges(messages, material: Material, cutoff, horizon) -> list[Exchange]:
    """Walk one conversation, pairing questions with the shop's own answers.

    Consecutive messages on each side are joined, because people send a
    question across three bubbles and answer it across two.
    """
    found: list[Exchange] = []
    asked: list[str] = []
    answered: list[str] = []
    answered_at: datetime | None = None

    def close() -> None:
        if asked and answered and answered_at:
            found.append(
                Exchange(
                    asked=" ".join(asked)[:600],
                    answered=" ".join(answered)[:600],
                    at=answered_at,
                )
            )

    for entry in messages:
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        when = _aware(entry.get("at"))
        if when is None:
            continue

        if entry.get("fromMe"):
            # The heart of it: at or after the cutoff this may be the agent's
            # own output, so it is not evidence of anything about the shop.
            if cutoff is not None and when >= cutoff:
                material.skipped_after_cutoff += 1
                continue
            if when < horizon:
                continue
            cleaned = scrub(text)
            if not cleaned:
                continue
            material.replies.append(ShopReply(cleaned, when, "phone"))
            material.from_phone += 1
            answered.append(cleaned)
            answered_at = when
        else:
            if answered:
                close()
                asked, answered, answered_at = [], [], None
            if when >= horizon:
                asked.append(scrub(text))

    close()
    return found


async def _dashboard_replies(db, organization_id, horizon) -> list[ShopReply]:
    """Messages typed by a person in the dashboard. Human by construction."""
    rows = (
        await db.execute(
            select(Message)
            .where(
                Message.organization_id == organization_id,
                Message.sender == SENDER_OPERATOR,
                Message.created_at >= horizon,
            )
            .order_by(Message.created_at)
        )
    ).scalars().all()

    out: list[ShopReply] = []
    for row in rows:
        cleaned = scrub(row.content)
        if cleaned:
            out.append(ShopReply(cleaned, _aware(row.created_at), "dashboard"))
    return out


async def _dashboard_exchanges(db, organization_id, horizon) -> list[Exchange]:
    """Customer questions a person answered here, paired up per contact.

    The agent's own replies are not in this query at all. A fact extracted from
    one would be the model's guess laundered into the knowledge base, where it
    would be retrieved later as though a person had confirmed it.
    """
    rows = (
        await db.execute(
            select(Message)
            .where(
                Message.organization_id == organization_id,
                Message.created_at >= horizon,
                Message.sender.in_((SENDER_CUSTOMER, SENDER_OPERATOR)),
            )
            .order_by(Message.contact_id, Message.created_at)
        )
    ).scalars().all()

    out: list[Exchange] = []
    asked: list[str] = []
    current = None
    for row in rows:
        if row.contact_id != current:
            current, asked = row.contact_id, []
        if row.sender == SENDER_CUSTOMER:
            asked.append(scrub(row.content))
        elif asked:
            out.append(
                Exchange(
                    asked=" ".join(asked)[:600],
                    answered=scrub(row.content)[:600],
                    at=_aware(row.created_at),
                )
            )
            asked = []
    return out


def spread(items: list, limit: int) -> list:
    """An even sample across the whole period rather than the tail of it."""
    if len(items) <= limit:
        return list(items)
    step = len(items) / limit
    return [items[int(index * step)] for index in range(limit)]


def pick_examples(replies: list[ShopReply], limit: int = MAX_EXAMPLES) -> list[str]:
    """A few of the shop's own lines, chosen for form and stripped of content.

    Selected here rather than by the model, for two reasons. A model asked for
    verbatim quotes paraphrases them, and a paraphrase of a shop's voice is not
    the shop's voice. And the filter that matters most is mechanical: anything
    carrying a figure is refused outright, because an example is reproduced in
    front of customers it was never written for, and a price is the one thing
    that must never travel that way.
    """
    candidates = [
        reply
        for reply in replies
        if 25 <= len(reply.text) <= 200
        and not FIGURES.search(reply.text)
        and "[" not in reply.text  # something was scrubbed out of it
    ]

    seen: set[str] = set()
    unique: list[ShopReply] = []
    for reply in candidates:
        key = reply.text.lower()[:40]
        if key not in seen:
            seen.add(key)
            unique.append(reply)

    return [reply.text for reply in spread(unique, limit)]


# --------------------------------------------------------------------------
# Deriving
# --------------------------------------------------------------------------
VOICE_PROMPT = """Below are real messages a shop sent its customers on WhatsApp,
written by a person who works there.

Describe HOW THEY WRITE, so that another writer could sound like them.

Cover only form: typical length, whether they greet and how, sign-offs, the
language and script they use (including mixed or romanised languages),
formality, emoji and punctuation habits, and how directly they answer a
question.

Rules:
- Describe form only. Never mention a product, a price, a figure or a customer.
- Only say what these messages actually show. If they are mixed, say so.
- Each trait one short sentence, at most 6 traits.
- Return strict JSON: {{"traits": ["...", "..."]}}
- No explanation, no code fences.

MESSAGES:
{messages}
"""

FACTS_PROMPT = """Below are real exchanges between a shop and its customers on
WhatsApp. Every answer was typed by a person who works at the shop.

Extract only DURABLE FACTS ABOUT THE BUSINESS - things that would still be true
for a different customer tomorrow.

Keep: what is sold, prices, sizes and variants carried, delivery areas and
times, payment methods, opening hours, location, returns and exchange policy,
warranty, minimum order.

Discard: anything about one customer (their name, their order, their address),
greetings and small talk, one-off arrangements or discounts, anything the shop
said it would check or confirm later, and anything you are not certain of.

If two answers contradict each other, keep neither.

Write each fact as one plain standalone sentence that makes sense without the
conversation. Merge duplicates.

- Return strict JSON: {{"facts": [{{"topic": "two or three words", "fact": "..."}}]}}
- At most {limit} facts. If there are none, return {{"facts": []}}.
- No explanation, no code fences.

EXCHANGES:
{exchanges}
"""


def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("learning returned non-JSON: %s", text[:160])
        return {}


async def derive_voice(material: Material) -> dict | None:
    """A description of how this shop writes, for a person to approve or edit.

    None when there is not enough to go on. A voice invented from four messages
    would be applied to every customer this shop has, so too little material is
    reported as too little rather than quietly filled in.
    """
    if len(material.replies) < MIN_REPLIES_FOR_VOICE:
        return None

    from app.services.llm_service import _call_groq, exact

    sample = spread(material.replies, MAX_REPLIES_SAMPLED)
    prompt = VOICE_PROMPT.format(
        messages="\n".join(f"- {reply.text}" for reply in sample)
    )
    try:
        with exact():
            parsed = _parse_json(await _call_groq(prompt))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not derive a voice: %s", exc)
        return None

    traits = [
        str(trait).strip() for trait in (parsed.get("traits") or []) if str(trait).strip()
    ][:6]
    if not traits:
        return None

    return {
        "style": "\n".join(f"- {trait}" for trait in traits),
        "examples": pick_examples(material.replies),
        "based_on": len(material.replies),
        "from_phone": material.from_phone,
        "from_dashboard": material.from_dashboard,
    }


async def extract_facts(material: Material) -> list[dict]:
    """Durable business facts, from exchanges a person actually answered."""
    if not material.exchanges:
        return []

    from app.services.llm_service import _call_groq, exact

    sample = spread(material.exchanges, MAX_EXCHANGES_SAMPLED)
    rendered = "\n\n".join(
        f"Customer: {item.asked}\nShop: {item.answered}" for item in sample
    )
    prompt = FACTS_PROMPT.format(exchanges=rendered, limit=MAX_FACTS)
    try:
        with exact():
            parsed = _parse_json(await _call_groq(prompt))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not extract facts: %s", exc)
        return []

    facts: list[dict] = []
    seen: set[str] = set()
    for entry in parsed.get("facts") or []:
        if not isinstance(entry, dict):
            continue
        fact = str(entry.get("fact") or "").strip()
        topic = str(entry.get("topic") or "").strip() or "From conversations"
        if not fact:
            continue
        key = re.sub(r"[^a-z0-9]", "", fact.lower())
        if key in seen:
            continue
        seen.add(key)
        facts.append({"topic": topic[:60], "fact": fact[:600]})
    return facts[:MAX_FACTS]
