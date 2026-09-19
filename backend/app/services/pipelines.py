"""Each organization's own board.

The board was a constant, which is right until the second industry arrives:
"Estimate sent" carries a whole workflow for a contractor and means nothing to
a salon. So the columns are rows now, one set per organization, reorderable and
renamable.

Two properties keep that from becoming a source of broken data.

*Keys are stable and labels are not.* A contact stores `ESTIMATE_SENT`, and the
screen shows whatever that organization calls it. Renaming a column must not
orphan everybody standing in it, and translating a board must not change what
the analytics count — which is why `outcome` exists rather than the counting
code looking for a column called "Won".

*There is always a board.* An organization with no rows yet — one created in
the moments before seeding, or the whole test suite, which builds its schema
with create_all and runs no migrations — falls back to the defaults rather than
returning nothing. A dashboard with no columns is indistinguishable from a
broken one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.models import DEFAULT_PIPELINE, CRMContact, TenantPipeline

logger = logging.getLogger(__name__)


@dataclass
class Stage:
    """One column, whether it came from the database or the defaults."""

    key: str
    label: str
    order_index: int
    colour: str
    outcome: str | None
    is_entry: bool

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "order_index": self.order_index,
            "colour": self.colour,
            "outcome": self.outcome,
            "is_entry": self.is_entry,
        }


def default_stages() -> list[Stage]:
    return [
        Stage(
            key=key,
            label=label,
            order_index=index,
            colour=colour,
            outcome=outcome,
            is_entry=index == 0,
        )
        for index, (key, label, colour, outcome) in enumerate(DEFAULT_PIPELINE)
    ]


async def stages_for(db, organization_id) -> list[Stage]:
    """This organization's board, in order, falling back to the defaults."""
    rows = (
        await db.execute(
            select(TenantPipeline)
            .where(TenantPipeline.organization_id == organization_id)
            .order_by(TenantPipeline.order_index)
        )
    ).scalars().all()

    if not rows:
        return default_stages()

    return [
        Stage(
            key=row.key,
            label=row.label,
            order_index=row.order_index,
            colour=row.colour,
            outcome=row.outcome,
            is_entry=row.is_entry,
        )
        for row in rows
    ]


async def seed(db, organization_id) -> list[TenantPipeline]:
    """Give a new organization the default board. Safe to call twice."""
    existing = (
        await db.execute(
            select(TenantPipeline).where(TenantPipeline.organization_id == organization_id)
        )
    ).scalars().all()
    if existing:
        return existing

    created: list[TenantPipeline] = []
    for index, (key, label, colour, outcome) in enumerate(DEFAULT_PIPELINE):
        row = TenantPipeline(
            organization_id=organization_id,
            key=key,
            label=label,
            order_index=index,
            colour=colour,
            outcome=outcome,
            is_entry=index == 0,
        )
        db.add(row)
        created.append(row)
    await db.flush()
    logger.info("seeded the default board for organization %s", organization_id)
    return created


async def entry_stage(db, organization_id) -> str:
    """Where a brand-new contact lands on this organization's board."""
    stages = await stages_for(db, organization_id)
    for stage in stages:
        if stage.is_entry:
            return stage.key
    return stages[0].key if stages else DEFAULT_PIPELINE[0][0]


async def reorder(db, organization_id, keys: list[str]) -> list[Stage]:
    """Put the board in the given order.

    Only stages named in `keys` move; anything left out keeps its place at the
    end rather than being deleted, because a reorder request that silently
    dropped a column would take every contact in it out of sight.
    """
    rows = {
        row.key: row
        for row in (
            await db.execute(
                select(TenantPipeline).where(TenantPipeline.organization_id == organization_id)
            )
        ).scalars().all()
    }

    position = 0
    for key in keys:
        row = rows.pop(key, None)
        if row is not None:
            row.order_index = position
            position += 1
    for leftover in sorted(rows.values(), key=lambda r: r.order_index):
        leftover.order_index = position
        position += 1

    await db.flush()
    return await stages_for(db, organization_id)


async def contacts_in_use(db, organization_id, key: str) -> int:
    """How many contacts stand in this column — asked before deleting one."""
    from sqlalchemy import func

    return (
        await db.scalar(
            select(func.count(CRMContact.id)).where(
                CRMContact.organization_id == organization_id,
                CRMContact.pipeline_stage == key,
            )
        )
    ) or 0


async def stage_with_outcome(db, organization_id, outcome: str) -> str | None:
    """The key of this board's column meaning `outcome`, or None.

    A tenant may rename "Estimate scheduled" to "Booked in" or "Site visit
    agreed", so the column that means an appointment exists cannot be found by
    its label or by a hardcoded key. `outcome` is the stable answer to what a
    column means, which is exactly what is needed when a confirmed booking has
    to move a lead onto a board nobody here designed.

    None when the board has no such column: a tenant who deleted it has said
    they do not track that, and inventing the column back would put contacts
    somewhere they cannot see.
    """
    for stage in await stages_for(db, organization_id):
        if stage.outcome == outcome:
            return stage.key
    return None
