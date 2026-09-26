"""The ceiling on the public surface, and the ways it could make things worse.

Most of this file is about the second thing. A rate limiter is a piece of
infrastructure that sits in front of every request, and the two obvious ways
to write one turn a small problem into an outage:

  * refusing traffic when its own datastore is unreachable, so a Redis hiccup
    becomes downtime for the whole product;
  * throttling the Twilio webhook, which retries every non-2xx - so a 429 does
    not slow Twilio down, it multiplies the traffic and can double-send a real
    reply to a customer.

Both are tested here by name. The counting itself is the easy part.
"""

from __future__ import annotations

import pytest

from app import rate_limit
from app.config import settings


class _Request:
    """Just enough of a Request for the parts that read one."""

    def __init__(self, path="/api/v1/agent-config", headers=None, host="203.0.113.7"):
        self.url = type("U", (), {"path": path})()
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()


# ================================================= who is being counted
def test_an_authenticated_caller_is_counted_by_token():
    """A team behind one office NAT is one address and many people. Counting
    them together would throttle a paying client for being in the same
    building."""
    key, allowed = rate_limit._bucket(
        _Request(headers={"authorization": "Bearer pp_live_abc"})
    )
    assert key.startswith("rl:t:")
    assert allowed == rate_limit.AUTHENTICATED_PER_WINDOW


def test_the_key_never_carries_the_token_itself():
    """Redis keys turn up in logs, in MONITOR output and in a memory dump."""
    key, _ = rate_limit._bucket(
        _Request(headers={"authorization": "Bearer pp_live_secret_value"})
    )
    assert "pp_live_secret_value" not in key
    assert "secret" not in key


def test_two_tokens_are_counted_separately():
    a, _ = rate_limit._bucket(_Request(headers={"authorization": "Bearer one"}))
    b, _ = rate_limit._bucket(_Request(headers={"authorization": "Bearer two"}))
    assert a != b


def test_an_anonymous_caller_is_counted_by_address_and_allowed_less():
    key, allowed = rate_limit._bucket(_Request(host="198.51.100.4"))
    assert key == "rl:a:198.51.100.4"
    assert allowed == rate_limit.ANONYMOUS_PER_WINDOW
    assert allowed < rate_limit.AUTHENTICATED_PER_WINDOW


def test_the_address_comes_from_the_proxy_not_the_socket():
    """Behind Caddy every socket address is the proxy's, so counting that
    would put the entire internet in one bucket."""
    request = _Request(
        headers={"x-forwarded-for": "198.51.100.9, 10.0.0.1"}, host="10.0.0.1"
    )
    assert rate_limit._client_address(request) == "198.51.100.9"


def test_a_malformed_bearer_header_falls_back_to_the_address():
    for header in ("Bearer", "Bearer   ", "Basic abc", ""):
        key, allowed = rate_limit._bucket(
            _Request(headers={"authorization": header}, host="198.51.100.5")
        )
        assert key.startswith("rl:a:"), header
        assert allowed == rate_limit.ANONYMOUS_PER_WINDOW


# ======================================== the ways this causes an incident
def test_the_twilio_webhook_is_never_throttled():
    """Twilio retries any non-2xx. Answering 429 does not slow it down - it
    multiplies the traffic and can double-send a reply to a customer."""
    assert "/api/v1/whatsapp/webhook".startswith(rate_limit.EXEMPT_PREFIXES)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/whatsapp/webhook",
        "/media/photo.jpg",
        "/updates/latest.json",
        "/health",
    ],
)
def test_the_paths_that_must_not_be_throttled_are_exempt(path):
    assert path.startswith(rate_limit.EXEMPT_PREFIXES), path


@pytest.mark.parametrize(
    "path",
    ["/api/v1/agent-config", "/api/v1/calendar/abc.ics", "/api/v1/crm/contacts"],
)
def test_the_paths_that_should_be_throttled_are_not_exempt(path):
    assert not path.startswith(rate_limit.EXEMPT_PREFIXES), path


@pytest.mark.asyncio
async def test_a_redis_outage_lets_traffic_through(monkeypatch):
    """The failure that matters most. A limiter that rejects traffic when its
    own datastore is unreachable has converted a Redis hiccup into an outage
    for the whole product."""

    def explode(*args, **kwargs):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(rate_limit.aioredis, "from_url", explode)
    assert await rate_limit._count("rl:a:198.51.100.1") is None


