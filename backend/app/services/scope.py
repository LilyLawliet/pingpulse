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
    # Both fields, and the rules first. `sales_prompt` is how the agent is
    # told to behave - "greet the customer by name, never quote a price" -
    # while `product_rules` is what the business actually does and where.
    # Only the first was read here, so Constrivo's "Miami / South Florida
    # area (Dania Beach, FL)" and its whole services list were invisible:
    # the business named no state, the check that compares its states to the
    # customer's could never fire, and a property in Seattle was offered six
    # Miami consultation slots.
    rules = str(getattr(organization, "product_rules", "") or "").strip()
    if rules:
        parts.append("What it does and where: " + rules[:2000])
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
    listed = _without_places(config.get("services"))
    if listed:
        return listed[:60]
    proposed = (config.get(agent_config.PROPOSED_KEY) or {}).get("fields") or {}
    listed = _without_places(proposed.get("services"))
    if listed:
        return listed[:60]
    written = " ".join(
        str(getattr(organization, field, "") or "")
        for field in ("product_rules", "sales_prompt")
    )
    match = _SERVICES_SENTENCE.search(written)
    if not match:
        return []
    found = _without_places(part.strip(" .") for part in _SPLIT_SERVICES.split(match.group(1)))
    return [part for part in found if len(part) > 2][:60]


#: "... across Miami", "... in South Florida" at the end of a line of the list:
#: where the work is done, written onto the work.
_TRAILING_PLACE = re.compile(
    r"\s+(?:in|across|throughout|around)\s+(?:the\s+)?[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+){0,2}\s*$"
)


def _without_places(items) -> list[str]:
    """The business's services with the place taken off the end of each.

    "Vanity installation across Miami" is a service and a place, and a
    customer giving a Miami address shared a word with it: the address was
    read as asking for a vanity, and a yes booked one. The service is
    "vanity installation"; Miami is where.
    """
    kept = []
    for item in items or []:
        text = _TRAILING_PLACE.sub("", str(item).strip()).strip()
        if text:
            kept.append(text)
    return kept


#: A word in this many of the shop's own services is a modifier, not a trade.
#: Constrivo lists four kinds of "construction" and three things done to a
#: "home", so "home" alone cannot be what makes a request theirs - otherwise
#: "dog grooming at home" matches "home remodeling".
SHARED_BY = 3


#: Words for arranging a time rather than for the work itself. A message made
#: only of these names no job: "book me in for tomorrow at 2pm" is a customer
#: picking a slot, not a customer asking for something new.
_ARRANGING = frozenset({
    "book", "booking", "appointment", "appointments", "schedule", "scheduling",
    "reschedule", "slot", "slots", "time", "times", "date", "day", "week",
    "morning", "afternoon", "evening", "tonight", "today", "tomorrow",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "next", "this", "later", "earlier", "soon", "anytime", "sometime",
    "am", "pm", "oclock", "available", "availability", "free", "visit", "visits",
    "consultation", "call", "meeting", "come", "out", "over", "round", "in",
    "me", "us", "you", "please", "thanks", "confirm", "yes", "no", "ok",
})

