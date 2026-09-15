"""Twilio WhatsApp webhook: ingest -> generate -> dispatch -> progress lead.

Tenancy: the number Twilio delivered to identifies the organization, via
`channel_configs`. Everything after that — contact lookup, history, knowledge
retrieval, persistence — is filtered by that organization.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Sequence

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    STAGE_AGENT,
    ChannelConfig,
    CRMContact,
    LLMLog,
    Message,
    Organization,
)
from app.schemas import TwilioWebhookPayload
from app.services import (
    agent_config,
    analyzer,
    consent,
    qualification,
    summarise,
    customer_memory,
    llm_service,
    media_service,
    product_search,
    retrieval,
    sales_policy,
    scheduling,
    vision,
    ws_manager,
)
from app.services import analytics, oplog, outbox, pipelines, whatsapp
from app.services.twilio_service import (
    Sender,
    signature_url,
    twilio_service,
    validate_twilio_signature,
)
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp"])

# Keywords that justify advancing a lead one stage.
QUALIFYING_SIGNALS = (
    "price",
    "pricing",
    "cost",
    "how much",
    "budget",
    "interested",
    "quote",
    "plan",
)
DEMO_SIGNALS = ("demo", "call", "meeting", "schedule", "book", "appointment", "trial")
CLOSING_SIGNALS = ("sign up", "purchase", "buy", "invoice", "contract", "let us start")

# The default ladder the automatic advancement climbs. Only the default keys
# appear here: a tenant who renamed their board to ENQUIRY / BOOKED_IN / DONE
# has no "QUALIFIED" to be moved into, and inventing one would put contacts in
# a column that is not on their board.
STAGE_ORDER = (
    "NEW_LEAD",
    "CONTACTED",
    "QUALIFIED",
    "ESTIMATE_SCHEDULED",
    "ESTIMATE_SENT",
    "FOLLOW_UP",
    "WON",
)

# Durable customer facts kept on the contact record.
MEMORY_FIELDS = (
    "city",
    "shoe_size",
    "category_interest",
    "colour_preference",
    "budget_note",
)


def evaluate_stage(current_stage: str, customer_message: str) -> str:
    """Return the stage the contact should be in after this message.

    Stages only ever move forward - a later casual message must not demote a
    lead that already booked a demo.
    """
    text = (customer_message or "").lower()

    # A contact on a stage this ladder does not know - anyone on a customised
    # board - is left exactly where their operator put them.
    if current_stage not in STAGE_ORDER:
        return current_stage

    target = current_stage
    if any(signal in text for signal in CLOSING_SIGNALS):
        target = "WON"
    elif any(signal in text for signal in DEMO_SIGNALS):
        target = "ESTIMATE_SCHEDULED"
    elif any(signal in text for signal in QUALIFYING_SIGNALS):
        target = "QUALIFIED"

    current_index = STAGE_ORDER.index(current_stage) if current_stage in STAGE_ORDER else 0
    target_index = STAGE_ORDER.index(target)
    return STAGE_ORDER[max(current_index, target_index)]


async def resolve_organization(
    db: AsyncSession, to_number: str
) -> tuple[Organization | None, ChannelConfig | None]:
    """Which tenant owns the number this message was sent to, and on what channel.

    The channel comes back too because it carries the tenant's own Twilio
    credentials and sending number — replying on the platform default would
    send one client's messages from another client's number.

    Falls back to DEFAULT_ORGANIZATION_ID, then to the only organization that
    exists — so a single-tenant install keeps working without configuring a
    channel, while a multi-tenant one routes strictly by number.
    """
    number = (to_number or "").replace("whatsapp:", "").strip()
    if number:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.phone_number == number,
                ChannelConfig.is_active.is_(True),
            )
        )
        config = result.scalar_one_or_none()
        if config is not None:
            return await db.get(Organization, config.organization_id), config
        logger.info("no channel configured for %s, falling back", number)

    if settings.default_organization_id:
        try:
            organization = await db.get(
                Organization, uuid.UUID(settings.default_organization_id)
            )
            if organization is not None:
                return organization, await _channel_for(db, organization.id)
        except ValueError:
            logger.warning(
                "DEFAULT_ORGANIZATION_ID is not a valid UUID: %r",
                settings.default_organization_id,
            )

    result = await db.execute(select(Organization).order_by(Organization.created_at))
    organizations = result.scalars().all()
    if len(organizations) == 1:
        return organizations[0], await _channel_for(db, organizations[0].id)
    if organizations:
        logger.warning(
            "%d organizations exist and none matched %s — connect a channel "
            "or set DEFAULT_ORGANIZATION_ID",
            len(organizations),
            number,
        )
    return None, None


async def _channel_for(db: AsyncSession, organization_id) -> ChannelConfig | None:
    """The organization's active WhatsApp channel, if it has one."""
    return await whatsapp.active_channel(db, organization_id)


