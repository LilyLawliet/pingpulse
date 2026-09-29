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
    APPOINTMENT_BLOCKED,
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_KINDS,
    Appointment,
)
from app.services import agent_config, busy_calendar

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


async def _taken(db, organization, since: datetime, until: datetime) -> list:
    """Everything that makes a time unavailable: the diary, and the owner's own calendar.

    Raises busy_calendar.Unreadable when the owner's calendar is set and can't
    be read - a time is never called free on a guess.
    """
    held = await live_appointments(db, organization.id, since, until)
    return held + await busy_calendar.busy_between(organization, since, until)


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
    db, organization, starts_at: datetime, ends_at: datetime, ignore_id=None, *, notice: bool = True
) -> Refusal | None:
    """None if the slot can be taken, or the reason it cannot.

    Every check a customer could trip over, in the order they would hit them,
    so the first answer they get is the most useful one.
    """
    now = datetime.now(timezone.utc)

    if ends_at <= starts_at:
        return Refusal("backwards", "That appointment would end before it started.")

    if starts_at < now:
        return Refusal("past", "That time has already passed.")

    # The notice is for customers booking themselves. A person at the shop
    # fitting somebody in this afternoon knows whether they can.
    notice = min_notice_minutes(organization) if notice else 0
    if notice and starts_at < now + timedelta(minutes=notice):
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
    try:
        taken = await _taken(
            db,
            organization,
            starts_at - timedelta(minutes=buffer + 1),
            ends_at + timedelta(minutes=buffer + 1),
        )
    except busy_calendar.Unreadable:
        return Refusal(
            "calendar_unreadable",
            "Availability can't be checked right now, so no time can be confirmed.",
        )
    clash = _clashes(starts_at, ends_at, taken, buffer, ignore_id=ignore_id)
    if isinstance(clash, busy_calendar.Busy):
        # Said without saying what is there: the owner's calendar is theirs.
        return Refusal("busy", "That time isn't free.")
    if clash is not None:
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

    # The same span the loop below walks, which is `days + 1` calendar days
    # and not `days`.
    #
    # It was `days`, and the mismatch meant a slot generated on the final day
    # was checked against a diary that stopped short of it - so a time already
    # booked could be offered. The exclusion constraint in Postgres still
    # refused the second booking, so nobody was double-booked; the customer
    # was offered a time and then told they could not have it, which is the
    # same class of wrong answer this module exists to prevent.
    #
    # It hid because the loop skips closed days: only when the extra day
    # landed on a day the shop was open did the gap show at all, so the test
    # covering it passed except when run on a Friday.
    window_end = start_from + timedelta(days=days + 1)
    try:
        taken = await _taken(db, organization, start_from, window_end)
    except busy_calendar.Unreadable:
        return []

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
    notice: bool = True,
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

    refusal = await is_free(db, organization, starts_at, ends_at, notice=notice)
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


async def block(
    db, organization, starts_at: datetime, ends_at: datetime, *, note: str | None = None
) -> Booked | Refusal:
    """Block out time the owner is busy, so nobody is offered it.

    Refused only for what would make it wrong: backwards, over already, or
    on top of a customer who is booked - they would silently keep an
    appointment the owner can't keep, so they are moved or cancelled first.
    Outside opening hours is allowed; it is simply already unavailable.
    """
    now = datetime.now(timezone.utc)
    if ends_at <= starts_at:
        return Refusal("backwards", "The end has to be after the start.")
    if ends_at <= now:
        return Refusal("past", "That time has already passed.")
    if ends_at - starts_at > timedelta(days=31):
        return Refusal("too_long", "Block out a month at most at a time.")

    taken = await live_appointments(db, organization.id, starts_at, ends_at)
    held = _clashes(starts_at, ends_at, taken, 0)
    if held is not None:
        if getattr(held, "is_blocked", False):
            return Refusal("taken", "Part of that time is already blocked out.")
        return Refusal(
            "customer_booked",
            f"A customer is booked in that time ({describe(held)}). Move or cancel it first.",
        )

    appointment = Appointment(
        organization_id=organization.id,
        contact_id=None,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone_name=str(agent_config.zone_of(organization)),
        kind=APPOINTMENT_BLOCKED,
        status=APPOINTMENT_CONFIRMED,
        notes=(note or "").strip()[:500] or None,
        source="operator",
    )
    try:
        async with db.begin_nested():
            db.add(appointment)
            await db.flush()
    except IntegrityError:
        return Refusal("taken", "Something was booked in that time just now.")
    return Booked(appointment)


