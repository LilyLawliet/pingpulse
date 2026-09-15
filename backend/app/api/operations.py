"""The board, human takeover, and the two logs.

Grouped because they are what an operator reaches for when something needs
changing or explaining, as opposed to the conversation itself.

Every write here is audited. These are the settings that decide how the agent
treats customers, and "who turned the agent off on Tuesday" is a question that
gets asked in exactly the situations where nobody can remember.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import PIPELINE_OUTCOMES, CRMContact, Organization, SystemError, TenantPipeline
from app.services import agent_config, oplog, pipelines, ws_manager
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["operations"])


# ------------------------------------------------------------------- board
@router.get("/pipeline")
async def get_pipeline(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """This organization's board, in order.

    Always returns a board. An organization with no rows of its own gets the
    defaults, because a dashboard with no columns looks broken rather than
    unconfigured.
    """
    stages = await pipelines.stages_for(db, tenant.id)
    return {"stages": [stage.as_dict() for stage in stages]}


@router.put("/pipeline")
async def replace_pipeline(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Set this organization's columns.

    Refuses to remove a column that still has people standing in it. Deleting
    it would leave those contacts pointing at a stage that no longer exists —
    they would not appear on any column of the board, which reads as having
    lost them.
    """
    tenant.require_role(WRITE_ROLES)

    wanted = payload.get("stages")
    if not isinstance(wanted, list) or not wanted:
        raise HTTPException(status_code=422, detail="A board needs at least one stage")

    cleaned: list[dict] = []
    seen: set[str] = set()
    for index, entry in enumerate(wanted):
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip().upper().replace(" ", "_")[:40]
        label = str(entry.get("label") or "").strip()[:60]
        if not key or not label or key in seen:
            continue
        outcome = entry.get("outcome") or None
        if outcome is not None and outcome not in PIPELINE_OUTCOMES:
            raise HTTPException(
                status_code=422,
                detail=f"An outcome must be one of {', '.join(PIPELINE_OUTCOMES)}",
            )
        seen.add(key)
        cleaned.append(
            {
                "key": key,
                "label": label,
                "colour": str(entry.get("colour") or "slate")[:16],
                "outcome": outcome,
                "order_index": index,
                "is_entry": bool(entry.get("is_entry")) or index == 0,
            }
        )

    if not cleaned:
        raise HTTPException(status_code=422, detail="No usable stages were given")

    existing = {
        row.key: row
        for row in (
            await db.execute(
                select(TenantPipeline).where(TenantPipeline.organization_id == tenant.id)
            )
        ).scalars().all()
    }

    # Refuse before writing anything, so a rejected request changes nothing.
    for key, row in existing.items():
        if key not in seen:
            in_use = await pipelines.contacts_in_use(db, tenant.id, key)
            if in_use:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{in_use} contact(s) are still in '{row.label}'. Move them to "
                        "another stage before removing it."
                    ),
                )

    before = [
        {"key": row.key, "label": row.label, "order_index": row.order_index}
        for row in sorted(existing.values(), key=lambda r: r.order_index)
    ]

    entry_seen = False
    for spec in cleaned:
        # Exactly one entry column, or a new contact has nowhere defined to go.
        if spec["is_entry"] and entry_seen:
            spec["is_entry"] = False
        entry_seen = entry_seen or spec["is_entry"]

        row = existing.pop(spec["key"], None)
        if row is None:
            row = TenantPipeline(organization_id=tenant.id, key=spec["key"])
            db.add(row)
        row.label = spec["label"]
        row.colour = spec["colour"]
        row.outcome = spec["outcome"]
        row.order_index = spec["order_index"]
        row.is_entry = spec["is_entry"]

    for orphan in existing.values():
        await db.delete(orphan)

    await db.flush()
    stages = await pipelines.stages_for(db, tenant.id)
    await oplog.record(
        db,
        tenant.id,
        "pipeline.replace",
        user_id=getattr(tenant.user, "id", None),
        resource_type="pipeline",
        changes={"before": before, "after": [s.as_dict() for s in stages]},
    )
    return {"stages": [stage.as_dict() for stage in stages]}


