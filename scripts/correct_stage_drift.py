"""Put contacts back where the evidence says they belong.

Three stages used to be reachable from the text of a customer's message:
ESTIMATE_SCHEDULED, ESTIMATE_SENT and WON. Contacts that were moved that way
are still sitting in them after the rule was removed, so the board goes on
asserting things that never happened until somebody corrects the records.

Run with --dry-run first. It prints what it would do and changes nothing.

    python scripts/correct_stage_drift.py --dry-run
    python scripts/correct_stage_drift.py --apply

Only contacts whose stage was set by the agent are touched. A stage a person
put a contact in is their judgement and is left alone, even when it is one of
the three - the fault was the automation, not the operator.
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
from app.models import (  # noqa: E402
    STAGE_AGENT,
    STAGE_OPERATOR,
    CRMContact,
    Organization,
    StageEvent,
)
from app.services import analytics, qualification  # noqa: E402

# The three that describe something outside the conversation.
SUSPECT = ("ESTIMATE_SCHEDULED", "ESTIMATE_SENT", "WON")


async def honest_stage(organization, contact) -> str:
    """Where this contact belongs on the evidence actually held.

    QUALIFIED only if the configured qualification is complete, because that
    is a fact anybody can check by reading the record. Otherwise CONTACTED:
    we really did reply to them, and that much is true.
    """
    wanted = qualification.slots_for(organization)
    if wanted and not qualification.missing(organization, contact.qualification):
        return "QUALIFIED"
    return "CONTACTED"


async def main(apply: bool) -> int:
    moved = 0
    async with SessionLocal() as session:
        contacts = (
            await session.execute(
                select(CRMContact).where(CRMContact.pipeline_stage.in_(SUSPECT))
            )
        ).scalars().all()

        if not contacts:
            print("  nothing sitting in a stage it could not have earned")
            return 0

        for contact in contacts:
            organization = await session.get(Organization, contact.organization_id)

            # Who put them there. A move with no recorded event predates the
            # stage log, and everything that wrote a stage before the log
            # existed was the automatic rule, so it counts as the agent.
            last = (
                await session.execute(
                    select(StageEvent)
                    .where(
                        StageEvent.contact_id == contact.id,
                        StageEvent.to_stage == contact.pipeline_stage,
                    )
                    .order_by(StageEvent.at.desc())
                    .limit(1)
                )
            ).scalars().first()
            source = last.source if last else STAGE_AGENT

            if source != STAGE_AGENT:
                print(
                    f"  keep    {contact.name or contact.phone_number}: "
                    f"{contact.pipeline_stage} was set by a {source}"
                )
                continue

            target = await honest_stage(organization, contact)
            if target == contact.pipeline_stage:
                continue

            print(
                f"  correct {contact.name or contact.phone_number} "
                f"({organization.name}): {contact.pipeline_stage} -> {target}"
            )
            moved += 1

            if apply:
                previous = contact.pipeline_stage
                contact.pipeline_stage = target
                # Recorded as a correction rather than written over. The
                # funnel should show that this happened, not pretend the
                # contact was always here.
                await analytics.record_move(
                    session,
                    contact,
                    target,
                    from_stage=previous,
                    source=STAGE_OPERATOR,
                )

        if apply:
            await session.commit()
            print(f"\n  {moved} contact(s) corrected")
        else:
            print(f"\n  {moved} contact(s) would be corrected - nothing was changed")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply)))
