"""Catalogue and policy ingestion.

Nishat Linen runs on Shopify, which publishes a structured `products.json`
feed. That is used in preference to scraping HTML: it gives titles, prices,
images and variants directly, and is far less likely to break.

Only public catalogue data is read. robots.txt for the store explicitly allows
product and collection pages while prohibiting automated checkout — nothing
here goes near a cart.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "PingPulseBot/1.0 (sales-agent catalogue sync)"
PAGE_SIZE = 250

COLOUR_WORDS = (
    "red", "blue", "green", "black", "white", "pink", "yellow", "purple",
    "orange", "brown", "grey", "navy", "maroon", "beige", "cream", "gold",
    "silver", "teal", "olive", "peach", "lilac", "mustard", "rust", "ivory",
)


def strip_html(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def _field_from_description(description: str, label: str) -> str | None:
    """Pull 'Fabric: Latha' style labels out of the product description."""
    match = re.search(rf"{label}\s*:?\s*([A-Za-z0-9 &/\-]+)", description, re.IGNORECASE)
    return match.group(1).strip().strip(".") if match else None


def normalise_product(raw: dict[str, Any], domain: str) -> dict[str, Any]:
    """Turn one Shopify product into the shape the knowledge base stores."""
    description = strip_html(raw.get("body_html", ""))

    colour = _field_from_description(description, "Color") or _field_from_description(
        description, "Colour"
    )
    if not colour:
        blob = f"{raw.get('title', '')} {description}".lower()
        colour = next((c for c in COLOUR_WORDS if re.search(rf"\b{c}\b", blob)), None)

    variants = raw.get("variants") or []
    price = variants[0].get("price") if variants else None
    available = any(v.get("available") for v in variants) if variants else False

    images = [i.get("src") for i in (raw.get("images") or []) if i.get("src")]
    handle = raw.get("handle", "")

    attributes = {
        "colour": (colour or "").lower() or None,
        "fabric": _field_from_description(description, "Fabric"),
        "price": price,
        "currency": "PKR",
        "available": available,
        "collection": raw.get("vendor"),
        "product_type": raw.get("product_type"),
        "tags": raw.get("tags") or [],
        "product_url": f"{domain.rstrip('/')}/products/{handle}" if handle else None,
        "shopify_id": raw.get("id"),
    }

    # The searchable body: everything a customer might phrase their request in.
    content_bits = [
        raw.get("title", ""),
        description,
        f"Colour: {colour}" if colour else "",
        f"Price: PKR {price}" if price else "",
        f"Collection: {raw.get('vendor')}" if raw.get("vendor") else "",
        f"Type: {raw.get('product_type')}" if raw.get("product_type") else "",
        " ".join(raw.get("tags") or []),
    ]

    return {
        "title": raw.get("title", "Untitled product"),
        "content": "\n".join(bit for bit in content_bits if bit),
        "source": attributes["product_url"] or domain,
        "doc_type": "product",
        "media_urls": images[:3],
        "attributes": attributes,
    }


async def fetch_catalogue(
    domain: str, max_products: int = 120, pages: int | None = None
) -> list[dict[str, Any]]:
    """Read the public product feed. Returns [] rather than raising."""
    base = domain.rstrip("/")
    collected: list[dict[str, Any]] = []
    page = 1
    page_limit = pages or max(1, (max_products + PAGE_SIZE - 1) // PAGE_SIZE)

    async with httpx.AsyncClient(
        timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        while page <= page_limit and len(collected) < max_products:
            url = f"{base}/products.json?limit={PAGE_SIZE}&page={page}"
            try:
                response = await client.get(url)
                response.raise_for_status()
                batch = response.json().get("products", [])
            except Exception as exc:  # noqa: BLE001
                logger.error("catalogue fetch failed at page %d: %s", page, exc)
                break

            if not batch:
                break
            collected.extend(batch)
            page += 1
            # Be a considerate guest on someone else's storefront.
            await asyncio.sleep(0.5)

    logger.info("fetched %d raw products from %s", len(collected), base)
    return [normalise_product(item, base) for item in collected[:max_products]]


# --------------------------------------------------------------------------
# Operational policy facts
# --------------------------------------------------------------------------
def nishat_policy_documents() -> list[dict[str, Any]]:
    """The operational facts the agent must be able to answer from.

    Written as separate documents so retrieval can return just the relevant
    one, and phrased the way a customer would ask.
    """
    return [
        {
            "title": "Delivery time and areas",
            "doc_type": "policy",
            "content": (
                "Delivery takes 5 to 7 working days nationwide across Pakistan, including "
                "Lahore, Karachi, Islamabad, Rawalpindi, Faisalabad, Multan, Peshawar and "
                "Quetta. During mega sale events delivery can take up to 7 working days. "
                "Orders are dispatched from our warehouse and tracked by courier."
            ),
        },
        {
            "title": "Payment methods",
            "doc_type": "policy",
            "content": (
                "We accept Cash on Delivery (COD) anywhere in Pakistan, online payment by "
                "credit or debit card (Visa and Mastercard), and Tabby which lets you buy "
                "now and pay later in 4 interest-free installments. For orders above "
                "Rs. 15,000 an advance payment is required before dispatch."
            ),
        },
        {
            "title": "Advance payment rule",
            "doc_type": "policy",
            "content": (
                "Orders above Rs. 15,000 require advance payment and cannot be placed as "
                "Cash on Delivery. Orders of Rs. 15,000 or less can be paid on delivery."
            ),
        },
        {
            "title": "Returns and exchange policy",
            "doc_type": "policy",
            "content": (
                "Items can be returned or exchanged within 7 days of delivery, provided "
                "they are unused, unwashed and in their original packaging with tags "
                "attached. Stitched-to-order, altered and sale items are not returnable. "
                "Raise a return request through customer care and the courier will collect "
                "the parcel. Refunds are issued once the item passes inspection."
            ),
        },
        {
            "title": "Delivery charges",
            "doc_type": "policy",
            "content": (
                "A standard delivery charge applies per order and is shown at checkout "
                "before you confirm. Cash on Delivery orders may carry a small additional "
                "handling fee. Promotional free-delivery offers are announced on the site."
            ),
        },
        {
            "title": "Order tracking",
            "doc_type": "policy",
            "content": (
                "Once an order is dispatched a tracking number is sent by SMS and email. "
                "Orders can also be tracked from the account section on nishatlinen.com."
            ),
        },
    ]
