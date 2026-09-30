"""Prepare documents uploaded before 1.10 the way new uploads are prepared.

New uploads are studied as they arrive: the agent writes down how customers
would ask for each passage, so typos, shorthand and other languages still find
the right answer (see backend/app/services/study.py). This does the same for
what every business had already uploaded. Run it once after deploying:

    python scripts/study_documents.py            # every business
    python scripts/study_documents.py --dry-run  # count only, change nothing

It only adds to how passages are found; no passage's text is changed. Safe to
run again: passages already studied are skipped.
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


async def main(dry_run: bool) -> int:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.doc_type == "policy")
            )
        ).scalars().all()
        rows = [row for row in rows if row.source != taught.SOURCE]
        waiting = [row for row in rows if not study.asked_as(row)]
        print(f"{len(waiting)} of {len(rows)} passage(s) not yet studied")
        if dry_run or not waiting:
            return 0
        studied = await study.study_existing(session)
        await session.commit()
        print(f"studied {studied}; {len(waiting) - studied} left as they were")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args().dry_run)))
