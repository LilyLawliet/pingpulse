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
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import (
    PIPELINE_OUTCOMES,
    SENDER_CUSTOMER,
    CRMContact,
    Message,
    Organization,
    SystemError,
    TenantPipeline,
)
from app.services import (
    notifications,
    unanswered,
    agent_config,
    booking,
    handover_signals,
    llm_service,
    offers,
    oplog,
    pipelines,
    product_search,
    retrieval,
    sales_policy,
    trade_defaults,
    whatsapp,
    ws_manager,
)
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
    stored = organization.agent_config or {}
    return {
        # Without the undo snapshot: it is not a setting, the form has no
        # field for it, and a client that could send one back could forge a
        # history for itself.
        "agent_config": {
            key: value
            for key, value in stored.items()
            if key != agent_config.PREVIOUS_KEY
        },
        "timezone": organization.timezone,
        "open_now": agent_config.is_open(organization),
        "days": list(agent_config.DAYS),
        # So the page can offer to step back without a second round trip. None
        # when this tenant has never changed anything, which is when an undo
        # button would be a lie.
        "last_change": _describe_change(stored),
        # Whether this business can actually take an appointment, and what is
        # stopping it. A capability that silently is not there is worse than
        # one that is plainly off: a client ran for its whole life unable to
        # book anything, with its hours read correctly out of a document and
        # never confirmed, and nothing anywhere said so.
        "booking": booking.readiness(organization),
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

    # Judged strictly here, forgivingly at reply time. A shop that saves
    # {"monday": "9-5"} gets no error today, no hours, and an agent that
    # silently stops offering appointments - while the settings page shows the
    # broken value back as though it had taken. Every problem is reported at
    # once rather than one per attempt.
    if config is not None:
        problems = agent_config.validate(config)

        # A clock with no location on it. The column defaults to "UTC", so a
        # tenant who never touched it is indistinguishable from one who chose
        # it - and "we close at 5" read in the wrong zone is precisely how a
        # customer was offered an appointment at 1am. Asked for once, at the
        # moment the answer first matters; a shop genuinely on UTC says so and
        # is never asked again.
        if any(key in config for key in ("business_hours", "quiet_hours")):
            if payload.get("timezone") is None and organization.timezone == "UTC":
                problems.append(
                    "Set your timezone before setting hours. Times are stored "
                    "against it, and the wrong zone moves every appointment "
                    "you offer. Use an IANA name like America/New_York or "
                    "Asia/Karachi - or UTC, if that is really where you are."
                )

        if problems:
            raise HTTPException(status_code=422, detail=problems)

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
        # Once any of it is saved, what a document suggested has served its
        # purpose: this form is the confirmation, and it shows every field a
        # document can fill. Dropped here rather than trusted to the client,
        # so a suggestion cannot outlive the decision and ask to be confirmed
        # again. Saving an empty form is not a confirmation, so a suggestion
        # survives somebody clicking through without filling anything in.
        if any(config.get(key) for key in agent_config.DOCUMENT_FIELDS):
            config.pop(agent_config.PROPOSED_KEY, None)
            config.pop(agent_config.PROPOSED_HOURS_KEY, None)
        else:
            # Carried forward, because this endpoint replaces the config whole.
            # A suggestion is not a field somebody typed; it is what the
            # uploaded documents said, and clicking through the form without
            # filling anything in would otherwise throw the reading away and
            # leave re-uploading as the only way back.
            for key in (agent_config.PROPOSED_KEY, agent_config.PROPOSED_HOURS_KEY):
                if key in before and key not in config:
                    config[key] = before[key]

        # Whatever a client sent under this key is discarded: the history is
        # the server's, and a page that could write it could hand somebody an
        # undo button that restores something they never had.
        config.pop(agent_config.PREVIOUS_KEY, None)

        # Kept only where something actually moved. A save that changes
        # nothing must not offer an undo, because pressing it would appear to
        # do nothing and leave a person unsure which of their changes it ate.
        if agent_config.snapshot(before) != config:
            config[agent_config.PREVIOUS_KEY] = {
                "config": agent_config.snapshot(before),
                "at": datetime.now(timezone.utc).isoformat(),
                "was_undo": False,
            }
        elif agent_config.PREVIOUS_KEY in before:
            config[agent_config.PREVIOUS_KEY] = before[agent_config.PREVIOUS_KEY]

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
        "agent_config": {
            key: value
            for key, value in (organization.agent_config or {}).items()
            if key != agent_config.PREVIOUS_KEY
        },
        "timezone": organization.timezone,
        "open_now": agent_config.is_open(organization),
        "last_change": _describe_change(organization.agent_config or {}),
    }


