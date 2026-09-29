"""Each appointment, sent to the owner's own calendar as an invitation.

The subscription feed puts the diary in a phone's calendar, but Google and
Apple only re-read a subscribed calendar every few hours - fine for a shop
glancing at tomorrow, useless for somebody who books a demo at 10 for 11.

An email carrying an iCalendar invitation (METHOD:REQUEST) is added to Gmail,
Google Calendar, Outlook and Apple Calendar the moment it arrives, the same
way a meeting invite from a colleague is. Moving an appointment cancels the
old event and invites to the new one; cancelling sends METHOD:CANCEL, which
takes the event off their calendar. Nothing to connect, no OAuth: it goes to
the alert address the owner already set.

Every event's UID ends in "@pingpulse", which is how the owner's calendar,
read back for busy times, knows these are ours and not a second booking.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.services import calendar_feed, notifications

logger = logging.getLogger(__name__)

CONFIG_KEY = "invite_owner"


def wanted(organization) -> bool:
    """On unless switched off, whenever email can actually be sent."""
    config = (getattr(organization, "agent_config", None) or {}).get("appointments") or {}
    return config.get(CONFIG_KEY, True) is not False and notifications.email_available()


def uid(appointment) -> str:
    return f"invite-{appointment.id}@pingpulse"


def render(appointment, contact, attendee: str, method: str, now: datetime | None = None) -> str:
    """One event as an iCalendar invitation (REQUEST) or its withdrawal (CANCEL)."""
    moment = now or datetime.now(timezone.utc)
    cancel = method == "CANCEL"
    organizer = settings.smtp_from or attendee
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{calendar_feed.PRODID}",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid(appointment)}",
        f"DTSTAMP:{calendar_feed._stamp(moment)}",
        f"DTSTART:{calendar_feed._stamp(appointment.starts_at)}",
        f"DTEND:{calendar_feed._stamp(appointment.ends_at)}",
        calendar_feed._fold(
            f"SUMMARY:{calendar_feed._escape(calendar_feed._summary(appointment, contact))}"
        ),
        calendar_feed._fold(
            f"DESCRIPTION:{calendar_feed._escape(calendar_feed._description(appointment, contact))}"
        ),
        calendar_feed._fold(f"ORGANIZER;CN=PingPulse:mailto:{organizer}"),
        calendar_feed._fold(
            f"ATTENDEE;CN={calendar_feed._escape(attendee)};ROLE=REQ-PARTICIPANT;"
            f"PARTSTAT=ACCEPTED;RSVP=FALSE:mailto:{attendee}"
        ),
        f"STATUS:{'CANCELLED' if cancel else 'CONFIRMED'}",
        # A withdrawal has to be a later version of the same event, or the
        # calendar keeps the one it already has.
        f"SEQUENCE:{1 if cancel else 0}",
        "TRANSP:OPAQUE",
    ]
    if appointment.location:
        lines.append(calendar_feed._fold(f"LOCATION:{calendar_feed._escape(appointment.location)}"))
    if not cancel:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Reminder",
            "TRIGGER:-PT15M",
            "END:VALARM",
        ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def _message(organization, appointment, contact, attendee: str, method: str):
    from email.message import EmailMessage

    from app.services import booking

    said = booking.describe(appointment)
    who = (contact.name if contact else None) or (contact.phone_number if contact else "") or "A customer"
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = attendee
    if method == "CANCEL":
        message["Subject"] = f"Cancelled: {who} - {said}"
        text = f"{who}'s {said} was cancelled. It has been taken off your calendar."
    else:
        message["Subject"] = f"{who} - {said}"
        text = f"{who} is booked: {said}.\n\nIt has been added to your calendar."
    if appointment.notes:
        text += f"\n\n{appointment.notes}"
    text += f"\n\nOpen the dashboard: {settings.dashboard_url}"
    message.set_content(text)

    body = render(appointment, contact, attendee, method)
    # As an alternative part, which is what makes Gmail and Outlook treat it
    # as an invitation rather than an attachment somebody has to open.
    message.add_alternative(body, subtype="calendar", params={"method": method})
    message.add_attachment(
        body.encode("utf-8"),
        maintype="application",
        subtype="ics",
        filename="invite.ics",
    )
    return message


async def _send(message) -> str:
    import aiosmtplib

    try:
        await asyncio.wait_for(
            aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password or None,
                start_tls=settings.smtp_starttls,
            ),
            timeout=notifications.EMAIL_TIMEOUT,
        )
        return "sent"
    except Exception as exc:  # noqa: BLE001 - a calendar invite never costs a booking
        logger.warning("calendar invite not sent: %s", exc)
        return f"failed: {type(exc).__name__}"


async def send_for(db, organization, *, booked=None, cancelled=None) -> list[str]:
    """Put what just happened into the owner's calendar. Never raises.

    `booked` is a new or moved-to appointment, `cancelled` a cancelled or
    moved-from one. A move passes both.
    """
    try:
        if not wanted(organization):
            return []
        attendee = await notifications.address_for(db, organization)
        if not attendee:
            return []
        outcomes = []
        for appointment, method in ((cancelled, "CANCEL"), (booked, "REQUEST")):
            if appointment is None:
                continue
            from app.models import CRMContact

            # From the identity map, never a lazy load: this runs async.
            contact = await db.get(CRMContact, appointment.contact_id)
            outcomes.append(await _send(_message(organization, appointment, contact, attendee, method)))
        return outcomes
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar invite failed: %s", exc)
        return [f"failed: {type(exc).__name__}"]
