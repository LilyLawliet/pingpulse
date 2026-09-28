"""Twilio WhatsApp webhook: ingest -> generate -> dispatch -> progress lead.

Tenancy: the number Twilio delivered to identifies the organization, via
`channel_configs`. Everything after that — contact lookup, history, knowledge
retrieval, persistence — is filtered by that organization.
"""

from __future__ import annotations

import asyncio
import hmac
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
from sqlalchemy.exc import IntegrityError

from app.services import (
    booking,
    agent_config,
    analyzer,
    consent,
    qualification,
    summarise,
    customer_memory,
    llm_service,
    media_service,
    offers,
    product_search,
    retrieval,
    sales_policy,
    scheduling,
    vision,
    ws_manager,
)
from app.services import analytics, notifications, oplog, outbox, pipelines, whatsapp
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
# Stages that describe something which happened outside the conversation: an
# appointment exists, a quote went out, money was agreed. None of them can be
# earned by a customer typing a word, and all three used to be.
#
# "can I schedule an estimate?" contains "schedule", so it moved the lead to
# ESTIMATE_SCHEDULED with nothing booked and no time chosen - the board then
# said a thing that was not true, and the client believed it. The match was a
# substring too, so "I'll bookmark that" did it as well.
#
# WON was worse, because it feeds the money panel: "do I have to purchase
# today?" marked the deal won and put a figure in a forecast that nobody had
# agreed to pay.
#
# These are now reachable only from a human moving the card, or from a backend
# action that completed and was verified.
VERIFIED_ONLY = ("ESTIMATE_SCHEDULED", "ESTIMATE_SENT", "WON")

# The furthest a conversation on its own may carry a lead. Talking to somebody
# and finding out what they need are both things the conversation really is
# evidence of; everything past that is not.
CONVERSATIONAL_CEILING = "QUALIFIED"

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


def evaluate_stage(
    current_stage: str,
    customer_message: str,
    organization=None,
    collected: dict | None = None,
) -> str:
    """Return the stage the contact should be in after this message.

    Only two moves are on offer here, and both are things the conversation is
    genuinely evidence of: somebody messaged us, and we now know enough about
    what they want.

    QUALIFIED is decided by whether the configured qualification is actually
    complete, not by whether the customer said "price". Asking what something
    costs is a question, and it used to be enough on its own.

    Stages only ever move forward - a later casual message must not demote a
    lead who really does have an appointment.
    """
    # A contact on a stage this ladder does not know - anyone on a customised
    # board - is left exactly where their operator put them.
    if current_stage not in STAGE_ORDER:
        return current_stage

    # Already past what a conversation can justify. Leave them alone: a lead
    # with a booking must not be walked back to QUALIFIED by small talk.
    if STAGE_ORDER.index(current_stage) >= STAGE_ORDER.index(CONVERSATIONAL_CEILING):
        return current_stage

    # Staying put is the default. An inbound message is evidence the customer
    # did something, not that we did, and advancing on every one of them would
    # empty the "new lead" column within milliseconds of it filling.
    target = current_stage

    # Complete qualification is a fact about data we hold, and it is checkable
    # by anybody looking at the record - which is the whole difference between
    # this and matching a word.
    if organization is not None:
        from app.services import qualification

        wanted = qualification.slots_for(organization)
        # A shop with qualification switched off has no unanswered slots, and
        # "nothing is missing" would otherwise read as "fully qualified" and
        # promote every contact on their first message. No questions asked is
        # not the same as every question answered.
        if wanted and not qualification.missing(organization, collected):
            target = CONVERSATIONAL_CEILING

    current_index = STAGE_ORDER.index(current_stage)
    target_index = STAGE_ORDER.index(target)
    return STAGE_ORDER[max(current_index, target_index)]


# What the customer hears when their conversation stops being automated.
#
# Deliberately narrow about what it claims. It says what we actually did -
# stopped the automatic replies and flagged the conversation - and not that a
# person has been notified, because the alert is handed to a worker and has
# not been delivered yet at the moment this is sent. Claiming a notification
# that later failed would be the same lie in a new place.
#
# It also never says "I am a person". The agent is not one, and a customer who
# asked for a human is owed a straight answer about that.
DEFAULT_HANDOFF_REPLY = (
    "You're through to the automated assistant, so I've stopped replying here "
    "and passed this conversation to the team."
)


def handoff_reply(organization) -> str:
    """The tenant's own wording, or the honest default."""
    config = (getattr(organization, "agent_config", None) or {}) if organization else {}
    custom = str(config.get("handoff_message") or "").strip()
    return custom or DEFAULT_HANDOFF_REPLY


