"""Outbound WhatsApp, whichever way the tenant is connected.

One tenant sends through Twilio's API; another has paired a phone over
WhatsApp Web. Everything upstream of this module — the analyzer, the sales
policy, the CRM, the live dashboard — is identical in both cases, so the choice
lives here and nowhere else.

The routing rule is deliberately narrow: read `channel.whatsapp_provider`, pick
a transport, return the same `(delivered, reference)` tuple either way. Callers
cannot tell the difference, which is what keeps the two paths from drifting
apart.

QR_SESSION drives an unofficial WhatsApp Web session, so it is a stand-in
until official API access is in place. TWILIO stays the default and an
unrecognised value falls back to it, so a typo in the database cannot silently
move a tenant onto the other transport.
"""

from __future__ import annotations

import re

import logging

import httpx

from app.config import settings
from app.services import media_service
from app.services.twilio_service import Sender, twilio_service

logger = logging.getLogger(__name__)

TWILIO = "TWILIO"
QR_SESSION = "QR_SESSION"


def provider_of(channel) -> str:
    """Which transport this channel uses, defaulting to the safe one."""
    value = (getattr(channel, "whatsapp_provider", None) or TWILIO).upper()
    return value if value in (TWILIO, QR_SESSION) else TWILIO


async def _send_via_qr_session(
    channel, to_number: str, body: str, media_urls: list[str] | None, to_jid: str | None = None
) -> tuple[bool, str]:
    """Hand the message to the WhatsApp Web bridge.

    The bridge holds the paired session and speaks the WhatsApp protocol; this
    only needs to describe the message. A failure here is reported the same way
    a Twilio failure is, so the webhook's error handling is unchanged.
    """
    session_id = str(getattr(channel, "id", "") or "")
    payload = {
        "sessionId": session_id,
        "to": (to_number or "").replace("whatsapp:", "").strip(),
        # The exact chat JID, when we know it. WhatsApp addresses many chats by
        # LID rather than phone number, and a JID rebuilt from digits addresses
        # nobody — the send succeeds and the message is never delivered.
        "toJid": to_jid or None,
        "body": body,
        "mediaUrls": media_service.sendable(media_urls or []),
    }

    url = f"{settings.wa_qr_service_url.rstrip('/')}/send"
    try:
        async with httpx.AsyncClient(timeout=settings.wa_qr_timeout_seconds) as client:
            # The bridge refuses anything that does not carry the shared
            # secret. Leaving this off made every single outbound message 403,
            # on every provider-QR tenant, silently — the reply was generated,
            # stored and shown in the dashboard, and never left the building.
            response = await client.post(
                url,
                json=payload,
                headers={"X-PingPulse-Bridge": settings.wa_qr_shared_secret},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 - a transport failure is not a crash
        logger.error("QR session send failed for %s: %s", session_id, exc)
        return False, f"qr-session-error: {exc}"

    if not data.get("ok"):
        reason = data.get("error", "unknown error")
        logger.error("QR session refused the message: %s", reason)
        return False, f"qr-session-refused: {reason}"

    return True, str(data.get("id") or "qr-session")


async def show_typing(channel, to_number: str, to_jid: str | None, message_id: str | None) -> None:
    """Blue ticks and "typing…" while the agent writes its reply. Never raises.

    Only the WhatsApp Web bridge can show these; on Twilio this does nothing.
    Called only once it is decided the agent will answer, so "typing…" never
    promises a reply to somebody a person has taken over from. A slow bridge
    costs the reply nothing: this gives up after two seconds.
    """
    if channel is None or provider_of(channel) != QR_SESSION:
        return
    payload = {
        "sessionId": str(getattr(channel, "id", "") or ""),
        "to": (to_number or "").replace("whatsapp:", "").strip(),
        "toJid": to_jid or None,
        "messageId": message_id or None,
        "state": "composing",
    }
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            await client.post(
                f"{settings.wa_qr_service_url.rstrip('/')}/presence",
                json=payload,
                headers={"X-PingPulse-Bridge": settings.wa_qr_shared_secret},
            )
    except Exception as exc:  # noqa: BLE001 - cosmetic, never at the cost of a reply
        logger.debug("typing indicator not shown: %s", exc)


async def send_message(
    channel,
    to_number: str,
    body: str,
    media_urls: list[str] | None = None,
    to_jid: str | None = None,
) -> tuple[bool, str]:
    """Send one WhatsApp message on whichever transport this tenant uses.

    Returns `(delivered, reference)` — the reference being a provider message
    id when it worked, or a short error string when it did not. Identical shape
    for both providers so callers need no branching of their own.
    """
    provider = provider_of(channel)

    if provider == QR_SESSION:
        return await _send_via_qr_session(channel, to_number, body, media_urls, to_jid)

    return await twilio_service.send_whatsapp(
        to_number, body, media_urls=media_urls, sender=Sender.for_channel(channel)
    )

async def active_channel(db, organization_id):
    """The channel a message to this tenant should go out on.

    A tenant can have more than one connected number — a Twilio account and a
    paired handset, say, while they move between the two. Picking "the first
    active row" then depends on whatever order the database felt like, so an
    operator's reply could leave on a different number from the conversation it
    was answering.

    The order is therefore explicit: a live paired session first, then oldest
    first. It is still a heuristic — the exact answer is the channel the
    conversation arrived on, and messages do not record that yet — but it is
    stable, and it favours the transport that is demonstrably connected.
    """
    from sqlalchemy import case, select

    from app.models import ChannelConfig

    result = await db.execute(
        select(ChannelConfig)
        .where(
            ChannelConfig.organization_id == organization_id,
            ChannelConfig.is_active.is_(True),
        )
        .order_by(
            case((ChannelConfig.session_status == "AUTHENTICATED", 0), else_=1),
            ChannelConfig.created_at,
        )
    )
    return result.scalars().first()


# --------------------------------------------------------------- numbers
# Everything that is not a digit or a leading plus. WhatsApp reports a number
# bare ("923097209908"), a person types it dialled ("+92 309 720 9908"), and
# Twilio prefixes it ("whatsapp:+923097209908"). Three spellings of one phone.
_NOT_A_NUMBER = re.compile(r"[^0-9+]")


def normalise_number(raw: str | None) -> str:
    """One spelling per phone, so a uniqueness rule can mean what it says.

    The unique constraint is on the stored string, which made "+923097209908"
    and "923097209908" two different numbers - and let two organizations claim
    one handset. The clash only surfaced when the phone was paired and the
    bridge wrote back the bare form, by which point the failure was a 500
    inside a callback nobody was watching.

    Leaves a number with no country code alone rather than guessing one. A
    wrong guess here routes a customer's message to the wrong business, which
    is worse than a number that fails to match.
    """
    if not raw:
        return ""
    cleaned = _NOT_A_NUMBER.sub("", str(raw).replace("whatsapp:", "").strip())
    if not cleaned:
        return ""

    # A plus is only meaningful at the front, and only once.
    digits = cleaned.lstrip("+")
    if not digits:
        return ""
    return "+" + digits


def same_number(left: str | None, right: str | None) -> bool:
    """Are these two spellings of the same phone?"""
    normalised = normalise_number(left)
    return bool(normalised) and normalised == normalise_number(right)
