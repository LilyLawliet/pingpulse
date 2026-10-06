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
import re
import re
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


# How much of the business's own documents to quote. Enough for what it does
# and where, small enough that a scope check cannot eat the per-minute token
# budget it shares with every reply.
DOCUMENT_CHARS = 1400
# What a passage mentioning the trade or the patch looks like. Used only to
# choose which passages to quote, never to decide anything.
_TELLING = re.compile(
    r"\b(服务|service|services|we (do|offer|provide|handle|serve)|specialis|"
    r"areas?\s+(we\s+)?serve|serving|located|location|address|county|counties|"
    r"remodel\w*|construction|renovation|installation|repair|"
    r"[A-Z]{2}\s+\d{5}|florida|fl\b)",
    re.IGNORECASE,
)


def from_documents(documents) -> str:
    """The passages of the business's own documents that say what it does and where.

    Constrivo had no services and no areas filled in, and its description is
    the agent's instruction sheet - "greet the customer by name", "never quote
    a price" - which says nothing about the trade or the patch. So the model
    was asked whether a roof in Seattle fits a business whose words never
    mention Miami, answered "the words do not say", and the check fell open:
    the reply said plainly that Seattle is not served while the diary offered
    six times for it. The documents are where an unconfigured business
    actually states both, and they are what the reply had been reading all
    along.
    """
    telling, rest = [], []
    for document in documents or []:
        text = " ".join(str(getattr(document, "content", "") or "").split())
        if not text:
            continue
        (telling if _TELLING.search(text) else rest).append(text)
    kept, room = [], DOCUMENT_CHARS
    for text in telling + rest:
        if room <= 0:
            break
        kept.append(text[:room])
        room -= len(kept[-1])
    return " ... ".join(kept)


def what_the_business_says(organization, documents=()) -> str:
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
    quoted = from_documents(documents)
    if quoted:
        parts.append("In its own documents: " + quoted)
    return "\n".join(parts)


async def check(organization, said: str, documents=()) -> Verdict:
    """The model's reading of whether this request fits. Never raises; unknown on any doubt."""
    business = what_the_business_says(organization, documents)
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


async def for_contact(organization, contact, said: str, documents=()) -> Verdict:
    """The verdict for what they have said, asked once per change in what they said."""
    if not said:
        return remembered(contact) or Verdict()
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    held = metadata.get(SCOPE_KEY) or {}
    if isinstance(held, dict) and held.get("key") == _key(said):
        return remembered(contact) or Verdict()
    verdict = await check(organization, said, documents)
    metadata[SCOPE_KEY] = {"key": _key(said), "verdict": json.dumps(verdict.__dict__)}
    contact.contact_metadata = metadata
    return verdict


# --------------------------------------------------------------- US states
# Data, not rules: the fifty states and DC, so a state the customer names can
# be compared with the one the business names. Read only where it is plainly a
# state - after a comma ("Seattle, Washington"), after "in", or as a postal
# code before a ZIP ("WA 98101") - so "100 Washington Ave, Miami" is Florida.
_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_NAMES = "|".join(sorted((re.escape(n) for n in _STATES.values()), key=len, reverse=True))
# The customer's side: only where a state is plainly the job's place - "City,
# State" or a postal code before a ZIP. "I live in California but the
# property is in Miami" names a state that is not where the job is.
_BY_NAME = re.compile(rf",\s*({_NAMES})\b", re.IGNORECASE)
_BY_CODE = re.compile(r"(?:,\s*|\s)([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
# The business's side: its own words, so any state it names is one it works in.
_ANY_NAME = re.compile(rf"\b({_NAMES})\b", re.IGNORECASE)
_NAME_OF = {name.lower(): name for name in _STATES.values()}


def states_in(text: str) -> set[str]:
    """US states a customer names as the place of the job ("Seattle, Washington", "WA 98101")."""
    found = {_NAME_OF[m.group(1).lower()] for m in _BY_NAME.finditer(text or "")}
    found |= {_STATES[m.group(1)] for m in _BY_CODE.finditer(text or "") if m.group(1) in _STATES}
    return found


def states_the_business_names(organization) -> set[str]:
    """Every US state the business's own words mention."""
    text = what_the_business_says(organization)
    return {_NAME_OF[m.group(1).lower()] for m in _ANY_NAME.finditer(text)}