async def reschedule(
    db, organization, appointment, starts_at: datetime, *, source: str = "agent", notice: bool = True
) -> Booked | Refusal:
    """Move an appointment, leaving exactly one live row behind.

    The old row is cancelled and a new one written, chained by `replaces_id`.
    Cancelling first is what makes the new time bookable when somebody moves
    an appointment by an hour and the two would otherwise overlap each other.
    """
    if appointment is None:
        return Refusal("not_found", "There is no appointment booked to move.")
    if getattr(appointment, "is_blocked", False):
        return Refusal("blocked", "Blocked-out time is removed and added again, not moved.")
    if appointment.status != APPOINTMENT_CONFIRMED:
        return Refusal("not_confirmed", "There is no confirmed appointment to move.")

    ends_at = starts_at + timedelta(
        minutes=duration_minutes(organization, appointment.kind)
    )
    refusal = await is_free(
        db, organization, starts_at, ends_at, ignore_id=appointment.id, notice=notice
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
    if getattr(appointment, "is_blocked", False):
        until = _aware(appointment.ends_at).astimezone(zone)
        day = local.strftime("%A %d %B").replace(" 0", " ")
        begin = local.strftime("%I:%M %p").lstrip("0").lower()
        end = until.strftime("%I:%M %p").lstrip("0").lower()
        if until.date() != local.date():
            end = until.strftime("%A %d %B").replace(" 0", " ") + " " + end
        return f"time blocked out on {day} from {begin} to {end}"

    # %-d and %-I are not portable to Windows, so the padding is stripped by
    # hand rather than by a format code that works on one developer's machine.
    day = local.strftime("%A %d %B").replace(" 0", " ")
    clock = local.strftime("%I:%M %p").lstrip("0").lower()
    label = KIND_WORDS.get(appointment.kind, "appointment")

    location = appointment.location or ""
    if location.lower().startswith("http"):
        where = f" (join: {location})"
    else:
        where = f" at {location}" if location else ""
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
# A claim is only a claim when it is *asserted*. "I can book you in for
# Tuesday" is an offer and the agent must be free to make it; "I've booked you
# in for Tuesday" is a statement about the world that has to be true. The
# patterns below match the verb, and `_asserted` is what tells the two apart -
# without it, widening these to catch real phrasings would block the agent
# from offering anything at all.
_MODAL = re.compile(
    r"\b(can|could|shall|should|would|might|may|want me|like me|happy to|"
    r"able to|if you|whether|let me know|do you want|shall i|want to)\b",
    re.IGNORECASE,
)

# Sentence-ish. Split on terminators and on newlines, keeping the terminator
# so a question can be recognised as one.
_CLAUSE = re.compile(r"[^.!?\n]+[.!?]?")


# Denying that something happened is the honest half of this. "Nothing has
# been cancelled" is exactly what the agent should say when nothing has, and
# reading it as a cancellation claim would block the correction and leave the
# customer with the original mistake.
_NEGATED = re.compile(
    r"\b(not|never|nothing|none|no|cannot|can't|couldn't|didn't|haven't|"
    # No comma inside the gap: "No problem, I have called it off" is an
    # interjection followed by a claim, not a denial of one.
    r"hasn't|won't|isn't|unable)\b[^.!?,]{0,24}$",
    re.IGNORECASE,
)


def _asserted(pattern: re.Pattern, text: str) -> str | None:
    """The phrase, if some clause states it outright.

    A clause that asks ("shall I cancel that?"), offers ("I can move it") or
    denies ("nothing has been cancelled") is not a claim about what has
    happened, and treating any of them as one would stop the agent doing its
    job - which is how a guard gets switched off rather than fixed.
    """
    for clause in _CLAUSE.findall(text or ""):
        stripped = clause.strip()
        if not stripped or stripped.endswith("?"):
            continue
        if _MODAL.search(stripped):
            continue
        for found in pattern.finditer(stripped):
            # Only a negator close in front of the verb suppresses it, so
            # "I cancelled Tuesday, not Wednesday" still reads as a claim.
            if _NEGATED.search(stripped[: found.start()]):
                continue
            return found.group(0)
    return None


# Written broadly, because the failure being guarded against is a confident
# sentence and a model has a hundred ways to write one. A probe of two dozen
# phrasings a model actually produces found eighteen of them walking straight
# through the first version of these: "that's booked for you", "I cancelled
# it", "moved to Wednesday", "you're all set for Tuesday". Each one is the
# 1am incident waiting to happen again with different words.
_BOOKING_CLAIMS = re.compile(
    r"\b("
    r"(appointment|booking|slot|visit) is (now )?(confirmed|booked|scheduled|set|reserved)"
    r"|you('re| are) (all )?(booked|scheduled|confirmed|set|in)\b"
    r"|i('ve| have)? ?(now )?(booked|scheduled|confirmed|reserved|locked)\b"
    r"|that('s| is) (now )?(booked|confirmed|reserved|scheduled|set)"
    r"|(booking|appointment) (is )?confirmed"
    r"|confirmed for \w+"
    r"|locked in"
    r"|(done|ok|okay|sorted|right|great|perfect|all done)[,!.:]?\s+(booked|scheduled|confirmed|reserved)\b"
    r"|all set for"
    r"|see you (on|at|then)"
    r"|we('ll| will) see you"
    r"|your (visit|estimate|consultation|appointment) (is|on) "
    r")",
    re.IGNORECASE,
)

_CANCEL_CLAIMS = re.compile(
    r"\b("
    r"i('ve| have)? ?(now )?cancell?ed"
    r"|(has|have|is|are) been cancell?ed"
    r"|(booking|appointment|slot|visit|that|it) (is |has been )?cancell?ed"
    r"|cancell?ed (your|the|that|it)"
    r"|that('s| is) (now )?cancell?ed"
    r"|called it off"
    r"|(removed|taken off) (that|your) (booking|appointment)"
    r"|(done|ok|okay|sorted|right|great|perfect|all done)[,!.:]?\s+cancell?ed\b"
    r"|^cancell?ed\b"
    r")",
    re.IGNORECASE,
)

_MOVE_CLAIMS = re.compile(
    r"\b("
    r"i('ve| have)? ?(now )?(moved|rescheduled|shifted|changed it)"
    r"|(has|have|is) been (moved|rescheduled|changed)"
    r"|(booking|appointment|slot|visit|that|it) (is |has been )?(moved|rescheduled)"
    r"|that('s| is) (now )?(moved|rescheduled)"
    r"|your appointment is now"
    r"|(done|ok|okay|sorted|right|great|perfect|all done)[,!.:]?\s+(moved|rescheduled)\b"
    r"|^moved to\b"
    r")",
    re.IGNORECASE,
)


def claims_appointment(text: str) -> str | None:
    """The phrase asserting a booking exists, or None."""
    return _asserted(_BOOKING_CLAIMS, text)


def claims_cancellation(text: str) -> str | None:
    """The phrase asserting something was cancelled, or None.

    "I am sorry for the confusion; I have cancelled the September 19
    appointment" was written about an appointment that never existed, so this
    matters as much as the booking claim and was the half nobody reported.
    """
    return _asserted(_CANCEL_CLAIMS, text)


def claims_reschedule(text: str) -> str | None:
    return _asserted(_MOVE_CLAIMS, text)


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


# What an offer was for. Times offered to somebody who asked to move their
# appointment are a move when they pick one; the same pick after an offer to
# book is a new booking. The purpose was not remembered, so a customer who
# asked to move, was offered times and answered "the second one" - which says
# nothing about moving - was answered from their old appointment and nothing
# moved.
OFFER_BOOK = "book"
OFFER_MOVE = "move"


def remember_offer(
    contact,
    slots: list[datetime],
    purpose: str = OFFER_BOOK,
    *,
    kind: str | None = None,
    about: str | None = None,
    meeting: bool = False,
) -> None:
    """Write down exactly what was offered, and why, so a reply can be matched to it.

    A meeting offered as a 45-minute video call is still one when they answer
    "the second one", so the kind and what it was about are kept with it.
    """
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    metadata[OFFER_KEY] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "slots": [slot.isoformat() for slot in slots],
        "for": purpose,
        "kind": kind,
        "about": about,
        "meeting": meeting,
    }
    contact.contact_metadata = metadata


