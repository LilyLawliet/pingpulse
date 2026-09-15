"""Recording what changed, and what broke.

Two tables, one purpose: answering a question after the fact that nobody could
answer at the time.

`audit_logs` covers changes a person made to how the agent treats customers — a
prompt edited, a voice applied, a board reordered, a conversation taken over.
Not every request: an audit trail nobody can read is the same as none, so this
only holds the things somebody would later have to account for.

`system_errors` covers failures a client should be able to see. These already
reach the container logs, which is the wrong place — a client cannot read those,
and by the time anyone does the question has become "why did messages stop on
Tuesday". Recorded per organization so the dashboard can answer it.

Both are written on the caller's session and neither raises. A failure to write
an audit row must not roll back the change it was describing, and a failure to
record an error must not become a second error — that is how one broken
provider turns into an unserviceable API.
"""

from __future__ import annotations

import logging
import traceback

from app.models import AuditLog, SystemError

logger = logging.getLogger(__name__)

# Categories a person filtering the log would actually pick.
WHATSAPP = "whatsapp"
CALENDAR = "calendar"
LLM = "llm"
DELIVERY = "delivery"
SYSTEM = "system"

MAX_DETAIL = 8000


def changes_between(before: dict, after: dict) -> dict:
    """Only the fields that moved, with both values.

    Whole objects are not stored. A sales prompt is long, most of it is
    unchanged, and the question asked of an audit log is always what changed
    rather than what the record looked like.
    """
    diff: dict = {}
    for field in set(before) | set(after):
        was, now = before.get(field), after.get(field)
        if was != now:
            diff[field] = {"from": was, "to": now}
    return diff


async def record(
    db,
    organization_id,
    action: str,
    *,
    user_id=None,
    resource_type: str | None = None,
    resource_id=None,
    changes: dict | None = None,
) -> None:
    """Write one audit row. Never raises."""
    try:
        db.add(
            AuditLog(
                organization_id=organization_id,
                user_id=user_id,
                action=action[:80],
                resource_type=(resource_type or None) and resource_type[:40],
                resource_id=str(resource_id)[:64] if resource_id is not None else None,
                changes=changes or {},
            )
        )
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        # Deliberately swallowed. The change this was describing has already
        # happened and is the thing the caller cares about.
        logger.warning("could not write an audit row for %s: %s", action, exc)


async def fail(
    db,
    category: str,
    message: str,
    *,
    organization_id=None,
    error: BaseException | None = None,
    detail: str | None = None,
) -> None:
    """Record an operational failure against a tenant. Never raises."""
    if detail is None and error is not None:
        detail = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )

    try:
        db.add(
            SystemError(
                organization_id=organization_id,
                category=category[:20],
                message=message[:2000],
                detail=(detail or None) and detail[:MAX_DETAIL],
            )
        )
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record a %s failure: %s", category, exc)
