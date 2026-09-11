"""Knowledge base: documents and hybrid search, scoped to one organization."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import WRITE_ROLES, Tenant, current_org
from app.models import KnowledgeDocument
from app.schemas_tenancy import (
    KnowledgeDocumentCreate,
    KnowledgeDocumentOut,
    RetrievedChunk,
)
from app.services import documents, retrieval

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
    logger.info(
        "indexed %s for %s: %d passage(s), %d table(s), %d page(s)",
        file.filename,
        tenant.id,
        len(stored),
        extracted.tables,
        extracted.pages,
    )
    return {
        "filename": file.filename,
        "kind": extracted.kind,
        "pages": extracted.pages,
        "tables_found": extracted.tables,
        "passages_indexed": len(stored),
        "characters": len(extracted.text),
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


@router.get("/search", response_model=list[RetrievedChunk])
async def search_knowledge(
    q: str = Query(min_length=1, description="What to look for"),
    limit: int = Query(default=4, le=20),
    tenant: Tenant = Depends(current_org),
    db: AsyncSession = Depends(get_db),
):
    """Hybrid search — vector similarity plus keyword overlap — within this tenant."""
    return await retrieval.search(db, tenant.id, q, limit=limit)