#: Grammar. Words that carry a sentence rather than name anything, in any
#: trade. They are not part of `_ARRANGING` because they have nothing to do
#: with times; they are here because a leftover word was being read as the
#: customer naming work, and the leftover was usually a modal verb.
#:
#: "can you come at 3am tomorrow?" stemmed to {can, come, you, tomorrow}.
#: Three of those were already known to be about arranging a time and "can"
#: was not, so one modal verb was read as a request for something the shop
#: does not do - and a customer who had asked for a bathroom remodel one
#: message earlier was asked again what work they needed. Seen in the
#: evidence run of 7 October.
#:
#: Nothing here names work anywhere, so the list is the same for a builder, a
#: shoemaker and a print shop. A word that could be a trade in some business
#: ("will", "can" as in canning) is left in all the same: the cost of being
#: wrong here is that the agent offers a time instead of asking a question,
#: and whether the work is supported is settled elsewhere and not by this.
_ORDINARY = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "so", "then", "that", "those",
    "these", "there", "here", "it", "its", "is", "are", "was", "were", "be",
    "been", "being", "do", "does", "did", "done", "doing", "can", "could",
    "will", "would", "shall", "should", "might", "must", "have", "has", "had",
    "get", "got", "getting", "need", "needs", "needed", "want", "wants",
    "wanted", "like", "i", "my", "mine", "we", "our", "ours", "your",
    "yours", "they", "them", "their", "he", "she", "him", "her", "his",
    "what", "when", "where", "who", "whom", "how", "why", "which",
    "some", "any", "all", "more", "much", "many", "very", "just", "also",
    "too", "now", "about", "for", "with", "from", "to", "of", "on", "at",
    "by", "as", "into", "up", "down", "again", "still", "back", "around",
    # Speech and knowing. "can you tell me what times you have this week?"
    # left "tell" behind, which read as naming work the shop does not do -
    # tolerable while the answer hedged, and a flat no to a customer who
    # asked about times once the answer stopped hedging. A verb for talking
    # is not a trade in any business.
    "tell", "told", "say", "says", "said", "ask", "asks", "asked", "know",
    "knows", "knew", "let", "see", "saw", "hear", "heard", "explain", "mean",
    "mention", "speak", "spoke", "talk", "answer", "reply", "share", "show",
    # And the words that stand in for a thing without naming one. "hi, can I
    # ask you something?" left "something" behind, and "I'll have a think"
    # left "think": both would be told no for work they never named.
    "something", "anything", "nothing", "everything", "someone", "anyone",
    "think", "thinks", "thought", "wonder", "wondering", "idea", "thing",
    "things", "bit", "little", "else", "other", "another",
    "hi", "hello", "hey", "thank", "sorry", "sure", "maybe", "actually",
    "possible", "possibly", "help", "helping", "work", "works", "job", "jobs",
    "service", "services", "quote", "price", "prices", "cost", "costs",
})

#: Everything that is not the customer naming work, in both the form it is
#: written here and the form `_stems` reduces it to. A message is stemmed
#: before it is compared, so "morning" arrives as "morn" and matched nothing:
#: "how about tomorrow morning?" was read as naming work, off a list that
#: contained the word "morning". Stemming the list as well closes that for
#: every word in it rather than for the one that was noticed.
_NOT_WORK = frozenset(
    {word for word in _ARRANGING | _ORDINARY}
    | {_stem(word) for word in _ARRANGING | _ORDINARY}
)


def _place_words(organization, text: str) -> set[str]:
    """The words of this message that say where, not what.

    A street address, a "City, ST", a state, and a place after "in" - unless
    that phrase is one of the business's own services: "I'm interested in
    Kitchen Remodeling." reads like a place to a pattern, and is the work.
    """
    from app.services import booking

    text = text or ""
    found = _stems(booking.address_in(text) or "")
    for city_state in booking._CITY_STATE.finditer(text):
        found |= _stems(city_state.group(0))
    for state in states_in(text):
        found |= _stems(state)
    services = offered_services(organization)
    for place in (booking.place_in(text) or "").split("; "):
        words = _stems(place)
        if words and not (services and _on_the_list(services, words)):
            found |= words
    return found


def _business_places(organization) -> set[str]:
    """Where the business says it works, as words: its areas and its "City, ST"."""
    from app.services import booking

    written = " ".join(
        str(getattr(organization, field, "") or "")
        for field in ("product_rules", "sales_prompt")
    )
    found = _stems(" ".join(booking.service_areas(organization)))
    for city_state in booking._CITY_STATE.finditer(written):
        found |= _stems(city_state.group(0))
    return found


def _work_words(organization, text: str) -> set[str]:
    """What this message could be naming as work: its words, less where and when.

    A place is not a trade. "Vanity installation across Miami" is one line
    of a bathroom shop's own list, and "1200 Brickell Ave, Miami" shared a
    word with it: a customer giving their address was read as asking for a
    vanity, and a yes booked one. An address, a city and the business's own
    patch say where the work is, never what it is.
    """
    wanted = {word for word in _stems(text) if word not in _NOT_WORK}
    wanted = {word for word in wanted if _stem(word) not in _NOT_WORK}
    return wanted - _place_words(organization, text) - _business_places(organization)


