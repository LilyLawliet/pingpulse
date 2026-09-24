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
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Where hours read out of an uploaded document wait for a person to confirm
# them.
#
# Deliberately not `business_hours`: booking reads that key, so writing it
# would switch appointments on off the back of prose nobody had checked. This
# key changes nothing by itself. It only prefills the hours form, which is
# where a person says yes.
PROPOSED_KEY = "from_document"
# What the key was called when it only ever held hours. Read so a suggestion
# stored before this widened does not vanish; never written.
PROPOSED_HOURS_KEY = "hours_from_document"

# The config fields a document is allowed to fill in. Everything else on the
# form - what the agent must never promise, its pricing rules, the words that
# fetch a person - is an instruction to an agent rather than a description of
# a business, and no handbook contains one. A parser reaching for those would
# be guessing at policy and writing the guess into what customers are told.
DOCUMENT_FIELDS = ("business_hours", "services", "service_areas")

# Reasons to stop and fetch a person, whatever the conversation looked like up
# to that point. Deliberately blunt: the cost of handing over a conversation
# that did not need it is a person reading one extra message, and the cost of
# missing one is a sales pitch answering a complaint.
# Words that mean this conversation has gone wrong. Matched on word
# boundaries: "sue" as a bare substring fires on "issue", "tissue" and
# "pursue", and an escalation nobody can explain is one the shop learns to
# ignore.
ESCALATION_SIGNALS = (
    "refund", "complaint", "complain", "lawyer", "legal", "sue", "suing",
    "scam", "fraud", "terrible", "worst", "angry", "furious", "unacceptable",
    "ridiculous", "manager", "supervisor",
)

_WORDS = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in ESCALATION_SIGNALS) + r")\b",
    re.IGNORECASE,
)

# Asking for a person, in the ways people actually ask.
#
# The old list held "speak to a human" and "talk to a person" but not "talk to
# a human" or "talk to someone", so the two commonest phrasings in English
# missed it entirely. The agent then answered under a policy that tells it
# "You are the shop. You answer now" - which is what a client meant when they
# reported it presenting itself as a live team member. It was not pretending.
# It was never told to stand down.
_HUMAN_PATTERNS = (
    r"\b(speak|talk|chat|connect|deal)\s+(to|with)\s+(a\s+|an\s+|the\s+)?"
    r"(human|person|people|someone|somebody|agent|rep|representative|manager|"
    r"supervisor|owner|staff|real\s+\w+)",
    r"\b(put|get)\s+me\s+(through|onto|in\s+touch)",
    r"\bi\s+(want|need|would\s+like)\s+(to\s+\w+\s+(to\s+|with\s+)?)?(a\s+)?"
    r"(human|person|real\s+person|manager|supervisor)\b",
    r"\b(is\s+this|are\s+you)\s+(a\s+)?(bot|robot|ai|machine|human|real|"
    r"automated)",
    r"\bam\s+i\s+(talking|speaking|chatting)\s+(to|with)\s+"
    r"(a\s+)?(bot|robot|human|person|machine|computer)",
    r"\bwho\s+am\s+i\s+(talking|speaking)\s+(to|with)",
    r"\b(human|real\s+person)\s+please\b",
    r"\bcancel\s+my\s+order\b",
)

_HUMAN = re.compile("|".join(_HUMAN_PATTERNS), re.IGNORECASE)

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

    # A tenant's own words first: they added them because they know something
    # about their trade that this list does not.
    config = (getattr(organization, "agent_config", None) or {}) if organization else {}
    for word in (config.get("escalate_on") or []):
        candidate = str(word).lower().strip()
        if candidate and candidate in lowered:
            return candidate

    asking = _HUMAN.search(lowered)
    if asking:
        return asking.group(0)

    upset = _WORDS.search(lowered)
    if upset:
        return upset.group(0)

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


# ------------------------------------------------------------- quiet hours
# When an automated message must not arrive. Nothing here affects a reply to
# somebody who just wrote in - answering a customer at 3am is fine, because
# they chose the hour. This is only for messages the system starts itself.
DEFAULT_QUIET_START = time(21, 0)
DEFAULT_QUIET_END = time(8, 0)


