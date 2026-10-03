"""Whether the job and the place a customer asks about are ones this business takes.

The October 3 regression run booked a site visit for dog grooming and a roof
in Seattle, for a Miami remodeller. Both were read back faithfully and the
customer said yes - a read-back stops a misreading, not a request the
business should never have offered times for.

The shop's own lists decide where it has filled them in: an address checked
against its areas is a string match, and needs no model. Where it has not, the
business still says what it does and where, in its own description and in the
documents it uploaded ("construction and remodeling in Miami / South
Florida"), and no list of trades or cities written here could stand in for
that. So the question is put to a model, narrowly and at temperature 0: does
this request fit what the business says about itself? Its answer is enforced
in code - no times, no read-back, and checked again when they say yes.

It fails open. With no description, no model, or no clear answer, nothing is
refused: a request the business does take must never be turned away because
a model was slow, and the shop's own lists remain the way to be certain.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SCOPE_KEY = "job_scope"
SAID_KEY = "job_said"
TIMEOUT_SECONDS = 6

PROMPT = """You check whether a customer's request fits what a business says it does.
Judge ONLY from the business's own words below. Never assume a service or an area
the business does not mention.

THE BUSINESS SAYS:
{business}

THE CUSTOMER WROTE:
{said}

Return ONLY this JSON:
{{"job": "<the work or service they want, in a few words, or null>",
  "service_fits": true | false | null,
  "place": "<where the job is, as they wrote it, or null>",
  "area_fits": true | false | null}}

- service_fits: true if the business does that kind of work; false only if it
  clearly does not (a remodeller asked for dog grooming); null if no job is named
  or the business's words do not say.
- area_fits: true if the place is inside where the business works; false only if
  it is clearly outside (a Miami business asked for Seattle); null if no place is
  named or the business's words do not say where it works.
- A customer living elsewhere but with the job in the area: judge the job's place."""


@dataclass(frozen=True)
class Verdict:
    job: str | None = None
    service_fits: bool | None = None
    place: str | None = None
    area_fits: bool | None = None

    @property
    def known(self) -> bool:
        return self.service_fits is not None or self.area_fits is not None


def what_the_business_says(organization) -> str:
    """Everything the business has said about what it does and where, in its own words."""
    from app.services import agent_config

    config = getattr(organization, "agent_config", None) or {}
    proposed = (config.get(agent_config.PROPOSED_KEY) or {}).get("fields") or {}
    parts: list[str] = []

    def listed(label: str, values) -> None:
        values = [str(v).strip() for v in (values or []) if str(v).strip()]
        if values:
            parts.append(f"{label}: " + "; ".join(values[:40]))

    listed("Services it offers", config.get("services"))
    listed("Areas it serves", config.get("service_areas"))
    if not config.get("services"):
        listed("Services, as its documents state them", proposed.get("services"))
    if not config.get("service_areas"):
        listed("Areas, as its documents state them", proposed.get("service_areas"))
    description = str(getattr(organization, "sales_prompt", "") or "").strip()
    if description:
        parts.append("In its own description: " + description[:2000])
    return "\n".join(parts)


async def check(organization, said: str) -> Verdict:
    """The model's reading of whether this request fits. Never raises; unknown on any doubt."""
    business = what_the_business_says(organization)
    if not business or not (said or "").strip():
        return Verdict()
    from app.services import understanding

    try:
        answer = await understanding.structured(
            PROMPT.format(business=business, said=said[:1500]), TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - a scope check never breaks a turn
        logger.info("scope check failed: %s", exc)
        return Verdict()
    if not isinstance(answer, dict):
        return Verdict()

    def flag(key):
        value = answer.get(key)
        return value if isinstance(value, bool) else None

    def text(key):
        value = answer.get(key)
        return str(value).strip()[:120] if isinstance(value, str) and value.strip() else None

    return Verdict(
        job=text("job"),
        service_fits=flag("service_fits"),
        place=text("place"),
        area_fits=flag("area_fits"),
    )


def _key(said: str) -> str:
    return hashlib.sha1(said.encode("utf-8")).hexdigest()[:16]


def remembered(contact) -> Verdict | None:
    held = (getattr(contact, "contact_metadata", None) or {}).get(SCOPE_KEY)
    if not isinstance(held, dict):
        return None
    try:
        return Verdict(**json.loads(held["verdict"]))
    except (KeyError, TypeError, ValueError):
        return None


def note_said(contact, text: str, relevant: bool) -> str:
    """What the customer has said about the job so far, kept short, for the check."""
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    said = list(metadata.get(SAID_KEY) or [])
    if relevant and (text or "").strip():
        said = (said + [text.strip()[:500]])[-3:]
        metadata[SAID_KEY] = said
        contact.contact_metadata = metadata
    return "\n".join(said)


async def for_contact(organization, contact, said: str) -> Verdict:
    """The verdict for what they have said, asked once per change in what they said."""
    if not said:
        return remembered(contact) or Verdict()
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    held = metadata.get(SCOPE_KEY) or {}
    if isinstance(held, dict) and held.get("key") == _key(said):
        return remembered(contact) or Verdict()
    verdict = await check(organization, said)
    metadata[SCOPE_KEY] = {"key": _key(said), "verdict": json.dumps(verdict.__dict__)}
    contact.contact_metadata = metadata
    return verdict