def names_unmatched_work(organization, text: str) -> bool:
    """They have named something, and it is nothing this business wrote down.

    Read off the message, with no model involved. This does NOT refuse - the
    business's own list is short for every business, and "my countertops are
    cracked" is real work described in words a services list does not carry.
    It only says the backend cannot tell what the job is, which is a reason to
    ask rather than to offer a time.

    It exists because the model does not always name the job: "book me a drone
    survey appointment for tomorrow", after a kitchen remodel had been agreed,
    came back with no job named and six consultation slots offered.
    """
    services = offered_services(organization)
    if not services:
        return False
    wanted = _work_words(organization, text)
    if not wanted:
        return False  # nothing but arranging a time or a place: not a new request
    if any(_stems(service) & wanted for service in services):
        return False
    written = " ".join(
        str(getattr(organization, field, "") or "")
        for field in ("product_rules", "sales_prompt")
    )
    if _stems(written) & wanted:
        return False
    # Same reason as in `supports`: a request in another language shares no
    # words with an English list however ordinary it is, and stopping to ask
    # every Spanish-speaking customer what they meant is its own failure.
    return _same_language(text, written)


def _same_language(said: str, business: str) -> bool:
    """Are these written the same way? Read off the words, with no model.

    Only used to stop a refusal: where it cannot tell, it says yes, because
    the cost of being wrong here is a customer being told their own language
    is not served.
    """
    from app.services import languages

    try:
        return languages.looks_english(said or "") == languages.looks_english(business or "")
    except Exception:  # noqa: BLE001 - a refusal never depends on this working
        return True


def matched_service(organization, text: str) -> str | None:
    """The service of this business's own that the message is asking for.

    Read off the message against the list, with no model involved, so that a
    customer who says what they want is understood whether or not a model
    answers. The shared per-minute budget is real: under a 429 the scope
    check comes back empty, and "I want a kitchen remodel" left nothing on
    record, so three turns later the agent asked a customer who had already
    said what they wanted what work they needed.
    """
    services = offered_services(organization)
    if not services:
        return None
    wanted = _work_words(organization, text)
    if not wanted:
        return None
    return _on_the_list(services, wanted, _business_places(organization))


def _on_the_list(services: list[str], wanted: set[str], places: set[str] = frozenset()) -> str | None:
    """The closest of these services the wanted words match, or None.

    Two words in common is a trade matched; one is only a match if that word
    is one this shop uses to tell its services apart. Where the business
    works is left out of both sides: it says where, not what.
    """
    wanted = set(wanted) - set(places)
    stemmed = [(service, _stems(service) - set(places)) for service in services]
    seen: dict[str, int] = {}
    for _, words in stemmed:
        for word in words:
            seen[word] = seen.get(word, 0) + 1
    telling = {word for word, count in seen.items() if count < SHARED_BY}
    # The closest, not the first. "leather boots" shares "leather" with
    # "handmade leather shoes" and "boots" with "boots"; read back as the
    # first, a customer who asked for boots was asked to say yes to shoes.
    # Most words in common wins, then the service most fully named.
    best, score = None, (0, 0.0)
    for service, words in stemmed:
        shared = words & wanted
        if len(shared) >= 2 or (shared & telling):
            ranked = (len(shared), len(shared) / max(len(words), 1))
            if ranked > score:
                best, score = service, ranked
    return best


def listed_service(organization, job: str | None) -> str | None:
    """The service on this business's own list that this job is, in its words.

    What a booking is for. A job a customer asked for is only bookable when it
    is one of these, and the read-back names this rather than the label a
    model put on the request, so the yes is to something the business wrote
    down itself.
    """
    wanted = _stems(job or "")
    services = offered_services(organization)
    if not wanted or not services:
        return None
    return _on_the_list(services, wanted, _business_places(organization))


