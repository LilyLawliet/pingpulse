"""The words that actually preceded a person stepping in.

`agent_config.ESCALATION_SIGNALS` is a list somebody wrote down: refund,
complaint, lawyer, manager. It is a good list and it is not this shop's list.
Every trade has words that mean trouble in it and nowhere else - "patch test"
in a salon, "failed MOT" in a garage, "chain" in an estate agency - and a
shop only finds out its list was incomplete when an alert it needed never
fired.

There is already a record of when the agent was out of its depth, and it is
not the model's opinion. It is the operator messages: the moments a person at
the shop read a conversation and typed into it themselves. That is a human
judgement, made at the time, about a real customer, and it is written down in
the messages table with its own sender label.

So: find the customer's own words immediately before a person took over, and
count which of them keep coming back.

Three rules keep this from producing a list that ruins the agent.

*Only what is missed.* A handover that `needs_escalation` already catches
teaches nothing - the word is in the list. Those are counted and reported, so
a shop can see how much of this it already has, but they are not mined for
candidates.

*Conversations, not messages.* One furious customer writing "disgusting" nine
times is one piece of evidence, not nine. Everything is counted once per
conversation.

*Precision, not frequency.* The commonest words before a handover are "the",
"is" and "price", because they are the commonest words anywhere. A candidate
is only worth proposing if it is *disproportionately* present before
handovers - so every phrase is also counted in conversations that went fine,
and one that shows up in both is dropped. This is the rule that matters: a
trigger that fires on ordinary messages sends an alert on every third
conversation, and a shop that stops reading its alerts misses the one it
needed. A missed word costs an alert. A noisy word costs the whole channel.

Nothing here is written. The endpoint returns candidates with their evidence
and the shop adds the ones it recognises, because "these six words came up
before somebody had to step in" is a finding, and only the shop knows which
of them mean trouble rather than just meaning Tuesday.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import SENDER_AGENT, SENDER_CUSTOMER, SENDER_OPERATOR, Message
from app.services import agent_config
from app.services.learning import scrub

logger = logging.getLogger(__name__)

# How far back to look. Longer than the voice window: handovers are rarer than
# replies, and a shop needs a year of them before the tail is worth reading.
DEFAULT_WINDOW_DAYS = 365

# A phrase has to precede a handover in at least this many separate
# conversations. Two is coincidence. This is the number below which a shop
# would be adding a trigger on the strength of one bad afternoon.
MIN_CONVERSATIONS = 3

# Of the conversations a phrase appears in, this share must be ones where a
# person stepped in. At 0.6 a phrase that shows up in ordinary chat as often
# as it does before a handover is dropped, which is the intent: the cost of a
# noisy trigger is a shop that stops reading its alerts.
MIN_PRECISION = 0.6

# Words per phrase. Single words catch "flooding", pairs catch "water damage",
# triples catch "still not finished". Beyond three a phrase is specific to one
# customer and will never match again.
MAX_PHRASE_WORDS = 3

MAX_CANDIDATES = 12
MAX_EXAMPLES = 2
EXAMPLE_CHARS = 180

# The customer messages before a handover, at most. A person scrolling back
# through a long thread before replying does not make the whole thread
# evidence; what they reacted to is at the end of it.
LOOKBACK_MESSAGES = 3

_WORD = re.compile(r"[a-z][a-z'-]*", re.IGNORECASE)

# Words that carry no signal about a trade. Not a general stopword list: it
# includes the WhatsApp filler that would otherwise dominate every count
# ("hi", "ok", "thanks", "please") precisely because those do cluster before
# a handover - a customer being polite at a person is not a trigger.
STOPWORDS = frozenset(
    """
    a about after again all also am an and any are aren as at back be because
    been before being but by can cant cannot could couldnt did didnt do does
    doesnt doing done dont down each even ever every for from get gets getting
    give go going good got had has have havent he her here hers him his how i
    id if ill im in into is isnt it its ive just know let like ll me more most
    much must my need needs no nor not now of off ok okay on once one only or
    other our out over own please pls re really right said same say see she
    should so some still such sure take tell than thank thanks that the their
    them then there these they thing things this those through to too up us
    very want wanted was wasnt we well were what when where which while who
    why will with would wouldnt yes yet you your youre hi hey hello dear sir
    madam morning afternoon evening cheers regards
    """.split()
)


@dataclass
class Candidate:
    """A phrase, and the evidence for and against making it a trigger."""

    phrase: str
    handovers: int = 0      # conversations where it preceded a person stepping in
    ordinary: int = 0       # conversations where it appeared and nobody had to
    examples: list[str] = field(default_factory=list)

    @property
    def seen_in(self) -> int:
        return self.handovers + self.ordinary

    @property
    def precision(self) -> float:
        return self.handovers / self.seen_in if self.seen_in else 0.0

    def as_dict(self) -> dict:
        return {
            "phrase": self.phrase,
            "handovers": self.handovers,
            "ordinary": self.ordinary,
            "precision": round(self.precision, 2),
            "examples": self.examples,
        }


@dataclass
class Report:
    """What the history says, including when it says nothing."""

    candidates: list[Candidate] = field(default_factory=list)
    conversations: int = 0
    handovers: int = 0
    already_caught: int = 0

    def as_dict(self) -> dict:
        return {
            "candidates": [c.as_dict() for c in self.candidates],
            "conversations": self.conversations,
            "handovers": self.handovers,
            "already_caught": self.already_caught,
            "enough_history": self.handovers >= MIN_CONVERSATIONS,
        }


def phrases(text: str) -> set[str]:
    """Every one-, two- and three-word phrase worth counting, once each.

    A set rather than a list: a customer who writes "leak" four times in one
    message is one mention of a leak.
    """
    words = [w.lower() for w in _WORD.findall(text or "")]
    found: set[str] = set()

    for size in range(1, MAX_PHRASE_WORDS + 1):
        for index in range(len(words) - size + 1):
            window = words[index : index + size]
            # A phrase made only of filler says nothing. One that *starts* or
            # *ends* on filler is the same phrase with a word stuck to it -
            # "the leak" and "leak" would otherwise both be proposed, and a
            # shop would be asked to choose between them.
            if window[0] in STOPWORDS or window[-1] in STOPWORDS:
                continue
            # At least one real word. Without this, "b c" out of a list of
            # bullet letters is a phrase, and two initials in a signature
            # become a candidate trigger.
            if not any(len(word) >= 3 for word in window):
                continue
            found.add(" ".join(window))

    return found


def _already_known(text: str, organization) -> bool:
    """Is this one the existing rules would already have caught?"""
    return agent_config.needs_escalation(text, organization) is not None


async def evidence(db, organization_id, organization, window_days: int = DEFAULT_WINDOW_DAYS) -> Report:
    """Phrases that keep turning up just before a person takes over.

    Reads only the customer's own words. The agent's replies are not evidence
    of anything - they are the output being judged - and an operator's are the
    signal rather than its content.
    """
    horizon = datetime.now(timezone.utc) - timedelta(days=window_days)

    rows = (
        await db.execute(
            select(Message)
            .where(
                Message.organization_id == organization_id,
                Message.created_at >= horizon,
                Message.sender.in_((SENDER_CUSTOMER, SENDER_AGENT, SENDER_OPERATOR)),
            )
            .order_by(Message.contact_id, Message.created_at)
        )
    ).scalars().all()

    report = Report()

    # Counted per conversation, then folded in once each, so a long thread
    # cannot outvote ten short ones.
    handover_counts: dict[str, int] = defaultdict(int)
    ordinary_counts: dict[str, int] = defaultdict(int)
    samples: dict[str, list[str]] = defaultdict(list)

    for contact_phrases, plain_phrases, examples, stats in _walk(rows, organization):
        report.conversations += 1
        report.handovers += stats["handovers"]
        report.already_caught += stats["already_caught"]

        for phrase in contact_phrases:
            handover_counts[phrase] += 1
            if len(samples[phrase]) < MAX_EXAMPLES and examples.get(phrase):
                samples[phrase].append(examples[phrase])
        # Only where it was not already counted as evidence for this
        # conversation, or a phrase said both before and after a handover
        # would count against itself.
        for phrase in plain_phrases - contact_phrases:
            ordinary_counts[phrase] += 1

    for phrase, count in handover_counts.items():
        candidate = Candidate(
            phrase=phrase,
            handovers=count,
            ordinary=ordinary_counts.get(phrase, 0),
            examples=samples.get(phrase, []),
        )
        if candidate.handovers < MIN_CONVERSATIONS:
            continue
        if candidate.precision < MIN_PRECISION:
            continue
        report.candidates.append(candidate)

    report.candidates = _prune(report.candidates)[:MAX_CANDIDATES]
    return report


def _walk(rows, organization):
    """One conversation at a time, as (evidence, all, examples, counts)."""
    current = None
    recent: list[Message] = []   # the customer's last few messages
    took_over = False            # a person has already stepped in this turn
    evidence_phrases: set[str] = set()
    all_phrases: set[str] = set()
    examples: dict[str, str] = {}
    stats = {"handovers": 0, "already_caught": 0}

    def finish():
        return evidence_phrases, all_phrases, examples, stats

    for row in rows:
        if row.contact_id != current:
            if current is not None:
                yield finish()
            current = row.contact_id
            recent, took_over = [], False
            evidence_phrases, all_phrases, examples = set(), set(), {}
            stats = {"handovers": 0, "already_caught": 0}

        if row.sender == SENDER_CUSTOMER:
            text = (row.content or "").strip()
            if not text:
                continue
            recent.append(row)
            recent[:] = recent[-LOOKBACK_MESSAGES:]
            all_phrases |= phrases(text)
            took_over = False
            continue

        if row.sender == SENDER_AGENT:
            # The agent answering does not end the customer's run: a person
            # very often steps in *after* reading a reply that missed the
            # point, and what the customer said is still what they reacted to.
            continue

        # An operator message: somebody at the shop typed into this thread.
        # That is the judgement this whole module is built on.
        if not recent or took_over:
            # Nothing preceding it, or a second reply in the same handover -
            # one person deciding once is one piece of evidence.
            continue

        took_over = True
        stats["handovers"] += 1

        preceding = " ".join((m.content or "").strip() for m in recent)
        if _already_known(preceding, organization):
            # The existing rules would have caught this. Counted so a shop can
            # see its coverage, but it teaches nothing new.
            stats["already_caught"] += 1
            recent = []
            continue

        for phrase in phrases(preceding):
            evidence_phrases.add(phrase)
            examples.setdefault(phrase, scrub(preceding)[:EXAMPLE_CHARS])
        recent = []

    if current is not None:
        yield finish()


def _prune(candidates: list[Candidate]) -> list[Candidate]:
    """Drop a longer phrase that says nothing its shorter form did not.

    "water damage" and "damage" both surviving means a shop is asked to pick
    between two triggers that fire on the same messages. The shorter one wins
    where it is just as precise, because it also catches "damage to the
    ceiling"; the longer one wins where it is meaningfully more precise, which
    is the case where the short word is ordinary and the pair is not.
    """
    ranked = sorted(
        candidates,
        key=lambda c: (c.precision, c.handovers, -len(c.phrase)),
        reverse=True,
    )

    kept: list[Candidate] = []
    for candidate in ranked:
        words = candidate.phrase.split()
        redundant = False
        for other in kept:
            shorter = other.phrase.split()
            if len(shorter) >= len(words):
                continue
            if not _contains(words, shorter):
                continue
            # The pair only earns its place by being clearly cleaner than the
            # word already kept.
            if candidate.precision <= other.precision + 0.15:
                redundant = True
                break
        if not redundant:
            kept.append(candidate)

    return sorted(kept, key=lambda c: (c.handovers, c.precision), reverse=True)


def _contains(words: list[str], sub: list[str]) -> bool:
    return any(
        words[i : i + len(sub)] == sub for i in range(len(words) - len(sub) + 1)
    )
