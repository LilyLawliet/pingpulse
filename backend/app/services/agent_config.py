"""The rules a shop sets for how its agent behaves.

Business hours, the areas it serves, the services it offers, what it must never
promise, and when it should stop and fetch a person. All of it reaches the model
as prompt text rather than as branches in code, for one reason: these are
judgements about a business, and a shop that wants "we don't quote for anything
under two hundred" served by an `if` would need a release to change its mind.

The exception is escalation. Deciding a conversation needs a person is the one
thing here that must not depend on the model noticing, because the cases that
need it most — an angry customer, a legal threat, somebody asking for a refund —
are exactly the ones a sales-tuned model is inclined to smooth over. That check
is keyword-driven and runs on the customer's own words.

Everything is optional. An organization with an empty config gets the agent it
has today, which is what keeps this safe to deploy to a shop mid-conversation.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Reasons to stop and fetch a person, whatever the conversation looked like up
# to that point. Deliberately blunt: the cost of handing over a conversation
# that did not need it is a person reading one extra message, and the cost of
# missing one is a sales pitch answering a complaint.
ESCALATION_SIGNALS = (
    "refund", "complaint", "lawyer", "legal", "sue", "scam", "fraud",
    "terrible", "worst", "angry", "furious", "unacceptable", "cancel my order",
    "speak to a human", "speak to someone", "talk to a person", "manager",
)

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    match = _TIME.match(str(value).strip())
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def zone_of(organization) -> ZoneInfo:
    """The organization's timezone, falling back to UTC rather than raising.

    A misconfigured timezone must not take the reply path down with it: the
    worst case here is an agent that answers about opening hours in the wrong
    zone, which is a wrong sentence rather than a silent customer.
    """
    name = (getattr(organization, "timezone", None) or "UTC").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("organization %s has an unusable timezone %r", getattr(organization, "id", "?"), name)
        return ZoneInfo("UTC")


def is_open(organization, at: datetime | None = None) -> bool | None:
    """Is the shop open right now?

    None when no hours are configured, which is different from False: a shop
    that has not told us its hours is not a shop that is shut, and treating it
    as one would have the agent apologising for being closed at every hour of
    the day.
    """
    config = getattr(organization, "agent_config", None) or {}
    hours = config.get("business_hours") or {}
    if not hours:
        return None

    zone = zone_of(organization)
    now = (at or datetime.now(timezone.utc)).astimezone(zone)
    today = hours.get(DAYS[now.weekday()])
    if not today:
        return False

    opens = _parse_time(today.get("open"))
    closes = _parse_time(today.get("close"))
    if opens is None or closes is None:
        return None

    current = now.time()
    if closes <= opens:
        # Spans midnight — open late, closes the next morning.
        return current >= opens or current <= closes
    return opens <= current <= closes


def needs_escalation(text: str, organization=None) -> str | None:
    """The phrase that means this conversation should reach a person, if any.

    Runs on the customer's own words, and on a keyword list rather than the
    model's judgement, because the conversations most in need of a person are
    the ones a sales-tuned model is most inclined to smooth over.
    """
    lowered = (text or "").lower()
    config = (getattr(organization, "agent_config", None) or {}) if organization else {}
    extra = tuple(
        str(word).lower().strip()
        for word in (config.get("escalate_on") or [])
        if str(word).strip()
    )
    for signal in ESCALATION_SIGNALS + extra:
        if signal in lowered:
            return signal
    return None


def as_prompt_block(organization, at: datetime | None = None) -> str:
    """How this shop wants its agent to behave, as prompt text.

    Empty for an organization that has configured nothing, which is what keeps
    this from changing the way a running client's agent answers the moment it
    deploys.
    """
    config = getattr(organization, "agent_config", None) or {}
    if not config:
        return ""

    lines: list[str] = ["=== HOW THIS BUSINESS OPERATES ==="]

    services = [str(s).strip() for s in (config.get("services") or []) if str(s).strip()]
    if services:
        lines.append("Services offered: " + ", ".join(services[:20]))
        lines.append(
            "If asked for something not on that list, say plainly that it is not "
            "something this business does. Do not improvise an offering."
        )

    areas = [str(a).strip() for a in (config.get("service_areas") or []) if str(a).strip()]
    if areas:
        lines.append("Areas served: " + ", ".join(areas[:20]))
        lines.append(
            "For anywhere else, say it is outside the area served rather than "
            "promising to check."
        )

    languages = [str(s).strip() for s in (config.get("languages") or []) if str(s).strip()]
    if languages:
        lines.append("Languages this business answers in: " + ", ".join(languages[:8]))

    open_now = is_open(organization, at)
    if open_now is False:
        zone = zone_of(organization)
        now = (at or datetime.now(timezone.utc)).astimezone(zone)
        lines.append(
            f"The business is currently CLOSED (local time {now:%H:%M} {now:%A}). "
            "Answer the question anyway, and where something needs a person, say "
            "when they will be back rather than promising an immediate call."
        )

    if config.get("pricing_rules"):
        lines.append(f"Pricing rules: {str(config['pricing_rules'])[:600]}")

    if config.get("never_promise"):
        lines.append(f"Never promise: {str(config['never_promise'])[:400]}")

    if config.get("notes"):
        lines.append(str(config["notes"])[:800])

    # A block with only its heading is worse than no block: it spends attention
    # and says nothing.
    return "\n".join(lines) if len(lines) > 1 else ""
