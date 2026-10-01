"""/health must not say a provider is fine when it will not generate.

Listing models costs no generation quota, so it keeps answering 200 while
every real call is refused. /health reported Groq "ok" through an afternoon
in which all three keys returned 429 and the agent was running on its
no-model fallback. The probe generates now, and these pin what it reports.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import llm_service

_REQUEST = httpx.Request("POST", "https://example.invalid")


class _FakeClient:
    """Answers the model listing, then whatever the generation case needs."""

    def __init__(self, generate: httpx.Response):
        self._generate = generate

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, *_args, **_kwargs):
        return httpx.Response(
            200, json={"data": [{"id": "openai/gpt-oss-120b"}]}, request=_REQUEST
        )

    async def post(self, *_args, **_kwargs):
        return self._generate


@pytest.fixture
def groq_configured(monkeypatch):
    monkeypatch.setattr(llm_service.settings, "groq_api_key", "k1")
    monkeypatch.setattr(llm_service.settings, "groq_model", "openai/gpt-oss-120b")


def _with_generation(monkeypatch, response: httpx.Response):
    monkeypatch.setattr(
        llm_service.httpx, "AsyncClient", lambda *a, **k: _FakeClient(response)
    )


@pytest.mark.asyncio
async def test_a_model_that_answers_is_reported_ok(groq_configured, monkeypatch):
    _with_generation(monkeypatch, httpx.Response(200, json={}, request=_REQUEST))

    ok, detail = await llm_service.probe_groq()

    assert ok is True
    assert "answering" in detail


@pytest.mark.asyncio
async def test_a_rate_limited_model_is_not_reported_ok(groq_configured, monkeypatch):
    """The exact case that read green all afternoon."""
    _with_generation(monkeypatch, httpx.Response(429, json={}, request=_REQUEST))

    ok, detail = await llm_service.probe_groq()

    assert ok is False
    assert "rate limit" in detail


@pytest.mark.asyncio
async def test_a_listed_model_that_refuses_to_generate_is_not_ok(
    groq_configured, monkeypatch
):
    """Listed but 404 on generation - a name being retired out from under us."""
    _with_generation(monkeypatch, httpx.Response(404, json={}, request=_REQUEST))

    ok, detail = await llm_service.probe_groq()

    assert ok is False
    assert "will not generate" in detail