@pytest.mark.asyncio
async def test_an_unreachable_redis_does_not_refuse_the_request(monkeypatch, org_a):
    """The same thing end to end: the middleware sees None and lets it pass.

    On a path that is actually counted. The first version of this asked
    /health, which is on the exempt list - so it never reached the limiter at
    all and would have passed just as happily with fail-open broken. It also
    dragged in the health endpoint's database probe, which talks to the real
    configured database rather than the test one, so it failed on any machine
    where that was not running. Neither had anything to do with rate limiting.
    """

    def explode(*args, **kwargs):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(rate_limit.aioredis, "from_url", explode)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit, "_breaker_open_until", 0.0)

    response = await org_a.get("/api/v1/agent-config")
    assert response.status_code != 429
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_exempt_list_is_what_actually_skips_the_limiter(monkeypatch, client):
    """Belt and braces on the line above: prove the exemption is real, by
    counting how often the limiter is consulted rather than trusting a status
    code that several other things could have produced."""
    asked: list[str] = []

    async def counting(key):
        asked.append(key)
        return 1

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit, "_count", counting)

    await client.post("/api/v1/whatsapp/webhook", data={"Nonsense": "x"})
    assert asked == [], "an exempt path must never be counted"


# ============================================================ counting
@pytest.mark.asyncio
async def test_a_caller_under_the_limit_is_served(client, monkeypatch, org_a):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit, "_count", _fake_count(1))

    response = await org_a.get("/api/v1/agent-config")
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == str(
        rate_limit.AUTHENTICATED_PER_WINDOW
    )


@pytest.mark.asyncio
async def test_a_caller_over_the_limit_is_refused(client, monkeypatch, org_a):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(
        rate_limit, "_count", _fake_count(rate_limit.AUTHENTICATED_PER_WINDOW + 1)
    )

    response = await org_a.get("/api/v1/agent-config")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(rate_limit.WINDOW_SECONDS)
    # Plain words, and nothing about the internals.
    assert "Too many requests" in response.json()["detail"]


@pytest.mark.asyncio
async def test_being_over_the_limit_never_blocks_the_webhook(
    client, monkeypatch, db_session, default_org
):
    """Belt and braces: even with the counter pinned far over, the one
    endpoint Twilio retries still answers."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "twilio_validate_signature", False)
    monkeypatch.setattr(rate_limit, "_count", _fake_count(100_000))

    response = await client.post(
        "/api/v1/whatsapp/webhook", data={"Nonsense": "x"}
    )
    assert response.status_code != 429


@pytest.mark.asyncio
async def test_the_limiter_can_be_turned_off(client, monkeypatch, org_a):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(rate_limit, "_count", _fake_count(100_000))

    response = await org_a.get("/api/v1/agent-config")
    assert response.status_code == 200


def _fake_count(value):
    async def _count(key):
        return value

    return _count


# ========================================== failing open is not enough
@pytest.mark.asyncio
async def test_a_failure_stops_redis_being_asked_again_for_a_while(monkeypatch):
    """Failing open slowly is its own outage.

    With Redis unreachable this module paid the full connect timeout on every
    single request before allowing it through. The suite went from 160 seconds
    to 611 - which in production is not a slow test run, it is every request a
    second slower and every worker tied up waiting.

    So the first failure opens a breaker and the requests behind it skip Redis
    entirely.
    """
    attempts = {"n": 0}

    def explode(*args, **kwargs):
        attempts["n"] += 1
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(rate_limit.aioredis, "from_url", explode)
    monkeypatch.setattr(rate_limit, "_breaker_open_until", 0.0)

    for _ in range(50):
        assert await rate_limit._count("rl:a:198.51.100.2") is None

    # One attempt, not fifty.
    assert attempts["n"] == 1, attempts["n"]


@pytest.mark.asyncio
async def test_the_breaker_lets_go_once_the_cooldown_passes(monkeypatch):
    """It has to close again, or one blip disables rate limiting until the
    process restarts."""
    import time as _time

    attempts = {"n": 0}

    def explode(*args, **kwargs):
        attempts["n"] += 1
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(rate_limit.aioredis, "from_url", explode)
    monkeypatch.setattr(rate_limit, "_breaker_open_until", 0.0)

    await rate_limit._count("rl:a:198.51.100.3")
    assert attempts["n"] == 1

    # Wind the cooldown back rather than sleeping through it.
    monkeypatch.setattr(
        rate_limit, "_breaker_open_until", _time.time() - 0.01
    )
    await rate_limit._count("rl:a:198.51.100.3")
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_the_timeouts_are_short_enough_to_be_survivable():
    """A quarter of a second against a container one hop away. Anything
    approaching a full second is already an outage being waited out."""
    assert rate_limit.CONNECT_TIMEOUT_SECONDS <= 0.5
    assert rate_limit.OPERATION_TIMEOUT_SECONDS <= 0.5
