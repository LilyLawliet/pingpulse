"""Meeting links for customers who want to talk to a person.

Some conversations are not retail — a wholesale buyer, a B2B prospect, a
partnership enquiry. Those want a call, and the right answer is a booking link
they can open, not a promise that someone will ring them.

Supports Cal.com and a Google Calendar link builder. Both produce a plain URL
that works in WhatsApp; neither needs an OAuth round-trip at message time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlencode

from app.config import settings

logger = logging.getLogger(__name__)

# Signals that a conversation is business-to-business rather than retail.
B2B_MARKERS = (
    "wholesale", "bulk", "b2b", "reseller", "distributor", "partnership",
    "corporate", "franchise", "supplier", "trade", "stockist", "quantity",
    "for my shop", "for our store", "purchase order", "invoice for company",
)


def looks_like_b2b(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in B2B_MARKERS)


def _prefill(name: str | None, phone: str | None) -> dict[str, str]:
    fields = {}
    if name:
        fields["name"] = name
    if phone:
        # Cal.com and Google both accept this as a note rather than a field.
        fields["phone"] = phone
    return fields


def cal_com_link(
    contact_name: str | None = None,
    phone: str | None = None,
    note: str | None = None,
) -> str | None:
    """A personalised Cal.com booking URL, or None if not configured."""
    if not settings.calcom_link:
        return None

    base = settings.calcom_link.rstrip("/")
    params = _prefill(contact_name, phone)
    if note:
        params["notes"] = note[:200]
    return f"{base}?{urlencode(params)}" if params else base


def google_calendar_link(
    title: str,
    contact_name: str | None = None,
    phone: str | None = None,
    note: str | None = None,
    start: datetime | None = None,
    duration_minutes: int = 30,
) -> str:
    """A Google Calendar "add event" link, defaulting to tomorrow morning.

    This is a link builder, not a booking API: the customer picks and confirms
    in their own calendar, which needs no credentials from either side.

    No time is invented. This used to default to 05:00 UTC tomorrow when the
    caller passed no start, which is 1am on the US east coast - and a client
    reported an appointment "confirmed" for 1am that their customer had never
    chosen. Nobody had chosen it. A hardcoded hour had.

    With no start, the link carries no date at all and the customer picks one,
    which is the only honest thing a link can do.
    """
    begins = start
    ends = begins + timedelta(minutes=duration_minutes) if begins else None
    stamp = "%Y%m%dT%H%M%SZ"

    details = note or "Introductory call"
    if contact_name:
        details = f"{details}\nContact: {contact_name}"
    if phone:
        details = f"{details}\nWhatsApp: {phone}"

    fields = {"action": "TEMPLATE", "text": title, "details": details}
    if begins and ends:
        fields["dates"] = f"{begins.strftime(stamp)}/{ends.strftime(stamp)}"

    query = urlencode(fields, quote_via=quote_plus)
    return f"https://calendar.google.com/calendar/render?{query}"


def booking_link(
    organization_name: str,
    contact_name: str | None = None,
    phone: str | None = None,
    note: str | None = None,
) -> str | None:
    """The best available booking link, or None if scheduling is switched off."""
    if not settings.scheduling_enabled:
        return None

    link = cal_com_link(contact_name, phone, note)
    if link:
        return link
    if settings.calendar_fallback_enabled:
        return google_calendar_link(
            title=f"Call with {organization_name}",
            contact_name=contact_name,
            phone=phone,
            note=note,
        )
    return None


def as_prompt_block(
    organization_name: str,
    contact_name: str | None = None,
    phone: str | None = None,
    is_b2b: bool = False,
) -> str:
    """Give the agent a real link to send, or tell it plainly that it has none."""
    note = "Wholesale / B2B enquiry" if is_b2b else "Sales call"
    link = booking_link(organization_name, contact_name, phone, note)

    if not link:
        return (
            "=== BOOKING ===\n"
            "No booking link is configured. Do NOT promise a callback. Ask for the best "
            "time and what they need, and keep helping them here in the chat."
        )

    return (
        "=== BOOKING ===\n"
        f"They can book a call here: {link}\n"
        "Send this link exactly as written, in one short sentence. Do not say a person "
        "will contact them — they choose a slot themselves."
    )