# ------------------------------------------------- drafts, evidence, undo
# Three fields on the settings form describe nothing about a business and so
# cannot be read out of its documents: what the agent must never promise, how
# it may talk about price, and which words should fetch a person. They are
# decisions, and the form asked for them as three empty boxes.
#
# An empty box asks a person to author policy from nothing. Everything below
# turns that into correcting a draft instead, without ever writing anything a
# person has not looked at:
#
#   /trades       a starting draft by trade. A convention, not a finding.
#   /suggestions  words that really did precede a person stepping in. Evidence.
#   /preview      exactly what the agent will be told, before saving.
#   /undo         the last change put back, because a wrong one is quiet.
#
# None of the first three writes. The config still changes in one place, on a
# form somebody pressed save on.


@router.get("/agent-config/trades")
async def list_trades(tenant: Tenant = Depends(current_org)):
    """Starting drafts, one per trade. Shown in full, stored by nobody."""
    return {
        "trades": trade_defaults.listing(),
        "fields": list(trade_defaults.DRAFT_FIELDS),
    }


@router.get("/agent-config/suggestions")
async def config_suggestions(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Words that kept coming up just before a person took over a conversation.

    Derived from operator messages - a human judgement, made at the time, that
    the agent was out of its depth. Returned with the evidence for each one
    and applied by nobody: only the shop knows which of these mean trouble
    rather than just meaning Tuesday.
    """
    organization = await db.get(Organization, tenant.id)
    try:
        report = await handover_signals.evidence(db, tenant.id, organization)
    except Exception as exc:  # noqa: BLE001
        # A suggestion panel must never take the settings page down with it.
        logger.warning("handover signals failed for %s: %s", tenant.id, exc)
        return dict(handover_signals.Report().as_dict(), unavailable=True)

    existing = {
        str(word).lower().strip()
        for word in (organization.agent_config or {}).get("escalate_on") or []
    }
    payload = report.as_dict()
    payload["candidates"] = [
        row for row in payload["candidates"] if row["phrase"] not in existing
    ]
    payload["min_conversations"] = handover_signals.MIN_CONVERSATIONS
    return payload


@router.post("/agent-config/preview")
async def preview_agent_config(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """What the agent would be told, if this were saved. Saves nothing.

    The config reaches the model as prompt text, and until now the only way to
    find out what that text came out as was to save it and message the number.
    A shop editing the rules its agent answers under should be able to read
    them first: that is the difference between a settings form and a guess.
    """
    config = payload.get("agent_config")
    if config is not None and not isinstance(config, dict):
        raise HTTPException(status_code=422, detail="agent_config must be an object")

    organization = await db.get(Organization, tenant.id)
    zone = payload.get("timezone") or organization.timezone

    problems = agent_config.validate(config or {})

    # A stand-in rather than the real row: `as_prompt_block` reads its
    # organization by attribute, and assigning the candidate onto the mapped
    # object would leave it dirty in a session that is about to commit
    # something else.
    candidate = SimpleNamespace(
        id=organization.id, agent_config=config or {}, timezone=zone
    )

    return {
        "problems": problems,
        "valid": not problems,
        "prompt_block": agent_config.as_prompt_block(candidate),
        "open_now": agent_config.is_open(candidate),
        "timezone": zone,
        "changes": oplog.changes_between(
            dict(organization.agent_config or {}), config or {}
        ),
    }


def _describe_change(stored: dict) -> dict | None:
    """The last change, in the shape the button needs: what, and when.

    None where there is nothing to undo. An undo button that 404s is worse
    than no undo button.
    """
    snapshot = (stored or {}).get(agent_config.PREVIOUS_KEY)
    if not isinstance(snapshot, dict):
        return None

    was = snapshot.get("config") or {}
    now = agent_config.snapshot(stored)
    moved = sorted(oplog.changes_between(was, now).keys())
    if not moved:
        return None

    return {
        "at": snapshot.get("at"),
        "fields": moved,
        # So the button can say "Redo" rather than offering to undo an undo,
        # which reads as though it would go back two steps.
        "was_undo": bool(snapshot.get("was_undo")),
    }


@router.post("/agent-config/undo")
async def undo_agent_config(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Put the last change to these rules back.

    A wrong setting here is quiet. Nothing breaks, no error is raised, and the
    agent simply starts answering customers under a rule nobody meant - which
    is then found out from a customer rather than from the dashboard. Being
    able to step back without reconstructing what the form said an hour ago is
    what makes the rest of this safe to experiment with.

    The inverse of the recorded change is applied to the config as it stands
    now, rather than a stored snapshot being written over it. Where nothing
    else has changed since, the two are identical; where something has, this
    undoes the one change instead of silently reverting the others too.
    """
    tenant.require_role(WRITE_ROLES)

    organization = await db.get(Organization, tenant.id)
    before = dict(organization.agent_config or {})

    snapshot = before.get(agent_config.PREVIOUS_KEY)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("config"), dict):
        raise HTTPException(status_code=404, detail="There is no change to undo.")

    restored = dict(snapshot["config"])

    # A document read since the last save is not a settings change, and undo
    # must not throw it away - re-uploading would be the only way back. The
    # current proposal always wins where there is one, because documents only
    # ever move forward; the snapshot's is used when the save being undone was
    # the confirmation that consumed it.
    for key in (agent_config.PROPOSED_KEY, agent_config.PROPOSED_HOURS_KEY):
        if key in before:
            restored[key] = before[key]

    # Held to the same standard as a save. A config stored before a validation
    # rule existed could otherwise be restored into a state the form now
    # refuses, leaving somebody unable to save until they found the offending
    # field themselves.
    problems = agent_config.validate(restored)
    if problems:
        raise HTTPException(
            status_code=422,
            detail=["That change cannot be undone as it stands."] + problems,
        )

    undone = sorted(
        oplog.changes_between(restored, agent_config.snapshot(before)).keys()
    )

    # What is being undone becomes what a second press restores, so overshooting
    # is recoverable. Marked as an undo so the button can offer "Redo" rather
    # than appearing to go back twice.
    restored[agent_config.PREVIOUS_KEY] = {
        "config": agent_config.snapshot(before),
        "at": datetime.now(timezone.utc).isoformat(),
        "was_undo": True,
    }

    organization.agent_config = restored
    await db.flush()

    await oplog.record(
        db,
        tenant.id,
        "agent_config.undo",
        user_id=getattr(tenant.user, "id", None),
        resource_type="organization",
        resource_id=tenant.id,
        changes=oplog.changes_between(before, restored),
    )

    return {
        "agent_config": {
            key: value
            for key, value in restored.items()
            if key != agent_config.PREVIOUS_KEY
        },
        "timezone": organization.timezone,
        "open_now": agent_config.is_open(organization),
        "undone": undone,
        "last_change": _describe_change(restored),
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


# ------------------------------------------------------------ system status
@router.get("/whatsapp/status")
async def whatsapp_status(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Is this shop actually connected, and when did anything last happen?

    Answers the question an operator asks first when replies stop, in one
    place: which number, over which transport, still authenticated, and when a
    message last moved in either direction. Every part degrades on its own -
    an unreachable bridge reports the number and the last message from the
    database rather than failing the whole call, because "we cannot tell you
    anything" is the least useful answer to "is it working".
    """
    channel = await whatsapp.active_channel(db, tenant.id)
    if channel is None:
        return {
            "connected": False,
            "provider": None,
            "reason": "No WhatsApp number is connected yet.",
        }

    provider = whatsapp.provider_of(channel)
    last_inbound = await db.scalar(
        select(func.max(Message.created_at)).where(
            Message.organization_id == tenant.id, Message.sender == SENDER_CUSTOMER
        )
    )
    last_outbound = await db.scalar(
        select(func.max(Message.created_at)).where(
            Message.organization_id == tenant.id, Message.sender != SENDER_CUSTOMER
        )
    )
    failed = await db.scalar(
        select(func.count(Message.id)).where(
            Message.organization_id == tenant.id,
            Message.delivery_status == "FAILED",
            Message.created_at >= datetime.now(timezone.utc) - timedelta(days=1),
        )
    )

    # Twilio is connected whenever a number is configured; a paired session is
    # only connected while the handset says so.
    connected = provider == whatsapp.TWILIO or channel.session_status == "AUTHENTICATED"

    bridge_ok = None
    if provider == whatsapp.QR_SESSION:
        bridge_ok = await _bridge_reachable(channel)

    return {
        "connected": bool(connected),
        "provider": provider,
        "phone_number": channel.phone_number,
        "session_status": channel.session_status,
        "session_connected_at": channel.session_connected_at,
        "bridge_reachable": bridge_ok,
        "last_inbound_at": last_inbound,
        "last_outbound_at": last_outbound,
        "failed_last_day": failed or 0,
    }


async def _bridge_reachable(channel) -> bool | None:
    """Can we reach the WhatsApp Web bridge? None when we could not tell.

    Bounded hard: this is called to render a status light, and a status light
    that hangs the dashboard for thirty seconds is worse than one that admits
    it does not know.
    """
    import httpx

    from app.config import settings

    url = f"{settings.wa_qr_service_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            response = await client.get(
                url, headers={"X-PingPulse-Bridge": settings.wa_qr_shared_secret}
            )
            return response.status_code < 500
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------- the sandbox
@router.post("/agent/simulate")
async def simulate(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Try a message against this shop's real setup without sending anything.

    The whole point is that nothing leaves the building: no WhatsApp call, no
    contact created, no message stored. It uses the real prompt assembly, the
    real knowledge base and the real operating rules, because a sandbox that
    tests a different prompt from the live one tests nothing.
    """
    tenant.require_role(WRITE_ROLES)

    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="Type a message to try")

    organization = await db.get(Organization, tenant.id)

    # A contact that is never added to the session, so nothing is written.
    pretend = CRMContact(
        organization_id=tenant.id,
        phone_number="+10000000000",
        name=payload.get("name") or "Test customer",
        pipeline_stage=await pipelines.entry_stage(db, tenant.id),
        sales_stage="NEW",
        tags=[],
        qualification=payload.get("qualification") or {},
    )

    history = [
        _PretendMessage(entry.get("sender") or "user", entry.get("content") or "")
        for entry in (payload.get("history") or [])
        if isinstance(entry, dict)
    ]

    chunks = await retrieval.search(db, tenant.id, message, limit=3, doc_type="policy")
    knowledge = retrieval.as_prompt_block(chunks)

    escalation = agent_config.needs_escalation(message, organization)
    if escalation:
        return {
            "reply": None,
            "escalated": True,
            "reason": escalation,
            "note": (
                "A real conversation would stop here and wait for a person. "
                f"The word that triggered it was '{escalation}'."
            ),
            "knowledge_used": [chunk.title for chunk in chunks],
        }

    # The same price reading a real customer's message gets. A sandbox that
    # skipped it tested a different agent: this is where "20 m of cable" is
    # read against a 100 m coil, and where the totals come from.
    offer = await offers.for_turn(db, organization, message, history)
    if offer.prompt_block():
        knowledge = "\n\n".join(filter(None, [knowledge, offer.prompt_block()]))

    generation = await llm_service.generate_reply(
        organization,
        pretend,
        history,
        message,
        knowledge=knowledge,
        known_prices=offer.prices,
        known_quantities=offer.quote.quantities(),
        # The sandbox sends nothing, pictures included.
        photos_attached=False,
        photos_available=await product_search.has_photos(db, tenant.id),
        last_resort=""
        if offer.quote.unknown_place
        else offer.reply()
        or sales_policy.without_filler(
            sales_policy.deterministic_reply({}, chunks, organization, message=message)
        ),
    )
    # What a real conversation would do when nothing answers the question:
    # alert a person. The sandbox says so instead of alerting anybody.
    team = None
    if generation.needs_team is not None:
        reachable = await notifications.can_reach(db, organization)
        generation.text = (
            unanswered.passed_on(organization)
            if reachable
            else unanswered.reach_us(await unanswered.contact_line(db, tenant.id))
        )
        team = (
            "Nothing in your documents answers this. On WhatsApp, you would get an alert "
            "and the customer would be told the team will reply."
            if reachable
            else "Nothing in your documents answers this, and no alert address or device "
            "is set up - so the customer is not promised a reply. Add one under Alerts."
        )
    return {
        "reply": generation.text,
        "escalated": False,
        "provider": generation.provider,
        "latency_ms": generation.latency_ms,
        "fallback_used": generation.fallback_used,
        "knowledge_used": [chunk.title for chunk in chunks],
        # How the price list was read for this message, so a shop can see
        # which product and which sums the answer was built on.
        "quote": offers.as_dict(offer.quote),
        # Why a provider's reply was not used, when one was not. On a live
        # conversation this is only in the logs; here the shop testing its
        # agent can see it.
        "why": generation.error if generation.fallback_used else None,
        # Set when the agent did not know: what a live chat would do about it.
        "needs_team": team,
        "sent": False,
    }


class _PretendMessage:
    """Shaped like a Message for the prompt builder, backed by nothing."""

    def __init__(self, sender: str, content: str):
        self.sender = sender
        self.content = content
        self.media_urls: list[str] = []


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
