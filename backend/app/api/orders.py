"""The orders customers placed in the chat, for the shop to work through.

An order arrives as "placed": the customer said yes to its summary. From there
a person moves it along - confirmed, dispatched, delivered, or cancelled - and
marks it paid. Each change can be told to the customer on WhatsApp, in a
message rendered from the row, so it can only ever say what the record says.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.appointments import _changed, _tell
from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import (
    ORDER_CANCELLED,
    ORDER_CONFIRMED,
    ORDER_DELIVERED,
    ORDER_DISPATCHED,
    ORDER_STATUSES,
    PAYMENT_PAID,
    PAYMENT_UNPAID,
    Order,
)
from app.services import offers

router = APIRouter(prefix="/api/v1", tags=["orders"])

# What the customer is told when an order moves on. Figures come from the row.
_TOLD = {
    ORDER_CONFIRMED: "Your order #{number} is confirmed. Total: {total}.",
    ORDER_DISPATCHED: "Your order #{number} is on its way!",
    ORDER_DELIVERED: "Your order #{number} has been delivered. Thank you for shopping with us!",
    ORDER_CANCELLED: "Your order #{number} has been cancelled.",
}
_PAID = "We've received your payment of {total} for order #{number}. Thank you!"


class OrderChange(BaseModel):
    status: str | None = None
    payment_status: str | None = None
    tell_customer: bool = False


def _row(order: Order) -> dict:
    contact = order.contact
    return {
        "id": str(order.id),
        "number": order.number,
        "status": order.status,
        "payment_status": order.payment_status,
        "payment_method": order.payment_method,
        "lines": order.lines,
        "currency": order.currency,
        "goods_total": str(order.goods_total),
        "discount": str(order.discount) if order.discount is not None else None,
        "delivery_fee": str(order.delivery_fee) if order.delivery_fee is not None else None,
        "total": str(order.total),
        "total_text": offers.money(order.total, order.currency),
        "delivery_place": order.delivery_place,
        "address": order.address,
        "customer_note": order.customer_note,
        "source": order.source,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        "contact": {
            "id": str(contact.id),
            "name": contact.name,
            "phone_number": contact.phone_number,
        }
        if contact is not None
        else None,
    }


async def _order(db, tenant: Tenant, order_id: uuid.UUID) -> Order:
    found = (
        await db.execute(
            select(Order)
            .options(selectinload(Order.contact))
            .where(Order.id == order_id, Order.organization_id == tenant.id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return found


@router.get("/orders")
async def list_orders(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Order)
        .options(selectinload(Order.contact))
        .where(Order.organization_id == tenant.id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if status:
        query = query.where(Order.status == status)
    rows = (await db.execute(query)).scalars().all()
    return {"orders": [_row(row) for row in rows]}


@router.patch("/orders/{order_id}")
async def change_order(
    order_id: uuid.UUID,
    payload: OrderChange,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Move an order on, or mark it paid, and optionally tell the customer."""
    tenant.require_role(WRITE_ROLES)
    order = await _order(db, tenant, order_id)
    told_lines: list[str] = []
    fields = {"number": order.number, "total": offers.money(order.total, order.currency)}

    if payload.status is not None and payload.status != order.status:
        if payload.status not in ORDER_STATUSES:
            raise HTTPException(status_code=422, detail="That is not an order status.")
        if order.status == ORDER_CANCELLED:
            raise HTTPException(status_code=409, detail="A cancelled order can't be moved on.")
        order.status = payload.status
        if payload.status == ORDER_CANCELLED:
            order.cancelled_at = datetime.now(timezone.utc)
        if payload.status in _TOLD:
            told_lines.append(_TOLD[payload.status].format(**fields))
    if payload.payment_status is not None and payload.payment_status != order.payment_status:
        if payload.payment_status not in (PAYMENT_PAID, PAYMENT_UNPAID):
            raise HTTPException(status_code=422, detail="Paid or unpaid.")
        order.payment_status = payload.payment_status
        if payload.payment_status == PAYMENT_PAID:
            told_lines.append(_PAID.format(**fields))

    order.updated_at = datetime.now(timezone.utc)
    told = None
    if payload.tell_customer and told_lines and order.contact is not None:
        told = await _tell(db, tenant, order.contact, "\n".join(told_lines))
    await db.commit()
    order = await _order(db, tenant, order_id)
    await _changed(db, tenant, order.contact_id)
    return {"order": _row(order), "told": told}
