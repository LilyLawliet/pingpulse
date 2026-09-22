"""Knowledge base: documents and hybrid search, scoped to one organization."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import KnowledgeDocument, Organization
from app.schemas_tenancy import (
    KnowledgeDocumentCreate,
    KnowledgeDocumentOut,
    RetrievedChunk,
)
from app.services import (
    agent_config,
    catalogue,
    documents,
    opening_hours,
    retrieval,
    whatsapp,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

# Large enough for a photo-heavy catalogue, small enough that a mis-drag does
# not put a video through the extractor.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@router.post("/documents", response_model=KnowledgeDocumentOut, status_code=201)
async def add_document(
    payload: KnowledgeDocumentCreate,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Embed and store a document against the active organization."""
    tenant.require_role(WRITE_ROLES)
    document = await retrieval.index_document(
        db,
        organization_id=tenant.id,
        title=payload.title,
        content=payload.content,
        source=payload.source,
    )
    await db.refresh(document)
    return document


async def _adopt_opening_hours(db, organization_id, text: str) -> dict:
    """Take the opening hours out of an uploaded document and use them.

    A shop uploads the sheet that says when it is open, and until now that
    sentence reached the knowledge base and nothing else. The agent could
    recite the hours to a customer while `business_hours` - the field the
    booking code actually reads - stayed empty, so booking stayed off and the
    agent had to hand every appointment to a person. The document said one
    thing and the product did another.

    Three things stop this writing hours it should not:

    * Hours already configured are never overwritten. Somebody set those by
      hand, and a document uploaded a year later must not quietly move them.
    * Nothing is applied until the timezone is set. "09:00" with no zone is
      09:00 UTC, which is how a Miami shop ended up with an appointment at one
      in the morning. Hours without a zone are worse than no hours.
    * The result goes through the same validation as the settings form. One
      writer, one set of rules.

    Every outcome is reported back to the uploader in plain words, because a
    setup step that happens invisibly is one nobody can tell has not happened.
    """
    found = opening_hours.parse(text)
    if not found:
        return {
            "found": None,
            "applied": False,
            "detail": "This document does not state opening hours, so booking stays off "
            "until you set them. Until then the agent hands anyone asking for an "
            "appointment to a person and alerts you.",
        }

    summary = opening_hours.describe(found)
    organization = await db.get(Organization, organization_id)
    if organization is None:
        return {"found": summary, "applied": False, "detail": "No organization to apply them to."}

    config = dict(organization.agent_config or {})
    if config.get("business_hours"):
        same = config["business_hours"] == found
        return {
            "found": summary,
            "applied": False,
            "detail": (
                "These match the hours you already have."
                if same
                else "You already have opening hours set, so these were left alone. "
                "Change them in Setup if the document is the newer one."
            ),
        }

    zone = (getattr(organization, "timezone", None) or "").strip()
    if not zone or zone == "UTC":
        return {
            "found": summary,
            "applied": False,
            "detail": f"Found {summary}, but your timezone is not set yet — these would be "
            "read as UTC and book people in the middle of the night. Set your "
            "timezone in Setup and upload this again.",
        }

    problems = agent_config.validate({**config, "business_hours": found})
    if problems:
        logger.warning("parsed hours for %s did not validate: %s", organization_id, problems)
        return {"found": summary, "applied": False, "detail": problems[0]}

    config["business_hours"] = found
    organization.agent_config = config
    await db.flush()
    logger.info("adopted opening hours for %s from an upload: %s", organization_id, summary)
    return {
        "found": summary,
        "applied": True,
        "detail": f"Opening hours were read from this document and saved: {summary} "
        f"({zone}). The agent can offer appointments now.",
    }