async def _merge_contacts(
    db: AsyncSession, keep: CRMContact, drop: CRMContact
) -> CRMContact:
    """Fold one contact into another, transcript and all.

    Reached when WhatsApp tells us two records are the same person — it
    addressed them by LID for a while, then finally handed over the number we
    already had a conversation under. Repointing the messages is the whole
    point: leaving them behind would split one conversation across two entries
    in the dashboard, which is the symptom this is here to remove.
    """
    await db.execute(
        update(Message).where(Message.contact_id == drop.id).values(contact_id=keep.id)
    )
    # The surviving record wins every field it already has; the other fills gaps.
    keep.contact_metadata = {**(drop.contact_metadata or {}), **(keep.contact_metadata or {})}
    for field in ("name", "city", "shoe_size", "category_interest", "colour_preference"):
        if not getattr(keep, field, None) and getattr(drop, field, None):
            setattr(keep, field, getattr(drop, field))

    await db.flush()
    await db.delete(drop)
    await db.flush()
    logger.info("merged contact %s into %s (same person, two identifiers)", drop.id, keep.id)
    return keep


async def _resolve_contact(
    db: AsyncSession,
    organization: Organization,
    phone_number: str,
    profile_name: str | None,
    *,
    wa_lid: str | None = None,
    wa_jid: str | None = None,
) -> tuple[CRMContact, bool]:
    """Find this organization's contact for whoever sent this, or create them.

    Scoped by organization: the same person messaging two businesses on the
    platform is two separate contacts, and neither can see the other.

    Identity is matched on two things, because WhatsApp does not consistently
    give us either one. A phone number is what the shop recognises. A LID is
    what WhatsApp increasingly sends instead, and it is the only identifier
    that survives someone switching the account on their handset. Matching on
    both, and reconciling them the moment WhatsApp reveals the pairing, is what
    stops one person becoming two conversations.
    """
    lid = (wa_lid or "").strip() or None
    number = (phone_number or "").strip()

    by_lid = None
    if lid:
        by_lid = (
            await db.execute(
                select(CRMContact).where(
                    CRMContact.organization_id == organization.id,
                    CRMContact.wa_lid == lid,
                )
            )
        ).scalar_one_or_none()

    # When all WhatsApp gave us was the LID, `number` *is* the LID. Matching a
    # contact on it would be matching an identifier against a number column.
    by_number = None
    if number and number != lid:
        by_number = (
            await db.execute(
                select(CRMContact).where(
                    CRMContact.organization_id == organization.id,
                    CRMContact.phone_number == number,
                )
            )
        ).scalar_one_or_none()

    created = False
    if by_lid is not None and by_number is not None and by_lid.id != by_number.id:
        # Both exist and WhatsApp has just told us they are one person.
        contact = await _merge_contacts(db, keep=by_number, drop=by_lid)
    elif (contact := by_number or by_lid) is None:
        contact = CRMContact(
            organization_id=organization.id,
            # Only ever the LID when WhatsApp has given us nothing better; the
            # branch below replaces it the moment a real number turns up.
            phone_number=number or lid or "",
            wa_lid=lid,
            name=profile_name,
            # Wherever this organization's board starts, rather than a
            # hardcoded stage that may not be a column they have.
            pipeline_stage=await pipelines.entry_stage(db, organization.id),
            tags=[],
        )
        db.add(contact)
        created = True

    if lid and contact.wa_lid != lid:
        contact.wa_lid = lid

    # The record was created from a LID because that was all we had. Now that a
    # dialable number has arrived, it replaces the placeholder — the shop should
    # never be shown an identifier where a phone number belongs.
    if number and number != lid and contact.phone_number in ("", lid, contact.wa_lid):
        contact.phone_number = number

    if profile_name and not contact.name:
        contact.name = profile_name

    # The exact chat address, kept so replies go back the way the message came.
    # Rebuilding one from the digits produces a valid address for a number that
    # does not exist: WhatsApp accepts it and the reply reaches nobody.
    if wa_jid:
        metadata = dict(contact.contact_metadata or {})
        if metadata.get("wa_jid") != wa_jid:
            metadata["wa_jid"] = wa_jid
            contact.contact_metadata = metadata

    await db.flush()
    return contact, created


