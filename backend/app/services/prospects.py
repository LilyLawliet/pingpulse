"""Finding the customers a shop already has but never answered.

When a handset pairs, WhatsApp pushes across a chunk of its past conversations.
Somewhere in there are people who asked about a product and never got a reply —
the shop was busy, it was a Sunday, the message scrolled away. They are the
warmest leads a shop owns and nobody is working them.

This reads that history and decides which of those conversations are worth
picking up. It never sends anything: it produces a list for a person to look at.

Three filters, and each one exists to stop a different bad message going out.

*Did they ask about a product?* Matched against this shop's own catalogue and
the words people use when they are buying. A conversation about a delivery time
or a family matter is not a lead, and treating it as one is how a shop's WhatsApp
starts feeling like spam.

*Did the shop already answer?* If the last word in a conversation belongs to the
shop, it was handled. Messaging them again is not follow-up, it is pestering
somebody who already got what they asked for.

*Was it recent enough to make sense?* Answering a question from eight months ago
reads as a mailing list, not a reply. The window is deliberately short.

None of this makes the messages themselves safe to send in bulk. That decision
belongs to the operator, one conversation at a time, which is why nothing here
returns anything but candidates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.models import KnowledgeDocument

logger = logging.getLogger(__name__)

# Beyond this, a reply reads as a mailing list rather than an answer.
DEFAULT_WINDOW_DAYS = 30

# The shape of buying, independent of what is being sold. Kept short and
# specific: broad words like "want" match half of every conversation.
BUYING_SIGNALS = (
    "price", "pricing", "cost", "how much", "kitna", "kitne", "rate",
    "available", "availability", "in stock", "stock",
    "size", "colour", "color", "delivery", "deliver", "shipping",
    "order", "buy", "purchase", "discount", "sale",
    "do you have", "looking for", "interested",
)

# Words that mean "this conversation is not a sales lead", even when a buying
# signal is present. A complaint is a real conversation and a terrible thing to
# answer with a sales pitch.
EXCLUDE_SIGNALS = ("refund", "complaint", "broken", "faulty", "return it", "cancel my")

WORD = re.compile(r"[a-z0-9]{3,}")


@dataclass
class Prospect:
    """One past conversation worth picking up, and why."""

    jid: str
    number: str
    name: str
    last_message: str
    last_at: datetime
    matched: list[str] = field(default_factory=list)
    messages: int = 0

    @property
    def days_ago(self) -> int:
        return max(0, (datetime.now(timezone.utc) - self.last_at).days)


async def _catalogue_terms(db, organization_id) -> set[str]:
    """Words from this shop's own catalogue, so matching is theirs not generic.

    A shoe shop's leads say "size 42"; a lamp shop's do not. Without this the
    filter is the same for every tenant, which makes it wrong for most of them.
    """
    rows = await db.execute(
        select(KnowledgeDocument.title, KnowledgeDocument.content).where(
            KnowledgeDocument.organization_id == organization_id
        )
    )
    terms: set[str] = set()
    for title, content in rows.all():
        for word in WORD.findall(f"{title} {content}".lower()):
            # Long words only: short ones are articles and units, and matching
            # "the" against a conversation finds every conversation.
            if len(word) >= 5:
                terms.add(word)
    return terms


def _matches(text: str, terms: set[str]) -> list[str]:
    lowered = (text or "").lower()
    if any(bad in lowered for bad in EXCLUDE_SIGNALS):
        return []

    hits = [signal for signal in BUYING_SIGNALS if signal in lowered]
    hits += [term for term in terms if term in lowered]
    # Deduplicated, order kept, capped — this is shown to a person as the
    # reason a conversation is on the list.
    seen, unique = set(), []
    for hit in hits:
        if hit not in seen:
            seen.add(hit)
            unique.append(hit)
    return unique[:6]


async def read_history(channel) -> list[dict]:
    """Ask the bridge for the conversations WhatsApp pushed it. Read-only."""
    session_id = getattr(channel, "id", None)
    if session_id is None:
        return []

    url = f"{settings.wa_qr_service_url.rstrip('/')}/history/{session_id}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url, headers={"X-PingPulse-Bridge": settings.wa_qr_shared_secret}
            )
            response.raise_for_status()
            return list(response.json().get("chats") or [])
    except Exception as exc:  # noqa: BLE001
        logger.info("could not read conversation history: %s", exc)
        return []


async def find(db, organization_id, channel, window_days: int = DEFAULT_WINDOW_DAYS) -> list[Prospect]:
    """Conversations where a customer asked about something and got no reply."""
    chats = await read_history(channel)
    if not chats:
        return []

    terms = await _catalogue_terms(db, organization_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    found: list[Prospect] = []

    for chat in chats:
        messages = chat.get("messages") or []
        if not messages:
            continue

        last = messages[-1]
        # The shop got the last word, so the conversation was handled. Not a
        # lead — a customer who already has their answer.
        if last.get("fromMe"):
            continue

        when = datetime.fromtimestamp(int(last.get("at") or 0), tz=timezone.utc)
        if when < cutoff:
            continue

        # Match on what the customer said, never on the shop's own replies:
        # our catalogue words appear in our own messages by definition, and
        # matching those would make every conversation look like a lead.
        theirs = " ".join(m.get("text", "") for m in messages if not m.get("fromMe"))
        matched = _matches(theirs, terms)
        if not matched:
            continue

        jid = chat.get("jid") or ""
        found.append(
            Prospect(
                jid=jid,
                number=jid.split("@")[0].split(":")[0],
                name=chat.get("pushName") or "",
                last_message=(last.get("text") or "")[:280],
                last_at=when,
                matched=matched,
                messages=len(messages),
            )
        )

    # Most recent first: the freshest unanswered question is the one most
    # likely to still be worth answering.
    found.sort(key=lambda p: p.last_at, reverse=True)
    return found
