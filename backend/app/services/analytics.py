"""What actually happened, counted.

The dashboard could always say where leads are standing right now. It could
never say anything about the journey: how many got as far as qualified, where
people stop answering, whether replies go out in seconds or hours, how much of
the talking the agent is doing. Those are the questions a shop asks when
deciding whether this thing is worth paying for, and none of them can be
answered by a column holding a current stage.

Three decisions are worth knowing about before reading the code.

*The funnel is a cohort, not a snapshot.* "Thirty days" means the leads that
arrived in the last thirty days and how far each one got - not the moves that
happened in the last thirty days. The second is a different question that
looks like the first, and answering it under the first question's label is how
a chart ends up lying.

*Depth is the furthest rung reached, and lost is not a rung.* A lead marked
lost did not travel past one marked qualified; it stopped. Counting exits as
depth would make the funnel widen at the bottom, which is the one shape a
funnel cannot have. So exits come out of the ladder and are reported beside it.

*Nothing was backfilled.* `stage_events` starts empty for every contact that
existed before it did. Inventing dates for those journeys would put movement
on a chart that never happened, so instead the funnel reads a contact's
current stage alongside its events: an untracked lead still counts where it
stands, and only the timing of how it got there is unknown rather than wrong.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models import (
    SENDER_CUSTOMER,
    SENDER_OPERATOR,
    STAGE_AGENT,
    CRMContact,
    Message,
    StageEvent,
)
from app.services import pipelines

logger = logging.getLogger(__name__)

# Off-ramps rather than rungs. See the module docstring.
EXIT_OUTCOMES = ("lost", "unqualified")

# Above this many rows in one window the per-message passes stop being a
# dashboard query. Nothing near it is reachable at current volumes; it is here
# so that a tenant who imports five years of history gets a slow answer rather
# than an unresponsive process.
MAX_MESSAGE_ROWS = 200_000

# Where the chart stops being readable and starts being a smear. A span wider
# than this is bucketed by week instead of by day.
MAX_DAILY_BUCKETS = 120

# Guards the bucket walk below against a span nobody intended.
MAX_BUCKETS = 400


# The spans the dashboard offers. A window rather than two dates for the common
# case, because "this week" is what somebody actually asks, and computing it in
# the browser means two clients disagreeing about when a week starts.
WINDOWS = {"today": 1, "7d": 7, "30d": 30, "90d": 90}


def window_start(
    window: str | None, since: datetime | None = None, zone: ZoneInfo | None = None
) -> datetime | None:
    """Where the counting starts, or None for all time.

    "Today" is the only one of these that needs the tenant's timezone, and it
    needs it badly. A shop in Dubai opening the dashboard at nine in the
    morning was being shown a window that began at four the previous
    afternoon, because midnight was taken in UTC - so "today" quietly included
    most of yesterday evening, which is when a lot of them are busiest.

    The rolling windows are spans rather than calendar boundaries, so seven
    days is seven days wherever you are.
    """
    if since is not None:
        return since
    if not window or window == "all":
        return None
    name = window.lower()
    days = WINDOWS.get(name)
    if days is None:
        return None
    if name == "today":
        local = datetime.now(zone or timezone.utc)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(timezone.utc)
    return datetime.now(timezone.utc) - timedelta(days=days)


# ---------------------------------------------------------------- utilities
def _aware(moment: datetime | None) -> datetime | None:
    """Timestamps come back naive from SQLite and aware from Postgres.

    Both mean UTC. Comparing one of each raises, so everything is made aware
    on the way in rather than at each of the dozen places they meet.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def zone_for(name: str | None) -> ZoneInfo:
    """The tenant's timezone, falling back to UTC rather than raising.

    A shop that typed something not quite right into the timezone box should
    get days that start at the wrong hour, not a dashboard that will not load.
    """
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - any bad key lands on UTC
        return ZoneInfo("UTC")


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank, and None for an empty set.

    Medians rather than means throughout. One conversation answered the next
    morning drags a mean past every number around it, and the resulting figure
    describes no reply that was ever actually sent.
    """
    if not values:
        return None
    ordered = sorted(values)
    position = int(round(fraction * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, position))]


def _grain(start: datetime | None, until: datetime) -> str:
    """How wide each bar should be, given how much time is on screen."""
    if start is None:
        return "day"
    span = until - start
    if span <= timedelta(days=2):
        return "hour"
    if span <= timedelta(days=MAX_DAILY_BUCKETS):
        return "day"
    return "week"


def _bucket_of(moment: datetime, zone: ZoneInfo, grain: str) -> str:
    """Which bar a timestamp belongs in, in the shop's own local time.

    A shop in Dubai closing at 9pm wants that message on Tuesday, not on
    Wednesday because UTC had already rolled over.
    """
    local = _aware(moment).astimezone(zone)
    if grain == "hour":
        return local.strftime("%Y-%m-%dT%H:00")
    if grain == "week":
        return (local.date() - timedelta(days=local.weekday())).isoformat()
    return local.date().isoformat()


def _bucket_range(first: datetime, last: datetime, zone: ZoneInfo, grain: str) -> list[str]:
    """Every bar between two moments, including the empty ones.

    A chart that skips quiet days draws a busy Friday next to a busy Monday
    and makes the weekend disappear, which reads as steady traffic. The gaps
    are the information.
    """
    step = {"hour": timedelta(hours=1), "week": timedelta(days=7)}.get(grain, timedelta(days=1))
    keys: list[str] = []
    seen: set[str] = set()
    cursor = _aware(first)
    last = _aware(last)
    while cursor <= last and len(keys) < MAX_BUCKETS:
        key = _bucket_of(cursor, zone, grain)
        if key not in seen:
            seen.add(key)
            keys.append(key)
        cursor += step
    # The walk steps in fixed increments, so the final partial bucket can fall
    # just past `last` and be missed.
    tail = _bucket_of(last, zone, grain)
    if tail not in seen and len(keys) < MAX_BUCKETS:
        keys.append(tail)
    return keys


def _window(query, column, start: datetime | None, until: datetime | None):
    if start is not None:
        query = query.where(column >= start)
    if until is not None:
        query = query.where(column <= until)
    return query


# ------------------------------------------------------------------ writing
async def record_move(
    db,
    contact,
    to_stage: str,
    *,
    from_stage: str | None = None,
    source: str = STAGE_AGENT,
) -> None:
    """Write down a lead moving. Never raises.

    Same discipline as the audit log: the move itself has already happened and
    is what the caller cares about. Losing the note is a gap in a chart, and
    rolling back the move to avoid that gap would be a far worse trade.
    """
    if contact is None or not to_stage:
        return
    try:
        db.add(
            StageEvent(
                organization_id=contact.organization_id,
                contact_id=contact.id,
                from_stage=from_stage,
                to_stage=to_stage[:50],
                source=source[:16],
            )
        )
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record %s moving to %s: %s", getattr(contact, "id", "?"), to_stage, exc)


# ------------------------------------------------------------------ reading
async def funnel(db, organization_id, start: datetime | None, until: datetime | None) -> dict:
    """How far the leads that arrived in this window actually got."""
    stages = await pipelines.stages_for(db, organization_id)
    ladder = [stage for stage in stages if stage.outcome not in EXIT_OUTCOMES]
    exits = [stage for stage in stages if stage.outcome in EXIT_OUTCOMES]
    rung = {stage.key: index for index, stage in enumerate(ladder)}

    contacts = _window(
        select(CRMContact.id, CRMContact.pipeline_stage).where(
            CRMContact.organization_id == organization_id
        ),
        CRMContact.created_at,
        start,
        until,
    )
    rows = (await db.execute(contacts)).all()

    # A board where every column is an exit. Nonsense, but a tenant can build
    # it, and a funnel with no rungs is not a reason to fail the whole screen.
    if not ladder:
        return {
            "stages": [],
            "entered": len(rows),
            "converted": 0,
            "conversion_rate": None,
            "exits": [],
        }

    # Where each lead stands now. Off-board and exit stages fall to the first
    # rung: whatever else is true, they arrived.
    depth: dict = {contact_id: rung.get(stage, 0) for contact_id, stage in rows}

    if depth:
        moves = _window(
            select(StageEvent.contact_id, StageEvent.to_stage)
            .join(CRMContact, CRMContact.id == StageEvent.contact_id)
            .where(StageEvent.organization_id == organization_id),
            CRMContact.created_at,
            start,
            until,
        )
        for contact_id, to_stage in (await db.execute(moves)).all():
            reached = rung.get(to_stage)
            if reached is not None and contact_id in depth:
                depth[contact_id] = max(depth[contact_id], reached)

    # Suffix sums: reaching rung three means having reached one and two.
    at_rung = [0] * len(ladder)
    for value in depth.values():
        at_rung[value] += 1
    reached_counts = [0] * len(ladder)
    running = 0
    for index in range(len(ladder) - 1, -1, -1):
        running += at_rung[index]
        reached_counts[index] = running

    rungs = []
    for index, stage in enumerate(ladder):
        previous = reached_counts[index - 1] if index else None
        rungs.append(
            {
                "key": stage.key,
                "label": stage.label,
                "colour": stage.colour,
                "outcome": stage.outcome,
                "reached": reached_counts[index],
                "dropped": (previous - reached_counts[index]) if previous is not None else None,
                "rate_from_previous": (
                    round(reached_counts[index] / previous, 4) if previous else None
                ),
            }
        )

    entered = len(rows)
    current = Counter(stage for _, stage in rows)
    won_keys = [stage.key for stage in stages if stage.outcome == "won"]
    converted = sum(current.get(key, 0) for key in won_keys)

    return {
        "stages": rungs,
        "entered": entered,
        "converted": converted,
        "conversion_rate": round(converted / entered, 4) if entered else None,
        "exits": [
            {
                "key": stage.key,
                "label": stage.label,
                "outcome": stage.outcome,
                "count": current.get(stage.key, 0),
            }
            for stage in exits
        ],
    }


async def traffic(
    db, organization_id, start: datetime | None, until: datetime, zone: ZoneInfo
) -> dict:
    """Messages each way and new leads, over time.

    One pass over the messages in the window serves both this and the reply
    times below, so the rows are fetched once and handed to both.
    """
    message_rows = (
        await db.execute(
            _window(
                select(Message.contact_id, Message.sender, Message.created_at).where(
                    Message.organization_id == organization_id
                ),
                Message.created_at,
                start,
                until,
            )
            .order_by(Message.contact_id, Message.created_at)
            .limit(MAX_MESSAGE_ROWS)
        )
    ).all()

    contact_rows = (
        await db.execute(
            _window(
                select(CRMContact.created_at).where(
                    CRMContact.organization_id == organization_id
                ),
                CRMContact.created_at,
                start,
                until,
            )
        )
    ).all()

    grain = _grain(start, until)

    # All time: begin at the earliest thing that happened rather than at the
    # epoch, and fall back to now for a tenant with nothing yet.
    stamps = [_aware(row[2]) for row in message_rows] + [
        _aware(row[0]) for row in contact_rows
    ]
    first = start if start is not None else (min(stamps) if stamps else until)
    if start is None and stamps:
        grain = _grain(first, until)

    inbound: dict[str, int] = defaultdict(int)
    outbound: dict[str, int] = defaultdict(int)
    arrivals: dict[str, int] = defaultdict(int)

    for _, sender, at in message_rows:
        bucket = _bucket_of(at, zone, grain)
        if sender == SENDER_CUSTOMER:
            inbound[bucket] += 1
        else:
            outbound[bucket] += 1
    for (created_at,) in contact_rows:
        arrivals[_bucket_of(created_at, zone, grain)] += 1

    buckets = _bucket_range(first, until, zone, grain)
    # A message can sit outside the walk when the window starts mid-bucket, and
    # dropping it would make the totals disagree with the chart above them.
    for extra in sorted(set(inbound) | set(outbound) | set(arrivals)):
        if extra not in buckets:
            buckets.append(extra)
    buckets.sort()

    return {
        "grain": grain,
        "points": [
            {
                "bucket": bucket,
                "inbound": inbound.get(bucket, 0),
                "outbound": outbound.get(bucket, 0),
                "new_contacts": arrivals.get(bucket, 0),
            }
            for bucket in buckets
        ],
        "totals": {
            "inbound": sum(inbound.values()),
            "outbound": sum(outbound.values()),
            "messages": len(message_rows),
            "new_contacts": len(contact_rows),
        },
        "truncated": len(message_rows) >= MAX_MESSAGE_ROWS,
        "_rows": message_rows,
    }


def reply_times(message_rows: list) -> dict:
    """How long customers wait, and who answers them.

    A turn starts with the first customer message after a reply and ends with
    the next reply. Measuring every customer message instead would count a
    person sending three lines in a row as three waits, two of them near zero,
    and flatter the median into meaninglessness.

    Only replies that closed a turn are counted as answers. Follow-up nudges go
    out unprompted, and counting those as the agent answering customers would
    credit it for conversations nobody was having.
    """
    waits: list[float] = []
    first_waits: list[float] = []
    answered = {"agent": 0, "operator": 0}

    current = None
    waiting_since: datetime | None = None
    had_first = False

    for contact_id, sender, at in message_rows:
        if contact_id != current:
            current = contact_id
            waiting_since = None
            had_first = False

        at = _aware(at)
        if sender == SENDER_CUSTOMER:
            if waiting_since is None:
                waiting_since = at
            continue

        if waiting_since is None:
            continue

        gap = (at - waiting_since).total_seconds()
        waiting_since = None
        # Defensive: rows arrive ordered, so this is only reachable if two
        # timestamps were written out of order. Better dropped than negative.
        if gap < 0:
            continue
        waits.append(gap)
        answered["operator" if sender == SENDER_OPERATOR else "agent"] += 1
        if not had_first:
            first_waits.append(gap)
            had_first = True

    total = answered["agent"] + answered["operator"]
    return {
        "answered": total,
        "median_seconds": _percentile(waits, 0.5),
        "p90_seconds": _percentile(waits, 0.9),
        "first_median_seconds": _percentile(first_waits, 0.5),
        "by": answered,
        "agent_share": round(answered["agent"] / total, 4) if total else None,
    }


async def lead_sources(
    db, organization_id, start: datetime | None, until: datetime | None
) -> list[dict]:
    """Where leads said they came from, for the tenants that record it.

    Nulls are dropped rather than bucketed as "unknown". A shop that has never
    filled this in would otherwise get a chart with one bar on it, which says
    nothing and takes up the space of something that would.
    """
    rows = (
        await db.execute(
            _window(
                select(CRMContact.source, func.count(CRMContact.id)).where(
                    CRMContact.organization_id == organization_id,
                    CRMContact.source.is_not(None),
                    CRMContact.source != "",
                ),
                CRMContact.created_at,
                start,
                until,
            ).group_by(CRMContact.source)
        )
    ).all()
    return sorted(
        ({"source": source, "count": count} for source, count in rows),
        key=lambda row: row["count"],
        reverse=True,
    )


async def movement(
    db, organization_id, start: datetime | None, until: datetime | None
) -> dict:
    """How much the board moved, and since when there is any record of it."""
    changes = await db.scalar(
        _window(
            select(func.count(StageEvent.id)).where(
                StageEvent.organization_id == organization_id
            ),
            StageEvent.at,
            start,
            until,
        )
    )
    since = await db.scalar(
        select(func.min(StageEvent.at)).where(
            StageEvent.organization_id == organization_id
        )
    )
    return {"stage_changes": changes or 0, "tracked_from": _aware(since)}


async def overview(
    db,
    organization,
    *,
    start: datetime | None,
    until: datetime | None,
    window: str,
) -> dict:
    """Everything the analytics screen needs, in one call.

    One endpoint rather than five. The dashboard opens them all at once, and
    five parallel authenticated calls on first paint is exactly the shape that
    turned out to be racing for a licence seat last week.
    """
    until = until or datetime.now(timezone.utc)
    zone = zone_for(getattr(organization, "timezone", None))

    volume = await traffic(db, organization.id, start, until, zone)
    rows = volume.pop("_rows")

    return {
        "organization": organization.name,
        "window": window,
        "since": start,
        "until": until,
        "timezone": str(zone),
        "funnel": await funnel(db, organization.id, start, until),
        "traffic": volume,
        "replies": reply_times(rows),
        "sources": await lead_sources(db, organization.id, start, until),
        "movement": await movement(db, organization.id, start, until),
    }
