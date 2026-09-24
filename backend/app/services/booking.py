"""Appointments: checking a time is free, taking it, and giving it back.

Every function here returns what actually happened. That is the whole design
constraint, and it comes from what went wrong: a customer was told her
appointment was confirmed for 1am on a date she never chose, and then told it
had been cancelled. Neither statement was checkable, because there was nothing
in the database that either one referred to.

So nothing in this module reports success it has not verified. `book` returns
an Appointment row or a Refusal explaining why not. `cancel` and `reschedule`
do the same. A caller that wants to tell a customer something looks at the
result, never at its own intention.

Three rules the rest of the system depends on:

*A slot is free only if the database says so at the moment of writing.*
Checking availability and then inserting are two statements, and two customers
can sit between them. The check here is a courtesy that produces good error
messages; the guarantee is an exclusion constraint in PostgreSQL, and a clash
that gets past the check is caught as an IntegrityError and reported as a
clash rather than crashing.

*Times are held in UTC and spoken in the shop's zone.* The zone is written
onto the row, so a business that later moves timezone does not silently
reschedule every appointment it has already agreed.

*A cancellation updates, never deletes.* "Did you cancel that?" is a question
somebody has to be able to answer afterwards.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_KINDS,
    Appointment,
)
from app.services import agent_config

logger = logging.getLogger(__name__)

# How long an appointment runs when the shop has not said. Half an hour is the
# common case for a consultation and short enough that a wrong guess wastes
# less of somebody's day than a long one would.
DEFAULT_DURATION_MINUTES = 60

# Quiet gap kept either side of an appointment. For a trade that travels to
# the customer this is the drive; for a phone consultation it is usually zero.
DEFAULT_BUFFER_MINUTES = 0

# How far ahead a customer may book. A year out is almost always a typo.
MAX_DAYS_AHEAD = 180

# The shortest notice the shop will take. Booking something for nine minutes
# from now is a promise nobody can keep.
DEFAULT_MIN_NOTICE_MINUTES = 120

# How many candidate slots to offer at once. A wall of times is not a choice.
MAX_OFFERED_SLOTS = 6

# Granularity of the offered times. On the hour and half hour reads like a
# diary; every seven minutes reads like a machine.
SLOT_STEP_MINUTES = 30


@dataclass(frozen=True)
class Refusal:
    """Why something could not be done, in words a customer could be told.

    `reason` is a stable key for code to branch on. `message` is the sentence,
    and is deliberately free of blame and of internal vocabulary - a customer
    reading "IntegrityError" has learned nothing.
    """

    reason: str
    message: str

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class Booked:
    """A real appointment, and the only thing that proves one exists."""

    appointment: Appointment

    @property
    def ok(self) -> bool:
        return True


# ------------------------------------------------------------------ settings
def _config(organization) -> dict:
    return (getattr(organization, "agent_config", None) or {}) if organization else {}


def duration_minutes(organization, kind: str | None = None) -> int:
    """How long this kind of appointment takes at this shop.

    A phone consultation and a site visit are rarely the same length, and a
    shop that has said so should not have both booked as an hour.
    """
    config = _config(organization).get("appointments") or {}
    per_kind = config.get("duration_by_kind") or {}
    if kind and isinstance(per_kind, dict) and per_kind.get(kind):
        try:
            return max(5, int(per_kind[kind]))
        except (TypeError, ValueError):
            pass
    try:
        return max(5, int(config.get("duration_minutes", DEFAULT_DURATION_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_DURATION_MINUTES


def buffer_minutes(organization) -> int:
    config = _config(organization).get("appointments") or {}
    try:
        return max(0, int(config.get("buffer_minutes", DEFAULT_BUFFER_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_BUFFER_MINUTES


def min_notice_minutes(organization) -> int:
    config = _config(organization).get("appointments") or {}
    try:
        return max(0, int(config.get("min_notice_minutes", DEFAULT_MIN_NOTICE_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_NOTICE_MINUTES


def default_kind(organization) -> str:
    config = _config(organization).get("appointments") or {}
    kind = str(config.get("default_kind") or "onsite").lower()
    return kind if kind in APPOINTMENT_KINDS else "onsite"


def booking_enabled(organization) -> bool:
    """Whether this shop takes appointments at all.

    A shop with no business hours configured cannot have its availability
    checked, and offering times against hours nobody set would be inventing
    them - which is the fault this module exists to end.
    """
    config = _config(organization)
    if config.get("appointments", {}).get("enabled") is False:
        return False
    return bool(config.get("business_hours"))


# ------------------------------------------------------------- opening hours
def _window_for(organization, day: date) -> tuple[time, time] | None:
    """The shop's open and close on this local date, or None if shut."""
    hours = (_config(organization).get("business_hours") or {}).get(
        agent_config.DAYS[day.weekday()]
    )
    if not hours:
        return None
    opens = agent_config._parse_time(hours.get("open"))
    closes = agent_config._parse_time(hours.get("close"))
    if opens is None or closes is None:
        return None
    if closes <= opens:
        # Spans midnight. Appointments are not offered across a day boundary -
        # "Tuesday at 1am" is how this went wrong in the first place.
        return None
    return opens, closes


