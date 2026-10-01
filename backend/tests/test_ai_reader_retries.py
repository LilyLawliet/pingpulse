"""An AI call that is merely busy is waited out, not reported as unavailable.

A document upload sends its reading and its study in the same second, and a
free Groq key answers that burst with 429. Each key used to get one try, so
the file was read by the pattern reader alone and the page said the AI was
not available.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import understanding

_REQUEST = httpx.Request("POST", "https://example.invalid")


def _answer(status: int, body=None, headers=None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, headers=headers or {}, request=_REQUEST)


def _read(body):
    return body["text"]


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(understanding.asyncio, "sleep", instant)


@pytest.mark.asyncio
async def test_a_busy_key_is_asked_again_after_the_wait():
    calls = []

    async def send(_client, key):
        calls.append(key)
        if len(calls) == 1:
            return _answer(429, headers={"retry-after": "1"})
        return _answer(200, {"text": '{"items": []}'})

    found = await understanding._post_json("Groq", ["k1"], send, _read, 30)

    assert found == {"items": []}
    assert calls == ["k1", "k1"]


@pytest.mark.asyncio
async def test_a_key_that_fails_moves_on_to_the_next_one():
    calls = []

    async def send(_client, key):
        calls.append(key)
        if key == "k1":
            return _answer(401, {"error": "invalid key"})
        return _answer(200, {"text": '{"ok": true}'})

    found = await understanding._post_json("Groq", ["k1", "k2"], send, _read, 30)

    assert found == {"ok": True}
    assert calls == ["k1", "k2"]


@pytest.mark.asyncio
async def test_every_key_failing_is_still_none():
    async def send(_client, _key):
        return _answer(429)

    assert await understanding._post_json("Groq", ["k1", "k2"], send, _read, 30) is None


def test_the_default_groq_model_is_not_the_retired_one():
    from app.config import Settings

    assert Settings.model_fields["groq_model"].default != "llama-3.3-70b-versatile"
    assert understanding.groq_reasoning("openai/gpt-oss-120b") == {"reasoning_effort": "low"}
    assert understanding.groq_reasoning("some-other-model") == {}


def test_the_fallback_never_reads_out_a_line_written_to_the_agent():
    from types import SimpleNamespace

    from app.services import sales_policy

    passage = SimpleNamespace(
        content=(
            "Who it is for: Property managers, business owners, and commercial developers. "
            "Outside business hours, the agent can still answer questions and collect project details."
        )
    )
    answer = sales_policy.relevant_sentences("business hours for commercial work?", [passage])

    assert "the agent" not in answer