def offer_details(contact) -> dict:
    """The kind, subject and meeting flag of the standing offer, when there is one."""
    if not remembered_offer(contact):
        return {}
    offer = (getattr(contact, "contact_metadata", None) or {}).get(OFFER_KEY) or {}
    return {key: offer.get(key) for key in ("kind", "about", "meeting")}


def offer_purpose(contact) -> str | None:
    """What the standing offer was for, or None when there is none."""
    if not remembered_offer(contact):
        return None
    offer = (getattr(contact, "contact_metadata", None) or {}).get(OFFER_KEY) or {}
    return offer.get("for") or OFFER_BOOK


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


# ----------------------------------------------------- a time they named
# Customers do not only pick from a list. "Can I come Friday at 3pm?" names a
# time, and answering it with six other times is how a diary feels like a
# form. So a day and a clock time written plainly are read - and only read:
# the moment is then checked against the shop's hours and its diary exactly
# as an offered one would be, and the confirmation is rendered from the row,
# with the full date, so a misreading is visible to the customer at once.
#
# Anything that does not read one way only reads as nothing. "At 3" is 3pm at
# a shop open 9 to 5 and ambiguous at one open 8am to 11pm; "Monday or
# Tuesday" names two days and books neither.
_WEEKDAY_WORDS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_MONTH_WORDS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH = "|".join(sorted(_MONTH_WORDS, key=len, reverse=True))
_WEEKDAY = "|".join(sorted(_WEEKDAY_WORDS, key=len, reverse=True))

_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b\.?")
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*(?:am|pm|:))")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_WEEKDAY_NAMED = re.compile(rf"\b(next\s+|this\s+|coming\s+)?({_WEEKDAY})\b\.?")
_RELATIVE = re.compile(r"\b(day after tomorrow|tomorrow|tmrw|tmr|today|tonight)\b")

_CLOCK_MERIDIEM = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])"
)
_CLOCK_COLON = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_CLOCK_AT = re.compile(r"\b(?:at|around|by)\s+(\d{1,2})(?:\s*o'?clock)?\b(?![:.\d]|\s*(?:am|pm|a\.m|p\.m|st|nd|rd|th|%|/|-))")
# "for" counts far more often than it tells the time: "a table for 2 people",
# "for 3 hours", "book me in for 4 of us". Read as a clock, every one of those
# became two o'clock, three, four — on the day they named, which is enough to
# book somebody at a time they never asked for. It is a time only when the
# number is said the way a time is said: "for 3 o'clock", or "book me in for
# 3" with nothing counted after it.
_CLOCK_FOR = re.compile(r"\bfor\s+(\d{1,2})(?:\s*o'?clock\b|\s*(?=[.!?,]|$))")
_NOON = re.compile(r"\b(noon|midday)\b")


@dataclass(frozen=True)
class Named:
    """The day and time a customer wrote, as far as they can be read.

    `days` is every date named, in order. `clocks` is every reading of the time
    they gave: one when it is plain ("3pm", "15:00"), two when it could be
    morning or afternoon ("at 3"), and none when there is no time at all.
    """

    days: tuple = ()
    clocks: tuple = ()

    @property
    def any(self) -> bool:
        return bool(self.days or self.clocks)


def _next_weekday(today: date, weekday: int, skip_today: bool) -> date:
    ahead = (weekday - today.weekday()) % 7
    if ahead == 0 and skip_today:
        ahead = 7
    return today + timedelta(days=ahead)


def _dated(year: int, month: int, day: int, today: date) -> date | None:
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    if found < today:
        try:
            found = date(year + 1, month, day)
        except ValueError:
            return None
    return found


