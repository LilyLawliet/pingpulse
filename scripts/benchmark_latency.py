"""Measure real end-to-end reply latency, so the client documentation can quote
numbers that were actually observed rather than aspirational ones.

Times the full inbound path: webhook receipt, analyzer pass, retrieval, response
generation, dispatch and commit. The webhook only answers once the reply is
durable, so the wall time of the POST is the whole pipeline.

    python scripts/benchmark_latency.py

Writes nothing outside Drive D:.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

import httpx

BASE = "http://localhost:8000"
RETAIL_NUMBER = "whatsapp:+14155238886"
SAAS_NUMBER = "whatsapp:+14155238887"

OPERATOR_EMAIL = os.environ.get("PINGPULSE_EMAIL", "demo@pingpulse.app")
OPERATOR_PASSWORD = os.environ.get("PINGPULSE_PASSWORD", "")

PROBES = [
    ("retail", RETAIL_NUMBER, "Do you have wireless earbuds?"),
    ("retail", RETAIL_NUMBER, "Is cash on delivery available in Dubai?"),
    ("saas", SAAS_NUMBER, "What's the pricing for the enterprise plan?"),
    ("saas", SAAS_NUMBER, "Can we book a call?"),
    ("retail", RETAIL_NUMBER, "How long is delivery to Abu Dhabi?"),
    ("saas", SAAS_NUMBER, "Do you have a Salesforce integration?"),
]


async def main() -> int:
    run = int(time.time()) % 100000
    timings: list[tuple[str, str, float]] = []

    async with httpx.AsyncClient(timeout=240) as client:
        for index, (tenant, to, body) in enumerate(PROBES):
            phone = f"+9715{run}{index:02d}"
            form = {
                "MessageSid": f"SM_bench_{run}{index}",
                "From": f"whatsapp:{phone}",
                "To": to,
                "Body": body,
                "ProfileName": "Bench",
                "NumMedia": "0",
            }
            started = time.monotonic()
            response = await client.post(f"{BASE}/api/v1/whatsapp/webhook", data=form)
            elapsed = time.monotonic() - started
            response.raise_for_status()
            timings.append((tenant, body, elapsed))
            print(f"  {elapsed:6.2f}s  [{tenant:6}] {body[:52]}")

    values = sorted(t for _, _, t in timings)
    print("\n  samples : ", len(values))
    print(f"  fastest : {values[0]:.2f}s")
    print(f"  median  : {statistics.median(values):.2f}s")
    print(f"  slowest : {values[-1]:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
