"""Learning a shop's own facts and its own voice from what it has already said.

Every endpoint here follows the same shape as the catalogue import before it:
nothing is written until a person has seen exactly what would be written. That
is not caution for its own sake. Both halves of this feature change how the
agent talks to customers of a business that is already running, and a change
like that arriving silently on a deploy is the difference between an upgrade
and an outage.

So: previews are derived and returned, never stored. Saving is a separate,
explicit call carrying the text the person actually approved — which may be
text they edited, because a description of somebody's own voice is the sort of
thing they will want to correct.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import CRMContact, KnowledgeDocument, LearnedAnswer, Organization
from app.services import learning, retrieval, taught, whatsapp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/learning", tags=["learning"])

MAX_STYLE_CHARS = 2000
MAX_EXAMPLE_CHARS = 300
MAX_EXAMPLES = 6


async def _material(db: AsyncSession, organization_id) -> learning.Material:
    """Read everything this tenant can safely learn from, once.

    A Twilio tenant has no handset history, but it does have whatever a person
    typed in the dashboard — so this is not gated on the WhatsApp Web bridge.
    Less material, still material.
    """
    channel = await whatsapp.active_channel(db, organization_id)
    paired = channel is not None and whatsapp.provider_of(channel) == whatsapp.QR_SESSION
    return await learning.gather(
        db, organization_id, channel, chats=None if paired else []
    )


def _voice_of(organization: Organization) -> dict | None:
    if not organization.voice_style and not organization.voice_examples:
        return None
    return {
        "style": organization.voice_style or "",
        "examples": list(organization.voice_examples or []),
        "learned_at": organization.voice_learned_at,
    }


@router.get("/sources")
async def sources(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """What there is to learn from, before spending a model call on it.

    Cheap and read-only, so the dashboard can show a shop where it stands
    without committing them to anything.
    """
    material = await _material(db, tenant.id)
    organization = await db.get(Organization, tenant.id)

    learned = await db.scalar(
        select(func.count(KnowledgeDocument.id)).where(
            KnowledgeDocument.organization_id == tenant.id,
            KnowledgeDocument.source == learning.LEARNED_SOURCE,
        )
    )

    return {
        "replies": {
            "total": len(material.replies),
            "from_phone": material.from_phone,
            "from_dashboard": material.from_dashboard,
        },
        "exchanges": len(material.exchanges),
        "chats_seen": material.chats_seen,
        # Shown rather than hidden: a shop whose agent has been running for
        # months will see a large number here, and the honest explanation is
        # that those messages may be the agent's own and cannot be told apart.
        "skipped_after_cutoff": material.skipped_after_cutoff,
        "cutoff": material.cutoff,
        "enough_for_voice": len(material.replies) >= learning.MIN_REPLIES_FOR_VOICE,
        "minimum_replies": learning.MIN_REPLIES_FOR_VOICE,
        "voice": _voice_of(organization),
        "learned_facts": learned or 0,
    }


# ------------------------------------------------------------------ voice
@router.post("/voice/preview")
async def preview_voice(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Draft a description of how this shop writes. Stores nothing."""
    material = await _material(db, tenant.id)
    draft = await learning.derive_voice(material)

    if draft is None:
        if len(material.replies) < learning.MIN_REPLIES_FOR_VOICE:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Only {len(material.replies)} message(s) here were written by a "
                    f"person at the shop, and {learning.MIN_REPLIES_FOR_VOICE} are "
                    "needed. Reconnect the phone to bring across more history, or "
                    "reply to a few customers from the dashboard first."
                ),
            )
        raise HTTPException(
            status_code=503,
            detail="The model could not be reached, so no voice was drafted.",
        )

    return draft