def within_business_hours(organization, starts_at: datetime, ends_at: datetime) -> bool:
    """Does this whole appointment fall inside one day's opening hours?"""
    zone = agent_config.zone_of(organization)
    local_start = starts_at.astimezone(zone)
    local_end = ends_at.astimezone(zone)

    if local_start.date() != local_end.date():
        return False

    window = _window_for(organization, local_start.date())
    if window is None:
        return False
    opens, closes = window
    return local_start.time() >= opens and local_end.time() <= closes


# --------------------------------------------------------------- the diary
async def live_appointments(
    db, organization_id, since: datetime, until: datetime
) -> list[Appointment]:
    """Confirmed appointments overlapping this span. Nothing else counts."""
    rows = (
        await db.execute(
            select(Appointment).where(
                Appointment.organization_id == organization_id,
                Appointment.status == APPOINTMENT_CONFIRMED,
                Appointment.ends_at > since,
                Appointment.starts_at < until,
            )
        )
    ).scalars().all()
    return list(rows)


def _clashes(
    starts_at: datetime,
    ends_at: datetime,
    taken: list[Appointment],
    buffer: int,
    ignore_id=None,
) -> Appointment | None:
    """The appointment this one would run into, if any.

    The buffer is applied to the existing appointments rather than to the new
    one, so a shop with a thirty-minute travel buffer keeps half an hour clear
    on both sides of every visit it has already agreed.
    """
    gap = timedelta(minutes=buffer)
    for held in taken:
        if ignore_id is not None and held.id == ignore_id:
            continue
        if starts_at < _aware(held.ends_at) + gap and ends_at > _aware(held.starts_at) - gap:
            return held
    return None


