"""Attachment handling in both directions.

Inbound: Twilio hosts what a customer sends behind HTTP Basic auth and expires
it, so anything we want to keep is downloaded to the media volume on Drive D:
and re-served from our own domain.

Outbound: WhatsApp fetches media by URL, so whatever we send must be publicly
reachable — either a supplier CDN link or one of our own stored files.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

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

    auth = None
    if "twilio.com" in url and settings.twilio_account_sid:
        # Twilio media requires the account credentials to fetch.
        auth = (settings.twilio_account_sid, settings.twilio_auth_token)

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, auth=auth)
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
