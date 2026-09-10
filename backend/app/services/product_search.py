"""Matching catalogue products so the agent can send real pictures.

Retrieval finds *facts*; this finds *things to show*. It is deliberately
attribute-first rather than embedding-first: "red printed lawn" is a set of
filters (colour, print, fabric), and a customer who asks for red must never be
shown blue because the vectors were close.

Everything is scoped by organization_id at the SQL level.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeDocument
from app.services.analyzer import COLOUR_WORDS

logger = logging.getLogger(__name__)

FABRIC_WORDS = (
    "lawn", "cotton", "linen", "silk", "chiffon", "khaddar", "velvet",
    "cambric", "latha", "karandi", "jacquard", "organza", "net", "viscose",
)

STYLE_WORDS = (
    "printed", "embroidered", "unstitched", "stitched", "pret", "casual",
    "formal", "party", "wedding", "summer", "winter", "eid", "festive",
    "kurta", "suit", "shirt", "dupatta", "trouser", "shalwar", "sari", "saree",
)

PIECE_WORDS = ("1 piece", "2 piece", "3 piece", "one piece", "two piece", "three piece")


def wanted_attributes(text: str) -> dict[str, list[str]]:
    """Read colour, fabric and style words out of what the customer typed."""
    lowered = (text or "").lower()

    def found(words) -> list[str]:
        return [w for w in words if re.search(rf"\b{re.escape(w)}\b", lowered)]

    colours = found(COLOUR_WORDS)
    # "grey" and "gray" are the same request.
    if "gray" in colours and "grey" not in colours:
        colours.append("grey")

    return {
        "colours": colours,
        "fabrics": found(FABRIC_WORDS),
        "styles": found(STYLE_WORDS),
        "pieces": [p for p in PIECE_WORDS if p in lowered],
    }


# Shades a customer means when they name a colour. Asking for red and being
# offered maroon is helpful; being offered blue is not.
COLOUR_FAMILIES = {
    "red": ("red", "maroon", "rust", "crimson", "burgundy", "wine"),
    "maroon": ("maroon", "red", "burgundy", "wine"),
    "blue": ("blue", "navy", "teal", "denim", "sky"),
    "navy": ("navy", "blue"),
    "green": ("green", "olive", "teal", "mint", "sage"),
    "pink": ("pink", "peach", "rose", "fuchsia", "magenta"),
    "purple": ("purple", "lilac", "lavender", "plum"),
    "yellow": ("yellow", "mustard", "lemon", "gold"),
    "orange": ("orange", "rust", "peach", "tangerine"),
    "brown": ("brown", "tan", "coffee", "camel", "beige"),
    "grey": ("grey", "gray", "silver", "charcoal"),
    "white": ("white", "off white", "ivory", "cream"),
    "black": ("black", "charcoal"),
}


def _word_in(needle: str, haystack: str) -> bool:
    """Whole-word match.

    A plain substring test is wrong here: "red" appears inside "embroidered",
    which silently turns every embroidered item into a red one.
    """
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


def _haystack(document: KnowledgeDocument) -> str:
    attributes = document.attributes or {}
    parts = [document.title or "", document.content or ""]
    for key in ("colour", "color", "fabric", "collection", "product_type", "tags"):
        value = attributes.get(key)
        if isinstance(value, list):
            parts.append(" ".join(str(v) for v in value))
        elif value:
            parts.append(str(value))
    return " ".join(parts).lower()


def colour_match(document: KnowledgeDocument, wanted_colours: list[str]) -> float:
    """2.0 for the exact colour, 1.0 for the same family, 0.0 for no match.

    The structured colour attribute is trusted first; the description is only
    consulted when the feed did not give us one.
    """
    if not wanted_colours:
        return 0.0

    stated = str((document.attributes or {}).get("colour") or "").lower()
    fallback = _haystack(document)

    for colour in wanted_colours:
        if (stated and _word_in(colour, stated)) or (not stated and _word_in(colour, fallback)):
            return 2.0

    for colour in wanted_colours:
        for relative in COLOUR_FAMILIES.get(colour, ()):
            if relative == colour:
                continue
            if (stated and _word_in(relative, stated)) or (
                not stated and _word_in(relative, fallback)
            ):
                return 1.0
    return 0.0


def score_product(document: KnowledgeDocument, wanted: dict[str, list[str]], query: str) -> float:
    """How well one product answers the request.

    Colour is a hard filter, not a weight: asking for red and being shown blue
    is the single most obvious way for this to look broken.
    """
    haystack = _haystack(document)

    if wanted["colours"]:
        colour_score = colour_match(document, wanted["colours"])
        if colour_score == 0.0:
            return 0.0
    else:
        colour_score = 0.0

    score = colour_score * 1.5
    score += 1.5 * sum(1 for fabric in wanted["fabrics"] if _word_in(fabric, haystack))
    score += 1.0 * sum(1 for style in wanted["styles"] if _word_in(style, haystack))
    score += 1.0 * sum(1 for piece in wanted["pieces"] if piece in haystack)

    # Any remaining meaningful word from the request is a weak signal.
    for word in set(re.findall(r"[a-z]{4,}", (query or "").lower())):
        if _word_in(word, haystack):
            score += 0.15

    # Prefer products that can actually be shown.
    if document.media_urls:
        score += 0.5
    return score


async def find_products(
    db: AsyncSession,
    organization_id: uuid.UUID,
    query: str,
    limit: int = 3,
) -> list[KnowledgeDocument]:
    """Best matching catalogue items for this organization, best first."""
    result = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.organization_id == organization_id,
            KnowledgeDocument.doc_type == "product",
        )
    )
    products = result.scalars().all()
    if not products:
        return []

    wanted = wanted_attributes(query)
    scored = [(score_product(p, wanted, query), p) for p in products]
    scored = [(s, p) for s, p in scored if s > 0]
    if not scored:
        return []

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [product for _, product in scored[:limit]]


def media_for(products: list[KnowledgeDocument], per_product: int = 1) -> list[str]:
    """One image per product by default — a wall of photos reads as spam."""
    urls: list[str] = []
    for product in products:
        for url in (product.media_urls or [])[:per_product]:
            if url and url not in urls:
                urls.append(url)
    return urls


def as_prompt_block(products: list[KnowledgeDocument]) -> str:
    """Describe the matches so the reply can name them and quote real prices."""
    if not products:
        return ""

    lines = ["=== MATCHING PRODUCTS (these photos are attached to your reply) ==="]
    for product in products:
        attributes = product.attributes or {}
        bits = [product.title]
        price = attributes.get("price")
        currency = attributes.get("currency", "PKR")
        if price:
            bits.append(f"{currency} {price}")
        for key in ("colour", "fabric"):
            if attributes.get(key):
                bits.append(f"{key}: {attributes[key]}")
        if attributes.get("product_url"):
            bits.append(attributes["product_url"])
        lines.append("- " + " | ".join(str(b) for b in bits if b))
    return "\n".join(lines)


def no_match_note(query: str, wanted: dict[str, list[str]]) -> str:
    """Told to the model when nothing matched, so it says so instead of inventing."""
    asked = ", ".join(wanted["colours"] + wanted["fabrics"] + wanted["styles"])
    detail = f" matching {asked}" if asked else ""
    return (
        "=== MATCHING PRODUCTS ===\n"
        # Deliberately industry-neutral: the same agent serves a clothing shop
        # and a B2B SaaS company, and "a different fabric" is nonsense to one.
        f"Nothing in the catalogue{detail} right now. Say so plainly, do not invent "
        "anything, and either offer the closest thing you genuinely do have or ask "
        "one question to narrow down what they need."
    )
