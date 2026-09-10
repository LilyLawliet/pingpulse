"""Text embeddings, with a deterministic offline fallback.

Gemini produces the real vectors. When no key is configured or the API is
unreachable, a hashed bag-of-words vector is used instead: far weaker
semantically, but deterministic and dependency-free, so indexing and retrieval
still work in tests and during an outage. The model actually used is recorded
on each document so mixed-provenance vectors are visible rather than silent.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
)

FALLBACK_MODEL = "hashed-bow-256"
FALLBACK_DIMENSIONS = 256


def _tokenize(text: str) -> list[str]:
    """Tokens for the offline vector, stemmed so inflections share a bucket.

    Without this the fallback treats "return" and "returns" as unrelated, which
    real embeddings would not.
    """
    from app.services.retrieval import stem

    return [stem(word) for word in re.findall(r"[a-z0-9]{2,}", (text or "").lower())]


def hashed_embedding(text: str, dimensions: int = FALLBACK_DIMENSIONS) -> list[float]:
    """A stable bag-of-words vector. Same text always gives the same vector."""
    vector = [0.0] * dimensions
    for token in _tokenize(text):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    return normalise(vector)


def normalise(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in vector))
    if length == 0:
        return vector
    return [value / length for value in vector]


def cosine_similarity(left: list[float] | None, right: list[float] | None) -> float:
    """0.0 for anything missing or dimension-mismatched, never an exception."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    return max(-1.0, min(1.0, dot))


async def _gemini_embed(text: str, api_key: str) -> list[float]:
    url = GEMINI_EMBED_URL.format(model=settings.embedding_model)
    payload = {
        "model": f"models/{settings.embedding_model}",
        "content": {"parts": [{"text": text}]},
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(url, json=payload, params={"key": api_key})
        response.raise_for_status()
        data = response.json()

    values = (data.get("embedding") or {}).get("values")
    if not values:
        raise RuntimeError(f"unexpected embedding response: {str(data)[:160]}")
    return normalise([float(v) for v in values])


async def embed(text: str) -> tuple[list[float], str]:
    """Return (vector, model_name). Rotates keys, then falls back locally."""
    keys = settings.gemini_api_keys
    for index, api_key in enumerate(keys, start=1):
        try:
            return await _gemini_embed(text, api_key), settings.embedding_model
        except Exception as exc:  # noqa: BLE001
            last = exc
            if index < len(keys):
                continue
            logger.warning("embedding failed (%s); using the offline fallback", last)

    if not keys:
        logger.debug("no Gemini key configured; using the offline embedding")
    return hashed_embedding(text), FALLBACK_MODEL