def named_time(text: str, zone: ZoneInfo, now: datetime | None = None) -> Named:
    """The days and times written in this message, read in the shop's zone."""
    lowered = (text or "").lower().replace("’", "'")
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()

    days: list[date] = []

    def add(found: date | None) -> None:
        if found is not None and found not in days:
            days.append(found)

    for match in _ISO_DATE.finditer(lowered):
        try:
            add(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            pass
    for match in _DAY_MONTH.finditer(lowered):
        add(_dated(today.year, _MONTH_WORDS[match.group(2)], int(match.group(1)), today))
    for match in _MONTH_DAY.finditer(lowered):
        # "may" is a month and a verb. Only "May 3" with a number reads as a
        # date here, and "may 3 of us come" is rare enough to be asked again.
        add(_dated(today.year, _MONTH_WORDS[match.group(1)], int(match.group(2)), today))
    for match in _RELATIVE.finditer(lowered):
        word = match.group(1)
        add(today + timedelta(days={"day after tomorrow": 2, "tomorrow": 1, "tmrw": 1, "tmr": 1}.get(word, 0)))
    if not days:
        # A weekday next to a date names the same day twice ("Friday 3
        # October"); it only counts on its own.
        for match in _WEEKDAY_NAMED.finditer(lowered):
            word = match.group(2)
            # "sat", "sun" and "wed" are English words too. They count as days
            # only where a day would stand: before a time or a part of the day.
            if word in ("sat", "sun", "wed") and not re.match(
                r"\.?\s*(\d|at\b|morning|afternoon|evening|[?.!,]|$)", lowered[match.end():]
            ):
                continue
            add(_next_weekday(today, _WEEKDAY_WORDS[word], skip_today=bool(match.group(1) and "next" in match.group(1))))

    clocks: list[time] = []

    def clock(hour: int, minute: int) -> None:
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            moment = time(hour, minute)
            if moment not in clocks:
                clocks.append(moment)

    for match in _CLOCK_MERIDIEM.finditer(lowered):
        hour = int(match.group(1))
        if not 1 <= hour <= 12:
            continue
        hour %= 12
        if match.group(3).startswith("p"):
            hour += 12
        clock(hour, int(match.group(2) or 0))
    if not clocks:
        for match in _NOON.finditer(lowered):
            clock(12, 0)
    if not clocks:
        for match in _CLOCK_COLON.finditer(lowered):
            hour, minute = int(match.group(1)), int(match.group(2))
            clock(hour, minute)
            if 1 <= hour <= 11:
                # "1:30" is half past one in the afternoon at most shops. Both
                # readings are kept and the shop's hours choose.
                clock(hour + 12, minute)
    if not clocks:
        for pattern in (_CLOCK_AT, _CLOCK_FOR):
            for match in pattern.finditer(lowered):
                hour = int(match.group(1))
                if 1 <= hour <= 12:
                    clock(hour % 12, 0)
                    clock(hour % 12 + 12, 0)

    return Named(days=tuple(days), clocks=tuple(clocks))


def named_moment(organization, named: Named, kind: str | None = None) -> datetime | None:
    """The one moment this names at this shop, or None when it is not exactly one.

    Several readings of the time are narrowed by the shop's hours: of "at 3"
    only 3pm fits a day that closes at 5. What is left must be a single
    reading, on a single day.
    """
    if len(named.days) != 1 or not named.clocks:
        return None
    day = named.days[0]
    zone = agent_config.zone_of(organization)
    if len(named.clocks) == 1:
        return datetime.combine(day, named.clocks[0], tzinfo=zone).astimezone(timezone.utc)

    window = _window_for(organization, day)
    if window is None:
        return None
    length = timedelta(minutes=duration_minutes(organization, kind))
    fits = []
    for reading in named.clocks:
        start = datetime.combine(day, reading, tzinfo=zone)
        if start.time() >= window[0] and (start + length).time() <= window[1] and (
            (start + length).date() == day
        ):
            fits.append(start)
    if len(fits) != 1:
        return None
    return fits[0].astimezone(timezone.utc)


# A short yes, answering a single time put to them. Nothing longer counts: "yes
# but can we do later" is not agreement to the time offered.
_YES = re.compile(
    r"^\W*(yes|yes please|yeah|yep|yup|ok|okay|ok please|sure|confirm|confirmed|book it|"
    r"go ahead|please do|perfect|great|sounds good|that works|works for me|done|"
    r"haan|han|ji|ji haan|theek hai|thik hai|sahi hai|si|oui|ja)\b[\s\W]*"
    r"(please|thanks|thank you|go ahead|book it|do it|kar do|kardo)?[\s\W]*$",
    re.IGNORECASE,
)


def agreed(text: str, offered: list[datetime]) -> datetime | None:
    """The one time put to them, when their reply is a plain yes to it."""
    if len(offered) == 1 and _YES.match(text or ""):
        return offered[0]
    return None


# Asking whether a time is free is not asking for it. "Is Friday at 3 free?"
# is answered with whether it is, and booked when they say yes.
_ONLY_ASKING = re.compile(
    r"\b(free|available|possible|ok|okay|fine|work|any (space|room|slot)|do you have)\b[^.!]*\?\s*$",
    re.IGNORECASE,
)
_ASKS_FOR_IT = re.compile(r"\b(book|reserve|schedule|put me (in|down)|lock)\b", re.IGNORECASE)


# "Is delivery possible tomorrow?" asks about a parcel, not a visit. A question
# about an order is never read as a question about the diary.
_ORDER_TALK = re.compile(
    r"\b(deliver\w*|ship\w*|dispatch\w*|arriv\w*|courier|parcel|order\w*|stock|restock\w*|"
    r"pick ?up|collect\w*|open(ing)? hours|close|closing)\b",
    re.IGNORECASE,
)


def only_asking(text: str) -> bool:
    """A question whether a time is free, from somebody not yet asking to book it."""
    return (
        bool(_ONLY_ASKING.search(text or ""))
        and not _ASKS_FOR_IT.search(text or "")
        and not _ORDER_TALK.search(text or "")
    )


def _say_day(day: date) -> str:
    return day.strftime("%A %d %B").replace(" 0", " ")


def _say_moment(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{_say_day(local.date())} at {local.strftime('%I:%M %p').lstrip('0').lower()}"


def hours_on(organization, day: date) -> str:
    """The shop's hours on this date, as a sentence part."""
    window = _window_for(organization, day)
    if window is None:
        return f"closed on {_say_day(day)}"
    opens, closes = window
    return (
        f"open {opens.strftime('%I:%M %p').lstrip('0').lower()} to "
        f"{closes.strftime('%I:%M %p').lstrip('0').lower()} on {_say_day(day)}"
    )


async def slots_on(db, organization, days, *, kind: str | None = None) -> list[datetime]:
    """Free times on these particular dates, soonest first."""
    zone = agent_config.zone_of(organization)
    found: list[datetime] = []
    for day in days:
        start = datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(timezone.utc)
        found.extend(await free_slots(db, organization, from_time=start, days=0, kind=kind))
    found = [slot for slot in found if slot.astimezone(zone).date() in set(days)]
    return found[:MAX_OFFERED_SLOTS]


def as_prompt_block(
    organization, contact, slots: list[datetime], appointment=None, *, meeting_kind_: str | None = None
) -> str:
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

    as_meeting = (
        f"They want a meeting. It will be a {KIND_WORDS.get(meeting_kind_, 'meeting')} of "
        f"{duration_minutes(organization, meeting_kind_)} minutes.\n"
        if meeting_kind_
        else ""
    )
    return (
        "=== APPOINTMENTS ===\n"
        + as_meeting
        + "These times are genuinely free in the diary right now:\n"
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
    r"|\b(different|another|earlier|later) (time|day|slot|date)\b"
    # On their own these only ever mean one thing.
    r"|\b(reschedul\w*|postpone\w*|prepone\w*|re-?date)\b",
    re.IGNORECASE,
)

_WANTS_BOOKING = re.compile(
    r"\b("
    # Naming the thing.
    r"book|booking|bookings|schedul\w*|appointments?|consultations?|estimate|survey|"
    r"viewing|site visit|home visit"
    # Meetings: a demo of the product, a call, a sit-down.
    r"|meetings?|meet (up|with|you|your team)|demos?|walk-?through|discovery call|"
    r"sales call|intro call|introductory call|video call|zoom|google meet|teams call|"
    r"(have|set up|arrange|jump on|hop on|get on) a (quick |short )?(phone |video )?call|phone call"
    # Asking when.
    r"|when (can|could|are|do|would) you"
    r"|what (times?|days?|slots?)"
    r"|which (times?|days?|slots?)"
    r"|any (times?|days?|slots?|openings?)"
    r"|(times?|days?|slots?) (are |is )?(free|available|open)"
    # "Available" and "free" are about the diary only next to a word about
    # time: "is the planner available?" asks about a planner.
    r"|(availab\w*|free)\b[^.?!]{0,30}\b(on|this|next|tomorrow|today|week|morning|"
    r"afternoon|evening|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|(tomorrow|today|this week|next week|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b[^.?!]{0,30}\b(availab\w*|free)"
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
    previous: "Appointment | None" = None  # what a move replaced
    calendar_down: bool = False            # the owner's own calendar couldn't be read
    meeting: bool = False                  # a meeting, not a visit

    def __post_init__(self):
        if self.offered is None:
            self.offered = []

    @property
    def booked(self) -> bool:
        return self.performed == "booked"

    def plain_reply(self, organization) -> str | None:
        """What to send when no model answers, rendered from the rows alone.

        A booking made on a turn when both providers are down is still a
        booking, and the customer is owed its confirmation - not a holding
        line about something else. Nothing here comes from a model.
        """
        zone = agent_config.zone_of(organization)
        if self.booked and self.appointment is not None:
            return f"You're booked: {describe(self.appointment)}."
        if self.moved and self.appointment is not None:
            return f"Done - it's moved. Your booking is now: {describe(self.appointment)}."
        if self.cancelled and self.appointment is not None:
            return f"Your {describe(self.appointment)} has been cancelled."
        lines = []
        if self.refusal is not None and self.refusal.reason not in ("needs_qualification",):
            lines.append(f"Sorry, that time isn't possible: {self.refusal.message}")
        if self.offered:
            if len(self.offered) == 1 and self.refusal is None:
                lines.append(f"{_say_moment(self.offered[0], zone)} is free. Shall I book it?")
            else:
                times = "\n".join(
                    f"{index}. {_say_moment(slot, zone)}"
                    for index, slot in enumerate(self.offered, start=1)
                )
                lines.append(f"These times are free:\n{times}\nWhich one suits you?")
        elif self.refusal is not None and self.refusal.reason == "not_found":
            lines = ["I can't find an appointment booked in your name."]
        return "\n".join(lines) or None

    @property
    def cancelled(self) -> bool:
        return self.performed == "cancelled"

    @property
    def moved(self) -> bool:
        return self.performed == "moved"


# What a meeting is called. Trade words are deliberately absent: "wholesale",
# "distributor", "b2b" and "partnership" are what a trade supplier sells, not a
# request to talk about it, and a wholesaler's customers say them all day. Read
# as meeting requests they turned "can I get the wholesale price list" into a
# video call and skipped the questions the shop asks before sending anybody
# out. They count when the customer attaches them to a conversation - "a
# wholesale call", "a partnership chat". "Teams" is the same problem with a
# capital T nobody types: it is Microsoft's product only where it is used like
# one, never "we supply 3 teams".
_MEETING = re.compile(
    r"\b(meetings?|meet (up|with|you|your team)|demos?|walk-?through|discovery call|sales call|"
    r"intro(ductory)? call|video call|zoom|google meet|"
    r"(ms |microsoft )?teams (call|meeting|link)|(on|over|via) (ms |microsoft )?teams|"
    r"(have|set up|arrange|jump on|hop on|get on|book|schedule) a (quick |short )?(phone |video )?call|phone call|"
    r"(partnership|wholesale|distribution|distributor|reseller|b2b|pricing|onboarding) "
    r"(call|chat|meeting|discussion))\b",
    re.IGNORECASE,
)
_VIDEO = re.compile(r"\b(video|zoom|google meet|meet link|teams|online|screen ?share|demo)\b", re.IGNORECASE)
_PHONE = re.compile(r"\b(phone|voice call|ring me|call me|whatsapp call)\b", re.IGNORECASE)


def is_meeting(text: str) -> bool:
    """A meeting with the business - a demo, a call - rather than a visit."""
    return bool(_MEETING.search(text or ""))


def meeting_link(organization) -> str | None:
    """The owner's own video room (Zoom, Google Meet, Teams), when they gave one."""
    config = _config(organization).get("appointments") or {}
    link = str(config.get("meeting_link") or "").strip()
    return link if link.lower().startswith("https://") else None


def meeting_kind(organization, text: str = "") -> str:
    """What kind of appointment a meeting is booked as.

    What the customer said wins ("a quick phone call", "over Zoom"); then the
    shop's choice; then video when the shop has a video room, phone otherwise.
    """
    if _PHONE.search(text or "") and not _VIDEO.search(text or ""):
        return "phone"
    if _VIDEO.search(text or "") and not _PHONE.search(text or ""):
        return "video"
    config = _config(organization).get("appointments") or {}
    chosen = str(config.get("meeting_kind") or "").lower()
    if chosen in ("phone", "video"):
        return chosen
    return "video" if meeting_link(organization) else "phone"


def add_to_calendar_link(organization, appointment) -> str:
    """A Google Calendar link with this exact appointment, for the customer's own calendar."""
    from app.services import scheduling

    details = f"With {organization.name}"
    if appointment.location:
        details += f"\nJoin: {appointment.location}"
    return scheduling.google_calendar_link(
        title=f"{KIND_WORDS.get(appointment.kind, 'Appointment').capitalize()} with {organization.name}",
        note=details,
        start=_aware(appointment.starts_at),
        duration_minutes=int(
            (_aware(appointment.ends_at) - _aware(appointment.starts_at)).total_seconds() // 60
        ),
    )


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


async def handle_turn(
    db, organization, contact, text: str, *, wants_meeting: bool = False
) -> TurnResult:
    """Do the appointment work for one inbound message. Never raises.

    Returns what actually happened. The caller puts `prompt_block` in front of
    the model and passes `performed` to the reply guard, so a sentence
    announcing a booking can only survive if a booking was made.
    """
    try:
        return await _handle_turn(db, organization, contact, text, wants_meeting=wants_meeting)
    except Exception as exc:  # noqa: BLE001 - never at the cost of a reply
        logger.warning("appointment handling failed for %s: %s", contact.id, exc)
        return TurnResult()


_INSTEAD = re.compile(r"\b(instead|rather|swap|switch)\b", re.IGNORECASE)


def _offer_block(organization, slots, purpose, existing=None, lead: str = "") -> str:
    block = as_prompt_block(organization, None, slots)
    if purpose == OFFER_MOVE and existing is not None:
        block += (
            f"\n\nThey want to move their existing {describe(existing)}. Nothing has "
            "changed yet - offer these times and let them pick one."
        )
    return (lead + "\n" + block) if lead else block


async def _near(db, organization, moment: datetime, kind: str | None) -> list[datetime]:
    """Free times on the day they asked for, or the soonest ones when it is full."""
    zone = agent_config.zone_of(organization)
    same_day = await slots_on(db, organization, [moment.astimezone(zone).date()], kind=kind)
    return same_day or await free_slots(db, organization, days=7, kind=kind)


def _why_not(organization, refusal: Refusal, moment: datetime) -> str:
    """The refusal, with the hours of that day when the shop is shut then."""
    zone = agent_config.zone_of(organization)
    said = f"{_say_moment(moment, zone)} could NOT be booked: {refusal.message}"
    if refusal.reason == "closed":
        said += f" (The business is {hours_on(organization, moment.astimezone(zone).date())}.)"
    return said


async def _handle_turn(
    db, organization, contact, text: str, *, wants_meeting: bool = False
) -> TurnResult:
    existing = await upcoming_for(db, contact.id)
    zone = agent_config.zone_of(organization)
    offered = remembered_offer(contact)
    purpose = offer_purpose(contact)
    named = named_time(text, zone)
    kind = existing.kind if existing is not None else None
    moment = named_moment(organization, named, kind) if named.any else None
    picked = chosen_slot(text, offered, zone) or agreed(text, offered)
    # "Move it to 4 October at 1pm" names its own day. A 1pm on another day in
    # the standing offer is not what they asked for, however well the clock
    # matches.
    if picked is not None and named.days and picked.astimezone(zone).date() not in named.days:
        picked = None

    # A meeting - a demo, a call, a partnership talk - is booked as a call of
    # the shop's meeting kind and length, not as the visit a retail customer
    # gets, and it keeps what it was about.
    details = offer_details(contact)
    asked_meeting = is_meeting(text) or wants_meeting
    meeting = existing is None and (asked_meeting or bool(offered and details.get("meeting")))
    new_kind = None
    about = None
    if meeting:
        new_kind = meeting_kind(organization, text) if asked_meeting else details.get("kind")
        new_kind = new_kind or meeting_kind(organization, text)
        about = details.get("about") or text.strip()[:300]
        if named.any:
            moment = named_moment(organization, named, new_kind)
    keep = {"kind": new_kind, "about": about, "meeting": meeting}

    # With the owner's own calendar set, nothing about a time can be said
    # until it has been read. Cancelling needs no calendar and still works.
    about_times = (
        bool(offered) or named.any or wants_booking(text) or wants_move(text) or wants_meeting
    )
    if about_times and not (wants_cancel(text) and not named.any) and booking_enabled(organization):
        try:
            await busy_calendar.read(organization)
        except busy_calendar.Unreadable:
            return TurnResult(
                prompt_block=(
                    "=== APPOINTMENTS ===\n"
                    "Availability can't be checked right now. Do NOT offer, suggest or "
                    "confirm any time, and do NOT say anything is booked or changed. Ask "
                    "which days and times suit them."
                ),
                appointment=existing,
                refusal=Refusal(
                    "calendar_unreadable",
                    "Availability can't be checked right now, so no time can be confirmed.",
                ),
                calendar_down=True,
            )

    # "I don't want the Friday one, can we do Monday?" says cancel and means
    # move. A cancellation that names another time is a move.
    moving = existing is not None and (
        wants_move(text)
        or (purpose == OFFER_MOVE and not wants_cancel(text))
        or (bool(_INSTEAD.search(text)) and (moment is not None or bool(named.days)))
        or (wants_cancel(text) and (moment is not None or bool(named.days)))
    )

    # ---------------------------------------------------------- cancelling
    if wants_cancel(text) and not moving:
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
                appointment=existing,
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
    if existing is not None and moving:
        target = picked or moment
        if target is not None:
            if picked is None and only_asking(text):
                return await _put_to_them(db, organization, contact, target, OFFER_MOVE, existing)
            result = await reschedule(db, organization, existing, target)
            if result.ok:
                forget_offer(contact)
                return TurnResult(
                    prompt_block=(
                        "=== APPOINTMENTS ===\n"
                        f"Their appointment has been MOVED, just now, successfully. "
                        f"It was: {describe(existing)}. It is now: "
                        f"{describe(result.appointment)}. Confirm exactly that and "
                        "nothing else."
                    ),
                    appointment=result.appointment,
                    performed="moved",
                    previous=existing,
                )
            slots = await _near(db, organization, target, existing.kind)
            remember_offer(contact, slots, OFFER_MOVE)
            return TurnResult(
                prompt_block=_offer_block(
                    organization,
                    slots,
                    OFFER_MOVE,
                    existing,
                    lead=(
                        "=== APPOINTMENTS ===\n"
                        f"The move did NOT happen. {_why_not(organization, result, target)} "
                        f"They still have their original {describe(existing)}. Say so."
                    ),
                ),
                appointment=existing,
                refusal=result,
                offered=slots,
            )

        if named.days:
            slots = await slots_on(db, organization, list(named.days), kind=existing.kind)
            lead = ""
            if not slots:
                days = ", ".join(hours_on(organization, day) for day in named.days)
                lead = (
                    "=== APPOINTMENTS ===\n"
                    f"Nothing is free on the day they asked for (the business is {days}). "
                    "Say so and offer these other times instead."
                )
                slots = await free_slots(db, organization, days=7, kind=existing.kind)
            remember_offer(contact, slots, OFFER_MOVE)
            return TurnResult(
                prompt_block=_offer_block(organization, slots, OFFER_MOVE, existing, lead),
                appointment=existing,
                offered=slots,
            )

        if wants_move(text) or wants_cancel(text):
            slots = await free_slots(db, organization, days=7, kind=existing.kind)
            remember_offer(contact, slots, OFFER_MOVE)
            return TurnResult(
                prompt_block=_offer_block(organization, slots, OFFER_MOVE, existing),
                appointment=existing,
                offered=slots,
            )

    # ------------------------------------------------------------- booking
    if existing is not None:
        if moment is not None and (wants_booking(text) or only_asking(text)):
            # A time named by somebody who already has one: a second
            # appointment or a move? Asked, never guessed.
            return await _put_to_them(db, organization, contact, moment, OFFER_MOVE, existing)
        # They already have one. Answer from the record rather than offering
        # a second appointment nobody asked for.
        return TurnResult(
            prompt_block=as_prompt_block(organization, contact, [], appointment=existing),
            appointment=existing,
        )

    # A customer picking a slot does not say "book" - they say "the first one"
    # or "Tuesday at 2". So the standing offer is consulted before intent is,
    # and answering one of our own questions counts as wanting to book.
    in_context = bool(offered) or (wants_booking(text) or wants_meeting) or (only_asking(text) and named.any)
    target = picked or (moment if in_context else None)

    if target is None and not (wants_booking(text) or wants_meeting) and not (
        named.days and (offered or only_asking(text))
    ):
        return TurnResult()

    if not booking_enabled(organization):
        return TurnResult(prompt_block=as_prompt_block(organization, contact, []))

    if target is not None:
        if picked is None and only_asking(text):
            return await _put_to_them(db, organization, contact, target, OFFER_BOOK, keep=keep)

        # Everything the shop said it needs before sending somebody out.
        # A meeting is a conversation, not a van: the questions a shop asks
        # before sending somebody out don't hold it back.
        outstanding = [] if meeting else _missing_for_booking(organization, contact)
        if outstanding:
            name, asks = outstanding[0]
            remember_offer(contact, [target], OFFER_BOOK, **keep)
            return TurnResult(
                prompt_block=(
                    "=== APPOINTMENTS ===\n"
                    f"They chose {_say_moment(target, zone)} - but this business "
                    f"needs to know {asks or name} before an appointment can be "
                    "made. Ask them for that one thing. Nothing is booked yet, so "
                    "do NOT say it is."
                ),
                refusal=Refusal("needs_qualification", f"Still need: {asks or name}"),
                offered=[target],
            )

        result = await book(
            db,
            organization,
            contact,
            target,
            kind=new_kind,
            location=meeting_link(organization) if meeting and new_kind == "video" else None,
            notes=f"Meeting request: {about}" if meeting and about else None,
        )
        if result.ok:
            forget_offer(contact)
            return TurnResult(
                prompt_block=(
                    "=== APPOINTMENTS ===\n"
                    f"BOOKED, just now, successfully: {describe(result.appointment)}. "
                    "Confirm exactly that - the same day, the same time, the same "
                    "kind of appointment. Do not add a detail that is not in it."
                    + (
                        f" Give them this link to add it to their own calendar: "
                        f"{add_to_calendar_link(organization, result.appointment)}"
                        if meeting
                        else ""
                    )
                ),
                appointment=result.appointment,
                performed="booked",
                meeting=meeting,
            )

        slots = await _near(db, organization, target, new_kind)
        remember_offer(contact, slots, OFFER_BOOK, **keep)
        return TurnResult(
            prompt_block=(
                "=== APPOINTMENTS ===\n"
                f"{_why_not(organization, result, target)}\n"
                + as_prompt_block(organization, contact, slots, meeting_kind_=new_kind if meeting else None)
                + "\nSay what happened and offer these instead. Nothing is booked."
            ),
            refusal=result,
            offered=slots,
        )

    if named.days:
        slots = await slots_on(db, organization, list(named.days), kind=new_kind)
        if slots:
            remember_offer(contact, slots, OFFER_BOOK, **keep)
            return TurnResult(
                prompt_block=as_prompt_block(
                    organization, contact, slots, meeting_kind_=new_kind if meeting else None
                ),
                offered=slots,
            )
        days = ", ".join(hours_on(organization, day) for day in named.days)
        slots = await free_slots(db, organization, days=7, kind=new_kind)
        remember_offer(contact, slots, OFFER_BOOK, **keep)
        return TurnResult(
            prompt_block=(
                "=== APPOINTMENTS ===\n"
                f"Nothing is free on the day they asked for (the business is {days}). "
                "Say so, and offer these other times instead.\n"
                + as_prompt_block(
                    organization, contact, slots, meeting_kind_=new_kind if meeting else None
                )
            ),
            offered=slots,
        )

    slots = await free_slots(db, organization, days=7, kind=new_kind)
    remember_offer(contact, slots, OFFER_BOOK, **keep)
    return TurnResult(
        prompt_block=as_prompt_block(
            organization, contact, slots, meeting_kind_=new_kind if meeting else None
        ),
        offered=slots,
    )


async def _put_to_them(
    db, organization, contact, moment, purpose, existing=None, *, keep: dict | None = None
) -> TurnResult:
    """Say whether this one time is free, and ask before taking it.

    The time is remembered as a one-item offer, so a plain "yes" next books or
    moves exactly it - and nothing else could be what they agreed to.
    """
    zone = agent_config.zone_of(organization)
    keep = keep or {}
    kind = existing.kind if existing is not None else keep.get("kind")
    length = timedelta(minutes=duration_minutes(organization, kind))
    refusal = await is_free(
        db, organization, moment, moment + length,
        ignore_id=existing.id if existing is not None else None,
    )
    if refusal is None:
        remember_offer(contact, [moment], purpose, **keep)
        question = (
            f"Ask whether they want their {describe(existing)} moved to it"
            if existing is not None
            else "Ask whether they would like it booked"
        )
        return TurnResult(
            prompt_block=(
                "=== APPOINTMENTS ===\n"
                f"{_say_moment(moment, zone)} is FREE. {question}. Nothing has been "
                "booked or changed yet, so do NOT say it has."
            ),
            appointment=existing,
            offered=[moment],
        )
    slots = await _near(db, organization, moment, kind)
    remember_offer(contact, slots, purpose, **keep)
    return TurnResult(
        prompt_block=_offer_block(
            organization, slots, purpose, existing,
            lead="=== APPOINTMENTS ===\n" + _why_not(organization, refusal, moment),
        ),
        appointment=existing,
        refusal=refusal,
        offered=slots,
    )


def alert_for(turn: TurnResult, who: str) -> tuple[str, str] | None:
    """The alert a shop gets for what this turn did to its diary, or None.

    Written from the rows, like the customer's confirmation, so what the shop
    reads and what the customer was told are the same appointment.
    """
    if not turn.performed or turn.appointment is None:
        return None
    if turn.booked:
        return (
            "New meeting booked" if turn.meeting else "New appointment booked",
            f"{who} booked a {describe(turn.appointment)}. It is in your calendar.",
        )
    if turn.moved:
        was = f" from {describe(turn.previous)}" if turn.previous is not None else ""
        return (
            "Appointment moved",
            f"{who} moved their appointment{was} to {describe(turn.appointment)}. "
            "The calendar has the new time and the old one is free again.",
        )
    if turn.cancelled:
        return (
            "Appointment cancelled",
            f"{who} cancelled their {describe(turn.appointment)}. That time is free again.",
        )
    return None


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
                "fix": "Pick it in Where you are.",
            }
        )

    return {
        "can_book": booking_enabled(organization),
        "blockers": blockers,
        "timezone": zone,
        "days_open": sum(1 for day in agent_config.DAYS if hours.get(day)),
    }
