"""Asking before handing a conversation to a person, when only a guess says to.

Handing over is not a small thing on WhatsApp: the agent stops replying until
somebody at the shop picks the conversation up. So it follows the same rule as
a booking. The customer's own words for a person - "a human", "the manager",
"STOP" - act at once, because they are what they say. A reading of the message
by the model is a guess, and a guess is put to them as a question; their yes
hands it over.

The reading is loose in a way no list of phrasings fixes: "book one with ahmed
name" - book it under the name Ahmed - was read as asking for somebody called
Ahmed, and handed over. Asked first, the worst a misreading costs is one
question the customer answers with "no".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

PENDING_KEY = "pending_handover"
VALID_MINUTES = 30

DEFAULT_QUESTION = (
    "Would you like me to pass this conversation to the team? "
    "Reply YES and I'll hand it over."
)


def question(organization) -> str:
    """The shop's own wording, or the default. Never says it has already happened."""
    config = (getattr(organization, "agent_config", None) or {}) if organization else {}
    return str(config.get("handover_question") or "").strip() or DEFAULT_QUESTION


def ask(metadata: dict | None) -> dict:
    """The metadata with the question recorded as asked.

    Any booking read-back waiting on a yes is dropped: one "yes" must not both
    book a visit and hand the conversation over.
    """
    from app.services import booking

    kept = dict(metadata or {})
    kept.pop(booking.PENDING_KEY, None)
    kept[PENDING_KEY] = {"made": datetime.now(timezone.utc).isoformat()}
    return kept


def asked(metadata: dict | None) -> bool:
    """Whether the question was put to them recently enough for a yes to answer it."""
    held = (metadata or {}).get(PENDING_KEY)
    if not isinstance(held, dict):
        return False
    try:
        made = datetime.fromisoformat(held["made"])
    except (KeyError, TypeError, ValueError):
        return False
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - made <= timedelta(minutes=VALID_MINUTES)


def forget(metadata: dict | None) -> dict:
    kept = dict(metadata or {})
    kept.pop(PENDING_KEY, None)
    return kept
