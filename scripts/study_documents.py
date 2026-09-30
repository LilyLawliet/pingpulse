"""Prepare documents uploaded before 1.10 the way new uploads are prepared.

New uploads are studied as they arrive: the agent writes down how customers
would ask for each passage, so typos, shorthand and other languages still find
the right answer (see backend/app/services/study.py). This does the same for
what every business had already uploaded. Run it once after deploying:

    python scripts/study_documents.py            # every business
    python scripts/study_documents.py --dry-run  # count only, change nothing

It only adds to how passages are found; no passage's text is changed. Safe to
run again: passages already studied are skipped.

Why it goes slowly on purpose
-----------------------------
study_existing sends its batches all at once, which is right for one document
being uploaded and wrong for a whole backfill: a batch of ten passages is
about seven thousand tokens, and the account's ceiling is eight thousand a
minute, so firing three together had every key answer 429 and nothing was
studied at all. Worse, that looked like success - the run printed "studied 0"
and gave no reason. So the backfill asks for a few passages at a time, waits
between rounds, and says plainly when the model refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import KnowledgeDocument  # noqa: E402
from app.services import study, taught  # noqa: E402


async def _waiting(session) -> list:
    rows = (
        await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.doc_type == "policy")
        )
    ).scalars().all()
    rows = [row for row in rows if row.source != taught.SOURCE]
    return [row for row in rows if not study.asked_as(row)]


async def main(dry_run: bool, slice_size: int, pause: float) -> int:
    async with SessionLocal() as session:
        waiting = await _waiting(session)
        total = len(waiting)
        print(f"{total} passage(s) not yet studied")
        if dry_run or not total:
            return 0

        done = 0
        rounds = 0
        while True:
            studied = await study.study_existing(session, limit=slice_size)
            await session.commit()
            rounds += 1
            if studied:
                done += studied
                print(f"  studied {done} of {total}")
            else:
                # Nothing came back. One more round would only ask again at the
                # same rate, so stop and say so rather than printing a zero.
                print(
                    f"\nStopped after {done} of {total}. The model answered nothing "
                    f"for the last {slice_size} passage(s) - usually the per-minute "
                    "token limit, sometimes a passage it could not read. Nothing was "
                    "changed for those, and they are still searched as they always "
                    "were. Run this again to pick them up."
                )
                return 1 if done == 0 else 0
            if done >= total:
                break
            await asyncio.sleep(pause)

        print(f"\nStudied all {done} passage(s) in {rounds} round(s).")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--slice",
        type=int,
        default=3,
        dest="slice_size",
        help="passages per round; keep the round well under the token-per-minute limit",
    )
    parser.add_argument(
        "--pause", type=float, default=20.0, help="seconds to wait between rounds"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.dry_run, args.slice_size, args.pause)))