def quiet_window(organization) -> tuple[time, time]:
    """The shop's own quiet hours, or a civilised default."""
    config = (getattr(organization, "agent_config", None) or {}) if organization else {}
    hours = config.get("quiet_hours") or {}
    start = _parse_time(hours.get("start")) or DEFAULT_QUIET_START
    end = _parse_time(hours.get("end")) or DEFAULT_QUIET_END
    return start, end


def in_quiet_hours(organization, at: datetime | None = None) -> bool:
    """Is it a time of night we should not be messaging anybody?"""
    start, end = quiet_window(organization)
    local = (at or datetime.now(timezone.utc)).astimezone(zone_of(organization))
    current = local.time()

    if start <= end:
        return start <= current < end
    # Spans midnight, which the default does: 21:00 to 08:00.
    return current >= start or current < end


def next_sendable_time(organization, at: datetime | None = None) -> datetime:
    """The next moment an automated message may go out, in UTC.

    Returns `at` unchanged when it is already fine, so a caller can compare
    the two to find out whether anything was deferred.
    """
    moment = at or datetime.now(timezone.utc)
    if not in_quiet_hours(organization, moment):
        return moment

    zone = zone_of(organization)
    _, end = quiet_window(organization)
    local = moment.astimezone(zone)

    candidate = local.replace(
        hour=end.hour, minute=end.minute, second=0, microsecond=0
    )
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


# --------------------------------------------------------------- validation
# Everything above is written to survive a config it does not understand: a
# duration of "soon" falls back to an hour, an unusable timezone falls back to
# UTC. That tolerance is right at reply time, where the alternative is a
# customer left with silence.
#
# It is wrong at save time. A shop that types its hours into the wrong shape
# gets no error, no hours, and an agent that quietly never offers an
# appointment again - and the settings page will happily show them back the
# broken value as though it had taken. So the same data is judged twice, and
# strictly here: refuse it while somebody is sitting in front of a form and
# can fix it.
#
# Unknown keys at the top level are left alone. Tenants carry notes there that
# predate this function, and a save that throws away what it does not
# recognise is a worse failure than one that keeps it.

# Keys whose value must be a list of strings. Written as a bare string these
# do not raise - they iterate character by character, which turns "refund"
# into six single-letter triggers, and `escalate_on` in particular then fires
# on the letter "r" in "hello there". Every conversation escalates to a human
# and nothing in the logs says why.
_LIST_KEYS = ("services", "service_areas", "languages", "escalate_on", "qualification_slots")

# Keys whose value is free text folded into the prompt.
_TEXT_KEYS = ("pricing_rules", "never_promise", "notes", "handoff_message")

# Minutes, and the range outside which a value is certainly a mistake. An
# eight-hour ceiling on a visit and a thirty-day one on notice are not
# opinions about how to run a business; they are the point past which the
# number is a typo.
_APPOINTMENT_NUMBERS = {
    "duration_minutes": (5, 8 * 60),
    "buffer_minutes": (0, 8 * 60),
    "min_notice_minutes": (0, 30 * 24 * 60),
}


def _check_time(problems: list[str], where: str, value) -> time | None:
    parsed = _parse_time(value)
    if parsed is None:
        problems.append(
            where + " is not a time. Use 24-hour HH:MM, like 09:00 or 17:30."
        )
    return parsed


def _check_business_hours(problems: list[str], hours) -> None:
    if not isinstance(hours, dict):
        problems.append(
            "business_hours must be an object keyed by day name, like "
            "monday: {open: 09:00, close: 17:00}."
        )
        return

    for day, window in hours.items():
        name = str(day).lower()
        if name not in DAYS:
            # A day this code will never look up. Ignored in silence before,
            # so "mon" or "Tues" meant a shop that had set its hours and had
            # none.
            problems.append(
                str(day) + " is not a day. Use one of: " + ", ".join(DAYS) + "."
            )
            continue
        if window in (None, False, {}, ""):
            continue  # Shut that day, said plainly.
        if not isinstance(window, dict):
            problems.append(
                name + " must have an open and a close time, like "
                "open 09:00, close 17:00."
            )
            continue

        opens = _check_time(problems, name + " open", window.get("open"))
        closes = _check_time(problems, name + " close", window.get("close"))
        if opens is not None and closes is not None and closes <= opens:
            # Appointments are never offered across midnight - that is how a
            # customer was told 1am. A shop saving 17:00-09:00 would get a day
            # that looks configured and can never be booked.
            problems.append(
                name + " closes at " + str(window.get("close")) + ", which is not "
                "after " + str(window.get("open")) + ". Hours that run past "
                "midnight cannot be used for appointments; split them across "
                "two days."
            )