@router.put("/voice")
async def save_voice(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Apply a voice to every reply this shop's agent writes from now on."""
    tenant.require_role(WRITE_ROLES)

    style = (payload.get("style") or "").strip()
    raw_examples = payload.get("examples") or []
    if not isinstance(raw_examples, list):
        raise HTTPException(status_code=422, detail="Examples must be a list")
    if not style and not raw_examples:
        raise HTTPException(status_code=422, detail="There is nothing here to save")

    examples: list[str] = []
    for entry in raw_examples[:MAX_EXAMPLES]:
        example = str(entry).strip()
        if not example:
            continue
        # Refused rather than trimmed, and the reason is given. An example is
        # reproduced in front of customers it was never written for, so a
        # figure inside one becomes a price quoted for the wrong thing.
        if learning.FIGURES.search(example):
            raise HTTPException(
                status_code=422,
                detail=(
                    "An example cannot contain a price or a number: it gets shown to "
                    "customers it was not written for. Put figures in your catalogue "
                    "or price list instead — the agent reads them from there."
                ),
            )
        examples.append(example[:MAX_EXAMPLE_CHARS])

    organization = await db.get(Organization, tenant.id)
    organization.voice_style = style[:MAX_STYLE_CHARS] or None
    organization.voice_examples = examples
    organization.voice_learned_at = datetime.now(timezone.utc)
    await db.flush()

    logger.info(
        "voice applied for %s (%d example(s))", organization.name, len(examples)
    )
    return {"style": organization.voice_style, "examples": examples, "applied": True}


@router.delete("/voice", status_code=204)
async def clear_voice(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Back to the agent's default voice, in one action."""
    tenant.require_role(WRITE_ROLES)

    organization = await db.get(Organization, tenant.id)
    organization.voice_style = None
    organization.voice_examples = []
    organization.voice_learned_at = None
    await db.flush()
    return None


# ------------------------------------------------------------------ facts
@router.post("/facts/preview")
async def preview_facts(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Exactly what importing would add to the knowledge base. Stores nothing."""
    material = await _material(db, tenant.id)
    if not material.exchanges:
        raise HTTPException(
            status_code=422,
            detail=(
                "There are no conversations here where a person at the shop answered "
                "a customer. Reconnect the phone to bring across past chats."
            ),
        )

    facts = await learning.extract_facts(material)
    return {"facts": facts, "from_exchanges": len(material.exchanges)}


@router.post("/facts", status_code=201)
async def import_facts(
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Write the facts a person picked into the knowledge base.

    Replaces everything previously imported this way rather than adding to it.
    A shop that re-runs this after changing its delivery charge means the new
    answer, and leaving the old one beside it gives the agent two answers to
    one question and no way to choose.
    """
    tenant.require_role(WRITE_ROLES)

    entries = payload.get("facts")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=422, detail="No facts were chosen")

    chosen: list[tuple[str, str]] = []
    for entry in entries[: learning.MAX_FACTS]:
        if not isinstance(entry, dict):
            continue
        fact = str(entry.get("fact") or "").strip()
        topic = str(entry.get("topic") or "").strip() or "From conversations"
        if fact:
            chosen.append((topic[:120], fact[:600]))

    if not chosen:
        raise HTTPException(status_code=422, detail="No facts were chosen")

    previous = (
        await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == tenant.id,
                KnowledgeDocument.source == learning.LEARNED_SOURCE,
            )
        )
    ).scalars().all()
    for stale in previous:
        await db.delete(stale)
    await db.flush()

    for topic, fact in chosen:
        await retrieval.index_document(
            db,
            organization_id=tenant.id,
            title=topic,
            content=fact,
            source=learning.LEARNED_SOURCE,
        )
    await db.flush()

    logger.info(
        "learned %d fact(s) from past conversations for %s (replacing %d)",
        len(chosen),
        tenant.id,
        len(previous),
    )
    return {"imported": len(chosen), "replaced": len(previous), "source": learning.LEARNED_SOURCE}


# ------------------------------------------------ answers the team gave
class AnswerIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=1, max_length=1500)


def _answer_row(row: LearnedAnswer, contact: CRMContact | None) -> dict:
    return {
        "id": str(row.id),
        "status": row.status,
        "question": row.question,
        "answer": row.answer,
        "asked_at": row.asked_at.isoformat() if row.asked_at else None,
        "answered_at": row.answered_at.isoformat() if row.answered_at else None,
        "taught_at": row.taught_at.isoformat() if row.taught_at else None,
        "customer": (contact.name or contact.phone_number) if contact else None,
        "contact_id": str(row.contact_id) if row.contact_id else None,
        "watch_out": taught.watch_out(row.answer or "", contact.name if contact else None)
        if row.status == taught.SUGGESTED
        else [],
    }


async def _answer(db, tenant: Tenant, answer_id: uuid.UUID) -> LearnedAnswer:
    row = (
        await db.execute(
            select(LearnedAnswer).where(
                LearnedAnswer.id == answer_id, LearnedAnswer.organization_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.get("/answers")
async def list_answers(tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)):
    """Questions the agent couldn't answer, the team's replies, and what it was taught."""
    rows = (
        await db.execute(
            select(LearnedAnswer, CRMContact)
            .join(CRMContact, CRMContact.id == LearnedAnswer.contact_id, isouter=True)
            .where(
                LearnedAnswer.organization_id == tenant.id,
                LearnedAnswer.status != taught.DISMISSED,
            )
            .order_by(LearnedAnswer.asked_at.desc())
            .limit(200)
        )
    ).all()
    out = {"suggested": [], "waiting": [], "taught": []}
    for row, contact in rows:
        out.setdefault(row.status, []).append(_answer_row(row, contact))
    return out


@router.post("/answers", status_code=201)
async def teach_new(
    payload: AnswerIn, tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)
):
    """Teach an answer typed in by hand."""
    tenant.require_role(WRITE_ROLES)
    row = LearnedAnswer(organization_id=tenant.id, question=payload.question.strip(), status=taught.WAITING)
    db.add(row)
    await db.flush()
    await taught.teach(db, row, payload.question, payload.answer)
    await db.commit()
    return _answer_row(row, None)


@router.post("/answers/{answer_id}/teach")
async def teach_answer(
    answer_id: uuid.UUID,
    payload: AnswerIn,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Approve an answer - as the team wrote it, or edited - so the agent knows it."""
    tenant.require_role(WRITE_ROLES)
    row = await _answer(db, tenant, answer_id)
    await taught.teach(db, row, payload.question, payload.answer)
    await db.commit()
    return _answer_row(row, None)


@router.post("/answers/{answer_id}/dismiss")
async def dismiss_answer(
    answer_id: uuid.UUID, tenant: Tenant = Depends(current_org), db: AsyncSession = Depends(get_db)
):
    """Set a suggestion aside, or take a taught answer back out of what the agent knows."""
    tenant.require_role(WRITE_ROLES)
    row = await _answer(db, tenant, answer_id)
    await taught.untaught(db, row)
    await db.commit()
    return {"id": str(row.id), "status": row.status}
