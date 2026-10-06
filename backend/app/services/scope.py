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
{{"job": "<the work or service they want, in a few words, written in the same
         language the business uses above - not the customer's - or null>",
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


# Words that carry no trade meaning, so two requests sharing only these share
# nothing. "service", "work" and "job" are here because a shop writes "roofing
# services" and a customer writes "dog grooming service", and matching on
# "service" would make every request fit every business.
_NOISE = frozenset({
    "a", "an", "and", "the", "or", "of", "for", "to", "my", "our", "your", "in",
    "on", "at", "with", "new", "full", "some", "any", "service", "services",
    "work", "works", "job", "jobs", "project", "projects", "need", "want",
    "please", "help", "get", "general", "custom", "other",
})
_WORD = re.compile(r"[a-z]+")
#: "Services: general construction, home remodeling, ..." - how a shop that has
#: not filled in the services field still writes down what it does.
_SERVICES_SENTENCE = re.compile(
    r"\b(?:"
    r"services?\s*(?:it offers|offered|include[sd]?|we offer|:)"
    r"|we\s+(?:offer|provide|sell|supply|specialis[ez]e in)"
    r"|what we do\s*:"
    r")\s*(.{3,600}?)(?:\.\s|\.$|$)",
    re.IGNORECASE | re.DOTALL,
)
_SPLIT_SERVICES = re.compile(r"\s*(?:,|;|\band\b|•|\n)\s*")


def _stem(word: str) -> str:
    """Enough of a word to compare trades by. "roofing" and "roof" are one trade."""
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 6 and word.endswith("ment"):
        word = word[:-4]
    # "remodelling" -> "remodell" -> "remodel", so it meets "remodeling".
    if len(word) > 4 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]
    return word


def _stems(text: str) -> set[str]:
    """Content words, crudely stemmed, for comparing one trade to another."""
    found = set()
    for word in _WORD.findall((text or "").lower()):
        if word in _NOISE or len(word) < 3:
            continue
        word = _stem(word)
        if word not in _NOISE and len(word) >= 3:
            found.add(word)
    return found


def offered_services(organization) -> list[str]:
    """What this business says it does, as a list, from its own settings.

    In order: the services field the owner filled in; the services read out of
    its documents and shown to it for approval; and failing both, a "Services:
    ..." sentence in what it wrote about itself. All three are the business's
    own words, held by us, and none of them is a model's opinion formed at the
    moment a customer asks.
    """
    from app.services import agent_config

    config = getattr(organization, "agent_config", None) or {}
    listed = [str(v).strip() for v in (config.get("services") or []) if str(v).strip()]
    if listed:
        return listed[:60]
    proposed = (config.get(agent_config.PROPOSED_KEY) or {}).get("fields") or {}
    listed = [str(v).strip() for v in (proposed.get("services") or []) if str(v).strip()]
    if listed:
        return listed[:60]
    written = " ".join(
        str(getattr(organization, field, "") or "")
        for field in ("product_rules", "sales_prompt")
    )
    match = _SERVICES_SENTENCE.search(written)
    if not match:
        return []
    found = [part.strip(" .") for part in _SPLIT_SERVICES.split(match.group(1))]
    return [part for part in found if len(part) > 2][:60]


#: A word in this many of the shop's own services is a modifier, not a trade.
#: Constrivo lists four kinds of "construction" and three things done to a
#: "home", so "home" alone cannot be what makes a request theirs - otherwise
#: "dog grooming at home" matches "home remodeling".
SHARED_BY = 3


def worth_checking(text: str) -> bool:
    """Enough said to be naming something, rather than "yes" or "thanks"."""
    return len(_stems(text)) >= 2


def supports(organization, job: str | None) -> bool | None:
    """Whether this business does that work. Decided here, from its own list.

    Three answers, and the middle one matters. True and False are this
    function's own, read off the list the business keeps. None means it cannot
    be told from the list - the business has listed nothing, the request was
    not named, or the only thing it shares with the list is a word like "home"
    that half the services carry. Only then is the model's reading used.

    So a request naming a trade the shop does not list - dog grooming, to a
    remodeller - is refused here, by us, whatever the model thought. A vague
    one is still a conversation, and is left to be read as one.
    """
    wanted = _stems(job or "")
    services = offered_services(organization)
    if not wanted or not services:
        return None
    stemmed = [_stems(service) for service in services]
    seen: dict[str, int] = {}
    for words in stemmed:
        for word in words:
            seen[word] = seen.get(word, 0) + 1
    telling = {word for word, count in seen.items() if count < SHARED_BY}

    for words in stemmed:
        shared = words & wanted
        # Two words in common is a trade matched; one is only a match if that
        # word is one this shop uses to tell its services apart.
        if len(shared) >= 2 or (shared & telling):
            return True
    if any(words & wanted for words in stemmed):
        return None  # a modifier in common and nothing else: not ours to call
    # The list is not everything the business has written about itself. A shop
    # whose list says "general construction" still describes itself as doing
    # residential work, and refusing "something residential" off the list alone
    # would turn away its own trade. Saying no needs the word to be absent from
    # everything it has said, not just from the list.
    written = " ".join(
        str(getattr(organization, field, "") or "")
        for field in ("product_rules", "sales_prompt")
    )
    if _stems(written) & wanted:
        return None
    return False


async def check(organization, said: str, documents=()) -> Verdict:
    """Whether this request fits. The model reads it; this decides.

    The model is asked what the customer is after and where. Whether that is
    work the business does is then settled here, against the list the business
    itself keeps - so a model that is feeling helpful cannot admit a job the
    shop does not do, and a client cannot be told its own services are not its
    own. Where the business has listed nothing, there is nothing to decide
    with and the model's reading is all there is.
    """
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

    job = text("job")
    # The model named the job; the list decides whether it is ours. Its own
    # answer is kept only where the business has listed nothing to decide with.
    settled = supports(organization, job)
    return Verdict(
        job=job,
        service_fits=flag("service_fits") if settled is None else settled,
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


#: The job this business refused, kept for the whole conversation rather than
#: for as long as it happens to sit in the three-message window above.
REFUSED_KEY = "job_refused"


def refused_job(contact) -> str | None:
    """The work this business already told them it does not do, if any."""
    held = (getattr(contact, "contact_metadata", None) or {}).get(REFUSED_KEY)
    if isinstance(held, dict):
        job = str(held.get("job") or "").strip()
        return job or None
    return None


def forget_refusal(contact) -> None:
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    if metadata.pop(REFUSED_KEY, None) is not None:
        contact.contact_metadata = metadata


def _remember_refusal(contact, verdict: Verdict) -> None:
    # A refusal has to name the work it refuses. A model that says "no" without
    # saying no to what used to be written down as a refusal of "that", which
    # then stuck to the whole conversation: in production it answered "Sorry -
    # That is not something we do" to "what times do you have?" and went on
    # refusing the kitchen remodel the same customer had already been promised.
    if not verdict.job:
        return
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    metadata[REFUSED_KEY] = {"job": verdict.job, "place": verdict.place}
    contact.contact_metadata = metadata


#: The work this business DOES do that the same customer has asked for. Kept
#: because one conversation can hold both: "do you do kitchen remodels? ... and
#: can you groom my dog while you're here?" is one customer with two requests,
#: and a refusal of the second must not take the first down with it.
ACCEPTED_KEY = "job_accepted"


def accepted_job(contact) -> str | None:
    held = (getattr(contact, "contact_metadata", None) or {}).get(ACCEPTED_KEY)
    if isinstance(held, dict):
        job = str(held.get("job") or "").strip()
        return job or None
    return None


def _remember_acceptance(contact, verdict: Verdict) -> None:
    if not verdict.job:
        return
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    metadata[ACCEPTED_KEY] = {"job": verdict.job}
    contact.contact_metadata = metadata


def remember_refusal(contact, job: str, place: str | None = None) -> None:
    """Keep a refusal that was decided somewhere other than a booking turn.

    Most refusals come from `for_contact`, which stores its own. This one is
    for the path that actually answers the customer first: "can you groom my
    dog?" is read as wanting a person, is refused by `booking.not_our_trade`
    before any booking code runs, and used to leave nothing behind. Four
    turns later "can you book me in?" names no job at all, so there was
    nothing left to refuse and the agent offered times for work it had
    already said it does not do.
    """
    _remember_refusal(contact, Verdict(job=job, service_fits=False, place=place, area_fits=None))


def _mentions(job: str | None, text: str) -> bool:
    """Is this message about that work? Decided on the words, not by asking.

    The scope check reads a window of the last three messages that looked like
    they were about a job, so a grooming request keeps being found in it three
    turns after it was answered. Whether THIS turn is about the refused work is
    a different question, and the customer's own words settle it.
    """
    wanted = _stems(job or "")
    return bool(wanted and wanted & _stems(text))


async def for_contact(organization, contact, said: str, documents=(), message: str = "") -> Verdict:
    """The verdict for what they have said, asked once per change in what they said.

    A refusal, once given, outlives the window. `note_said` keeps the last
    three messages that look like they are about a job, which is a cost
    control - the scope prompt must stay small against a shared per-minute
    budget - and it meant the *reason* a request was refused expired while
    the conversation was still going.

    A client found it on October 6: dog grooming was refused four turns
    running, and on the fifth the customer said "the 3:00 pm one please". By
    then the window held an address, a question about times and a time, and
    the grooming was gone, so the check was asked about a Miami address and
    had no reason to say no. The customer never argued. They only kept
    talking.

    So a "no" is held on the contact until they name different work. Anything
    that is not a fresh job - an address, a phone number, a time, a yes -
    leaves it standing.
    """
    # Asked once per message, and only about messages with something in them.
    # "yes", "ok", "3pm" name no work, so there is nothing to ask about: the
    # verdict already on the contact is the answer, and a model that is asked
    # anyway is a model given the chance to change its mind about a job it was
    # not shown.
    if not said or not worth_checking(said):
        return remembered(contact) or Verdict()
    metadata = dict(getattr(contact, "contact_metadata", None) or {})
    held = metadata.get(SCOPE_KEY) or {}
    if isinstance(held, dict) and held.get("key") == _key(said):
        return remembered(contact) or Verdict()
    verdict = await check(organization, said, documents)

    # A standing refusal is only lifted by a job this business does take. A
    # verdict with no job in it at all says nothing about the work and must
    # not clear one: that is precisely the turn the address arrives on.
    standing = refused_job(contact)
    if standing:
        # Only work they have named, and that this business takes, lifts it.
        # "service_fits: true" with no job in it is the model saying nothing
        # about the work - which is the answer every address, phone number
        # and "yes" produces, and the one that let this through.
        named_other_work = verdict.service_fits is True and bool(verdict.job)
        if named_other_work:
            forget_refusal(contact)
            metadata = dict(getattr(contact, "contact_metadata", None) or {})
        elif accepted_job(contact) and not _mentions(verdict.job, message or said):
            # Two requests in one conversation, one of them ours. This turn
            # does not mention the work that was refused, and there is work
            # this business agreed to do, so the turn is about that. The
            # refusal stays on the contact - it is simply not what is being
            # answered here. Without this, "what times do you have?" came back
            # "that is not something we do" to a customer who had already been
            # told yes to a kitchen remodel, because the window the check
            # reads still held their question about the dog.
            verdict = Verdict(
                job=None,
                service_fits=None,
                place=verdict.place,
                area_fits=verdict.area_fits,
            )
        else:
            verdict = Verdict(
                job=verdict.job or standing,
                service_fits=False,
                place=verdict.place,
                area_fits=verdict.area_fits,
            )

    if verdict.service_fits is False:
        _remember_refusal(contact, verdict)
        metadata = dict(getattr(contact, "contact_metadata", None) or {})
    elif verdict.service_fits is True:
        _remember_acceptance(contact, verdict)
        metadata = dict(getattr(contact, "contact_metadata", None) or {})

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