async def acknowledge_handoff(
    db, organization, contact, channel, phone_number: str
) -> bool:
    """Send the one message a handed-over customer should get. Never raises.

    Recorded as a message like any other, so the dashboard shows the operator
    exactly what the customer was told before they picked the conversation up.
    """
    from app.services import outbox

    text = handoff_reply(organization)
    try:
        outbound = Message(
            organization_id=organization.id,
            contact_id=contact.id,
            sender="agent",
            content=text,
            delivery_status=outbox.QUEUED,
        )
        db.add(outbound)
        await db.flush()

        delivery = await outbox.deliver(
            channel,
            phone_number,
            text,
            [],
            message_id=outbound.id,
            organization_id=organization.id,
            to_jid=(contact.contact_metadata or {}).get("wa_jid"),
        )
        outbound.delivery_status = delivery.status
        outbound.twilio_sid = delivery.reference
        await db.flush()
        return bool(delivery.sent or delivery.queued)
    except Exception as exc:  # noqa: BLE001 - the handover matters more
        logger.warning("could not acknowledge the handoff to %s: %s", phone_number, exc)
        return False


async def already_handled(db, organization_id, message_sid: str | None) -> bool:
    """Have we processed this exact delivery before?

    Twilio retries a webhook that did not answer fast enough, and generating
    a reply takes seconds, so a slow turn is redelivered as a matter of
    course. Without this the customer's message is stored twice and answered
    twice - and since booking became real, the second pass would try to book
    the slot the first pass had just taken and tell them it was gone.

    Keyed on the provider's own id. A message with no id cannot be
    deduplicated and is processed, because dropping a real message is worse
    than sending a rare duplicate.
    """
    if not message_sid:
        return False
    found = await db.scalar(
        select(Message.id)
        .where(
            Message.organization_id == organization_id,
            Message.twilio_sid == message_sid,
            Message.sender == "user",
        )
        .limit(1)
    )
    return found is not None


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

    # A redelivery of something already answered. Checked here, after the
    # contact exists, so the sid lookup is scoped to one organization.
    if await already_handled(db, organization.id, payload.message_sid):
        logger.info(
            "ignoring a repeat delivery of %s from %s", payload.message_sid, phone_number
        )
        return {
            "status": "duplicate",
            "contact_id": str(contact.id),
            "twilio_sid": payload.message_sid,
        }

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
        # Worth telling somebody about even though nothing is broken: a shop
        # usually wants to know which customer has just gone quiet on purpose.
        await notifications.raise_and_send(
            db,
            organization,
            "opt_out",
            "Someone opted out",
            f"{contact.name or phone_number} asked to stop being messaged. "
            "Nothing further will be sent to them.",
            contact_id=contact.id,
        )
        await db.commit()
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
        # The reason this whole subsystem exists. The agent has just switched
        # itself off for this conversation, and until now the only way to find
        # that out was to have the dashboard open at the time.
        await notifications.raise_and_send(
            db,
            organization,
            "escalation",
            "Someone needs a person",
            f"{contact.name or phone_number} said: {body.strip()[:200]}"
            "\n\nThe agent has stopped replying to them and is waiting for you.",
            contact_id=contact.id,
        )
        await db.commit()

        # Said before returning, because returning is all this branch used to
        # do. A customer who asks for a person and hears nothing back has been
        # handed over as far as the database is concerned and ignored as far
        # as they are concerned.
        acknowledged = await acknowledge_handoff(
            db, organization, contact, channel, phone_number
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
            "customer_told": acknowledged,
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

    # Appointments, done before the prompt exists.
    #
    # This is the ordering that matters. The model used to be handed a booking
    # link and left to describe what it meant, and it described an appointment
    # confirmed for 1am. Now the checking, booking, cancelling and moving all
    # happen here against the real diary, and what reaches the prompt is a
    # report of what occurred.
    # Can the agent actually finish what this customer is asking for?
    #
    # The case that produced the incident: somebody asks to book, the shop has
    # never set its opening hours, so there is nothing to offer and nothing
    # the agent can do. Forbidden from saying a colleague would follow up, it
    # invented a time instead - and then, asked to cancel it, said it was a
    # live team member.
    #
    # Handing over is only honest if somebody is told, so the alert is raised
    # here and its destination checked before the agent is allowed to mention
    # a person at all.
    handed_to_a_person = False
    if booking.wants_booking(body) and not booking.booking_enabled(organization):
        handed_to_a_person = await notifications.can_reach(db, organization)
        await notifications.raise_and_send(
            db,
            organization,
            "unanswered",
            "Someone wants to book and the agent cannot",
            f"{contact.name or phone_number} asked to book: {body.strip()[:200]}"
            "\n\nThis business has no opening hours set, so the agent has no times "
            "to offer and has not booked anything. "
            + (
                "They have been told a person will come back to them with times."
                if handed_to_a_person
                else "They have NOT been promised a callback, because this "
                "organization has no alert address or device - so nothing "
                "would have been behind the promise."
            ),
            contact_id=contact.id,
        )

    appointment_turn = await booking.handle_turn(db, organization, contact, body)

    extra_blocks = [vision.as_prompt_block(image_analysis, bool(stored_media))]
    if appointment_turn.prompt_block:
        extra_blocks.append(appointment_turn.prompt_block)
    if handed_to_a_person:
        # Said plainly, and only here. The guard lets a handoff phrase through
        # for this turn because the alert behind it has already been raised.
        extra_blocks.append(
            "A colleague has just been alerted about this booking request. "
            "You MAY tell the customer that a team member will get back to them "
            "with available times. Do NOT offer a time yourself, do NOT say "
            "anything is booked, and never claim to be a person."
        )

    elif booking_only or analysis.get("wants_meeting") or scheduling.looks_like_b2b(body):
        # A B2B caller wanting a sales call is a different thing from a
        # customer booking a site visit, and the link is still the right
        # answer for it - now only when there is no real diary in play.
        extra_blocks.append(
            scheduling.as_prompt_block(
                organization.name,
                contact.name,
                phone_number,
                is_b2b=scheduling.looks_like_b2b(body),
            )
        )
    knowledge = "\n\n".join(filter(None, [knowledge, *extra_blocks]))

    # The price list, read and worked out for this message: which products,
    # in which sale units, how many, and the sums. The model is handed the
    # figures instead of being left to do the arithmetic, the guard accepts
    # them, and if both providers are down they are the reply.
    offer = (
        None if booking_only else await offers.for_turn(db, organization, body, history)
    )
    if offer and offer.prompt_block():
        knowledge = "\n\n".join(filter(None, [knowledge, offer.prompt_block()]))

    generation = await llm_service.generate_reply(
        organization,
        contact,
        history,
        body,
        knowledge=knowledge,
        memory_block=customer_memory.as_prompt_block(memory, contact),
        policy_block=sales_policy.as_prompt_block(analysis),
        # What the reply is allowed to claim. A sentence announcing a booking,
        # a cancellation or a move survives only if one actually happened on
        # this turn - checked against these rather than against the prompt.
        appointment=appointment_turn.appointment,
        did_cancel=appointment_turn.cancelled,
        did_move=appointment_turn.moved,
        handoff_allowed=handed_to_a_person,
        known_prices=offer.prices if offer else (),
        known_quantities=offer.quote.quantities() if offer else (),
        # If both providers are down the customer still gets a real answer built
        # from retrieved facts — never a promise that a human will call back.
        # A worked-out quote comes first: it is the answer to what they asked.
        last_resort=(offer.reply() if offer and offer.reply() else None)
        or sales_policy.deterministic_reply(
            analysis, chunks, organization, products,
            booking_url=scheduling.booking_link(
                organization.name, contact.name, phone_number
            ),
            message=body,
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
        await notifications.raise_and_send(
            db,
            organization,
            "delivery_failure",
            "A message could not be delivered",
            f"The reply to {contact.name or phone_number} did not go out: "
            f"{(delivery.detail or 'no reason given')[:200]}",
            contact_id=contact.id,
        )
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
    new_stage = evaluate_stage(
        previous_stage, body, organization, contact.qualification
    )

    # The one thing that may put a lead in the column meaning "booked": a row
    # in the appointments table, written and confirmed a moment ago. The word
    # "schedule" in a question used to be enough.
    if appointment_turn.booked:
        booked_stage = await pipelines.stage_with_outcome(db, organization.id, "booked")
        if booked_stage:
            new_stage = booked_stage
    # The model reading a conversation as NEGOTIATION or CLOSED is an opinion
    # about a conversation, not a quote that went out or money that changed
    # hands. It was allowed to move the board into both, which is the same
    # fault as the keyword route and harder to see, because it looks like
    # understanding rather than a string match. A live contact reached WON
    # this way from a test conversation about wedding shoes.
    #
    # Capped at what the conversation itself justifies rather than merely kept
    # out of the verified stages: capping it at QUALIFIED still let it *raise*
    # anybody to QUALIFIED, because the two candidates are compared with max()
    # further down - so the model could still assert a qualification the
    # record did not contain. The pipeline column is now decided by human
    # moves, verified actions and the qualification data, and by nothing that
    # reads a conversation. The model still drives `sales_stage`, which is how
    # it talks rather than a claim about the business.
    if from_analyzer in STAGE_ORDER and new_stage in STAGE_ORDER:
        if STAGE_ORDER.index(from_analyzer) > STAGE_ORDER.index(new_stage):
            from_analyzer = new_stage
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

    # Raised after the reply has been composed and sent, never before it. The
    # customer's answer is the thing on the critical path; being told about it
    # is not.
    who = contact.name or phone_number
    if created:
        await notifications.raise_and_send(
            db, organization, "new_lead", "A new lead",
            f"{who} messaged for the first time: {body.strip()[:200]}",
            contact_id=contact.id,
        )
    if analysis.get("intent") == "book_call":
        await notifications.raise_and_send(
            db, organization, "booking", "Someone wants to book",
            f"{who} asked to book a time. The agent has replied, but a booking "
            "usually wants a person to confirm it.",
            contact_id=contact.id,
        )
    if generation.provider == "none":
        # Both providers failed, so what went out was the holding reply rather
        # than an answer. The customer has been left waiting without being
        # told they are waiting.
        await notifications.raise_and_send(
            db, organization, "unanswered", "The agent could not answer",
            f"{who} asked: {body.strip()[:200]}"
            "\n\nBoth AI providers failed, so they got a holding reply.",
            contact_id=contact.id,
        )

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


def _bridge_authorised(request: Request) -> bool:
    """Whether this request carries the bridge secret.

    Constant-time. The comparison used to be `!=`, which returns the moment
    two bytes differ - so the time to reject a guess grows with how many
    leading bytes were right, and a caller measuring that can rebuild the
    secret one byte at a time without ever seeing it. `compare_digest` takes
    the same time whatever the input, and encoding to bytes keeps it from
    raising on a non-ASCII header.

    An empty configured secret fails every request rather than matching an
    empty header: a missing secret is a locked door, not an open one.
    """
    secret = settings.wa_qr_shared_secret or ""
    presented = request.headers.get("X-PingPulse-Bridge", "")
    if not secret:
        return False
    return hmac.compare_digest(secret.encode("utf-8"), presented.encode("utf-8"))


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
    if not _bridge_authorised(request):
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
    if not _bridge_authorised(request):
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

    # The status first, alone, and committed before anything that can fail.
    # These used to share a transaction with the number below, so a number we
    # could not store discarded the far more important fact that the session
    # is up - which is exactly what happened in production: a successful
    # pairing recorded as GENERATING_QR, permanently.
    channel.session_status = status_value[:24]
    if status_value == "AUTHENTICATED":
        channel.session_connected_at = datetime.now(timezone.utc)
    await db.commit()

    # Then the number, which is allowed to fail. The bridge learns the real
    # one only after pairing, and it may already belong to somebody else.
    if status_value == "AUTHENTICATED" and body.get("phoneNumber"):
        reported = whatsapp.normalise_number(body["phoneNumber"])
        # Looked up whether or not this channel needs the number written. The
        # conflict is that two organizations hold one handset, and that is
        # true regardless of which spelling each of them stored - which is
        # exactly the production case, where the channel already had the
        # number and the clash was invisible from this row alone.
        if reported:
            owner = await db.scalar(
                select(ChannelConfig.organization_id).where(
                    ChannelConfig.channel == channel.channel,
                    ChannelConfig.phone_number.in_(
                        (reported, reported.lstrip("+"))
                    ),
                    ChannelConfig.id != channel.id,
                )
            )
            if owner is not None:
                # Another organization holds this handset. The session is
                # genuinely up and genuinely receiving, so the status stays
                # AUTHENTICATED - saying otherwise would tell the operator
                # their phone is dead while it answers customers. The clash is
                # reported separately, worked out when the channel is read.
                #
                # The number is simply not taken from its current owner. A
                # foreseeable conflict, and not a reason to 500 into a caller
                # that does not retry.
                logger.warning(
                    "channel %s paired with %s, which organization %s already holds; "
                    "leaving the number where it is",
                    channel.id,
                    reported,
                    owner,
                )
            else:
                channel.phone_number = reported
                try:
                    await db.commit()
                except IntegrityError:
                    # Lost a race with another pairing.
                    await db.rollback()
                    logger.warning(
                        "could not store %s for channel %s: already taken",
                        reported,
                        channel.id,
                    )
                    # The session is up and already recorded as such. Only
                    # the number could not be stored, and the channel keeps
                    # the one it had.

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