async def _recent_history(
    db: AsyncSession, organization_id, contact_id
) -> Sequence[Message]:
    """Last N messages for this contact, oldest first."""
    result = await db.execute(
        select(Message)
        .where(
            Message.contact_id == contact_id,
            Message.organization_id == organization_id,
        )
        .order_by(Message.created_at.desc())
        .limit(settings.chat_history_limit)
    )
    return list(reversed(result.scalars().all()))


async def process_inbound_message(
    db: AsyncSession,
    payload: TwilioWebhookPayload,
    channel: ChannelConfig | None = None,
) -> dict:
    """The full Phase 2-4 pipeline for one inbound WhatsApp message.

    `channel` is passed when the caller already knows which one this arrived
    on — the WhatsApp Web bridge does, because its session id *is* the channel
    id. Twilio does not, so that path still resolves by the number it
    delivered to.
    """
    phone_number = payload.clean_from
    body = (payload.body or "").strip()

    if channel is not None:
        organization = await db.get(Organization, channel.organization_id)
    else:
        organization, channel = await resolve_organization(db, payload.clean_to)
    if organization is None:
        logger.error("no organization for inbound message to %s", payload.clean_to)
        await manager.broadcast(
            ws_manager.EVENT_ERROR,
            {"stage": "routing", "detail": f"No organization owns {payload.clean_to}"},
        )
        return {"error": "unrouted", "to": payload.clean_to}

    raw_payload = payload.raw or {}
    contact, created = await _resolve_contact(
        db,
        organization,
        phone_number,
        payload.profile_name,
        # Both are empty for Twilio, which addresses everyone by phone number.
        wa_lid=(raw_payload.get("WaLid") or "").strip() or None,
        wa_jid=(raw_payload.get("WaJid") or "").strip() or None,
    )
    history = await _recent_history(db, organization.id, contact.id)

    # Attachments the customer sent: Twilio expires them and hides them behind
    # auth, so anything we want to keep is copied to the media volume on D:.
    stored_media: list[str] = []
    image_analysis: dict = {}
    for url, content_type in media_service.extract_inbound(payload.raw or {}):
        saved = await media_service.download_inbound(url, content_type)
        if saved:
            stored_media.append(saved)
        # Read the first image so the agent can answer from what it can see
        # instead of asking "which colour?" about a photo it was just sent.
        if not image_analysis and (content_type or "").startswith("image/"):
            image_analysis = await vision.analyse_image(url, content_type)
            if image_analysis and saved:
                image_analysis["stored_url"] = saved

    if stored_media and not body:
        body = "(sent a photo)"

    if image_analysis:
        metadata = dict(contact.contact_metadata or {})
        metadata["last_received_image_analysis"] = image_analysis
        contact.contact_metadata = metadata

    inbound = Message(
        organization_id=organization.id,
        contact_id=contact.id,
        sender="user",
        content=body,
        twilio_sid=payload.message_sid or None,
        media_urls=stored_media,
    )
    db.add(inbound)
    # Commit the contact and their message before the slow LLM call, so a
    # dashboard that re-reads mid-generation already sees the conversation.
    await db.commit()

    await manager.broadcast(
        ws_manager.EVENT_INBOUND,
        {
            # The row's real primary key, so a dashboard can reconcile this
            # live event against the same message fetched from the API.
            "message_id": str(inbound.id),
            "contact_id": str(contact.id),
            "organization_id": str(organization.id),
            "phone_number": phone_number,
            "name": contact.name,
            "content": body,
            "pipeline_stage": contact.pipeline_stage,
            "new_contact": created,
            "twilio_sid": payload.message_sid,
            "organization": organization.name,
            "media_urls": stored_media,
            "image_analysis": image_analysis or None,
        },
    )

    # ---- Step 0: is the agent allowed to answer this at all? -------------
    # Both checks sit after the message is stored and broadcast, never before.
    # A customer who opted out and a conversation a person has taken over both
    # still have their messages recorded and shown on the dashboard: the shop
    # needs to see what was said. What stops is the generating and the sending.
    if consent.is_opt_out(body):
        consent.record_opt_out(contact)
        await db.commit()
        await manager.broadcast(
            ws_manager.EVENT_SYNC,
            {"contact_id": str(contact.id), "organization_id": str(organization.id)},
        )
        logger.info("inbound from %s was an opt-out; nothing will be sent", phone_number)
        return {"status": "opted_out", "contact_id": str(contact.id)}

    if consent.is_opt_in(body) and contact.opt_out:
        consent.record_opt_in(contact)
        await db.commit()

    # A complaint, a refund demand, or somebody asking for a person. Handing
    # the conversation over is a keyword decision rather than the model's,
    # because the conversations most in need of a person are exactly the ones a
    # sales-tuned model is inclined to smooth over with an offer.
    escalation = agent_config.needs_escalation(body, organization)
    if escalation and contact.ai_enabled:
        contact.ai_enabled = False
        await db.commit()
        await oplog.record(
            db,
            organization.id,
            "contact.escalated",
            resource_type="contact",
            resource_id=contact.id,
            changes={"trigger": escalation},
        )
        await db.commit()
        await manager.broadcast(
            ws_manager.EVENT_SYNC,
            {"contact_id": str(contact.id), "organization_id": str(organization.id)},
        )
        logger.info("handed %s to a person after %r", phone_number, escalation)
        return {
            "status": "escalated",
            "reason": escalation,
            "contact_id": str(contact.id),
        }

    if not consent.agent_may_reply(contact):
        reason = "opted out" if contact.opt_out else "a person has taken this conversation over"
        await manager.broadcast(
            ws_manager.EVENT_SYNC,
            {"contact_id": str(contact.id), "organization_id": str(organization.id)},
        )
        logger.info("no reply generated for %s: %s", phone_number, reason)
        return {
            "status": "not_answered",
            "reason": "opt_out" if contact.opt_out else "human_takeover",
            "contact_id": str(contact.id),
        }

    # ---- Step 1: understand the message before answering it -------------
    analysis = await analyzer.analyse(history, body, contact.sales_stage)

    memory = customer_memory.apply_analysis(contact.memory, analysis)
    contact.memory = memory
    contact.last_intent = analysis.get("intent")
    contact.next_action = analysis.get("next_action")

    # ---- Step 2: retrieve only what this intent needs --------------------
    # The remembered colour keeps a bare "show me more" pointed at the right
    # things instead of the whole catalogue.
    remembered_colour = (memory.get("facts", {}).get("colour_preference") or {}).get("value")
    search_terms = " ".join(
        filter(
            None,
            [
                body,
                analysis.get("colour_preference") or remembered_colour,
                analysis.get("category_interest"),
                analysis.get("fabric_preference"),
                # What the photo showed is as good a search term as typed words.
                vision.search_terms(image_analysis),
            ],
        )
    )

    # A booking request is answered with the link and nothing else. Retrieval
    # is skipped outright rather than fetched and ignored: whatever reaches the
    # prompt competes for attention, and the bug this fixes was the agent
    # replying to "can we book a call?" with store policy and a product FAQ.
    booking_only = (
        analysis.get("intent") == "book_call"
        or analysis.get("next_action") == "book_call"
    )

    # Policy documents only: product text reaches the prompt through the
    # compact product block below, never as a wall of catalogue copy.
    chunks = (
        []
        if booking_only
        else await retrieval.search(
            db, organization.id, search_terms, limit=3, doc_type="policy"
        )
    )
    knowledge = retrieval.as_prompt_block(chunks)

    # What is known about the job and the one gap worth closing. Empty until a
    # slot has been filled or a tenant has configured slots, so a shop that
    # never touches this sees no change in how its agent answers.
    job = qualification.as_prompt_block(organization, contact.qualification)
    if job:
        knowledge = "\n\n".join(filter(None, [knowledge, job]))

    products: list = []
    outbound_media: list[str] = []

    # Product context is loaded for any product-shaped question, so a price
    # follow-up can be answered from real rows. Photos, though, are only
    # attached when they were actually asked for.
    wants_images = bool(analysis.get("wants_images")) or bool(image_analysis)
    needs_products = not booking_only and (
        wants_images
        or analysis.get("next_action") == "show_products"
        or analysis.get("intent") in ("product_question", "price_question", "purchase")
    )

    if needs_products:
        products = await product_search.find_products(db, organization.id, search_terms)
        # Anything they have turned down is dropped before it can be shown.
        products = [
            item
            for item in products
            if not customer_memory.is_rejected(
                memory, f"{item.title} {(item.attributes or {}).get('colour') or ''}"
            )
        ]
        if products:
            if wants_images:
                outbound_media = media_service.sendable(product_search.media_for(products))
            extra = product_search.as_prompt_block(products)
        else:
            extra = product_search.no_match_note(
                search_terms, product_search.wanted_attributes(search_terms)
            )
        knowledge = "\n\n".join(filter(None, [knowledge, extra]))

    await manager.broadcast(
        ws_manager.EVENT_THINKING,
        {
            "contact_id": str(contact.id),
            "step": "building_prompt",
            "detail": (
                f"Assembling prompt from organization rules + "
                f"{len(history)} history message(s)"
                + (f" + {len(chunks)} knowledge chunk(s)" if chunks else "")
                + (f" + {len(products)} product match(es)" if products else "")
            ),
        },
    )

    # Vision, and a booking link when they asked to talk to someone.
    extra_blocks = [vision.as_prompt_block(image_analysis, bool(stored_media))]
    if booking_only or analysis.get("wants_meeting") or scheduling.looks_like_b2b(body):
        extra_blocks.append(
            scheduling.as_prompt_block(
                organization.name,
                contact.name,
                phone_number,
                is_b2b=scheduling.looks_like_b2b(body),
            )
        )
    knowledge = "\n\n".join(filter(None, [knowledge, *extra_blocks]))

    generation = await llm_service.generate_reply(
        organization,
        contact,
        history,
        body,
        knowledge=knowledge,
        memory_block=customer_memory.as_prompt_block(memory, contact),
        policy_block=sales_policy.as_prompt_block(analysis),
        # If both providers are down the customer still gets a real answer built
        # from retrieved facts — never a promise that a human will call back.
        last_resort=sales_policy.deterministic_reply(
            analysis, chunks, organization, products,
            booking_url=scheduling.booking_link(
                organization.name, contact.name, phone_number
            ),
        ),
    )

    await manager.broadcast(
        ws_manager.EVENT_GENERATION,
        {
            "contact_id": str(contact.id),
            "provider": generation.provider,
            "model": (
                settings.groq_model
                if generation.provider == "groq"
                else settings.gemini_model
            ),
            "latency_ms": generation.latency_ms,
            "fallback_used": generation.fallback_used,
            "prompt_used": generation.prompt_used,
            "raw_response": generation.text,
            "error": generation.error,
        },
    )

    # Sent on the tenant's own Twilio account and number when they have one.
    # Routed by the channel's provider: Twilio's API, or the paired WhatsApp
    # Web session. Everything above and below this line is identical either
    # way, which is what keeps the two paths from drifting apart.
    #
    # The row is written *before* the send, not after. A message that has to be
    # parked in the retry queue needs an id to be reconciled against when it
    # finally leaves, and there is no id until the row exists.
    outbound = Message(
        organization_id=organization.id,
        contact_id=contact.id,
        sender="agent",
        content=generation.text,
        media_urls=outbound_media,
        delivery_status=outbox.QUEUED,
    )
    db.add(outbound)
    await db.flush()

    delivery = await outbox.deliver(
        channel,
        phone_number,
        generation.text,
        outbound_media,
        message_id=outbound.id,
        organization_id=organization.id,
        to_jid=(contact.contact_metadata or {}).get("wa_jid"),
    )
    outbound.delivery_status = delivery.status
    outbound.twilio_sid = delivery.reference
    sent = delivery.sent
    sid_or_error = delivery.reference or delivery.detail

    if delivery.queued:
        # Not an error: the transport is briefly missing and the drainer will
        # carry this one out. Said plainly so the operator is not alarmed.
        logger.info("reply to %s parked for retry: %s", phone_number, delivery.detail)
    elif not sent:
        logger.warning("outbound dispatch failed: %s", delivery.detail)
        await manager.broadcast(
            ws_manager.EVENT_ERROR,
            {"contact_id": str(contact.id), "stage": "dispatch", "detail": delivery.detail},
        )

    db.add(
        LLMLog(
            organization_id=organization.id,
            message_id=outbound.id,
            provider=generation.provider,
            prompt_used=generation.prompt_used,
            raw_response=generation.text,
            latency_ms=generation.latency_ms,
            error=generation.error,
        )
    )

    await manager.broadcast(
        ws_manager.EVENT_OUTBOUND,
        {
            "message_id": str(outbound.id),
            "contact_id": str(contact.id),
            "phone_number": phone_number,
            "content": generation.text,
            "provider": generation.provider,
            "latency_ms": generation.latency_ms,
            "delivered": sent,
            "delivery_status": delivery.status,
            "twilio_sid": delivery.reference,
            "media_urls": outbound_media,
        },
    )

    # Learn durable facts about this customer. Runs after the reply is already
    # on its way, so it costs the customer nothing, and never raises.
    # Skipped once everything is known — extraction is a second LLM call per
    # message, and re-asking a provider for facts already on file wastes the
    # rate-limit budget the reply itself depends on.
    if any(not getattr(contact, field, None) for field in MEMORY_FIELDS):
        learned = await llm_service.extract_profile(list(history) + [inbound], body)
        for field, value in learned.items():
            if field in MEMORY_FIELDS and value:
                setattr(contact, field, value)
        if learned:
            logger.info("learned about %s: %s", phone_number, learned)

    # What this job actually is. Runs after the reply is on its way, only ever
    # adds, and never raises - a customer who named a budget once has not
    # withdrawn it by failing to repeat it.
    slots = await qualification.extract(organization, list(history) + [inbound], body)
    if slots:
        contact.qualification = qualification.merge(contact.qualification, slots)

    # A standing summary for whoever opens this thread cold, refreshed on a
    # cadence rather than every turn: rewriting it after "ok thanks" spends a
    # model call to produce the same sentence.
    if summarise.is_due(len(history) + 1, bool(contact.summary)):
        written = await summarise.write(list(history) + [inbound], body)
        if written.get("summary"):
            contact.summary = written["summary"]
        if written.get("next_action"):
            contact.next_action = written["next_action"][:120]

    # The customer wrote back, so any pending nudge is cancelled by clearing
    # the token the queued tasks check against.
    metadata = dict(contact.contact_metadata or {})
    cancelled = metadata.pop("followup_token", None)
    if cancelled:
        logger.info("cancelled pending follow-up for %s", phone_number)
    contact.contact_metadata = metadata

    # The analyzer owns the sales stage; the CRM board shows a rollup of it.
    # Keeping both means the keyword rules still act as a floor if the analyzer
    # under-reads a message.
    contact.sales_stage = analyzer.advance_stage(
        contact.sales_stage, analysis.get("stage", contact.sales_stage)
    )
    previous_stage = contact.pipeline_stage
    from_analyzer = analyzer.STAGE_TO_PIPELINE.get(contact.sales_stage, previous_stage)
    new_stage = evaluate_stage(previous_stage, body)
    # Both candidates have to be on the ladder to be compared on it. On a
    # customised board they will not be, and the contact stays put rather than
    # being moved into a column that does not exist for this tenant.
    if new_stage in STAGE_ORDER and from_analyzer in STAGE_ORDER:
        new_stage = max(new_stage, from_analyzer, key=STAGE_ORDER.index)
    elif previous_stage not in STAGE_ORDER:
        new_stage = previous_stage

    on_board = {stage.key for stage in await pipelines.stages_for(db, organization.id)}
    if new_stage not in on_board:
        new_stage = previous_stage

    if new_stage != previous_stage:
        contact.pipeline_stage = new_stage
        # Written down as it happens. A current-stage column can say where a
        # lead is standing and never how it got there, so the funnel has
        # nothing to read unless the move is recorded at the moment it is made.
        await analytics.record_move(
            db, contact, new_stage, from_stage=previous_stage, source=STAGE_AGENT
        )
        await manager.broadcast(
            ws_manager.EVENT_STAGE,
            {
                "contact_id": str(contact.id),
                "phone_number": phone_number,
                "from": previous_stage,
                "to": new_stage,
            },
        )

    # Warm conversations get a nudge if they go quiet. Queued after the reply,
    # never inside the request path the customer is waiting on.
    try:
        from app.tasks import schedule_followups

        token = schedule_followups(contact)
        if token:
            metadata = dict(contact.contact_metadata or {})
            metadata["followup_token"] = token
            contact.contact_metadata = metadata
    except Exception as exc:  # noqa: BLE001 - a broker outage must not cost a reply
        logger.warning("follow-up scheduling unavailable: %s", exc)

    await db.commit()

    # Now that everything is durable, tell dashboards to re-read.
    await manager.broadcast(
        ws_manager.EVENT_SYNC,
        {
            "contact_id": str(contact.id),
            "organization_id": str(organization.id),
            "pipeline_stage": contact.pipeline_stage,
        },
    )

    return {
        "contact_id": str(contact.id),
        "organization_id": str(organization.id),
        "new_contact": created,
        "provider": generation.provider,
        "latency_ms": generation.latency_ms,
        "delivered": sent,
        "pipeline_stage": contact.pipeline_stage,
        "knowledge_chunks": len(chunks),
        "products": len(products),
        "media_sent": len(outbound_media),
        "sales_stage": contact.sales_stage,
        "intent": analysis.get("intent"),
        "image_analysed": bool(image_analysis),
        "rejected_items": customer_memory.rejected_items(memory),
        "reply": generation.text,
    }