# ------------------------------------------------------------ agent config
@router.get("/agent-config")
async def get_agent_config(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """How this business wants its agent to behave."""
    organization = await db.get(Organization, tenant.id)
    return {
        "agent_config": organization.agent_config or {},
        "timezone": organization.timezone,
        "open_now": agent_config.is_open(organization),
        "days": list(agent_config.DAYS),
    }


@router.put("/agent-config")
async def save_agent_config(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Set the operating rules. Everything is optional.

    An empty config is a valid one and means the agent behaves as it did before
    any of this existed, which is what makes it safe to leave alone.
    """
    tenant.require_role(WRITE_ROLES)

    organization = await db.get(Organization, tenant.id)
    before = dict(organization.agent_config or {})

    config = payload.get("agent_config")
    if config is not None and not isinstance(config, dict):
        raise HTTPException(status_code=422, detail="agent_config must be an object")

    zone = payload.get("timezone")
    if zone is not None:
        # Validated now rather than discovered at reply time, where a bad zone
        # would have the agent apologising about the wrong opening hours.
        try:
            ZoneInfo(str(zone))
        except Exception:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail=f"'{zone}' is not a timezone. Use an IANA name like Asia/Dubai.",
            )
        organization.timezone = str(zone)[:64]

    if config is not None:
        organization.agent_config = config
    await db.flush()

    await oplog.record(
        db,
        tenant.id,
        "agent_config.update",
        user_id=getattr(tenant.user, "id", None),
        resource_type="organization",
        resource_id=tenant.id,
        changes=oplog.changes_between(before, organization.agent_config or {}),
    )
    return {
        "agent_config": organization.agent_config,
        "timezone": organization.timezone,
        "open_now": agent_config.is_open(organization),
    }


# ---------------------------------------------------------------- takeover
@router.post("/contacts/{contact_id}/takeover")
async def set_takeover(
    contact_id: uuid.UUID,
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Stop or resume the agent on one conversation.

    `{"ai_enabled": false}` hands the conversation to a person: messages keep
    arriving and are still shown, and nothing is generated for them. It takes
    effect on the next inbound message, which is checked before any reply is
    composed rather than after.
    """
    tenant.require_role(WRITE_ROLES)

    contact = (
        await db.execute(
            select(CRMContact).where(
                CRMContact.id == contact_id,
                CRMContact.organization_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    # 404 rather than 403 for another tenant's contact: whether a row exists is
    # itself information.
    if contact is None:
        raise HTTPException(status_code=404, detail="No such contact")

    was = contact.ai_enabled
    contact.ai_enabled = bool(payload.get("ai_enabled", False))
    await db.flush()

    await oplog.record(
        db,
        tenant.id,
        "contact.takeover" if not contact.ai_enabled else "contact.handback",
        user_id=getattr(tenant.user, "id", None),
        resource_type="contact",
        resource_id=contact.id,
        changes={"ai_enabled": {"from": was, "to": contact.ai_enabled}},
    )
    await manager.broadcast(
        ws_manager.EVENT_SYNC,
        {"contact_id": str(contact.id), "organization_id": str(tenant.id)},
    )
    return {"contact_id": str(contact.id), "ai_enabled": contact.ai_enabled}


@router.post("/contacts/{contact_id}/read")
async def mark_read(
    contact_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Note that somebody has opened this thread, for the unread filter."""
    contact = (
        await db.execute(
            select(CRMContact).where(
                CRMContact.id == contact_id,
                CRMContact.organization_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="No such contact")

    contact.last_read_at = datetime.now(timezone.utc)
    await db.flush()
    return {"contact_id": str(contact.id), "last_read_at": contact.last_read_at}


# -------------------------------------------------------------------- logs
@router.get("/errors")
async def list_errors(
    category: str | None = None,
    days: int = Query(default=7, ge=1, le=90),
    include_resolved: bool = False,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Operational failures this organization should know about."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = select(SystemError).where(
        SystemError.organization_id == tenant.id,
        SystemError.created_at >= since,
    )
    if category:
        query = query.where(SystemError.category == category)
    if not include_resolved:
        query = query.where(SystemError.resolved_at.is_(None))

    rows = (
        await db.execute(query.order_by(SystemError.created_at.desc()).limit(200))
    ).scalars().all()

    return {
        "errors": [
            {
                "id": str(row.id),
                "category": row.category,
                "message": row.message,
                "detail": row.detail,
                "created_at": row.created_at,
                "resolved_at": row.resolved_at,
            }
            for row in rows
        ]
    }


@router.post("/errors/{error_id}/resolve", status_code=200)
async def resolve_error(
    error_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(WRITE_ROLES)

    row = (
        await db.execute(
            select(SystemError).where(
                SystemError.id == error_id,
                SystemError.organization_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such error")

    row.resolved_at = datetime.now(timezone.utc)
    await db.flush()
    return {"id": str(row.id), "resolved_at": row.resolved_at}