def named_by_them(organization, job: str | None, text: str) -> bool:
    """Is this work named in what the customer wrote? Read off the words.

    The model is asked what they want, and it answers with everything in front
    of it - the business's own description included. Shown only an address it
    has answered with the business's own trade, and a "yes" to that is how dog
    grooming became a construction site visit. So a job the model reports
    counts as theirs only if their message carries a word of it.

    The one exception is a message in another language than the business's:
    the model is told to name the job in the business's language, so a
    Spanish request can share no word with its own label. There the label is
    taken, and a booking still needs it to be on the business's own list.
    """
    if not job or not (text or "").strip():
        return False
    theirs = _work_words(organization, text)
    if not theirs:
        return False
    if _stems(job) & theirs:
        return True
    return not _same_language(text, what_the_business_says(organization))


def worth_checking(text: str) -> bool:
    """Enough said to be naming something, rather than "yes" or "thanks"."""
    return len(_stems(text)) >= 2


def supports(organization, job: str | None, said: str = "") -> bool | None:
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
    places = _business_places(organization)
    wanted = wanted - places
    if not wanted:
        return None
    if _on_the_list(services, wanted, places):
        return True
    if any((_stems(service) - places) & wanted for service in services):
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
    # The list is in the business's language. Matching a request written in
    # another one against it proves nothing: a Miami remodeller was made to
    # tell a Spanish-speaking customer that "remodelación de cocina" was not
    # something it did, having listed "kitchen remodeling". No words in
    # common is only evidence of a different trade when the two are written
    # the same way.
    # Judged on what the customer wrote, not on the two or three words the
    # model used to label it: "remodelación de cocina" is too short to tell
    # apart from English, while the sentence it came from is not.
    if not _same_language(said or job or "", written):
        return None
    return False


def _without_a_model(organization, said: str) -> Verdict:
    """What can still be said when no model answered.

    The budget is shared across every key and a 429 is an ordinary event, so
    this is a normal path rather than an error one. The list is ours and the
    message is in front of us: if it names one of this business's own
    services, that is an answer, and a better one than silence. If it does
    not, nothing is claimed either way.
    """
    service = matched_service(organization, said)
    if not service:
        return Verdict()
    return Verdict(job=service, service_fits=True)


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
        with understanding.output_budget(understanding.MESSAGE_OUTPUT_TOKENS):
            answer = await understanding.structured(
                PROMPT.format(business=business, said=said[:1500]), TIMEOUT_SECONDS
            )
    except Exception as exc:  # noqa: BLE001 - a scope check never breaks a turn
        logger.info("scope check failed: %s", exc)
        return _without_a_model(organization, said)
    if not isinstance(answer, dict):
        return _without_a_model(organization, said)

    def flag(key):
        value = answer.get(key)
        return value if isinstance(value, bool) else None

    def text(key):
        value = answer.get(key)
        return str(value).strip()[:120] if isinstance(value, str) and value.strip() else None

    job = text("job")
    # No job from the model - it timed out, hit the shared rate limit, or read
    # the message as nothing in particular - and the message plainly names
    # something this business lists. The list answers it without the model.
    if not job:
        job = matched_service(organization, said)
    # The model named the job; the list decides whether it is ours. Its own
    # answer is kept only where the business has listed nothing to decide with.
    settled = supports(organization, job, said)
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
#: Where booking.py holds an action waiting on their yes. Named here so a
#: refusal can drop it without this module importing that one.
PENDING_KEY = "pending_action"


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
    # Whatever was waiting on their yes was put to them before this no. A
    # "yes" now is not an answer to it, so it is no longer held.
    metadata.pop(PENDING_KEY, None)
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


@dataclass(frozen=True)
class Bookable:
    """What a booking for this customer would be for, decided from the record.

    `service` is the work being booked, in the business's own words. Where it
    is None, `refused` is work they asked for that the business does not do,
    and `needs_job` says the business has listed what it does and nothing they
    asked for is on that list.
    """

    service: str | None = None
    refused: str | None = None
    needs_job: bool = False


