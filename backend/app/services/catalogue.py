"""Reading a shop's WhatsApp Business catalogue, so they need not retype it.

A WhatsApp Business account can carry a product catalogue, and a shop that has
one has already done the work we would otherwise ask them to repeat by uploading
a price list. Where it exists, this is the shortest path from "connected" to
"the agent can quote".

Two things learned by trying it against a live session, both of which shape this:

*WhatsApp does not answer "no".* Asking a personal account for its catalogue
does not return empty — it returns nothing at all, until Baileys' own query
timeout fires about two minutes later. The bridge bounds the wait and reports
silence as absence, so the dashboard answers in seconds either way.

*The price is a number of unknown scale.* WhatsApp reports catalogue prices as
an integer, and whether that is 8900 for $89.00 or 89000 depends on conventions
we cannot confirm without a real catalogue in front of us. Guessing is how an
agent ends up quoting a hundredth of the real price with total confidence. So
nothing is imported unseen: a preview shows exactly what would be written, a
person confirms it once, and the scale is chosen from what they saw.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# How the integer WhatsApp reports maps onto real money. "cents" covers the
# common case and "whole" the shops whose catalogue is already in units.
SCALES = {"cents": 100, "thousandths": 1000, "whole": 1}
DEFAULT_SCALE = "cents"


@dataclass
class Catalogue:
    """What the paired account had, as the bridge reported it."""

    business: bool = False
    profile: dict | None = None
    products: list[dict] = field(default_factory=list)
    truncated: bool = False
    reachable: bool = True
    error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.products)


async def read(channel) -> Catalogue:
    """Ask the bridge what this paired account's catalogue holds. Read-only."""
    session_id = getattr(channel, "id", None)
    if session_id is None:
        return Catalogue(reachable=False, error="no channel")

    url = f"{settings.wa_qr_service_url.rstrip('/')}/catalog/{session_id}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url, headers={"X-PingPulse-Bridge": settings.wa_qr_shared_secret}
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001 - the bridge being down is not an error here
        logger.info("could not read the catalogue: %s", exc)
        return Catalogue(reachable=False, error=str(exc)[:200])

    if not body.get("ok"):
        return Catalogue(reachable=False, error=body.get("error") or "unavailable")

    return Catalogue(
        business=bool(body.get("business")),
        profile=body.get("profile"),
        products=list(body.get("products") or []),
        truncated=bool(body.get("truncated")),
    )


def money(raw, currency: str, scale: str = DEFAULT_SCALE) -> str:
    """Render WhatsApp's integer price as something a customer would recognise."""
    divisor = SCALES.get(scale, SCALES[DEFAULT_SCALE])
    try:
        amount = float(raw) / divisor
    except (TypeError, ValueError):
        return ""
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return f"{currency} {text}".strip()


def as_passage(product: dict, scale: str = DEFAULT_SCALE) -> str:
    """One product as a line the agent can quote from.

    Name, price and availability stay on one line for the same reason a table
    row does: the binding between a product and its price is the thing worth
    preserving, and anything that separates them invites the agent to pair the
    right price with the wrong item.
    """
    price = money(product.get("price"), product.get("currency") or "", scale)
    headline = " — ".join(part for part in (product.get("name"), price) if part)

    details = []
    if product.get("availability"):
        details.append(str(product["availability"]))
    if product.get("retailerId"):
        details.append(f"SKU {product['retailerId']}")

    lines = [headline + (f" ({', '.join(details)})" if details else "")]
    if product.get("description"):
        lines.append(str(product["description"]).strip())
    return "\n".join(lines)


def preview(catalogue: Catalogue, scale: str = DEFAULT_SCALE) -> list[dict]:
    """What would be written, for a person to check before anything is."""
    return [
        {
            "name": product.get("name") or "",
            "price": money(product.get("price"), product.get("currency") or "", scale),
            "raw_price": product.get("price"),
            "currency": product.get("currency") or "",
            "passage": as_passage(product, scale),
            "images": product.get("images") or [],
        }
        for product in catalogue.products
    ]