@router.post("/upload", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form(default="policy"),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Read a PDF, Word or text file and index it against this organization.

    The whole point of the feature: a shop's catalogue already exists as a file,
    and onboarding them should not mean retyping it.

    The file is read, split into passages and embedded one passage at a time.
    A single vector for a forty-page document points at the average of
    everything in it, which is to say at nothing.

    Errors are returned as 422 with the reason in plain words, because the
    person hitting them is a shop owner uploading their own price list, not an
    engineer reading a stack trace.
    """
    tenant.require_role(WRITE_ROLES)

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{file.filename} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    if not data:
        raise HTTPException(status_code=422, detail=f"{file.filename} is empty")

    try:
        extracted = documents.extract(file.filename or "upload", data)
    except (documents.UnsupportedDocument, documents.UnreadableDocument) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    passages = documents.chunk(extracted.text)
    if not passages:
        raise HTTPException(status_code=422, detail=f"{file.filename} had no readable text")

    stored = []
    for index, passage in enumerate(passages):
        document = await retrieval.index_document(
            db,
            organization_id=tenant.id,
            title=documents.title_for(file.filename or "document", index, len(passages)),
            content=passage,
            source=file.filename,
        )
        document.doc_type = doc_type
        stored.append(document)

    await db.flush()

    # Read from the whole extracted document rather than the passages, so
    # hours split across a chunk boundary are not lost to where the splitter
    # happened to cut.
    hours = await _adopt_opening_hours(db, tenant.id, extracted.text)

    logger.info(
        "indexed %s for %s: %d passage(s), %d table(s), %d page(s); hours %s",
        file.filename,
        tenant.id,
        len(stored),
        extracted.tables,
        extracted.pages,
        hours["found"] or "not stated",
    )
    return {
        "filename": file.filename,
        "kind": extracted.kind,
        "pages": extracted.pages,
        "tables_found": extracted.tables,
        "passages_indexed": len(stored),
        "characters": len(extracted.text),
        "opening_hours": hours,
    }


@router.get("/documents", response_model=list[KnowledgeDocumentOut])
async def list_documents(
    limit: int = Query(default=100, le=500),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.organization_id == tenant.id)
        .order_by(KnowledgeDocument.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentOut)
async def get_document(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.organization_id == tenant.id,
        )
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    tenant.require_role(WRITE_ROLES)
    result = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.organization_id == tenant.id,
        )
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await db.delete(document)
    return None


@router.get("/sources")
async def list_sources(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """The files this organization has uploaded, one row each.

    A file becomes as many rows as it has passages, which is right for
    retrieval and wrong for a person looking at what they uploaded: they sent
    one price list, not eleven. Grouped back together here so the dashboard can
    show what they actually did.
    """
    rows = await db.execute(
        select(
            KnowledgeDocument.source,
            func.count(KnowledgeDocument.id),
            func.max(KnowledgeDocument.created_at),
        )
        .where(
            KnowledgeDocument.organization_id == tenant.id,
            KnowledgeDocument.source.is_not(None),
        )
        .group_by(KnowledgeDocument.source)
        .order_by(func.max(KnowledgeDocument.created_at).desc())
    )
    return [
        {"source": source, "passages": passages, "added_at": added_at}
        for source, passages, added_at in rows.all()
    ]


@router.delete("/sources", status_code=204)
async def delete_source(
    source: str = Query(min_length=1, description="The filename to remove"),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Remove every passage that came from one file.

    Deleting a file passage by passage from the dashboard would be a dozen
    requests, and half-deleting a price list leaves the agent quoting from the
    half that survived — which is worse than either extreme.
    """
    tenant.require_role(WRITE_ROLES)
    result = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.organization_id == tenant.id,
            KnowledgeDocument.source == source,
        )
    )
    found = result.scalars().all()
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nothing from that file")
    for document in found:
        await db.delete(document)
    logger.info("removed %d passage(s) from %s for %s", len(found), source, tenant.id)
    return None


CATALOGUE_SOURCE = "WhatsApp catalogue"