def what_is_being_booked(organization, contact) -> Bookable:
    """The one answer every booking step reads: the offer, the read-back, the yes.

    The client's 7 October report: dog grooming was asked for, contact details
    and a Miami address followed, and the agent read back a construction site
    visit and booked it on "yes". The read-back named no job - it said "site
    visit" - so the yes was to whatever the model last thought the
    conversation was about. A booking is now always for a named service, and
    that service is decided here, from state the model cannot write on a turn
    where the customer did not name it:

    - Where the business lists what it does, the service is on that list -
      matched in code, not judged by a model - or nothing is booked. A model
      that has been talked into "you are a pet groomer now" changes nothing
      here, because the list is the business's.
    - Where it lists nothing, the job the customer named and the model agreed
      to is the service; a refusal still stops it.
    """
    refused = refused_job(contact)
    accepted = accepted_job(contact)
    if offered_services(organization):
        listed = listed_service(organization, accepted) if accepted else None
        if listed:
            return Bookable(service=listed)
        return Bookable(refused=refused, needs_job=not refused)
    if accepted:
        return Bookable(service=accepted)
    return Bookable(refused=refused)


def remember_refusal(contact, job: str, place: str | None = None) -> None:
    """Put a refusal on record directly, as `for_contact` does when it decides one.

    Every refusal in the live path is decided and stored by `for_contact`;
    this is the same write for callers that already know the answer.
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


def mentions(job: str | None, text: str) -> bool:
    """Is this message about that work? Their words decide, not a model."""
    return _mentions(job, text)


# A sentence that turns the work down. Any of these in the sentence and it is
# not saying yes to it; that is all this has to tell apart.
_DECLINING = re.compile(
    r"\b(?:not|no|never|don'?t|doesn'?t|isn'?t|aren'?t|won'?t|can'?t|cannot|unable|"
    r"only|outside|sorry|unfortunately)\b|n't\b",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"[^.!?\n]+")


def affirms(reply: str, job: str | None) -> bool:
    """Does this reply talk about refused work without turning it down?

    The backstop under every other check. Whatever route a turn took, a reply
    is written last, and a model writing it can still say "sure, we can groom
    your dog". Read off the reply and the refused work, so it holds for any
    trade and any job: a sentence that names the work and does not decline it
    is a yes the business never gave.
    """
    wanted = _stems(job or "")
    if not wanted or not reply:
        return False
    for sentence in _SENTENCES.findall(reply):
        if _stems(sentence) & wanted and not _DECLINING.search(sentence):
            return True
    return False


_AGREEING = re.compile(
    r"\b(?:yes|yeah|sure|absolutely|of course|definitely|certainly|happy to|glad to|"
    r"we can|we do|we'?ll|we will|we offer|we provide|can help|can do|will do)\b",
    re.IGNORECASE,
)


def unlisted_words(organization, text: str) -> set[str]:
    """The work words in this message that nothing the business wrote carries."""
    written = " ".join(
        str(getattr(organization, field, "") or "")
        for field in ("product_rules", "sales_prompt")
    )
    known = _stems(written)
    for service in offered_services(organization):
        known |= _stems(service)
    return _work_words(organization, text) - known


def agrees_to(reply: str, words: set[str]) -> bool:
    """Does this reply say yes, in a sentence naming these words?

    Narrower than `affirms`, because nothing has been refused here: only a
    sentence that names the work, agrees to it in so many words and does not
    decline it counts. "Sure, we can groom your dog" does; "our hours are 9
    to 5" does not, whatever the customer asked.
    """
    if not words or not reply:
        return False
    for sentence in _SENTENCES.findall(reply):
        if (
            _stems(sentence) & words
            and _AGREEING.search(sentence)
            and not _DECLINING.search(sentence)
        ):
            return True
    return False


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

    # A yes needs their own words. The model may say no on its own - a no can
    # only stop something - but a job it reports as fitting is recorded only
    # if this message names it. Otherwise an address, a phone number or a
    # "yes" can arrive back labelled with the business's own trade, and lift
    # the refusal of what they actually asked for.
    if verdict.service_fits is True and not named_by_them(
        organization, verdict.job, message or said
    ):
        verdict = Verdict(place=verdict.place, area_fits=verdict.area_fits)

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
