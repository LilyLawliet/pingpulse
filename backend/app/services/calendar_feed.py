"""The diary, as a calendar a phone can subscribe to.

A business that takes appointments through WhatsApp has them in one place and
lives its day out of another. Asking somebody to keep the dashboard open to
find out where they are supposed to be at eleven is asking them to do the
job the calendar on their phone already does.

This is an **iCalendar feed**, served at a secret URL, and that shape is the
whole point of it:

  * **No OAuth, no Google account, nothing official.** A subscription URL is
    read by iOS Calendar, Google Calendar, Outlook and every other client
    without any of them being told who we are. Nobody signs anything, no
    token expires at 3am, and this product does not become a Google
    integration that breaks when a consent screen changes.
  * **Read-only, by construction.** A feed cannot move an appointment. The
    diary stays the one in the database, which is the only place allowed to
    say an appointment exists, and a phone showing a stale copy for an hour
    cannot double-book anybody.
  * **One direction.** A calendar the business can edit would be a second
    source of truth about when somebody is expected, and the first thing this
    subsystem was built to end was two answers to "am I booked?".

The URL carries a secret rather than a login, because that is what a
subscribing client can send: it will fetch this every few hours for years
with no way to be prompted for anything. The secret is per organization, long,
and rotatable, and it is the reason the feed exposes customer names - a link
handed to somebody is a link they can hand on, which is why rotating it is
one press and is described in those terms.

Cancelled appointments are published as CANCELLED rather than dropped. A row
that simply vanishes leaves the phone showing it forever, which is exactly the
"is it cancelled or not" question the appointments table exists to answer.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import APPOINTMENT_CANCELLED, Appointment, CRMContact

logger = logging.getLogger(__name__)

TOKEN_BYTES = 32

# How far back a subscribed phone still shows. Enough to answer "who was that
# on Tuesday" without the feed growing without limit.
PAST_DAYS = 60
FUTURE_DAYS = 365
MAX_EVENTS = 2000

# What a client is asked to wait between fetches. Advisory - iOS and Google
# both apply their own floor, and neither goes below about an hour - so the
# feed is never the place a booking is confirmed to a customer.
REFRESH = "PT1H"

PRODID = "-//PingPulse//Appointments//EN"


def new_token() -> str:
    """A secret long enough that the URL is the only way in."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _fold(line: str) -> str:
    """Wrap to 75 octets, as iCalendar requires.

    Not cosmetic. Outlook rejects a calendar with over-long lines, and the
    failure is the whole feed refusing to load rather than one event looking
    wrong - so a single long address would take the day's diary with it.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    chunks, current = [], b""
    for char in line:
        raw = char.encode("utf-8")
        # 74 leaves room for the leading space on continuation lines.
        limit = 75 if not chunks else 74
        if len(current) + len(raw) > limit:
            chunks.append(current)
            current = b""
        current += raw
    if current:
        chunks.append(current)

    first, *rest = (chunk.decode("utf-8") for chunk in chunks)
    return "\r\n".join([first] + [" " + part for part in rest])


def _escape(value: str | None) -> str:
    """Text as iCalendar wants it: commas, semicolons and newlines escaped."""
    if not value:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _stamp(moment: datetime) -> str:
    """UTC, in the basic format. Everything is published in UTC and rendered
    by the phone in whatever zone it is standing in, which is the behaviour
    somebody travelling actually wants."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _summary(appointment, contact) -> str:
    """What shows on the phone at a glance: who, and what kind of visit."""
    who = (contact.name if contact else None) or (
        contact.phone_number if contact else None
    ) or "Customer"
    kind = (appointment.kind or "").replace("_", " ").strip()
    return f"{who} — {kind}" if kind else str(who)


def _description(appointment, contact) -> str:
    """The detail, including the number, so a call is one tap from the event."""
    parts: list[str] = []
    if contact and contact.name:
        parts.append(f"Contact: {contact.name}")
    if contact and contact.phone_number:
        parts.append(f"WhatsApp: {contact.phone_number}")
    if appointment.notes:
        parts.append(str(appointment.notes))
    if appointment.status == APPOINTMENT_CANCELLED:
        parts.append("CANCELLED")
    parts.append("Booked through PingPulse.")
    return "\n".join(parts)


async def appointments_for(db, organization_id, now: datetime | None = None):
    """The rows this feed publishes, newest window first."""
    moment = now or datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Appointment, CRMContact)
            .join(CRMContact, CRMContact.id == Appointment.contact_id, isouter=True)
            .where(
                Appointment.organization_id == organization_id,
                Appointment.starts_at >= moment - timedelta(days=PAST_DAYS),
                Appointment.starts_at <= moment + timedelta(days=FUTURE_DAYS),
            )
            .order_by(Appointment.starts_at)
            .limit(MAX_EVENTS)
        )
    ).all()
    return rows


def render(organization_name: str, rows, now: datetime | None = None) -> str:
    """The whole calendar, as one iCalendar document.

    CRLF throughout, because the specification says so and because the clients
    that tolerate bare newlines are not the ones a client will be using.
    """
    moment = now or datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold(f"X-WR-CALNAME:{_escape(organization_name)} appointments"),
        f"X-PUBLISHED-TTL:{REFRESH}",
        f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH}",
    ]

    for appointment, contact in rows:
        cancelled = appointment.status == APPOINTMENT_CANCELLED
        lines += [
            "BEGIN:VEVENT",
            # Stable across every fetch, so an edit updates the event a phone
            # already has instead of adding a second copy of it.
            f"UID:{appointment.id}@pingpulse",
            f"DTSTAMP:{_stamp(moment)}",
            f"DTSTART:{_stamp(appointment.starts_at)}",
            f"DTEND:{_stamp(appointment.ends_at)}",
            _fold(f"SUMMARY:{_escape(_summary(appointment, contact))}"),
            _fold(f"DESCRIPTION:{_escape(_description(appointment, contact))}"),
            f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
            # Bumped for a cancellation so a client that has already cached
            # the event accepts the replacement rather than keeping the old one.
            f"SEQUENCE:{1 if cancelled else 0}",
            f"TRANSP:{'TRANSPARENT' if cancelled else 'OPAQUE'}",
        ]
        if appointment.location:
            lines.append(_fold(f"LOCATION:{_escape(appointment.location)}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
