"""The agent studying a business's document before its first customer.

A document says "Annual plans are billed at ten months"; a customer types
"yrly price??" or "saal ka kitna". Nothing in the first shares a word with the
second, so a search on the customer's words alone misses the one passage that
answers them, and the agent says it doesn't know.

So when a document is uploaded, the model reads each passage and writes down
the ways real customers would ask for what it says - short, casual,
misspelled, in the languages customers use. Those questions are kept beside
the passage and searched along with it.

Why this is safe: the questions only decide *which* passage is found. What
reaches the reply is still the passage itself, in the document's own words,
checked by the same price and promise guard as before. A question the model
imagined badly can at worst make a passage easier to find; it can never put a
fact in front of a customer that the owner did not write.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

logger = logging.getLogger(__name__)

# Per passage. Enough to cover the usual phrasings without drowning the
# passage's own words in the search.
MAX_QUESTIONS = 8
MAX_LENGTH = 140
# Passages per call: small enough to answer quickly, large enough that a
# typical document is one or two calls.
BATCH = 10
TIMEOUT_SECONDS = 25.0

STUDY_PROMPT = """You are preparing a WhatsApp sales assistant for a business. Below are
numbered passages from the business's own document. For EACH passage, write the
questions real customers would send on WhatsApp that this passage answers.

Write them the way customers actually type:
- short and casual ("hw much", "yrly price?", "u deliver karachi?"), with the typos and
  shorthand people really use;
- a few in plain full sentences;
- if the business looks like it serves Pakistan or South Asia, include one or two in
  Roman Urdu; for other regions, include the local language customers would likely use.

Rules:
- Only questions this passage really answers. Never add facts, prices or promises.
- Up to {limit} questions per passage.
- Return strict JSON: {{"passages": [{{"n": 1, "questions": ["...", "..."]}}, ...]}}

PASSAGES:
{passages}
"""


def _clean(questions: Iterable, limit: int = MAX_QUESTIONS) -> list[str]:
    kept: list[str] = []
    for question in questions or []:
        if not isinstance(question, str):
            continue
        text = " ".join(question.split())[:MAX_LENGTH]
        if len(text) >= 3 and text.lower() not in {k.lower() for k in kept}:
            kept.append(text)
        if len(kept) >= limit:
            break
    return kept


async def _batch(passages: list[str]) -> list[list[str]]:
    from app.services import understanding

    numbered = "\n\n".join(f"[{n}] {text[:2500]}" for n, text in enumerate(passages, start=1))
    answer = await understanding.structured(
        STUDY_PROMPT.format(limit=MAX_QUESTIONS, passages=numbered), TIMEOUT_SECONDS
    )
    found: list[list[str]] = [[] for _ in passages]
    rows = (answer or {}).get("passages")
    if not isinstance(rows, list):
        return found
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("n")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(passages):
            found[index] = _clean(row.get("questions"))
    return found


async def questions_for(passages: list[str]) -> list[list[str]]:
    """How customers would ask for each passage, one list per passage.

    Never raises: a document that could not be studied is searched exactly as
    it always was.
    """
    if not passages:
        return []
    groups = [passages[i : i + BATCH] for i in range(0, len(passages), BATCH)]
    try:
        results = await asyncio.gather(*(_batch(group) for group in groups), return_exceptions=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not study the document: %s", exc)
        return [[] for _ in passages]
    found: list[list[str]] = []
    for group, result in zip(groups, results):
        if isinstance(result, Exception):
            logger.warning("could not study part of the document: %s", result)
            found.extend([] for _ in group)
        else:
            found.extend(result)
    return found


def asked_as(document) -> list[str]:
    """The questions a passage was studied for, if it was."""
    value = (getattr(document, "attributes", None) or {}).get("asked_as")
    return [q for q in value if isinstance(q, str)] if isinstance(value, list) else []


def searchable(title: str, content: str, questions: Iterable[str]) -> str:
    """What a passage is found by: its own words, then how customers ask for it."""
    questions = list(questions or [])
    if not questions:
        return f"{title}\n{content}"
    return f"{title}\n{content}\nCustomers ask: " + " | ".join(questions)


async def study_existing(db, organization_id=None, limit: int | None = None) -> int:
    """Study passages uploaded before this existed. Returns how many were studied.

    Only passages of text from documents: a product imported from a catalogue
    is found by its name and fields, and a taught answer is already written as
    the question. A passage the AI could not study is left as it was.
    """
    from sqlalchemy import select

    from app.models import KnowledgeDocument
    from app.services.embeddings import embed

    statement = select(KnowledgeDocument).where(KnowledgeDocument.doc_type == "policy")
    if organization_id is not None:
        statement = statement.where(KnowledgeDocument.organization_id == organization_id)
    from app.services.taught import SOURCE as TAUGHT

    rows = [
        row
        for row in (await db.execute(statement)).scalars().all()
        if not asked_as(row) and row.source != TAUGHT
    ]
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return 0

    studied = 0
    for start in range(0, len(rows), BATCH):
        group = rows[start : start + BATCH]
        found = await questions_for([row.content for row in group])
        for row, questions in zip(group, found):
            if not questions:
                continue
            row.attributes = {**(row.attributes or {}), "asked_as": questions}
            row.embedding, row.embedding_model = await embed(
                searchable(row.title, row.content, questions)
            )
            studied += 1
        await db.flush()
    return studied
