"""What a shop's own document already says about how it operates.

A handbook or service sheet states the same things the setup form asks for:
when they are open, what they do, where they will travel to. Retyping it is
the work onboarding was supposed to remove, so the document is read for those
facts and they are offered back as a filled-in form.

Only what a customer-facing document actually contains is read:

  * opening hours, via `opening_hours`
  * the services they offer
  * the areas they serve

Deliberately NOT read: what the agent must never promise, its pricing rules,
and the words that should fetch a person. Those are instructions to an agent,
not descriptions of a business, and no handbook contains them - a parser
looking for them would be guessing at the shop's internal policy and writing
the guess into the thing that governs what customers are told.

Headings are what make this safe. A section is read only where the document
names it, so prose that happens to mention a city is not mistaken for a
service area. Missing a section costs a person a moment of typing they were
going to do anyway; inventing one puts words in a shop's mouth.
"""

from __future__ import annotations

import logging
import re

from app.services import opening_hours

logger = logging.getLogger(__name__)

# The headings a real document uses for each thing. Matched on the whole line,
# so "Services" is a heading and "Our services are booked out" is not.
HEADINGS: dict[str, tuple[str, ...]] = {
    "services": (
        "services", "our services", "services we offer", "services offered",
        "what we do", "what we offer", "work we do", "treatments", "products",
    ),
    "service_areas": (
        "areas we serve", "areas served", "service areas", "service area",
        "where we work", "areas we cover", "coverage", "we serve", "locations",
    ),
}

# Headings that belong to something else. A section stops when it reaches one,
# so the services list does not swallow the opening hours underneath it.
OTHER_HEADINGS = (
    "opening hours", "hours", "business hours", "opening times", "our hours",
    "payment", "payments", "pricing", "prices", "price list", "contact",
    "contact us", "about", "about us", "terms", "policies", "policy",
    "delivery", "returns", "warranty", "guarantee", "booking", "appointments",
)

_BULLET = re.compile(r"^\s*(?:[-*•●▪–—]|\d+[.)])\s*")


def _split_heading(line: str) -> tuple[str, str]:
    """A line as (label, whatever followed a colon).

    Split rather than matched. The regex this replaced used a lazy quantifier
    beside a greedy tail, so "Services" was read as the label "S" followed by
    "ervices" - and every section silently failed to be found.
    """
    label, _, rest = line.partition(":")
    return label.strip(), rest.strip()

# A section is a list of short entries. Anything longer is a paragraph, and a
# paragraph about a service is not the name of one.
MAX_ITEM_CHARS = 160
MAX_ITEMS = 25


def _normalise_label(text: str) -> str:
    return re.sub(r"[^a-z ]+", "", text.strip().lower()).strip()


def _is_other_heading(line: str) -> bool:
    label = _normalise_label(line.split(":")[0])
    return label in OTHER_HEADINGS or any(
        label in names for names in HEADINGS.values()
    )


def _clean(entry: str) -> str:
    return _BULLET.sub("", entry).strip(" .;•-").strip()


# A list item is a name. A sentence is a statement about the business, and
# reading one as an item puts a sentence into a list the agent answers from.
#
# Both signals are needed, and the test runs on the raw line because `_clean`
# strips the trailing stop that carries half of it. Length alone judged
# "Wet room conversion - from $9,500" - six words - to be prose and threw
# away every service in the document.
PROSE_WORDS = 5


def _is_prose(line: str) -> bool:
    """Does this line read as a sentence rather than as an entry?"""
    stripped = (line or "").strip()
    if not stripped.endswith((".", "!")):
        return False
    return len(stripped.split()) >= PROSE_WORDS


def _split_inline(text: str) -> list[str]:
    """One line naming several things: "Miami-Dade, Broward and Palm Beach"."""
    # Not a comma inside a number. "Wet room conversion - from $9,500" is one
    # service, and splitting it produces a service called "500".
    parts = re.split(r",(?!\d)|;| and (?=[A-Z])", text)
    return [part.strip() for part in parts if part.strip()]


# A run of empty lines long enough to mean a real gap. Word documents come out
# of the extractor with a blank line between every single paragraph, so one
# blank separates two items in the same list just as often as it separates two
# sections - which made "stop at the first blank line" end every section at its
# first entry, and read nothing at all out of a real .docx.
BLANK_RUN_ENDS_SECTION = 3


def _section(lines: list[str], start: int) -> list[str]:
    """The entries under a heading, stopping where the next section begins.

    The next heading is the terminator that can be trusted. A blank line
    cannot: see above.
    """
    collected: list[str] = []
    blanks = 0
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            blanks += 1
            if blanks >= BLANK_RUN_ENDS_SECTION:
                break
            continue
        blanks = 0
        if _is_other_heading(stripped):
            break
        entry = _clean(stripped)
        if not entry:
            continue
        if len(entry) > MAX_ITEM_CHARS:
            # A paragraph. Everything after it is prose too, so stop rather
            # than picking the short lines out of the middle of it.
            break
        if _is_prose(stripped):
            # A sentence, not a list item. "Delivery of fittings takes 2-3
            # working days." sits under the areas list in a real handbook and
            # was being read as a place the business travels to - and, worse,
            # its presence made the section two entries long, which stopped
            # the comma-splitting below from running at all. So the list read
            # "Miami-Dade, Broward, Palm Beach" as a single area.
            break
        collected.append(entry)
        if len(collected) >= MAX_ITEMS:
            break
    return collected


def _list_section(lines: list[str], names: tuple[str, ...]) -> list[str]:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 80:
            continue
        label, inline = _split_heading(stripped)
        if _normalise_label(label) not in names:
            continue

        # "Areas we serve: Miami-Dade, Broward" - the heading carries the list.
        if inline:
            found = _split_inline(inline)
            if found:
                return [item for item in (_clean(f) for f in found) if item][:MAX_ITEMS]

        found = _section(lines, index + 1)
        if len(found) == 1 and ("," in found[0] or " and " in found[0]):
            # A single line listing several. Commas mean separate entries here,
            # and one entry reading "Miami-Dade, Broward and Palm Beach" makes
            # the agent answer as though that were one place.
            split = [_clean(part) for part in _split_inline(found[0])]
            return [item for item in split if item][:MAX_ITEMS]
        return found
    return []


def extract(text: str) -> dict:
    """Everything this document states, in `agent_config` shape.

    Only keys the document actually states are present. An absent key means
    the document said nothing about it, which is different from saying it is
    empty - and it is the difference that lets a second document add to what a
    first one said without erasing it.
    """
    if not text:
        return {}

    found: dict = {}

    hours = opening_hours.parse(text)
    if hours:
        found["business_hours"] = hours

    lines = text.splitlines()
    for key, names in HEADINGS.items():
        entries = _list_section(lines, names)
        if entries:
            found[key] = entries

    return found


def describe(facts: dict) -> str:
    """What was read, in one line, for telling somebody."""
    if not facts:
        return "nothing it could use"

    parts: list[str] = []
    if facts.get("business_hours"):
        parts.append(opening_hours.describe(facts["business_hours"]))
    if facts.get("services"):
        parts.append(f"{len(facts['services'])} service(s)")
    if facts.get("service_areas"):
        parts.append(f"{len(facts['service_areas'])} area(s)")
    return "; ".join(parts)
