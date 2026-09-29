"""Knowledge base: documents and hybrid search, scoped to one organization."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import Catalogue, KnowledgeDocument, Organization
from app.schemas_tenancy import (
    KnowledgeDocumentCreate,
    KnowledgeDocumentOut,
    RetrievedChunk,
)
from app.services import (
    offers,
    agent_config,
    catalogue,
    document_facts,
    documents,
    media_service,
    retrieval,
    understanding,
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


def _stored_proposal(config: dict) -> tuple[dict, dict]:
    """What earlier documents offered, as (fields, which file said each one).

    Reads the older shape too, where the key held hours alone, so a suggestion
    stored before this widened is carried forward rather than dropped.
    """
    stored = dict(config.get(agent_config.PROPOSED_KEY) or {})
    fields = dict(stored.get("fields") or {})
    sources = dict(stored.get("sources") or {})

    if not fields:
        legacy = config.get(agent_config.PROPOSED_HOURS_KEY) or {}
        if legacy.get("hours"):
            fields = {"business_hours": legacy["hours"]}
            sources = {"business_hours": legacy.get("source") or "an earlier document"}

    return fields, sources


async def _adopt_document_facts(db, organization_id, text: str, source: str) -> dict:
    """Read what this document states about the business and offer it back.

    A handbook already says when the shop is open, what it does and where it
    will travel to. That is the same set of questions the setup form asks, so
    the document is read for them and the answers are offered as a filled-in
    form.

    What is found is written where the form can prefill from it, and nowhere
    else. It is deliberately not written to the live config:

    * Booking reads `business_hours`. Writing it would switch appointments on
      off the back of parsed prose, and the agent would begin offering real
      times to real customers on the strength of a regular expression.
    * A document has no timezone in it, and no handbook ever will. "09:00"
      means nothing until somebody says where they are, and read in the wrong
      zone it is how a Miami customer was offered one in the morning.

    Several documents add up. Each one replaces the fields it states and
    leaves alone the fields it does not, because a price list that says
    nothing about opening hours is not a statement that the shop has none -
    and erasing an earlier document's answer because a later one was silent
    would make uploading a second file a destructive act.
    """
    facts = document_facts.extract(text)
    if not facts:
        return {
            "found": None,
            "proposed": False,
            "fields": [],
            "detail": "Nothing in this document describes your hours, services or "
            "areas, so nothing was filled in for you. It is still searchable, and "
            "the agent will quote from it.",
        }

    organization = await db.get(Organization, organization_id)
    if organization is None:
        return {"found": None, "proposed": False, "fields": [], "detail": "No organization."}

    config = dict(organization.agent_config or {})
    fields, sources = _stored_proposal(config)

    merged = {**fields, **facts}
    problems = agent_config.validate({**config, **merged})
    if problems:
        logger.warning("facts parsed for %s did not validate: %s", organization_id, problems)
        return {"found": None, "proposed": False, "fields": [], "detail": problems[0]}

    for key in facts:
        sources[key] = source

    config[agent_config.PROPOSED_KEY] = {
        "fields": merged,
        "sources": sources,
        "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # The older key would otherwise sit alongside the new one, and the next
    # read would have two answers to choose between.
    config.pop(agent_config.PROPOSED_HOURS_KEY, None)
    organization.agent_config = config
    await db.flush()

    summary = document_facts.describe(facts)
    logger.info("read %s for %s from %s", summary, organization_id, source)

    zone = (getattr(organization, "timezone", None) or "").strip()
    if facts.get("business_hours") and (not zone or zone == "UTC"):
        waiting = (
            "Set your timezone in Where you are first — without one these hours "
            "would be read as UTC. Then open Hours and booking to save them."
        )
    else:
        waiting = "Open Hours and booking to check them and save."

    return {
        "found": summary,
        "proposed": True,
        "fields": sorted(facts.keys()),
        "detail": f"Read from this document: {summary}. Nothing changes until you "
        f"save. {waiting}",
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

    # The same file again is a new version of it, not more of it: what the
    # previous upload of this name left is replaced, or the old prices would
    # go on being retrieved beside the new ones.
    previous = (
        await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == tenant.id,
                KnowledgeDocument.source == file.filename,
            )
        )
    ).scalars().all()
    for old_passage in previous:
        await db.delete(old_passage)
    await db.flush()

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

    photos = await _products_with_photos(db, tenant.id, extracted, file.filename or "document")

    # What the file sells and on what terms, read once by the model and
    # checked against the file's own text, kept for quoting and for the owner
    # to review. Replaces any earlier reading of a file with the same name.
    organization = await db.get(Organization, tenant.id)
    reading = await understanding.read_document(
        file.filename or "document",
        extracted.text,
        getattr(organization, "default_currency", None),
    )
    await _replace_catalogue(db, tenant.id, file.filename or "document", reading)

    await db.flush()

    # Read from the whole extracted document rather than the passages, so
    # hours split across a chunk boundary are not lost to where the splitter
    # happened to cut.
    facts = await _adopt_document_facts(
        db, tenant.id, extracted.text, file.filename or "document"
    )

    logger.info(
        "indexed %s for %s: %d passage(s), %d table(s), %d page(s); hours %s",
        file.filename,
        tenant.id,
        len(stored),
        extracted.tables,
        extracted.pages,
        facts["found"] or "not stated",
    )
    return {
        "filename": file.filename,
        "kind": extracted.kind,
        "pages": extracted.pages,
        "tables_found": extracted.tables,
        "passages_indexed": len(stored),
        "characters": len(extracted.text),
        "from_document": facts,
        # How many priced products were understood, so a shop can see at once
        # whether its table was read - a price list that yields none is one
        # the agent will not be able to quote from.
        "products_found": len(reading["items"]),
        # How it was read and what did not survive the checks, in plain words.
        "catalogue": {
            "read_by": reading["read_by"],
            "items": len(reading["items"]),
            "rules": len(reading["rules"]),
            "left_out": [d["why"] for d in reading["dropped"]][:20],
        },
        # Products whose row in the table carried a picture: the ones the
        # agent can now send a photo of.
        "photos_found": photos,
    }


async def _replace_catalogue(db, organization_id, source: str, reading: dict) -> Catalogue:
    previous = (
        await db.execute(
            select(Catalogue).where(
                Catalogue.organization_id == organization_id, Catalogue.source == source
            )
        )
    ).scalars().all()
    for row in previous:
        await db.delete(row)
    row = Catalogue(
        organization_id=organization_id,
        source=source,
        items=reading["items"],
        rules=reading["rules"],
        dropped=reading["dropped"],
        read_by=reading["read_by"],
        status="read",
    )
    db.add(row)
    await db.flush()
    return row


async def _forget_catalogue(db, organization_id, source: str) -> None:
    rows = (
        await db.execute(
            select(Catalogue).where(
                Catalogue.organization_id == organization_id, Catalogue.source == source
            )
        )
    ).scalars().all()
    for row in rows:
        await db.delete(row)


async def _products_with_photos(db, organization_id, extracted, filename: str) -> int:
    """Store the pictures in a price table as photos of the products beside them.

    Each becomes a catalogue entry carrying the picture, found the same way a
    WhatsApp catalogue product is, so "can I see it?" is answered with the
    photo from the owner's own document. The price stays read from the table:
    these entries are marked, and the quote engine does not read them twice.
    """
    if not extracted.pictures:
        return 0
    items = offers.read_items([(filename, extracted.text)])
    stored = 0
    for picture in extracted.pictures:
        row = picture.row
        matches = [item for item in items if item.name and item.name in row]
        if not matches:
            continue
        # A SKU on the row settles which of two same-named products it is.
        item = next((i for i in matches if i.sku and i.sku in row), matches[0])
        url = media_service.store_picture(picture.data, picture.content_type)
        if not url:
            continue
        document = await retrieval.index_document(
            db,
            organization_id=organization_id,
            title=item.label,
            content=row.replace(" | ", ", "),
            source=filename,
        )
        document.doc_type = "product"
        document.media_urls = [url]
        document.attributes = {
            "price": str(item.price),
            "currency": item.currency or "",
            "photo_from": filename,
        }
        stored += 1
    return stored


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
    await db.flush()
    # The last passage of a file gone: so is what the file was read into.
    if document.source:
        remaining = await db.scalar(
            select(func.count(KnowledgeDocument.id)).where(
                KnowledgeDocument.organization_id == tenant.id,
                KnowledgeDocument.source == document.source,
            )
        )
        if not remaining:
            await _forget_catalogue(db, tenant.id, document.source)
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
    # What the file was read into goes with it: a deleted price list must not
    # go on being quoted.
    await _forget_catalogue(db, tenant.id, source)
    logger.info("removed %d passage(s) from %s for %s", len(found), source, tenant.id)
    return None


# ---------------------------------------------------------------- the reading
def _catalogue_out(row: Catalogue) -> dict:
    return {
        "id": str(row.id),
        "source": row.source,
        "read_by": row.read_by,
        "status": row.status,
        "items": row.items or [],
        "rules": row.rules or [],
        "left_out": [d.get("why") for d in (row.dropped or [])],
        "updated_at": row.updated_at or row.created_at,
    }


@router.get("/catalogue")
async def list_catalogue(
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """What each uploaded file was read into: the products and rules quoted from."""
    rows = (
        await db.execute(
            select(Catalogue)
            .where(Catalogue.organization_id == tenant.id)
            .order_by(Catalogue.created_at.desc())
        )
    ).scalars().all()
    return [_catalogue_out(row) for row in rows]


_ITEM_FIELDS = ("name", "sku", "details", "price", "currency", "sold_as", "holds", "pack",
                "per_measure", "starting")
_RULE_FIELDS = ("topic", "sentence", "place", "min_order", "max_order", "percent", "fee", "free")


def _positive(value, what: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise HTTPException(status_code=422, detail=f"{what} is needed")
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail=f"{what} must be a number") from None
    if number < 0 or (required and number == 0):
        raise HTTPException(status_code=422, detail=f"{what} must be more than nothing")
    return str(number)


@router.patch("/catalogue/{catalogue_id}")
async def correct_catalogue(
    catalogue_id: uuid.UUID,
    payload: dict,
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """The owner's corrections to a reading, and their confirmation of it.

    What they send replaces what was read: the owner is the authority on their
    own prices, so nothing here is checked against the file - only that each
    item has a name and a price, and every figure is a number.
    """
    tenant.require_role(WRITE_ROLES)
    row = await db.get(Catalogue, catalogue_id)
    if row is None or row.organization_id != tenant.id:
        raise HTTPException(status_code=404, detail="Not found")
    if "items" in payload:
        items = []
        for index, raw in enumerate(payload.get("items") or [], start=1):
            if not isinstance(raw, dict):
                continue
            item = {key: raw.get(key) for key in _ITEM_FIELDS}
            item["name"] = str(item.get("name") or "").strip()[:160]
            if not item["name"]:
                raise HTTPException(status_code=422, detail=f"Item {index} needs a name")
            item["price"] = _positive(item.get("price"), f"The price of {item['name']}", required=True)
            item["pack"] = _positive(item.get("pack"), f"The pack size of {item['name']}")
            item["per_measure"] = bool(item.get("per_measure"))
            item["starting"] = bool(item.get("starting"))
            item["id"] = str(raw.get("id") or f"p{index}")
            items.append(item)
        row.items = items
    if "rules" in payload:
        rules = []
        for raw in payload.get("rules") or []:
            if not isinstance(raw, dict) or not str(raw.get("sentence") or "").strip():
                continue
            rule = {key: raw.get(key) for key in _RULE_FIELDS}
            for key in ("min_order", "max_order", "percent", "fee"):
                rule[key] = _positive(rule.get(key), key.replace("_", " "))
            rule["free"] = bool(rule.get("free"))
            rules.append(rule)
        row.rules = rules
    if payload.get("status") in ("read", "confirmed"):
        row.status = payload["status"]
    elif "items" in payload or "rules" in payload:
        # A correction is a confirmation of everything else as it stands.
        row.status = "confirmed"
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return _catalogue_out(row)


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
