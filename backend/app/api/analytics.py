"""The analytics screen's one endpoint.

One call rather than five. Every panel on that screen is read at the same
moment by the same page, and five parallel authenticated requests on first
paint is the exact shape that turned out to be racing each other for a licence
seat. It is also a single consistent read: a funnel counted at one instant and
a traffic chart counted a second later can disagree, and the disagreement
always looks like a bug in the numbers.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import Tenant, current_org
from app.services import analytics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["analytics"])


@router.get("/analytics")
async def analytics_overview(
    window: str = Query(default="30d", description="today | 7d | 30d | 90d | all"),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Funnel, traffic, reply times and sources for one window.

    `window` is the common case and `since`/`until` the escape hatch for a
    span nobody anticipated. An unknown window name falls back to all time
    rather than erroring: the screen asking for it is ours, and a dashboard
    that refuses to draw because of a typo in a query string helps nobody.
    """
    # The zone first: "today" means the shop's today, not Greenwich's.
    zone = analytics.zone_for(getattr(tenant.organization, "timezone", None))
    start = analytics.window_start(window, since, zone)
    if start is not None and until is not None and until < start:
        raise HTTPException(
            status_code=422, detail="The end of that range comes before the start"
        )

    return await analytics.overview(
        db, tenant.organization, start=start, until=until, window=window
    )
