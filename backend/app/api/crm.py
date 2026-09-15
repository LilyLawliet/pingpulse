"""CRM: leads for the active organization only.

Every statement here carries `organization_id == tenant.id`. A contact id
belonging to another tenant returns 404, not 403 — existence itself is not
disclosed across a tenant boundary.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import STAGE_OPERATOR, SENDER_CUSTOMER, CRMContact, Message
from app.schemas import MessageOut
from app.services import analytics, pipelines
from app.schemas_tenancy import (
    CRMContactCreate,
    CRMContactOut,
    CRMContactUpdate,
    CRMSummary,
    TagRequest,
)

router = APIRouter(prefix="/api/v1/crm", tags=["crm"])


async def _get_contact(
    db: AsyncSession, tenant: Tenant, contact_id: uuid.UUID
) -> CRMContact:
    """Fetch a contact *within this tenant*, or 404.

    The organization filter is part of the lookup rather than a check
    afterwards, so there is no window in which another tenant's row is loaded.
    """
    result = await db.execute(
        select(CRMContact).where(
            CRMContact.id == contact_id,
            CRMContact.organization_id == tenant.id,
        )
    )
    contact = result.scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
    return contact


async def _valid_stage(db: AsyncSession, organization_id, key: str | None) -> None:
    """Refuse a stage this organization does not have.

    This used to be a Literal on the schema, which stopped working the moment
    boards became per-tenant: the type cannot know that this shop renamed its
    columns. Checked here instead, where the organization is known.
    """
    if key is None:
        return
    stages = await pipelines.stages_for(db, organization_id)
    if key.upper() not in {stage.key for stage in stages}:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{key}' is not a stage on this board. It has: "
                + ", ".join(stage.key for stage in stages)
            ),
        )


@router.get("/contacts", response_model=list[CRMContactOut])
async def list_contacts(
    stage: str | None = Query(default=None, description="Pipeline stage key"),
    tag: str | None = Query(default=None, description="Only contacts carrying this tag"),
    search: str | None = Query(default=None, description="Match name, phone, company or notes"),
    unread_only: bool = Query(default=False, description="Only threads nobody has opened since"),
    assigned_to: uuid.UUID | None = Query(default=None),
    since: datetime | None = Query(default=None, description="First seen on or after"),
    until: datetime | None = Query(default=None, description="First seen on or before"),
    taken_over: bool | None = Query(
        default=None, description="True for conversations a person has claimed"
    ),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """The inbox, filtered.

    Every filter is a WHERE clause on a query already pinned to one
    organization, so narrowing can only ever shrink a tenant's own rows.
    """
    query = select(CRMContact).where(CRMContact.organization_id == tenant.id)

    if stage:
        query = query.where(CRMContact.pipeline_stage == stage.upper())
    if assigned_to:
        query = query.where(CRMContact.assigned_to == assigned_to)
    if taken_over is not None:
        query = query.where(CRMContact.ai_enabled.is_(not taken_over))
    if since:
        query = query.where(CRMContact.created_at >= since)
    if until:
        query = query.where(CRMContact.created_at <= until)
    if search:
        # Widened past name and phone: an operator hunting for a lead searches
        # for the company or something they wrote in the notes just as often.
        pattern = f"%{search.strip()}%"
        query = query.where(
            CRMContact.name.ilike(pattern)
            | CRMContact.phone_number.ilike(pattern)
            | CRMContact.company.ilike(pattern)
            | CRMContact.notes.ilike(pattern)
        )

    query = query.order_by(CRMContact.created_at.desc()).limit(limit).offset(offset)
    contacts = (await db.execute(query)).scalars().all()

    # Tags live in a JSON column, which PostgreSQL and SQLite filter very
    # differently — doing it here keeps one behaviour on both.
    if tag:
        wanted = tag.strip().lower()
        contacts = [c for c in contacts if wanted in {t.lower() for t in (c.tags or [])}]

    if unread_only:
        # Derived from the last customer message rather than a stored flag: a
        # flag and the messages it describes drift apart the first time
        # anything writes one without the other.
        contacts = [c for c in contacts if await _is_unread(db, c)]

    return contacts


async def _is_unread(db: AsyncSession, contact) -> bool:
    """Has a customer message arrived since anybody last opened this thread?"""
    latest = await db.scalar(
        select(func.max(Message.created_at)).where(
            Message.contact_id == contact.id,
            Message.sender == SENDER_CUSTOMER,
        )
    )
    if latest is None:
        return False
    if contact.last_read_at is None:
        return True
    seen = contact.last_read_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest > seen


@router.post("/contacts", response_model=CRMContactOut, status_code=201)
async def create_contact(
    payload: CRMContactCreate,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(WRITE_ROLES)

    existing = await db.execute(
        select(CRMContact).where(
            CRMContact.organization_id == tenant.id,
            CRMContact.phone_number == payload.phone_number,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That number is already a contact in this organization",
        )

    fields = payload.model_dump()
    await _valid_stage(db, tenant.id, fields.get("pipeline_stage"))
    if fields.get("pipeline_stage"):
        fields["pipeline_stage"] = fields["pipeline_stage"].upper()

    contact = CRMContact(organization_id=tenant.id, **fields)
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    return contact


@router.get("/contacts/summary", response_model=CRMSummary)
async def contacts_summary(
    tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)
):
    stage_rows = await db.execute(
        select(CRMContact.pipeline_stage, func.count(CRMContact.id))
        .where(CRMContact.organization_id == tenant.id)
        .group_by(CRMContact.pipeline_stage)
    )
    by_stage = {stage: count for stage, count in stage_rows.all()}

    tag_rows = await db.execute(
        select(CRMContact.tags).where(CRMContact.organization_id == tenant.id)
    )
    counter: Counter[str] = Counter()
    for (tags,) in tag_rows.all():
        counter.update(tags or [])

    return CRMSummary(
        total=sum(by_stage.values()), by_stage=by_stage, by_tag=dict(counter)
    )


@router.get("/contacts/{contact_id}", response_model=CRMContactOut)
async def get_contact(
    contact_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    return await _get_contact(db, tenant, contact_id)


@router.patch("/contacts/{contact_id}", response_model=CRMContactOut)
async def update_contact(
    contact_id: uuid.UUID,
    payload: CRMContactUpdate,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(WRITE_ROLES)
    contact = await _get_contact(db, tenant, contact_id)

    changed = payload.model_dump(exclude_unset=True)
    await _valid_stage(db, tenant.id, changed.get("pipeline_stage"))
    if "pipeline_stage" in changed and changed["pipeline_stage"]:
        changed["pipeline_stage"] = changed["pipeline_stage"].upper()

    was = contact.pipeline_stage
    for field, value in changed.items():
        setattr(contact, field, value)
    await db.flush()

    # Only a real move. A PATCH that names the stage it is already on is not a
    # transition, and recording one would put a step in the history that
    # nobody took.
    if contact.pipeline_stage != was:
        await analytics.record_move(
            db, contact, contact.pipeline_stage, from_stage=was, source=STAGE_OPERATOR
        )

    await db.refresh(contact)
    return contact


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(WRITE_ROLES)
    contact = await _get_contact(db, tenant, contact_id)
    await db.delete(contact)
    return None


@router.post("/contacts/{contact_id}/tags", response_model=CRMContactOut)
async def add_tags(
    contact_id: uuid.UUID,
    payload: TagRequest,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Add tags, case-insensitively deduplicated, order preserved."""
    tenant.require_role(WRITE_ROLES)
    contact = await _get_contact(db, tenant, contact_id)

    merged = list(contact.tags or [])
    seen = {tag.lower() for tag in merged}
    for tag in payload.tags:
        clean = tag.strip()
        if clean and clean.lower() not in seen:
            merged.append(clean)
            seen.add(clean.lower())

    # Reassign rather than mutate: SQLAlchemy does not track in-place JSON edits.
    contact.tags = merged
    await db.flush()
    await db.refresh(contact)
    return contact


@router.delete("/contacts/{contact_id}/tags/{tag}", response_model=CRMContactOut)
async def remove_tag(
    contact_id: uuid.UUID,
    tag: str,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(WRITE_ROLES)
    contact = await _get_contact(db, tenant, contact_id)
    contact.tags = [t for t in (contact.tags or []) if t.lower() != tag.strip().lower()]
    await db.flush()
    await db.refresh(contact)
    return contact


@router.get("/contacts/{contact_id}/messages", response_model=list[MessageOut])
async def contact_messages(
    contact_id: uuid.UUID,
    limit: int = Query(default=200, le=500),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    # Resolving the contact within the tenant first is what keeps this
    # transcript from being readable across organizations.
    contact = await _get_contact(db, tenant, contact_id)
    result = await db.execute(
        select(Message)
        .where(
            Message.contact_id == contact.id,
            Message.organization_id == tenant.id,
        )
        .order_by(Message.created_at)
        .limit(limit)
    )
    return result.scalars().all()
