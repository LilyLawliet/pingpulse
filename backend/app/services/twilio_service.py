"""Thin async-friendly wrapper around the Twilio REST client.

Multi-tenant: each organization can bring its own Twilio account and WhatsApp
number via `channel_configs`. Anything a tenant leaves blank falls back to the
platform credentials, so a single-tenant install needs no per-org setup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.config import settings
from app.services import media_service

logger = logging.getLogger(__name__)

try:  # twilio is a hard dependency, but the app must still import without it
    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client

    TWILIO_AVAILABLE = True
except ImportError:  # pragma: no cover - only hit on a broken install
    Client = None  # type: ignore[assignment]
    TwilioRestException = Exception  # type: ignore[assignment,misc]
    TWILIO_AVAILABLE = False


def _as_whatsapp(number: str) -> str:
    number = (number or "").strip()
    if not number:
        return ""
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


@dataclass(frozen=True)
class Sender:
    """Which Twilio account and number a message goes out on."""

    account_sid: str
    auth_token: str
    from_number: str

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)

    @property
    def whatsapp_from(self) -> str:
        return _as_whatsapp(self.from_number)

    @classmethod
    def platform(cls) -> "Sender":
        return cls(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            from_number=settings.twilio_whatsapp_number,
        )

    @classmethod
    def for_channel(cls, channel) -> "Sender":
        """Build a sender from a channel config, filling gaps from the platform.

        A tenant that supplies only a number still sends on the platform
        account; one that supplies its own credentials is fully isolated.
        """
        if channel is None:
            return cls.platform()

        base = cls.platform()
        account_sid = (getattr(channel, "account_sid", "") or "").strip() or base.account_sid
        auth_token = (getattr(channel, "auth_token", "") or "").strip() or base.auth_token

        # Credentials must not be mixed: a tenant token only works with the
        # tenant's own SID, so if one is supplied both must be.
        if bool(getattr(channel, "account_sid", "")) != bool(getattr(channel, "auth_token", "")):
            logger.warning(
                "channel %s has only half its Twilio credentials; using platform account",
                getattr(channel, "phone_number", "?"),
            )
            account_sid, auth_token = base.account_sid, base.auth_token

        from_number = (getattr(channel, "phone_number", "") or "").strip() or base.from_number
        return cls(account_sid=account_sid, auth_token=auth_token, from_number=from_number)


class TwilioService:
    """Sends WhatsApp messages and reports client health.

    The Twilio SDK is synchronous, so every network call is pushed to a worker
    thread to keep the FastAPI event loop free. Clients are cached per account
    so a busy tenant does not re-authenticate on every message.
    """

    def __init__(self) -> None:
        self._clients: dict[str, "Client"] = {}

    @property
    def configured(self) -> bool:
        return TWILIO_AVAILABLE and Sender.platform().configured

    def client_for(self, sender: Sender) -> "Client":
        if not TWILIO_AVAILABLE:
            raise RuntimeError("twilio package is not installed")
        if not sender.account_sid or not sender.auth_token:
            raise RuntimeError("Twilio credentials are not configured")
        if sender.account_sid not in self._clients:
            self._clients[sender.account_sid] = Client(sender.account_sid, sender.auth_token)
        return self._clients[sender.account_sid]

    @property
    def client(self) -> "Client":
        """The platform client, used by the health probe."""
        return self.client_for(Sender.platform())

    def _send_sync(
        self, sender: Sender, to_number: str, body: str, media_urls: list[str] | None
    ) -> str:
        kwargs = {
            "from_": sender.whatsapp_from,
            "to": _as_whatsapp(to_number),
            "body": body,
        }
        if media_urls:
            # WhatsApp fetches these itself, so they must be publicly reachable.
            kwargs["media_url"] = media_urls
        return self.client_for(sender).messages.create(**kwargs).sid

    async def send_whatsapp(
        self,
        to_number: str,
        body: str,
        media_urls: list[str] | None = None,
        sender: Sender | None = None,
    ) -> tuple[bool, str]:
        """Return (ok, sid_or_error). Never raises - the caller logs the outcome.

        If sending with attachments fails, one retry goes out as text only: a
        customer who gets the answer without the photo is far better served
        than one who gets nothing because a CDN link was unreachable.
        """
        sender = sender or Sender.platform()
        if not TWILIO_AVAILABLE or not sender.configured:
            return False, "Twilio is not configured for this organization"

        usable = media_service.sendable(media_urls or [])
        try:
            sid = await asyncio.to_thread(self._send_sync, sender, to_number, body, usable)
            logger.info(
                "sent WhatsApp to %s from %s (sid=%s, media=%d)",
                to_number,
                sender.from_number,
                sid,
                len(usable),
            )
            return True, sid
        except Exception as exc:  # noqa: BLE001 - surfaced to the monitor stream
            if usable:
                logger.warning(
                    "send with %d attachment(s) failed (%s); retrying as text",
                    len(usable),
                    exc,
                )
                try:
                    sid = await asyncio.to_thread(
                        self._send_sync, sender, to_number, body, None
                    )
                    return True, sid
                except Exception as retry_exc:  # noqa: BLE001
                    exc = retry_exc
            logger.error("Twilio send failed for %s: %s", to_number, exc)
            return False, str(exc)

    def _probe_sync(self) -> str:
        sender = Sender.platform()
        account = self.client_for(sender).api.accounts(sender.account_sid).fetch()
        return f"account {account.friendly_name} status={account.status}"

    async def probe(self) -> tuple[bool, str]:
        """Verify the platform credentials against the live Twilio API."""
        if not TWILIO_AVAILABLE:
            return False, "twilio package not installed"
        if not self.configured:
            return False, "Twilio credentials not configured"
        try:
            return True, await asyncio.to_thread(self._probe_sync)
        except TwilioRestException as exc:
            return False, f"Twilio API error {exc.status}: {exc.msg}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


twilio_service = TwilioService()


# --------------------------------------------------------------------------
# Inbound authenticity
# --------------------------------------------------------------------------
def signature_url(public_base_url: str, path: str, query: str = "") -> str:
    """The URL Twilio signed.

    Twilio computes its signature over the *public* URL it called. Behind a
    reverse proxy the application sees an internal one, so the signature is
    checked against PUBLIC_BASE_URL plus the path rather than whatever the
    request object reports. Deriving it from proxy headers instead is the
    usual reason signature validation rejects every genuine message.
    """
    base = (public_base_url or "").rstrip("/")
    url = f"{base}{path}" if base else path
    return f"{url}?{query}" if query else url


def validate_twilio_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str
) -> bool:
    """Is this request really from the Twilio account holding `auth_token`?

    The token is the shared secret, so this must be the *tenant's* token when
    the tenant brought their own Twilio account. Validating a BYOK client's
    traffic against the platform token would reject every message they send.

    Any failure is a rejection: a malformed signature is not a reason to let a
    request through.
    """
    if not auth_token or not signature:
        return False

    try:
        from twilio.request_validator import RequestValidator

        return bool(RequestValidator(auth_token).validate(url, params, signature))
    except Exception as exc:  # noqa: BLE001 - never fail open
        logger.warning("signature validation error: %s", exc)
        return False
