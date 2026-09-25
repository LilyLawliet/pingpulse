"""Prove the ceiling on the public surface is holding, against production.

    python scripts/check_rate_limit.py

The one thing worth knowing before reading the output: the window is fixed, not
sliding. A burst that straddles a minute boundary is split across two counters
and can be served twice the limit - which looks exactly like a rate limiter
that is switched off. So this waits for a fresh window before counting, and
says how long it waited, because the first run that skips that step reports
zero throttled and is believed.

Nothing here needs a token and nothing here writes anything. The endpoint it
floods is a calendar feed for a token that does not exist, so every request it
makes is a 404 that touches no tenant's data.
"""

from __future__ import annotations

import asyncio
import time

import httpx

BASE = "https://pingpulse.duckdns.org"
FEED = "/api/v1/calendar/" + "z" * 43 + ".ics"

ANONYMOUS_LIMIT = 60
WINDOW_SECONDS = 60


async def burst(client, path, n, method="GET", data=None):
    async def one():
        try:
            r = await client.request(method, BASE + path, data=data)
            return r.status_code
        except Exception as exc:  # noqa: BLE001 - a hung request is a result
            return type(exc).__name__

    return await asyncio.gather(*(one() for _ in range(n)))


def tally(codes):
    out: dict = {}
    for c in codes:
        out[c] = out.get(c, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def wait_for_a_fresh_window():
    used = time.time() % WINDOW_SECONDS
    remaining = WINDOW_SECONDS - used
    # Only worth waiting when the current window is nearly spent. Starting 40
    # seconds in leaves room for 60 requests before the counter rolls, and the
    # rest land in the next one.
    if remaining > 25:
        print(f"  {remaining:.0f}s left in this window, enough - starting now")
        return
    print(f"  only {remaining:.0f}s left in this window, waiting for the next one")
    time.sleep(remaining + 0.5)


async def main():
    print("Rate limiting, checked against production")
    print("=" * 62)

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
        print("\n1. Is the limiter answering at all?")
        r = await c.get(BASE + FEED)
        limit = r.headers.get("x-ratelimit-limit")
        if limit is None:
            print("   NO X-RateLimit headers. The limiter is not running.")
            return
        print(f"   X-RateLimit-Limit: {limit}   (anonymous callers)")

        print("\n2. Lining up with a fresh counting window")
        wait_for_a_fresh_window()

        print(f"\n3. {ANONYMOUS_LIMIT + 40} anonymous requests at once")
        started = time.perf_counter()
        codes = await burst(c, FEED, ANONYMOUS_LIMIT + 40)
        counts = tally(codes)
        elapsed = time.perf_counter() - started
        print(f"   {elapsed:.1f}s -> {counts}")
        served, throttled = counts.get(404, 0), counts.get(429, 0)
        print(f"   served {served}, throttled {throttled}")
        if throttled:
            print("   HELD")
        else:
            print("   NOT LIMITED - re-run; a boundary straddle looks like this")

        r = await c.get(BASE + FEED)
        print(
            f"   now: {r.status_code}, "
            f"Retry-After={r.headers.get('retry-after')}, "
            f"Remaining={r.headers.get('x-ratelimit-remaining')}"
        )

        # The part that matters more than the counting. Twilio retries every
        # non-2xx, so a 429 here would not slow it down - it would multiply
        # the traffic and can double-send a real reply to a customer. This runs
        # while the address above is still being refused, which is the only
        # time the answer means anything.
        print("\n4. While that same address is throttled, are these still open?")
        checks = [
            ("the Twilio webhook", "/api/v1/whatsapp/webhook", "POST"),
            ("health", "/health", "GET"),
            ("desktop updates", "/updates/latest.json", "GET"),
        ]
        worst = 0
        for label, path, method in checks:
            codes = await burst(
                c,
                path,
                40,
                method=method,
                data={"Body": "x"} if method == "POST" else None,
            )
            counts = tally(codes)
            refused = counts.get(429, 0)
            worst = max(worst, refused)
            verdict = "THROTTLED - this is the bug" if refused else "never throttled"
            print(f"   {label:22} {counts}  {verdict}")

        print("\n5. Still healthy after all of that?")
        r = await c.get(BASE + "/health")
        print(f"   /health -> {r.status_code}")

    print("\n" + "=" * 62)
    if throttled and not worst:
        print("PASS - the public surface has a ceiling, the webhook does not.")
    elif worst:
        print("FAIL - something that must never be throttled was.")
    else:
        print("INCONCLUSIVE - nothing was throttled. Run it again.")


if __name__ == "__main__":
    asyncio.run(main())