def _check_appointments(problems: list[str], appointments) -> None:
    from app.models import APPOINTMENT_KINDS

    if not isinstance(appointments, dict):
        problems.append("appointments must be an object.")
        return

    known = set(_APPOINTMENT_NUMBERS) | {"enabled", "duration_by_kind", "default_kind"}
    for key in appointments:
        if key not in known:
            problems.append(
                "appointments has no setting called " + str(key) + ". It has: "
                + ", ".join(sorted(known)) + "."
            )

    if "enabled" in appointments and not isinstance(appointments["enabled"], bool):
        problems.append("appointments.enabled must be true or false.")

    for key, (low, high) in _APPOINTMENT_NUMBERS.items():
        if key not in appointments:
            continue
        value = appointments[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append("appointments." + key + " must be a number of minutes.")
        elif not low <= value <= high:
            problems.append(
                "appointments." + key + " must be between " + str(low) + " and "
                + str(high) + " minutes."
            )

    kind = appointments.get("default_kind")
    if kind is not None and str(kind).lower() not in APPOINTMENT_KINDS:
        problems.append(
            str(kind) + " is not a kind of appointment. Use one of: "
            + ", ".join(APPOINTMENT_KINDS) + "."
        )

    per_kind = appointments.get("duration_by_kind")
    if per_kind is not None:
        if not isinstance(per_kind, dict):
            problems.append(
                "appointments.duration_by_kind must be an object keyed by kind, "
                "like phone: 15, onsite: 90."
            )
        else:
            for name, minutes in per_kind.items():
                if str(name).lower() not in APPOINTMENT_KINDS:
                    problems.append(
                        str(name) + " is not a kind of appointment. Use one of: "
                        + ", ".join(APPOINTMENT_KINDS) + "."
                    )
                if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
                    problems.append(
                        "duration_by_kind." + str(name)
                        + " must be a number of minutes."
                    )
                elif not 5 <= minutes <= 8 * 60:
                    problems.append(
                        "duration_by_kind." + str(name)
                        + " must be between 5 and 480 minutes."
                    )


def _check_quiet_hours(problems: list[str], quiet) -> None:
    if not isinstance(quiet, dict):
        problems.append(
            "quiet_hours must be an object with a start and an end, like "
            "start 21:00, end 08:00."
        )
        return
    for edge in ("start", "end"):
        if quiet.get(edge) is not None:
            _check_time(problems, "quiet_hours." + edge, quiet[edge])


def validate(config) -> list[str]:
    """Everything wrong with this config, in words a shop owner can act on.

    An empty list means it is safe to store. Problems are returned rather than
    raised so the caller can report all of them at once: a form that rejects
    one field per attempt is how a person gives up halfway through setting
    their opening hours.
    """
    if not isinstance(config, dict):
        return ["Settings must be an object."]

    problems: list[str] = []

    if config.get("business_hours") is not None:
        _check_business_hours(problems, config["business_hours"])
    if config.get("appointments") is not None:
        _check_appointments(problems, config["appointments"])
    if config.get("quiet_hours") is not None:
        _check_quiet_hours(problems, config["quiet_hours"])

    for key in _LIST_KEYS:
        value = config.get(key)
        if value is None:
            continue
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            problems.append(
                key + " must be a list of separate entries, not one piece of text."
            )
        elif any(isinstance(item, (dict, list)) for item in value):
            problems.append("Every entry in " + key + " must be a piece of text.")

    for key in _TEXT_KEYS:
        value = config.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(key + " must be text.")

    return problems
