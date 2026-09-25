"""A ceiling on how fast one caller can hit the public surface.

There was none, and 120 anonymous requests went through in 2.8 seconds. That
is not a break on its own - the secrets are long and the timing signal is gone
- but nothing *slowed* an attempt to guess them, and nothing stopped the
unauthenticated calendar feed being hammered for CPU and database time.

Not in Caddy, which was the obvious place and the wrong one. Caddy has no
built-in rate limiting; it needs a third-party plugin and therefore a custom
`xcaddy` build, which would put a build step and an outside dependency into
the single component that terminates TLS for the whole product. Caddy is the
most reliable thing in this stack. Redis is already a hard dependency, and a
counter in it costs well under a millisecond.

Three decisions worth stating, because each one is a way this could make
things worse rather than better:

*It fails open, and fails fast.* A rate limiter that rejects traffic when its
own datastore is unreachable has converted a Redis hiccup into an outage. But
letting the request through after waiting out a connect timeout is barely
better: the traffic is served a second slower, every worker is tied up, and
the queue backs up behind it. So the first failure opens a breaker and the
next few seconds of requests skip Redis entirely.

*The webhook is exempt.* Twilio retries any non-2xx, so answering 429 does not
slow it down - it multiplies the traffic and can double-send a reply to a
customer. Throttling the one endpoint that must never be throttled is the
classic way this feature causes an incident.

*An authenticated caller is counted by token, not by address.* A team behind
one office NAT is one address and many people, and counting them together
would throttle a paying client for being in the same building.
"""

from __future__ import annotations

import hashlib
import logging
import time

import redis.asyncio as aioredis
from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

# The window, and how much fits in it. Generous on purpose: this is here to
# stop guessing and flooding, not to meter a customer. A browser loading the
# dashboard makes a handful of calls; a subscribed calendar client polls once
# an hour.
WINDOW_SECONDS = 60
AUTHENTICATED_PER_WINDOW = 300
ANONYMOUS_PER_WINDOW = 60

# How long to stop asking Redis after it fails, and how long to wait on it.
#
# Failing open is not enough on its own. With Redis unreachable, every request
# paid the full connect timeout before being allowed through - so an outage
# did not refuse traffic, it added a second to every request and saturated the
# workers instead. Slow is its own kind of down.
#
# So the first failure opens a breaker and the next few seconds of requests
# skip Redis entirely, costing nothing. The timeouts are short because Redis
# is a container away: a healthy call is well under a millisecond, and
# anything approaching a quarter second is already broken.
BREAKER_COOLDOWN_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 0.25
OPERATION_TIMEOUT_SECONDS = 0.25

# When to start asking again. Module state, per worker process, which is the
# right scope: each one discovers Redis is back on its own, a few seconds
# apart, rather than all of them stampeding it at the same instant.
_breaker_open_until = 0.0

# Paths that must never be throttled, and why.
#
#   the webhook   Twilio retries a non-2xx, so a 429 multiplies traffic and
#                 can double-send a reply. See the module docstring.
#   /media/       Twilio fetches outbound attachments from here; throttled,
#                 customers receive messages with broken images.
#   /updates/     the desktop updater. A client that cannot download an
#                 installer stays on an old build.
#   /health       uptime monitoring, which polls by design.
EXEMPT_PREFIXES = (
    "/api/v1/whatsapp/webhook",
    "/media/",
    "/updates/",
    "/health",
)


def _client_address(request: Request) -> str:
    """The caller's address as the proxy saw it.

    Caddy appends to X-Forwarded-For, so the first entry is the original
    client and the rest are hops. Falls back to the socket address, which is
    what a direct connection gives.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _bucket(request: Request) -> tuple[str, int]:
    """Who is being counted, and how much they are allowed.

    A bearer token identifies a tenant wherever they are sitting; an address
    is all there is for anyone else. The token is hashed rather than used
    directly, so a Redis key never carries a live credential.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
            return f"rl:t:{digest}", AUTHENTICATED_PER_WINDOW
    return f"rl:a:{_client_address(request)}", ANONYMOUS_PER_WINDOW


async def _count(key: str) -> int | None:
    """This caller's requests so far in the current window.

    None when Redis could not answer, which the caller reads as "let it
    through". A fixed window rather than a sliding log: two counters and an
    expiry, no per-request storage, and the worst case is a caller getting
    two windows' worth across a boundary - which at these limits is not a
    threat model, it is a rounding error.
    """
    global _breaker_open_until

    now = time.time()
    if now < _breaker_open_until:
        # Redis failed recently. Do not spend a timeout finding out again.
        return None

    window = int(now // WINDOW_SECONDS)
    slot = f"{key}:{window}"

    # Constructing the client is inside the try as well, and that is not
    # tidiness. It was outside, and a failure to build one - an unresolvable
    # host, a malformed URL - raised straight through the middleware and
    # turned every request into a 500. The fail-open promise this module makes
    # is only worth anything if it covers the connect, which is exactly where
    # a Redis outage shows up first.
    client = None
    try:
        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
            socket_timeout=OPERATION_TIMEOUT_SECONDS,
        )
        pipe = client.pipeline()
        pipe.incr(slot)
        # Set on every request rather than only the first: a key that somehow
        # lost its expiry would otherwise live forever.
        pipe.expire(slot, WINDOW_SECONDS * 2)
        used, _ = await pipe.execute()
        _breaker_open_until = 0.0
        return int(used)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        if _breaker_open_until <= now:
            # Logged once per cooldown rather than once per request: an
            # unreachable Redis under load would otherwise write the log faster
            # than anybody could read it.
            logger.warning(
                "rate limiting is unavailable, allowing traffic through for %.0fs: %s",
                BREAKER_COOLDOWN_SECONDS,
                exc,
            )
        _breaker_open_until = now + BREAKER_COOLDOWN_SECONDS
        return None
    finally:
        if client is not None:
            closer = getattr(client, "aclose", None) or client.close
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


async def limit_requests(request: Request, call_next):
    """Middleware: count this caller, and refuse them if they are over."""
    if not settings.rate_limit_enabled:
        return await call_next(request)

    path = request.url.path
    if path.startswith(EXEMPT_PREFIXES):
        return await call_next(request)

    key, allowed = _bucket(request)
    used = await _count(key)

    if used is not None and used > allowed:
        logger.warning("rate limited %s on %s (%d in the window)", key, path, used)
        return JSONResponse(
            status_code=429,
            content={
                "detail": (
                    "Too many requests. Wait a moment and try again."
                )
            },
            headers={
                "Retry-After": str(WINDOW_SECONDS),
                "X-RateLimit-Limit": str(allowed),
                "X-RateLimit-Remaining": "0",
            },
        )

    response = await call_next(request)
    if used is not None:
        response.headers["X-RateLimit-Limit"] = str(allowed)
        response.headers["X-RateLimit-Remaining"] = str(max(0, allowed - used))
    return response