def _aware(moment: datetime) -> datetime:
    """Naive from SQLite, aware from PostgreSQL. Both mean UTC."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


async def is_free(
    db, organization, starts_at: datetime, ends_at: datetime, ignore_id=None
) -> Refusal | None:
    """None if the slot can be taken, or the reason it cannot.

    Every check a customer could trip over, in the order they would hit them,
    so the first answer they get is the most useful one.
    """
    now = datetime.now(timezone.utc)

    if ends_at <= starts_at:
        return Refusal("backwards", "That appointment would end before it started.")

    notice = min_notice_minutes(organization)
    if starts_at < now + timedelta(minutes=notice):
        hours = max(1, round(notice / 60))
        return Refusal(
            "too_soon",
            f"That is too soon - appointments need at least {hours} hour"
            f"{'s' if hours != 1 else ''} notice.",
        )

    if starts_at > now + timedelta(days=MAX_DAYS_AHEAD):
        return Refusal(
            "too_far",
            f"That is further ahead than bookings are taken ({MAX_DAYS_AHEAD} days).",
        )

    if not within_business_hours(organization, starts_at, ends_at):
        return Refusal("closed", "The business is not open at that time.")

    buffer = buffer_minutes(organization)
    taken = await live_appointments(
        db,
        organization.id,
        starts_at - timedelta(minutes=buffer + 1),
        ends_at + timedelta(minutes=buffer + 1),
    )
    if _clashes(starts_at, ends_at, taken, buffer, ignore_id=ignore_id):
        return Refusal("taken", "That time has already been booked.")

    return None


async def free_slots(
    db,
    organization,
    *,
    from_time: datetime | None = None,
    days: int = 7,
    kind: str | None = None,
    limit: int = MAX_OFFERED_SLOTS,
) -> list[datetime]:
    """Real times this shop could actually see somebody, soonest first.

    Built from the shop's own hours and its own diary. If it returns nothing,
    the honest answer to "when are you free?" is that there is nothing to
    offer - not a time invented to fill the silence.
    """
    if not booking_enabled(organization):
        return []

    zone = agent_config.zone_of(organization)
    now = datetime.now(timezone.utc)
    start_from = max(from_time or now, now + timedelta(minutes=min_notice_minutes(organization)))

    length = timedelta(minutes=duration_minutes(organization, kind))
    buffer = buffer_minutes(organization)

    window_end = start_from + timedelta(days=days)
    taken = await live_appointments(db, organization.id, start_from, window_end)

    found: list[datetime] = []
    for offset in range(days + 1):
        day = (start_from.astimezone(zone) + timedelta(days=offset)).date()
        window = _window_for(organization, day)
        if window is None:
            continue
        opens, closes = window

        cursor = datetime.combine(day, opens, tzinfo=zone).astimezone(timezone.utc)
        day_ends = datetime.combine(day, closes, tzinfo=zone).astimezone(timezone.utc)

        # Start on a tidy boundary rather than at whatever minute it is now.
        if cursor < start_from:
            minutes = (start_from - cursor).total_seconds() / 60
            steps = int(minutes // SLOT_STEP_MINUTES) + 1
            cursor = cursor + timedelta(minutes=steps * SLOT_STEP_MINUTES)

        while cursor + length <= day_ends:
            if not _clashes(cursor, cursor + length, taken, buffer):
                found.append(cursor)
                if len(found) >= limit:
                    return found
            cursor += timedelta(minutes=SLOT_STEP_MINUTES)

    return found


# ------------------------------------------------------------- the operations
async def book(
    db,
    organization,
    contact,
    starts_at: datetime,
    *,
    kind: str | None = None,
    location: str | None = None,
    notes: str | None = None,
    source: str = "agent",
) -> Booked | Refusal:
    """Take a slot, or say why it could not be taken. Never claims success.

    The row is written as confirmed in one statement so the exclusion
    constraint arbitrates. Two customers asking for the same time at the same
    moment both pass the availability check above; exactly one of them gets
    past this.
    """
    if not booking_enabled(organization):
        return Refusal(
            "not_configured",
            "Appointments are not set up for this business yet.",
        )

    chosen_kind = (kind or default_kind(organization)).lower()
    if chosen_kind not in APPOINTMENT_KINDS:
        chosen_kind = default_kind(organization)

    ends_at = starts_at + timedelta(minutes=duration_minutes(organization, chosen_kind))

    refusal = await is_free(db, organization, starts_at, ends_at)
    if refusal is not None:
        return refusal

    appointment = Appointment(
        organization_id=organization.id,
        contact_id=contact.id,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone_name=str(agent_config.zone_of(organization)),
        kind=chosen_kind,
        status=APPOINTMENT_CONFIRMED,
        location=(location or None),
        notes=(notes or None),
        source=source,
    )
    # Inside a savepoint, so a clash undoes the appointment and nothing else.
    # A plain rollback here would discard everything else pending on this
    # session - including the customer's own inbound message, which is written
    # earlier in the same request. Losing the message that asked for the
    # booking would be a far stranger bug than the clash it came from.
    try:
        async with db.begin_nested():
            db.add(appointment)
            await db.flush()
    except IntegrityError:
        # Somebody else took it between the check and the write. This is the
        # case the constraint exists for, and it must read to the customer as
        # a taken slot rather than as an error.
        logger.info("slot %s was taken during booking for %s", starts_at, contact.id)
        return Refusal("taken", "That time has just been booked by somebody else.")

    return Booked(appointment)


async def cancel(db, appointment, *, source: str = "agent") -> Booked | Refusal:
    """Cancel a real appointment. Nothing here invents one to cancel."""
    if appointment is None:
        return Refusal("not_found", "There is no appointment booked to cancel.")
    if appointment.status == APPOINTMENT_CANCELLED:
        return Refusal("already_cancelled", "That appointment was already cancelled.")
    if appointment.status != APPOINTMENT_CONFIRMED:
        return Refusal(
            "not_confirmed", "There is no confirmed appointment to cancel."
        )

    appointment.status = APPOINTMENT_CANCELLED
    appointment.cancelled_at = datetime.now(timezone.utc)
    appointment.source = source
    await db.flush()
    return Booked(appointment)


async def reschedule(
    db, organization, appointment, starts_at: datetime, *, source: str = "agent"
) -> Booked | Refusal:
    """Move an appointment, leaving exactly one live row behind.

    The old row is cancelled and a new one written, chained by `replaces_id`.
    Cancelling first is what makes the new time bookable when somebody moves
    an appointment by an hour and the two would otherwise overlap each other.
    """
    if appointment is None:
        return Refusal("not_found", "There is no appointment booked to move.")
    if appointment.status != APPOINTMENT_CONFIRMED:
        return Refusal("not_confirmed", "There is no confirmed appointment to move.")

    ends_at = starts_at + timedelta(
        minutes=duration_minutes(organization, appointment.kind)
    )
    refusal = await is_free(
        db, organization, starts_at, ends_at, ignore_id=appointment.id
    )
    if refusal is not None:
        return refusal

    previous_status = appointment.status
    moved = Appointment(
        organization_id=organization.id,
        contact_id=appointment.contact_id,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone_name=appointment.timezone_name,
        kind=appointment.kind,
        status=APPOINTMENT_CONFIRMED,
        location=appointment.location,
        notes=appointment.notes,
        source=source,
        replaces_id=appointment.id,
    )

    # Both halves in one savepoint. A move is a single change from the
    # customer's point of view, and the half-done version of it - their old
    # appointment cancelled, the new one refused - is the worst outcome
    # available: they would be left with nothing, having asked for a change.
    try:
        async with db.begin_nested():
            appointment.status = APPOINTMENT_CANCELLED
            appointment.cancelled_at = datetime.now(timezone.utc)
            await db.flush()
            db.add(moved)
            await db.flush()
    except IntegrityError:
        appointment.status = previous_status
        appointment.cancelled_at = None
        logger.info("reschedule to %s clashed for contact %s", starts_at, appointment.contact_id)
        return Refusal("taken", "That time has just been booked by somebody else.")

    return Booked(moved)


# ------------------------------------------------------------- what they have
async def upcoming_for(db, contact_id) -> Appointment | None:
    """The contact's next confirmed appointment, or None.

    Every confirmation, reminder and answer to "when am I booked?" reads this
    rather than assembling a date from the conversation.
    """
    now = datetime.now(timezone.utc)
    return (
        await db.execute(
            select(Appointment)
            .where(
                Appointment.contact_id == contact_id,
                Appointment.status == APPOINTMENT_CONFIRMED,
                Appointment.ends_at >= now,
            )
            .order_by(Appointment.starts_at.asc())
            .limit(1)
        )
    ).scalars().first()



async def upcoming_for_many(db, contact_ids) -> dict:
    """Next confirmed appointment per contact, for a whole board at once.

    One query rather than one per contact: a lead list is drawn on every
    dashboard load, and a feature that makes that slow is a feature somebody
    turns off.
    """
    ids = [cid for cid in contact_ids if cid is not None]
    if not ids:
        return {}

    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Appointment)
            .where(
                Appointment.contact_id.in_(ids),
                Appointment.status == APPOINTMENT_CONFIRMED,
                Appointment.ends_at >= now,
            )
            .order_by(Appointment.starts_at.asc())
        )
    ).scalars().all()

    # Earliest wins: the first row seen for a contact is their next one.
    found: dict = {}
    for row in rows:
        found.setdefault(row.contact_id, row)
    return found


def as_summary(appointment) -> dict | None:
    """What the dashboard shows about an appointment, including the words.

    The sentence is rendered here rather than in the browser so that every
    surface - dashboard, desktop build, anything later - says the same thing,
    and so no client has to work out what "onsite" means to a customer.
    """
    if appointment is None:
        return None
    return {
        "id": str(appointment.id),
        "status": appointment.status,
        "kind": appointment.kind,
        "starts_at": appointment.starts_at,
        "ends_at": appointment.ends_at,
        "timezone": appointment.timezone_name,
        "location": appointment.location,
        "description": describe(appointment),
    }

# ------------------------------------------------------------------- wording
KIND_WORDS = {
    "phone": "phone consultation",
    "onsite": "site visit",
    "video": "video call",
    "other": "appointment",
}


def describe(appointment) -> str:
    """The confirmation sentence, rendered from the row and nothing else.

    Includes the kind, because "confirmed for Tuesday at 2pm" told one
    customer nothing about whether somebody was going to ring her or turn up
    at her house.
    """
    zone = ZoneInfo(appointment.timezone_name or "UTC")
    local = _aware(appointment.starts_at).astimezone(zone)

    # %-d and %-I are not portable to Windows, so the padding is stripped by
    # hand rather than by a format code that works on one developer's machine.
    day = local.strftime("%A %d %B").replace(" 0", " ")
    clock = local.strftime("%I:%M %p").lstrip("0").lower()
    label = KIND_WORDS.get(appointment.kind, "appointment")

    where = f" at {appointment.location}" if appointment.location else ""
    return f"{label} on {day} at {clock}{where}"


# ------------------------------------------------- claims the agent may not make
# Prompt wording is not a control. The model was told plainly that it was the
# shop and that it must not promise a callback, and it still wrote "I am a
# live team member here" - so the sentences that assert a booking are checked
# against the record before they are allowed out.
#
# This is the same shape as the banned-handoff check and the price guard: the
# output is inspected rather than trusted, because the failure mode is a
# confident sentence rather than an error.
_BOOKING_CLAIMS = re.compile(
    r"\b("
    r"appointment is (now )?(confirmed|booked|scheduled|set)"
    r"|you('re| are) (all )?(booked|scheduled|confirmed)"
    r"|i('ve| have) (now )?(booked|scheduled|confirmed|reserved)"
    r"|(booking|appointment) (is )?confirmed"
    r"|confirmed for \w+"
    r"|see you (on|at) \w+"
    r"|we('ll| will) see you (on|at)"
    r"|your (visit|estimate|consultation) (is|on)"
    r")",
    re.IGNORECASE,
)

_CANCEL_CLAIMS = re.compile(
    r"\b("
    r"i('ve| have) (now )?cancell?ed"
    r"|(has|have) been cancell?ed"
    r"|(booking|appointment) (is )?cancell?ed"
    r"|cancell?ed (your|the) (appointment|booking|visit)"
    r")",
    re.IGNORECASE,
)

_MOVE_CLAIMS = re.compile(
    r"\b("
    r"i('ve| have) (now )?(moved|rescheduled|changed)"
    r"|(has|have) been (moved|rescheduled)"
    r"|(booking|appointment) (is )?(moved|rescheduled)"
    r")",
    re.IGNORECASE,
)


def claims_appointment(text: str) -> str | None:
    """The phrase asserting a booking exists, or None."""
    found = _BOOKING_CLAIMS.search(text or "")
    return found.group(0) if found else None


def claims_cancellation(text: str) -> str | None:
    """The phrase asserting something was cancelled, or None.

    "I am sorry for the confusion; I have cancelled the September 19
    appointment" was written about an appointment that never existed, so this
    matters as much as the booking claim and was the half nobody reported.
    """
    found = _CANCEL_CLAIMS.search(text or "")
    return found.group(0) if found else None


def claims_reschedule(text: str) -> str | None:
    found = _MOVE_CLAIMS.search(text or "")
    return found.group(0) if found else None


def unverified_claims(
    text: str, *, appointment=None, cancelled: bool = False, moved: bool = False
) -> list[str]:
    """Every claim in this reply that the record does not support.

    `appointment` is the contact's live appointment, freshly read. `cancelled`
    and `moved` say whether this turn actually performed one of those
    operations - a reply may only announce an action the backend just took.
    """
    problems: list[str] = []

    booked = claims_appointment(text)
    if booked and appointment is None:
        problems.append(
            f'you wrote "{booked}", but there is no confirmed appointment for this '
            "customer; never state that a booking exists unless it does"
        )

    said_cancelled = claims_cancellation(text)
    if said_cancelled and not cancelled:
        problems.append(
            f'you wrote "{said_cancelled}", but nothing was cancelled; never say an '
            "appointment has been cancelled unless it has"
        )

    said_moved = claims_reschedule(text)
    if said_moved and not moved:
        problems.append(
            f'you wrote "{said_moved}", but nothing was rescheduled; never say an '
            "appointment has been moved unless it has"
        )

    return problems


# ---------------------------------------------------- offering and choosing
# The agent never parses a date out of free text, and never constructs one.
# It offers times that came out of `free_slots` - real gaps in a real diary -
# and the customer picks one of those. Matching a reply against a short list
# of known times is a small, checkable problem; understanding "the Tuesday
# after next, late morning" is not, and getting it wrong is how somebody was
# booked for 1am.
#
# The offer is remembered on the contact so the next message can be matched
# against it, and it expires, because a customer answering "yes, the first
# one" three days later means a different Tuesday.
OFFER_KEY = "offered_slots"
OFFER_VALID_MINUTES = 120

# Checked in this order, and the order is the point: "one" used to be a
# synonym for the first slot and it appears inside "the last one", so a
# customer asking for the last time offered was given the first one. Both
# times were real, which is what made it quiet.
#
# The bare number words are gone for the same reason - "two" is in "two weeks"
# and "three" is in "three bedrooms". Digits and true ordinals only.
_ORDINALS: tuple[tuple[str, int], ...] = (
    ("last", -1),
    ("first", 0),
    ("1st", 0),
    ("earliest", 0),
    ("soonest", 0),
    ("second", 1),
    ("2nd", 1),
    ("third", 2),
    ("3rd", 2),
    ("fourth", 3),
    ("4th", 3),
    ("fifth", 4),
    ("5th", 4),
    ("sixth", 5),
    ("6th", 5),
    ("1", 0),
    ("2", 1),
    ("3", 2),
    ("4", 3),
    ("5", 4),
    ("6", 5),
)

_TIME_IN_TEXT = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", re.IGNORECASE
)


def remember_offer(contact, slots: list[datetime]) -> None:
    """Write down exactly what was offered, so a reply can be matched to it."""
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    metadata[OFFER_KEY] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "slots": [slot.isoformat() for slot in slots],
    }
    contact.contact_metadata = metadata


def remembered_offer(contact) -> list[datetime]:
    """What this contact was last offered, if it is still fresh."""
    metadata = getattr(contact, "contact_metadata", None) or {}
    offer = metadata.get(OFFER_KEY) or {}
    try:
        made = datetime.fromisoformat(offer["at"])
    except (KeyError, TypeError, ValueError):
        return []
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - made > timedelta(minutes=OFFER_VALID_MINUTES):
        return []

    out = []
    for raw in offer.get("slots") or []:
        try:
            moment = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        out.append(moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))
    return out


def forget_offer(contact) -> None:
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    metadata.pop(OFFER_KEY, None)
    contact.contact_metadata = metadata


def chosen_slot(text: str, offered: list[datetime], zone: ZoneInfo) -> datetime | None:
    """Which of the offered times this reply picked, or None.

    Returns None whenever there is any doubt. A customer who said something
    ambiguous gets asked again, which costs one message; a wrong guess costs
    them a morning.
    """
    if not offered:
        return None
    lowered = (text or "").lower()

    # A day name that matches exactly one offered slot is unambiguous.
    days = {slot: slot.astimezone(zone).strftime("%A").lower() for slot in offered}
    named = [slot for slot, day in days.items() if day in lowered]

    # A clock time mentioned in the message.
    wanted_times: list[tuple[int, int]] = []
    for match in _TIME_IN_TEXT.finditer(lowered):
        if match.group(3):  # 12-hour with am/pm
            hour = int(match.group(1)) % 12
            if match.group(3).lower() == "pm":
                hour += 12
            wanted_times.append((hour, int(match.group(2) or 0)))
        else:  # 24-hour
            wanted_times.append((int(match.group(4)), int(match.group(5))))

    if wanted_times:
        pool = named or offered
        matching = [
            slot
            for slot in pool
            if (slot.astimezone(zone).hour, slot.astimezone(zone).minute) in wanted_times
        ]
        if len(matching) == 1:
            return matching[0]
        # A time that matches several days and no day was named: ambiguous.
        return None

    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        return None

    # "the first one", "number 2", "the last one".
    for word, index in _ORDINALS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            try:
                return offered[index]
            except IndexError:
                return None

    return None


def as_prompt_block(organization, contact, slots: list[datetime], appointment=None) -> str:
    """What the agent is allowed to say about appointments this turn.

    Either real times or none. There is deliberately no branch that produces
    an encouraging sentence with no times behind it, because that is the gap
    the model filled in by itself.
    """
    zone = agent_config.zone_of(organization)

    if appointment is not None:
        when = describe(appointment)
        return (
            "=== THIS CUSTOMER'S APPOINTMENT ===\n"
            f"They have a confirmed {when}.\n"
            "If they ask, tell them exactly that. Do not restate it in a different "
            "form and do not add a time, a date or an address that is not in it.\n"
            "To change or cancel it, ask them what they want and say you will see "
            "to it - do not state that it has been changed or cancelled."
        )

    if not booking_enabled(organization):
        return (
            "=== APPOINTMENTS ===\n"
            "This business has not set up appointment booking. Do NOT offer to book "
            "anything, do NOT suggest times, and do NOT say an appointment exists. "
            "Find out what they need and answer it here."
        )

    if not slots:
        return (
            "=== APPOINTMENTS ===\n"
            "There is nothing free in the diary for the next few days. Say so plainly "
            "and ask what times would suit them, so a person can look. Do NOT invent "
            "a time and do NOT say anything is booked."
        )

    lines = []
    for index, slot in enumerate(slots, start=1):
        local = slot.astimezone(zone)
        day = local.strftime("%A %d %B").replace(" 0", " ")
        clock = local.strftime("%I:%M %p").lstrip("0").lower()
        lines.append(f"  {index}. {day} at {clock}")

    return (
        "=== APPOINTMENTS ===\n"
        "These times are genuinely free in the diary right now:\n"
        + "\n".join(lines)
        + "\nOffer some of these and let them choose. Use these exact times - do not "
        "round them, shift them or invent others. Nothing is booked until they pick "
        "one and you are told it succeeded, so do NOT say anything is confirmed yet."
    )


# ------------------------------------------------------------ the turn itself
# Intent words, and they mean something different here than they used to. The
# old rule read "schedule" in a customer's message and moved the lead to
# ESTIMATE_SCHEDULED - the word *was* the event. These trigger a lookup: is
# there an appointment, is that time free, is the qualification complete. The
# record changes only if the lookup says it may.
_WANTS_CANCEL = re.compile(
    r"\b(cancel|call it off|drop the|don'?t (want|need) the)\b.{0,40}"
    r"\b(appointment|booking|visit|estimate|consultation|slot|it)\b"
    r"|\bcancel (it|that|my appointment|the appointment)\b",
    re.IGNORECASE,
)

_WANTS_MOVE = re.compile(
    r"\b(reschedul\w*|re-?book|move|change|shift|push)\b.{0,40}"
    r"\b(appointment|booking|visit|estimate|consultation|slot|time|it)\b"
    r"|\b(different|another|earlier|later) (time|day|slot)\b",
    re.IGNORECASE,
)

_WANTS_BOOKING = re.compile(
    r"\b("
    # Naming the thing.
    r"book|booking|schedul\w*|appointment|consultation|estimate|survey|"
    r"viewing|site visit|home visit"
    # Asking when.
    r"|when (can|could|are|do|would) you"
    r"|what (times?|days?|slots?)"
    r"|which (times?|days?|slots?)"
    r"|any (times?|days?|slots?|openings?)"
    r"|(times?|days?|slots?) (are |is )?(free|available|open)"
    r"|availab\w*"
    r"|free (on|this|next|tomorrow|today)"
    # Asking somebody to come.
    r"|come (out|round|over|and see|to see|by)"
    r"|(can|could) (you|someone|somebody) come"
    r"|send (someone|somebody)"
    r"|pop (round|over|by)"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class TurnResult:
    """What this turn did about appointments, and what to tell the agent.

    `performed` is the only thing that may license the agent to announce an
    action, and it is set from a completed operation rather than from an
    intention.
    """

    prompt_block: str = ""
    appointment: "Appointment | None" = None
    performed: str | None = None          # "booked" | "cancelled" | "moved"
    refusal: "Refusal | None" = None
    offered: list = None                  # noqa: RUF012 - set in __post_init__

    def __post_init__(self):
        if self.offered is None:
            self.offered = []

    @property
    def booked(self) -> bool:
        return self.performed == "booked"

    @property
    def cancelled(self) -> bool:
        return self.performed == "cancelled"

    @property
    def moved(self) -> bool:
        return self.performed == "moved"


def wants_cancel(text: str) -> bool:
    return bool(_WANTS_CANCEL.search(text or ""))


def wants_move(text: str) -> bool:
    return bool(_WANTS_MOVE.search(text or ""))


def wants_booking(text: str) -> bool:
    return bool(_WANTS_BOOKING.search(text or ""))


def _missing_for_booking(organization, contact) -> list:
    """Configured questions still unanswered, which hold a booking back.

    A shop that asks for the property address before sending somebody out is
    asking for a reason, and a booking made without it is a van with nowhere
    to go.
    """
    from app.services import qualification

    # Only what this shop explicitly asked for. `slots_for` falls back to six
    # sensible defaults so the agent has something to ask about in
    # conversation, and gating bookings on those would stop every shop that
    # never opened the settings page from taking a single appointment -
    # including the ones that took them happily before any of this existed.
    #
    # A gate is a promise the shop made to itself. It only exists once they
    # make it.
    config = _config(organization)
    if not isinstance(config.get("qualification_slots"), list):
        return []
    if not config["qualification_slots"]:
        return []

    return qualification.missing(organization, contact.qualification)


async def handle_turn(db, organization, contact, text: str) -> TurnResult:
    """Do the appointment work for one inbound message. Never raises.

    Returns what actually happened. The caller puts `prompt_block` in front of
    the model and passes `performed` to the reply guard, so a sentence
    announcing a booking can only survive if a booking was made.
    """
    try:
        return await _handle_turn(db, organization, contact, text)
    except Exception as exc:  # noqa: BLE001 - never at the cost of a reply
        logger.warning("appointment handling failed for %s: %s", contact.id, exc)
        return TurnResult()


async def _handle_turn(db, organization, contact, text: str) -> TurnResult:
    existing = await upcoming_for(db, contact.id)
    zone = agent_config.zone_of(organization)

    # ---------------------------------------------------------- cancelling
    if wants_cancel(text):
        if existing is None:
            return TurnResult(
                prompt_block=(
                    "=== APPOINTMENTS ===\n"
                    "They asked to cancel, and there is NO appointment booked for "
                    "them. Say plainly that you cannot find a booking in their name, "
                    "and do NOT say anything has been cancelled."
                ),
                refusal=Refusal("not_found", "There is no appointment booked to cancel."),
            )
        result = await cancel(db, existing)
        if result.ok:
            forget_offer(contact)
            return TurnResult(
                prompt_block=(
                    "=== APPOINTMENTS ===\n"
                    f"Their {describe(existing)} has been CANCELLED, just now, "
                    "successfully. Confirm that plainly and briefly."
                ),
                performed="cancelled",
            )
        return TurnResult(
            prompt_block=(
                "=== APPOINTMENTS ===\n"
                f"The cancellation did NOT go through: {result.message} "
                "Tell them what you see and do not claim it is cancelled."
            ),
            appointment=existing,
            refusal=result,
        )

    # -------------------------------------------------------- rescheduling
    if wants_move(text) and existing is not None:
        picked = chosen_slot(text, remembered_offer(contact), zone)
        if picked is not None:
            result = await reschedule(db, organization, existing, picked)
            if result.ok:
                forget_offer(contact)
                return TurnResult(
                    prompt_block=(
                        "=== APPOINTMENTS ===\n"
                        f"Their appointment has been MOVED, just now, successfully. "
                        f"It is now: {describe(result.appointment)}. Confirm exactly "
                        "that and nothing else."
                    ),
                    appointment=result.appointment,
                    performed="moved",
                )
            return TurnResult(
                prompt_block=(
                    "=== APPOINTMENTS ===\n"
                    f"The move did NOT happen: {result.message} They still have "
                    f"their original {describe(existing)}. Say so, and offer to "
                    "look at other times."
                ),
                appointment=existing,
                refusal=result,
            )

        slots = await free_slots(db, organization, days=7)
        remember_offer(contact, slots)
        block = as_prompt_block(organization, contact, slots)
        return TurnResult(
            prompt_block=(
                block
                + f"\n\nThey want to move their existing {describe(existing)}. "
                "Nothing has changed yet - offer these times and let them pick one."
            ),
            appointment=existing,
            offered=slots,
        )

    # ------------------------------------------------------------- booking
    if existing is not None:
        # They already have one. Answer from the record rather than offering
        # a second appointment nobody asked for.
        return TurnResult(
            prompt_block=as_prompt_block(organization, contact, [], appointment=existing),
            appointment=existing,
        )

    # A customer picking a slot does not say "book" - they say "the first one"
    # or "Tuesday at 2". So the standing offer is consulted before intent is,
    # and answering one of our own questions counts as wanting to book.
    picked = chosen_slot(text, remembered_offer(contact), zone)

    if picked is None and not wants_booking(text):
        return TurnResult()

    if not booking_enabled(organization):
        return TurnResult(prompt_block=as_prompt_block(organization, contact, []))
    if picked is not None:
        # Everything the shop said it needs before sending somebody out.
        outstanding = _missing_for_booking(organization, contact)
        if outstanding:
            name, asks = outstanding[0]
            return TurnResult(
                prompt_block=(
                    "=== APPOINTMENTS ===\n"
                    f"They chose a time, and it is still free - but this business "
                    f"needs to know {asks or name} before an appointment can be "
                    "made. Ask them for that one thing. Nothing is booked yet, so "
                    "do NOT say it is."
                ),
                refusal=Refusal("needs_qualification", f"Still need: {asks or name}"),
            )

        result = await book(db, organization, contact, picked)
        if result.ok:
            forget_offer(contact)
            return TurnResult(
                prompt_block=(
                    "=== APPOINTMENTS ===\n"
                    f"BOOKED, just now, successfully: {describe(result.appointment)}. "
                    "Confirm exactly that - the same day, the same time, the same "
                    "kind of appointment. Do not add a detail that is not in it."
                ),
                appointment=result.appointment,
                performed="booked",
            )

        slots = await free_slots(db, organization, days=7)
        remember_offer(contact, slots)
        return TurnResult(
            prompt_block=(
                "=== APPOINTMENTS ===\n"
                f"That time could NOT be booked: {result.message}\n"
                + as_prompt_block(organization, contact, slots)
                + "\nSay what happened and offer these instead. Nothing is booked."
            ),
            refusal=result,
            offered=slots,
        )

    slots = await free_slots(db, organization, days=7)
    remember_offer(contact, slots)
    return TurnResult(
        prompt_block=as_prompt_block(organization, contact, slots),
        offered=slots,
    )


# --------------------------------------------------------------- readiness
# Why a business cannot take an appointment, in words it can act on.
#
# Beluga ran for its whole life unable to book anything and nothing said so.
# Its documents had been read - hours, services, areas, all correct and all
# sitting unconfirmed - but the timezone was never set, so the hours could
# never be saved, so there were no hours, so `booking_enabled` was false, so
# every booking request fell through to a link that reached nobody. Each step
# was working as designed. The chain was invisible.
#
# A capability that silently is not there is worse than one that is plainly
# off, because nobody goes looking for it.

# What a blocker looks like: the thing that is wrong, and the one action that
# fixes it. Not a validation error - nothing here is a mistake somebody made,
# it is a step nobody has taken yet.
def readiness(organization) -> dict:
    """Whether appointments can be taken, and what is stopping them."""
    config = _config(organization)
    zone = (getattr(organization, "timezone", None) or "UTC").strip()
    hours = config.get("business_hours") or {}
    appointments = config.get("appointments") or {}

    blockers: list[dict] = []

    if appointments.get("enabled") is False:
        blockers.append(
            {
                "key": "switched_off",
                "says": "Appointments are switched off.",
                "fix": "Turn them on in Hours and booking.",
            }
        )

    if not hours:
        pending = (config.get(agent_config.PROPOSED_KEY) or {}).get("fields") or {}
        if pending.get("business_hours"):
            # The specific case above: read, correct, and never confirmed.
            # Saying "set your hours" to somebody whose hours are on screen in
            # front of them is how they conclude the feature is broken.
            blockers.append(
                {
                    "key": "hours_unconfirmed",
                    "says": (
                        "Your opening hours were read from your document but "
                        "have never been saved, so there are no times to offer."
                    ),
                    "fix": "Check them in Hours and booking and press Save rules.",
                }
            )
        else:
            blockers.append(
                {
                    "key": "no_hours",
                    "says": "No opening hours are set, so there are no times to offer.",
                    "fix": "Set them in Hours and booking.",
                }
            )

    if zone.upper() == "UTC":
        # Not fatal on its own - a business really on UTC is fine - but it is
        # what blocks the save above, and every hour is read against it.
        blockers.append(
            {
                "key": "timezone",
                "says": (
                    "Your timezone is UTC, which is the default nobody chose. "
                    "Hours cannot be saved until it is set, and every time "
                    "offered is read against it."
                ),
                "fix": "Pick it in Your business.",
            }
        )

    return {
        "can_book": booking_enabled(organization),
        "blockers": blockers,
        "timezone": zone,
        "days_open": sum(1 for day in agent_config.DAYS if hours.get(day)),
    }
