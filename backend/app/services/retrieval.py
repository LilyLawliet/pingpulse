"""Hybrid retrieval over an organization's knowledge base.

Two signals are combined: vector similarity for meaning, and keyword overlap
for exact terms like part numbers and product names that embeddings blur.

Tenancy: `organization_id` is a WHERE clause on the candidate query, so another
tenant's rows are never loaded into memory, let alone scored. There is no code
path that retrieves without it — the parameter is required.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeDocument
from app.schemas_tenancy import RetrievedChunk
from app.services.embeddings import cosine_similarity, embed

logger = logging.getLogger(__name__)

VECTOR_WEIGHT = 0.65
KEYWORD_WEIGHT = 0.35

STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "are", "have", "has",
    "how", "what", "does", "can", "want", "need", "would", "like", "please",
    "this", "that", "them", "they", "not", "any", "all", "from", "get", "one",
    "was", "were", "there", "about", "into", "than", "then", "when", "who",
}


def stem(word: str) -> str:
    """Fold the most common English inflections so "returns" matches "return".

    Deliberately conservative — a full stemmer would collide unrelated product
    names, which matters more here than recall.
    """
    for suffix in ("ies", "ing", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]{3,}", (text or "").lower())
    return {stem(word) for word in words if word not in STOPWORDS}


def keyword_score(query: str, document: str) -> float:
    """Share of the query's meaningful terms that appear in the document."""
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    document_terms = _terms(document)
    return len(query_terms & document_terms) / len(query_terms)


async def search(
    db: AsyncSession,
    organization_id: uuid.UUID,
    query: str,
    limit: int = 4,
    min_score: float = 0.05,
    doc_type: str | None = None,
) -> list[RetrievedChunk]:
    """Best chunks for this query, restricted to one organization.

    `doc_type` narrows the corpus. Policy answers pass "policy" so that a long
    product description cannot be returned as the answer to "how do I pay?" —
    products are surfaced separately, as compact rows with pictures.
    """
    if not query.strip():
        return []

    statement = select(KnowledgeDocument).where(
        KnowledgeDocument.organization_id == organization_id
    )
    if doc_type:
        statement = statement.where(KnowledgeDocument.doc_type == doc_type)
    result = await db.execute(statement)
    documents = result.scalars().all()
    if not documents:
        return []

    query_vector, _ = await embed(query, for_query=True)

    scored: list[RetrievedChunk] = []
    for document in documents:
        vector = cosine_similarity(query_vector, document.embedding)
        # Cosine runs -1..1; clamp so a negative cannot cancel a keyword hit.
        vector = max(0.0, vector)
        keyword = keyword_score(query, f"{document.title}\n{document.content}")
        combined = VECTOR_WEIGHT * vector + KEYWORD_WEIGHT * keyword
        if combined >= min_score:
            scored.append(
                RetrievedChunk(
                    id=document.id,
                    title=document.title,
                    content=document.content,
                    score=round(combined, 4),
                    vector_score=round(vector, 4),
                    keyword_score=round(keyword, 4),
                )
            )

    scored.sort(key=lambda chunk: chunk.score, reverse=True)
    return scored[:limit]


def as_prompt_block(chunks: list[RetrievedChunk]) -> str:
    """Render retrieved chunks for the prompt, or an empty string if none."""
    if not chunks:
        return ""
    lines = ["=== KNOWLEDGE BASE (use only if relevant) ==="]
    for chunk in chunks:
        lines.append(f"[{chunk.title}] {chunk.content}")
    return "\n".join(lines)


async def index_document(
    db: AsyncSession,
    organization_id: uuid.UUID,
    title: str,
    content: str,
    source: str | None = None,
) -> KnowledgeDocument:
    """Embed and store one document against an organization."""
    vector, model = await embed(f"{title}\n{content}")
    document = KnowledgeDocument(
        organization_id=organization_id,
        title=title,
        content=content,
        source=source,
        embedding=vector,
        embedding_model=model,
    )
    db.add(document)
    await db.flush()
    return document
