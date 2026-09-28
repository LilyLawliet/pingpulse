"""Attachment handling in both directions.

Inbound: Twilio hosts what a customer sends behind HTTP Basic auth and expires
it, so anything we want to keep is downloaded to the media volume on Drive D:
and re-served from our own domain.

Outbound: WhatsApp fetches media by URL, so whatever we send must be publicly
reachable — either a supplier CDN link or one of our own stored files.
"""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _is_public_host(host: str) -> bool:
    """Whether a hostname resolves only to public addresses.

    The inbound fetch downloads a URL and follows redirects, and this service
    shares a network with Redis, Postgres and the WhatsApp bridge. A URL that
    resolved to one of those - or to the cloud metadata endpoint - would turn
    "save the customer's photo" into a request to internal infrastructure. So
    every address a host resolves to is checked, and a single private one is
    enough to refuse the whole host: a name that returns one public and one
    loopback address is the shape of exactly this attack.

    Fails closed. A name that will not resolve is not fetched.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False

    for info in infos:
        raw = info[4][0].split("%")[0]  # drop any zone id
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return bool(infos)


def _fetchable(url: str) -> bool:
    """Whether this URL is one we will fetch at all.

    http/https only - no file://, no ftp:// reaching for the local disk - and
    a host that lives on the public internet rather than beside us on the
    network.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return _is_public_host(parsed.hostname or "")

# WhatsApp will not fetch an arbitrary file; keep to what it renders inline.
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",
    "application/pdf": ".pdf",
}

MAX_BYTES = 16 * 1024 * 1024  # Twilio's own per-media ceiling.


def media_root() -> Path:
    path = Path(settings.media_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def public_url(filename: str) -> str:
    """Absolute URL for a stored file, so WhatsApp can fetch it."""
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/media/{filename}" if base else f"/media/{filename}"


PICTURE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def store_picture(data: bytes, content_type: str) -> str | None:
    """Keep a product picture from an uploaded document; return its URL.

    Only the picture kinds WhatsApp shows inline, and nothing over the size
    it accepts. Anything else is left out rather than sent and dropped.
    """
    extension = PICTURE_TYPES.get((content_type or "").split(";")[0].strip().lower())
    if not extension or not data or len(data) > MAX_BYTES:
        return None
    name = f"{uuid.uuid4().hex}{extension}"
    (media_root() / name).write_bytes(data)
    return public_url(name)


def _extension_for(content_type: str, fallback_url: str) -> str | None:
    clean = (content_type or "").split(";")[0].strip().lower()
    if clean in ALLOWED_TYPES:
        return ALLOWED_TYPES[clean]
    guessed = mimetypes.guess_extension(clean) if clean else None
    if guessed:
        return guessed
    suffix = Path(fallback_url.split("?")[0]).suffix
    return suffix or None


async def download_inbound(url: str, content_type: str = "") -> str | None:
    """Save one inbound Twilio attachment locally; return its public URL.

    Returns None rather than raising — a customer's photo failing to save must
    not stop us replying to them.
    """
    if not url:
        return None

    if not _fetchable(url):
        # Not http(s), or a host that resolves onto our own network. A
        # customer's photo lives on a CDN; a request aimed at redis:6379 or
        # the metadata endpoint does not.
        logger.warning("refusing to fetch inbound media from a non-public URL")
        return None

    auth = None
    if "twilio.com" in url and settings.twilio_account_sid:
        # Twilio media requires the account credentials to fetch.
        auth = (settings.twilio_account_sid, settings.twilio_auth_token)

    try:
        # Redirects are followed by hand so each hop is re-checked. Left to the
        # client, a public URL answering 302 -> http://169.254.169.254/ would
        # sail straight through the check above, which only ever saw the first
        # URL.
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(url, auth=auth)
            for _ in range(5):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("location", "")
                nxt = str(httpx.URL(response.url).join(location))
                if not _fetchable(nxt):
                    logger.warning("refusing an inbound media redirect to a non-public URL")
                    return None
                response = await client.get(nxt, auth=auth)
            response.raise_for_status()
            payload = response.content
            if len(payload) > MAX_BYTES:
                logger.warning("inbound media too large (%d bytes), skipping", len(payload))
                return None
            extension = _extension_for(
                content_type or response.headers.get("content-type", ""), url
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not download inbound media %s: %s", url, exc)
        return None

    if not extension:
        logger.info("unsupported media type for %s, skipping", url)
        return None

    filename = f"{uuid.uuid4().hex}{extension}"
    try:
        (media_root() / filename).write_bytes(payload)
    except OSError as exc:
        logger.error("could not write media to %s: %s", settings.media_dir, exc)
        return None

    logger.info("stored inbound media as %s (%d bytes)", filename, len(payload))
    return public_url(filename)


def extract_inbound(payload: dict[str, str]) -> list[tuple[str, str]]:
    """Pull (url, content_type) pairs out of a Twilio form payload.

    Twilio numbers them MediaUrl0..N with a matching MediaContentType0..N.
    """
    try:
        count = int(payload.get("NumMedia", "0") or 0)
    except ValueError:
        count = 0

    found: list[tuple[str, str]] = []
    for index in range(min(count, 10)):
        url = payload.get(f"MediaUrl{index}")
        if url:
            found.append((url, payload.get(f"MediaContentType{index}", "")))
    return found


def sendable(urls: list[str], limit: int | None = None) -> list[str]:
    """Keep only URLs WhatsApp can actually fetch, capped to a sane count.

    A relative path means the file is ours but PUBLIC_BASE_URL is unset, so
    WhatsApp could not reach it — dropped rather than sent and silently failed.
    """
    cap = limit or settings.max_media_per_message
    usable = [u for u in urls if u and u.startswith(("http://", "https://"))]
    return usable[:cap]