@router.post("/webhook")
async def whatsapp_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Twilio POSTs x-www-form-urlencoded here on every inbound WhatsApp message.

    Always answers 200 with empty TwiML: a non-2xx makes Twilio retry, which
    would double-send replies for a transient downstream failure.
    """
    form = await request.form()
    raw = {key: str(value) for key, value in form.items()}
    logger.info("inbound webhook payload: %s", raw)

    # ---- prove the request really came from Twilio -----------------------
    # This endpoint is public: it has to be, because Twilio calls it. Without
    # this check anyone who finds the URL can post a fabricated message, which
    # spends the tenant's LLM quota and sends a real WhatsApp reply to any
    # number they name — on the tenant's own Twilio bill.
    #
    # The signature is verified against the auth token of the account that
    # *sent* it, which for a client who brought their own Twilio is their
    # token, not ours. Resolving the tenant first is therefore part of
    # authenticating, not just routing.
    if settings.twilio_validate_signature:
        signature = request.headers.get("X-Twilio-Signature", "")
        destination = raw.get("To", "")
        _, channel = await resolve_organization(db, destination)
        sender = Sender.for_channel(channel)

        url = signature_url(
            settings.public_base_url, request.url.path, request.url.query
        )
        if not validate_twilio_signature(
            sender.auth_token, url, raw, signature
        ):
            logger.warning(
                "rejected unsigned or mis-signed webhook for %s from %s",
                destination or "(no destination)",
                request.client.host if request.client else "unknown",
            )
            # 403 rather than the usual 200: this is not a transient failure
            # and there is nothing for Twilio to usefully retry.
            return Response(status_code=403)

    try:
        payload = TwilioWebhookPayload.model_validate({**raw, "raw": raw})
    except Exception as exc:  # noqa: BLE001
        logger.error("malformed Twilio payload: %s", exc)
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
            status_code=200,
        )

    if not payload.clean_from:
        logger.warning("webhook payload has no From number, ignoring")
    else:
        try:
            await process_inbound_message(db, payload)
        except Exception as exc:  # noqa: BLE001 - never hand Twilio a 500
            logger.exception("webhook processing failed")
            await manager.broadcast(
                ws_manager.EVENT_ERROR, {"stage": "webhook", "detail": str(exc)}
            )

    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
        status_code=200,
    )


@router.get("/webhook")
async def whatsapp_webhook_probe():
    """Convenience GET so the endpoint can be verified from a browser."""
    return {"status": "ok", "detail": "POST Twilio form payloads to this URL"}


# --------------------------------------------------------------------------
# Inbound from the WhatsApp Web bridge
# --------------------------------------------------------------------------
@router.post("/qr-inbound")
async def qr_session_inbound(request: Request, db: AsyncSession = Depends(get_db)):
    """A message that arrived over a paired WhatsApp Web session.

    The bridge speaks WhatsApp; this turns what it saw into the same payload
    shape Twilio posts, then hands it to the very same pipeline. That is
    deliberate — the analyzer, sales policy, CRM writes, follow-up scheduling
    and live dashboard events are shared, so a tenant who switches provider
    keeps every behaviour and every past conversation.

    Authenticated with a shared secret rather than a Twilio signature: the
    bridge is a service on the internal network, not a third party, and it has
    no auth token to sign with.
    """
    secret = settings.wa_qr_shared_secret
    if not secret or request.headers.get("X-PingPulse-Bridge") != secret:
        logger.warning("rejected qr-inbound with a bad or missing bridge secret")
        return Response(status_code=403)

    body = await request.json()

    # Reshaped into Twilio's field names so one payload model and one pipeline
    # serve both providers.
    raw = {
        "MessageSid": str(body.get("id") or ""),
        "From": f"whatsapp:{body.get('from', '')}",
        "To": f"whatsapp:{body.get('to', '')}",
        "Body": body.get("body") or "",
        "ProfileName": body.get("pushName") or "",
        "NumMedia": str(len(body.get("mediaUrls") or [])),
        # The exact chat JID. Carried through the raw payload because it has no
        # equivalent in Twilio's field set, which this shape otherwise mirrors.
        "WaJid": body.get("fromJid") or "",
        # WhatsApp's privacy identifier for the sender, when it used one. The
        # identifier that survives them switching accounts on the handset.
        "WaLid": body.get("fromLid") or "",
    }
    for index, url in enumerate(body.get("mediaUrls") or []):
        raw[f"MediaUrl{index}"] = url
        raw[f"MediaContentType{index}"] = "image/jpeg"

    logger.info("inbound qr-session payload: %s", raw)

    try:
        payload = TwilioWebhookPayload.model_validate({**raw, "raw": raw})
    except Exception as exc:  # noqa: BLE001
        logger.error("malformed bridge payload: %s", exc)
        return Response(status_code=400)

    # The session id *is* the channel id, which is exact. Matching on the
    # number the bridge reports is not: it arrives bare ("923052544605") while
    # channels are stored dialled ("+9230..."), so a tenant whose row was
    # perfectly fine could still fall through to "no organization owns this".
    channel = None
    session_id = body.get("sessionId")
    if session_id:
        try:
            channel = await db.get(ChannelConfig, uuid.UUID(str(session_id)))
        except (ValueError, TypeError):
            channel = None

    if session_id and channel is None:
        # A paired handset whose channel has been deleted. The credentials are
        # still on the bridge's disk, so this repeats for every message until
        # someone unpairs it — say so once, clearly, rather than reporting a
        # routing failure that looks like a configuration mistake.
        logger.warning(
            "session %s is paired but its channel no longer exists — the phone "
            "is still linked and its messages have nowhere to go. Re-create the "
            "channel with this id, or disconnect the phone from WhatsApp's "
            "linked devices screen.",
            session_id,
        )
        return {"ok": False, "error": "unknown session"}

    try:
        result = await process_inbound_message(db, payload, channel=channel)
        return {"ok": True, "delivered": result.get("delivered")}
    except Exception as exc:  # noqa: BLE001 - one bad message must not kill the bridge
        logger.exception("qr-session message failed: %s", exc)
        return {"ok": False, "error": str(exc)}


async def _drain_after_reconnect(channel_id) -> None:
    """Push out whatever was parked while a paired session was away.

    Its own task, so a slow or large drain never delays the bridge's status
    call, and its own error handling, because a background task that raises
    disappears with nothing in the log to say why.
    """
    try:
        await outbox.drain_now(channel_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not drain the outbox for %s: %s", channel_id, exc)


@router.post("/qr-status")
async def qr_session_status(request: Request, db: AsyncSession = Depends(get_db)):
    """The bridge reporting where a pairing has got to.

    Stored on the channel so the desktop app can show "Session active" or
    "Disconnected" without holding a socket open to the bridge itself.
    """
    if (
        not settings.wa_qr_shared_secret
        or request.headers.get("X-PingPulse-Bridge") != settings.wa_qr_shared_secret
    ):
        return Response(status_code=403)

    body = await request.json()
    session_id = body.get("sessionId")
    status_value = (body.get("status") or "").upper()

    try:
        channel = await db.get(ChannelConfig, uuid.UUID(str(session_id)))
    except (ValueError, TypeError):
        channel = None

    if channel is None:
        return {"ok": False, "error": "unknown session"}

    channel.session_status = status_value[:24]
    if status_value == "AUTHENTICATED":
        channel.session_connected_at = datetime.now(timezone.utc)
        # The bridge learns the real number only after pairing.
        if body.get("phoneNumber"):
            channel.phone_number = str(body["phoneNumber"]).replace("whatsapp:", "")
    await db.commit()

    if status_value == "AUTHENTICATED":
        # The transport is back. Anything parked while it was gone goes out
        # now rather than waiting for the next scheduled drain — this is the
        # difference between a customer waiting seconds and waiting a minute.
        # Detached deliberately: the bridge is holding this request open and
        # must not wait on a queue that could hold many messages.
        channel_id = channel.id
        asyncio.create_task(_drain_after_reconnect(channel_id))

    await manager.broadcast(
        ws_manager.EVENT_SYNC,
        {
            "contact_id": None,
            "organization_id": str(channel.organization_id),
            "wa_session_status": status_value,
        },
    )
    return {"ok": True}
