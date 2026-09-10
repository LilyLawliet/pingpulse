"""Vision analysis of images the customer sends.

When someone photographs an outfit they like, the agent should not ask "which
colour?" — the answer is in the picture. The image is sent to Gemini Flash,
which returns colour, pattern, category and fabric as strict JSON; that becomes
searchable state on the contact.

Never raises: an unreadable photo degrades to "we could not read it", which the
agent handles by asking, rather than to a failed conversation.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.services import colour

logger = logging.getLogger(__name__)

VISION_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

VISION_PROMPT = """Look at this photo of clothing and describe ONLY what you can actually see.
Return strict JSON with exactly these keys and no other text:

{"colour": "the dominant colour in one or two words, or null",
 "secondary_colour": "another prominent colour, or null",
 "pattern": "printed, embroidered, plain, striped, floral, geometric, or null",
 "category": "what garment this is (e.g. kurta, 3 piece suit, saree, shirt, trousers), or null",
 "fabric": "the fabric if it is obvious (lawn, cotton, silk, chiffon, linen), or null",
 "occasion": "casual, formal, party, bridal, or null",
 "description": "one short sentence describing the item"}

Use null for anything you cannot see clearly. Never guess a fabric from the colour alone."""


def _extension_of(content_type: str) -> str:
    return (content_type or "image/jpeg").split(";")[0].strip() or "image/jpeg"


async def _fetch_bytes(url: str) -> tuple[bytes, str] | None:
    """Download the image, using Twilio credentials when it is Twilio-hosted."""
    auth = None
    if "twilio.com" in url and settings.twilio_account_sid:
        auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
            response = await client.get(url, auth=auth)
            response.raise_for_status()
            return response.content, response.headers.get("content-type", "image/jpeg")
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not fetch image for vision (%s): %s", url, exc)
        return None


async def _call_vision(payload: dict[str, Any], api_key: str) -> str:
    url = VISION_URL.format(model=settings.vision_model)
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds + 20) as client:
        response = await client.post(url, json=payload, params={"key": api_key})
        response.raise_for_status()
        data = response.json()

    candidate = (data.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or [{}]
    return (parts[0].get("text") or "").strip()


def _parse(raw: str) -> dict[str, Any]:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    allowed = (
        "colour",
        "secondary_colour",
        "pattern",
        "category",
        "fabric",
        "occasion",
        "description",
    )
    clean: dict[str, Any] = {}
    for key in allowed:
        value = parsed.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value and value.lower() not in ("null", "none", "n/a", "unknown"):
                clean[key] = value[:120]
    return clean


async def analyse_image(url: str, content_type: str = "") -> dict[str, Any]:
    """Describe one customer image. Returns {} when it cannot be read."""
    if not settings.vision_enabled:
        return {}

    keys = settings.gemini_api_keys
    if not keys:
        logger.info("no Gemini key configured; skipping vision")
        return {}

    fetched = await _fetch_bytes(url)
    if fetched is None:
        return {}
    payload_bytes, detected_type = fetched

    # Colour comes from the pixels, always, with no API involved. Whatever the
    # model adds is a bonus; whatever it fails to add costs us nothing.
    local = colour.analyse(payload_bytes)

    request = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": VISION_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": _extension_of(content_type or detected_type),
                            "data": base64.b64encode(payload_bytes).decode(),
                        }
                    },
                ],
            }
        ],
        # Vision models spend part of the budget reasoning before writing.
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
    }

    # The vision endpoint is rate-limited far more aggressively than text, so
    # every key is tried, then the whole set is tried again after a pause. An
    # unparseable answer counts as a failure worth retrying, not a verdict.
    last: Exception | str | None = None
    for attempt in range(settings.vision_attempts):
        for api_key in keys:
            try:
                analysis = _parse(await _call_vision(request, api_key))
                if analysis:
                    # The model's reading wins where it has one; the pixel
                    # reading fills any gap it left.
                    analysis = {**local, **analysis}
                    analysis["analysed_at"] = datetime.now(timezone.utc).isoformat()
                    analysis["image_url"] = url
                    analysis["model"] = settings.vision_model
                    logger.info("vision read image as: %s", analysis.get("description"))
                    return analysis
                last = "unparseable response"
            except Exception as exc:  # noqa: BLE001
                last = exc
        if attempt + 1 < settings.vision_attempts:
            await asyncio.sleep(1.5 * (attempt + 1))

    if local:
        # Vision is unavailable, but the photo still told us its colour, which
        # is the field product matching actually uses.
        logger.info("vision unavailable (%s); using the local colour read", last)
        local["analysed_at"] = datetime.now(timezone.utc).isoformat()
        local["image_url"] = url
        local["model"] = "local-pixels"
        return local

    logger.warning("vision could not read the image after %d rounds: %s",
                   settings.vision_attempts, last)
    return {}


UNREADABLE_NOTE = (
    "=== THE PHOTO THE CUSTOMER SENT ===\n"
    "They sent a photo, but it could not be read this time. Acknowledge that you have "
    "received their picture, then ask ONE short question about it (for example the "
    "colour, or the kind of outfit) so you can find something similar. Never ignore "
    "the photo and never claim you cannot receive images."
)


def as_prompt_block(analysis: dict[str, Any], image_received: bool = False) -> str:
    """Tell the agent what it is looking at, and not to ask what it can see."""
    if not analysis:
        # A photo we failed to read must never produce a reply that pretends
        # nothing arrived.
        return UNREADABLE_NOTE if image_received else ""

    described = [
        f"Colour: {analysis['colour']}" if analysis.get("colour") else "",
        f"Also: {analysis['secondary_colour']}" if analysis.get("secondary_colour") else "",
        f"Pattern: {analysis['pattern']}" if analysis.get("pattern") else "",
        f"Type: {analysis['category']}" if analysis.get("category") else "",
        f"Fabric: {analysis['fabric']}" if analysis.get("fabric") else "",
        f"Occasion: {analysis['occasion']}" if analysis.get("occasion") else "",
    ]
    lines = [
        "=== THE PHOTO THE CUSTOMER SENT ===",
        analysis.get("description", "A clothing item."),
    ]
    lines += [bit for bit in described if bit]

    if analysis.get("confidence") == "low":
        # Read from pixels alone, which is unreliable on a photo of a person:
        # skin and warm backdrops can outvote the garment.
        lines.append(
            "This reading came from the image's colours only and may be wrong. "
            'Acknowledge their photo and CONFIRM the colour in one short question '
            '("is it the red one?") rather than stating it as fact.'
        )
    else:
        lines.append(
            "You can see this image. Do NOT ask them what colour or what type it is — "
            "say what you can see and offer the closest things you actually stock."
        )
    return "\n".join(lines)


def search_terms(analysis: dict[str, Any]) -> str:
    """Turn a vision result into words the product matcher understands."""
    if not analysis:
        return ""
    return " ".join(
        str(analysis.get(key))
        for key in ("colour", "pattern", "fabric", "category")
        if analysis.get(key)
    )
