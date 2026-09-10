"""Decompose reply latency on an unthrottled path.

The earlier benchmark fired six messages back to back at shared free-tier keys,
so most of what it measured was rate-limit retries — not what a client with
their own quota would see. This one answers the question that actually matters
for the client documentation: with no contention at all, how long does one
reply take, and where does the time go?

It times the two model calls separately, because the pipeline makes two per
message (a strict analyzer pass, then the response pass). Quota removes
queueing; it does not make inference faster, so the floor is set by these two
round trips, not by the retry behaviour.

Run inside the backend container so it uses the real keys and models:

    docker exec -i pingpulse-backend python - < scripts/benchmark_clean_path.py
"""

from __future__ import annotations

import asyncio
import statistics
import time

from sqlalchemy import select

from app.database import SessionLocal
from app.models import CRMContact, Message, Organization
from app.services import analyzer, llm_service

ROUNDS = 4
SPACING = 12.0  # seconds between rounds, so nothing throttles


async def timed(label: str, coro):
    started = time.monotonic()
    try:
        result = await coro
        return label, time.monotonic() - started, result, None
    except Exception as exc:  # noqa: BLE001
        return label, time.monotonic() - started, None, exc


async def main() -> None:
    async with SessionLocal() as db:
        org = (
            await db.execute(
                select(Organization).where(Organization.name == "Lumen Analytics")
            )
        ).scalar_one()
        contact = (
            await db.execute(select(CRMContact).limit(1))
        ).scalar_one_or_none()
        history = (
            (await db.execute(select(Message).limit(4))).scalars().all()
            if contact
            else []
        )

    message = "What's the pricing for the enterprise plan?"

    analyzer_times: list[float] = []
    generate_times: list[float] = []

    for round_number in range(1, ROUNDS + 1):
        label, seconds, analysis, error = await timed(
            "analyzer", analyzer.analyse(history, message, "NEW")
        )
        if error:
            print(f"  round {round_number} analyzer FAILED: {type(error).__name__}")
        else:
            analyzer_times.append(seconds)

        label, seconds, generation, error = await timed(
            "generate",
            llm_service.generate_reply(
                org, contact, history, message,
                knowledge="", memory_block="", policy_block="",
            ),
        )
        if error:
            print(f"  round {round_number} generate FAILED: {type(error).__name__}")
        else:
            generate_times.append(seconds)
            provider = getattr(generation, "provider", "?")
            print(
                f"  round {round_number}: analyzer {analyzer_times[-1]:5.2f}s  "
                f"generate {seconds:5.2f}s  total {analyzer_times[-1] + seconds:5.2f}s  "
                f"[{provider}]"
            )

        if round_number < ROUNDS:
            await asyncio.sleep(SPACING)

    def summarise(name: str, values: list[float]) -> None:
        if not values:
            print(f"  {name}: no successful samples")
            return
        print(
            f"  {name:9} min {min(values):5.2f}s   median {statistics.median(values):5.2f}s"
            f"   max {max(values):5.2f}s"
        )

    print("\n  --- unthrottled, one message at a time ---")
    summarise("analyzer", analyzer_times)
    summarise("generate", generate_times)
    if analyzer_times and generate_times:
        floor = min(analyzer_times) + min(generate_times)
        typical = statistics.median(analyzer_times) + statistics.median(generate_times)
        print(f"\n  best-case two-pass total : {floor:.2f}s")
        print(f"  typical two-pass total   : {typical:.2f}s")


asyncio.run(main())