@router.get("/readiness")
async def knowledge_readiness(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """What this agent actually knows, and what would most improve it.

    Three places its answers can come from, in descending order of how specific
    they let it be:

      1. the shop's WhatsApp Business catalogue, if the paired account has one;
      2. documents they have uploaded;
      3. the description of the business and how it should sell.

    The third is never empty — it is required to create an organization — so an
    agent is always able to hold a conversation. What the first two add is the
    ability to quote a price without inventing one, which is the difference
    between a demo and a shop.

    Reported rather than enforced. A shop with a good description and no files
    is working, not broken, and telling them otherwise would be wrong.
    """
    rows = await db.execute(
        select(
            func.count(KnowledgeDocument.id),
            func.count(func.distinct(KnowledgeDocument.source)),
        ).where(KnowledgeDocument.organization_id == tenant.id)
    )
    passages, files = rows.one()

    organization = tenant.organization
    description = (organization.product_rules or "").strip()

    channel = await whatsapp.active_channel(db, tenant.id)
    found = None
    if channel is not None and whatsapp.provider_of(channel) == whatsapp.QR_SESSION:
        found = await catalogue.read(channel)

    # Ordered so the dashboard can render the first thing worth doing next.
    if passages:
        status, advice = "ready", "Your agent is answering from the files you uploaded."
    elif found is not None and found.available:
        status, advice = (
            "catalogue",
            "Your WhatsApp catalogue is readable — import it and the agent can quote from it.",
        )
    elif len(description) > 120:
        status, advice = (
            "described",
            "Your agent is working from your description. Upload a price list and it can quote exact prices.",
        )
    else:
        status, advice = (
            "thin",
            "Add what you sell, or upload a price list, so the agent has something to quote.",
        )

    return {
        "status": status,
        "advice": advice,
        "documents": {"files": files or 0, "passages": passages or 0},
        "description": {"characters": len(description)},
        "catalogue": {
            # None means we did not ask: a Twilio tenant has no paired account,
            # so "no catalogue" would be a misleading thing to report.
            "checked": found is not None,
            "reachable": found.reachable if found else False,
            "business_account": found.business if found else False,
            "products": len(found.products) if found else 0,
            "truncated": found.truncated if found else False,
        },
    }


@router.get("/catalogue/preview")
async def catalogue_preview(
    scale: str = Query(default=catalogue.DEFAULT_SCALE),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Exactly what importing the catalogue would write, before it writes it.

    WhatsApp reports a price as an integer and does not say what scale it is
    on. Guessing is how an agent quotes a hundredth of the real price with
    complete confidence, so the guess is shown to a person once instead.
    """
    channel = await whatsapp.active_channel(db, tenant.id)
    if channel is None or whatsapp.provider_of(channel) != whatsapp.QR_SESSION:
        raise HTTPException(
            status_code=422,
            detail="A catalogue can only be read from a WhatsApp Web connection",
        )

    found = await catalogue.read(channel)
    if not found.reachable:
        raise HTTPException(status_code=503, detail="The WhatsApp bridge did not answer")
    if not found.available:
        raise HTTPException(
            status_code=404,
            detail=(
                "This WhatsApp account has no product catalogue. Upload your price "
                "list instead, or add one in WhatsApp Business."
            ),
        )

    return {
        "products": catalogue.preview(found, scale),
        "truncated": found.truncated,
        "scales": list(catalogue.SCALES),
    }


@router.post("/catalogue/import", status_code=201)
async def catalogue_import(
    scale: str = Query(default=catalogue.DEFAULT_SCALE),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Index the catalogue, replacing anything imported from it before.

    Replaced rather than added to: a catalogue is a current statement of what a
    shop sells, and leaving last week's prices beside this week's gives the
    agent two answers to the same question.
    """
    tenant.require_role(WRITE_ROLES)

    channel = await whatsapp.active_channel(db, tenant.id)
    if channel is None or whatsapp.provider_of(channel) != whatsapp.QR_SESSION:
        raise HTTPException(
            status_code=422,
            detail="A catalogue can only be read from a WhatsApp Web connection",
        )

    found = await catalogue.read(channel)
    if not found.available:
        raise HTTPException(status_code=404, detail="No catalogue to import")

    previous = (
        await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == tenant.id,
                KnowledgeDocument.source == CATALOGUE_SOURCE,
            )
        )
    ).scalars().all()
    for stale in previous:
        await db.delete(stale)
    await db.flush()

    for product in found.products:
        document = await retrieval.index_document(
            db,
            organization_id=tenant.id,
            title=product.get("name") or "Product",
            content=catalogue.as_passage(product, scale),
            source=CATALOGUE_SOURCE,
        )
        document.doc_type = "product"
        if product.get("images"):
            document.media_urls = list(product["images"])

    await db.flush()
    logger.info(
        "imported %d product(s) from the WhatsApp catalogue for %s (replacing %d)",
        len(found.products),
        tenant.id,
        len(previous),
    )
    return {
        "imported": len(found.products),
        "replaced": len(previous),
        "truncated": found.truncated,
    }


@router.get("/search", response_model=list[RetrievedChunk])
async def search_knowledge(
    q: str = Query(min_length=1, description="What to look for"),
    limit: int = Query(default=4, le=20),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Hybrid search — vector similarity plus keyword overlap — within this tenant."""
    return await retrieval.search(db, tenant.id, q, limit=limit)
