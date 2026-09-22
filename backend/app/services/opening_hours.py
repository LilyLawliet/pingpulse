"""Read opening hours out of a document the shop already has.

Every shop's price list, policy sheet or service brochure already says when it
is open. Until now that sentence went into the knowledge base as prose, which
meant the agent could *quote* it to a customer while the booking code, which
reads `business_hours` from the config, still believed the shop had never set
any hours at all. The two disagreed, and the one that decided whether an
appointment was real was the one nobody had filled in.

So the hours are parsed out deterministically and written to the config, and
booking works off the same numbers the document states. Nothing here asks a
model what the hours are: a model that misreads "9-6" as "9pm-6am" books
somebody for one in the morning, which is the exact fault this is fixing.

Precision over recall, deliberately. A document with hours this cannot parse
leaves `business_hours` empty, which switches booking off, which makes the
agent hand over to a person and raise an alert - a visibly unfinished setup.
A document this parses *wrongly* gives the agent confident, invented
availability, and the customer only finds out when nobody turns up. The first
failure is loud and recoverable; the second is silent. When in doubt, parse
nothing.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Spellings that appear in real documents. "tues" and "thurs" are both in use;
# leaving them out means a table of hours that parses for five days and
# silently drops two.
#
# The two-letter forms - "mo", "tu", "we", "th", "fr", "sa", "su" - are
# deliberately absent. "We are open Saturday 9-5" would otherwise record
# Wednesday as well, off the word "we", and a shop would find itself taking
# appointments on a day it is shut.
DAY_WORDS: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tuesdays": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wednesdays": 2, "weds": 2, "wed": 2,
    "thursday": 3, "thursdays": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fridays": 4, "fri": 4,
    "saturday": 5, "saturdays": 5, "sat": 5,
    "sunday": 6, "sundays": 6, "sun": 6,
}
for _plural in ("mondays", "tuesdays", "wednesdays", "thursdays", "fridays"):
    DAY_WORDS.setdefault(_plural, DAY_WORDS[_plural[:-1]])

GROUP_WORDS: dict[str, tuple[int, ...]] = {
    "weekday": (0, 1, 2, 3, 4),
    "weekdays": (0, 1, 2, 3, 4),
    "weekend": (5, 6),
    "weekends": (5, 6),
    "daily": (0, 1, 2, 3, 4, 5, 6),
    "everyday": (0, 1, 2, 3, 4, 5, 6),
}

_DAY_ALTERNATION = "|".join(sorted(DAY_WORDS, key=len, reverse=True))
_GROUP_ALTERNATION = "|".join(sorted(GROUP_WORDS, key=len, reverse=True))

_DAY_TOKEN = re.compile(r"\b(" + _DAY_ALTERNATION + r")\b")
_GROUP_TOKEN = re.compile(r"\b(" + _GROUP_ALTERNATION + r")\b")
_EVERY_DAY = re.compile(r"\b(?:every\s+day|all\s+week|seven\s+days\s+a\s+week|7\s+days\s+a\s+week)\b")

# A range connector between two day words: "mon-fri", "monday to friday",
# "monday through friday".
_DAY_RANGE_JOIN = re.compile(r"^\s*(?:-|to|through|thru|till|until)\s*$")

_MERIDIEM = r"(a\.?m\.?|p\.?m\.?)"
_CLOCK = r"(\d{1,2})(?:[:.h](\d{2}))?\s*" + _MERIDIEM + r"?"
_TIME_RANGE = re.compile(
    _CLOCK + r"\s*(?:-|to|until|till|thru|through)\s*" + _CLOCK,
    re.IGNORECASE,
)

_CLOSED = re.compile(r"\b(closed|shut)\b", re.IGNORECASE)

# Without one of these, a bare "2-3" on a line that happens to mention a day
# is as likely to be "2-3 working days" as it is to be opening hours.
_HOURS_CUE = re.compile(
    r"\b(hour|hours|open|opens|opening|close|closes|closing|shut|timing|timings|"
    r"business|working|availab|appointment|appointments|visit|visits)\b",
    re.IGNORECASE,
)

# Normalised away before anything is matched, so an en dash, a non-breaking
# space or a full-width colon does not decide whether a shop can take bookings.
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_SPACES = dict.fromkeys(map(ord, "     "), " ")
_COLONS = {ord("："): ":"}


def _normalise(text: str) -> str:
    return text.translate(_DASHES).translate(_SPACES).translate(_COLONS)


def _to_minutes(hour: str, minute: str | None, meridiem: str | None) -> int | None:
    """One clock reading as minutes past midnight, or None if it is not one."""
    value = int(hour)
    minutes = int(minute) if minute else 0
    if minutes > 59:
        return None

    if meridiem:
        flat = meridiem.replace(".", "").lower()
        if value < 1 or value > 12:
            return None
        if flat == "am":
            value = 0 if value == 12 else value
        else:
            value = 12 if value == 12 else value + 12
    elif value > 23:
        return None

    return value * 60 + minutes


def _resolve(match: re.Match) -> tuple[int, int] | None:
    """A time range as (open, close) minutes past midnight.

    The awkward part is a document that writes one meridiem for the pair -
    "9 - 6pm", or "9am - 6". Each is resolved by trying the reading that
    produces a sane working day and rejecting the rest, rather than by
    assuming: assuming is how "9-6" became an appointment at one in the
    morning.
    """
    h1, m1, mer1, h2, m2, mer2 = match.groups()

    if mer1 and mer2:
        start, end = _to_minutes(h1, m1, mer1), _to_minutes(h2, m2, mer2)
    elif mer2:
        # "9 - 6pm": the opening time takes whichever meridiem leaves the shop
        # open for a positive stretch of the same day.
        end = _to_minutes(h2, m2, mer2)
        start = None
        for guess in ("am", "pm"):
            candidate = _to_minutes(h1, m1, guess)
            if candidate is not None and end is not None and candidate < end:
                start = candidate
                break
    elif mer1:
        start = _to_minutes(h1, m1, mer1)
        end = _to_minutes(h2, m2, mer1)
        if start is not None and (end is None or end <= start):
            end = _to_minutes(h2, m2, "pm" if mer1.replace(".", "").lower() == "am" else "am")
    else:
        # Bare numbers. 24-hour if that reads sensibly, otherwise the common
        # shop shorthand where "9-6" means until the evening.
        start, end = _to_minutes(h1, m1, None), _to_minutes(h2, m2, None)
        if start is not None and end is not None and end <= start and end + 12 * 60 < 24 * 60:
            end += 12 * 60

    if start is None or end is None:
        return None
    if end <= start:
        return None
    if end > 24 * 60:
        return None
    # A thirty-second opening or a thirty-hour one is a misread, not a shop.
    if end - start < 15:
        return None
    return start, end


def _days_in(segment: str) -> list[int]:
    """Which days this piece of text names, ranges expanded."""
    if _EVERY_DAY.search(segment):
        return list(range(7))

    found: list[int] = []

    for match in _GROUP_TOKEN.finditer(segment):
        found.extend(GROUP_WORDS[match.group(1)])

    tokens = list(_DAY_TOKEN.finditer(segment))
    index = 0
    while index < len(tokens):
        current = DAY_WORDS[tokens[index].group(1)]
        joined = False
        if index + 1 < len(tokens):
            between = segment[tokens[index].end():tokens[index + 1].start()]
            if _DAY_RANGE_JOIN.match(between):
                last = DAY_WORDS[tokens[index + 1].group(1)]
                # Wraps the week the way a sign does: "saturday - monday" is
                # three days, not a negative range that silently adds nothing.
                span = (last - current) % 7
                found.extend((current + step) % 7 for step in range(span + 1))
                index += 2
                joined = True
        if not joined:
            found.append(current)
            index += 1

    seen: list[int] = []
    for day in found:
        if day not in seen:
            seen.append(day)
    return seen


def _as_clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse(text: str) -> dict[str, dict[str, str]]:
    """Opening hours stated in this document, in `business_hours` shape.

    Returns `{}` when the document does not state them plainly enough to act
    on, which is a real answer and not a failure: an empty result leaves
    booking switched off and the agent handing over to a person.

    A day the document calls closed is simply left out, which is how the
    booking code already reads a day with no window.
    """
    hours: dict[str, dict[str, str]] = {}
    if not text:
        return hours

    for raw in _normalise(text).splitlines():
        line = raw.strip()
        if not line or len(line) > 200:
            # A whole paragraph on one line is prose, and prose is where a
            # stray "10-4" is talking about something else.
            continue

        closed = _CLOSED.search(line)
        window = _TIME_RANGE.search(line)
        if not window and not closed:
            continue

        # Days are named before the times they apply to, in every layout this
        # is meant to read: "Monday - Friday: 9am - 6pm".
        boundary = window.start() if window else closed.start()
        days = _days_in(line[:boundary].lower())
        if not days:
            continue

        if closed and (not window or closed.start() < window.start()):
            # Said shut, so nothing is recorded: a day with no window is
            # already how the booking code spells closed.
            for day in days:
                hours.pop(DAYS[day], None)
            continue

        if not _HOURS_CUE.search(line) and not (
            window.group(2) or window.group(3) or window.group(5) or window.group(6)
        ):
            # Bare hour numbers with no minutes, no am/pm and no word saying
            # these are opening times. "Monday: 2-3" is not worth a booking.
            logger.debug("ignoring an ambiguous time range: %r", line)
            continue

        resolved = _resolve(window)
        if resolved is None:
            continue
        opens, closes = resolved

        for day in days:
            name = DAYS[day]
            # First statement wins. A document that contradicts itself is not
            # resolved by preferring whichever line happened to come last.
            hours.setdefault(name, {"open": _as_clock(opens), "close": _as_clock(closes)})

    return hours


def describe(hours: dict[str, dict[str, str]]) -> str:
    """The parsed hours as one line, for telling somebody what was read."""
    if not hours:
        return "no opening hours"
    parts = [
        f"{day[:3].title()} {hours[day]['open']}-{hours[day]['close']}"
        for day in DAYS
        if day in hours
    ]
    return ", ".join(parts)
