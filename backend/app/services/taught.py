"""The agent learning from the shop, one approved answer at a time.

When the agent can't answer something, the question is written down. When a
person at the shop then replies to that customer from the dashboard, the reply
is kept beside the question. Both appear in Setup > Learning, and one press of
"Teach" makes it a passage the agent draws on from then on - so the next
customer who asks gets the shop's own answer, in the agent's words.

Why this is safe, and the rest of the design follows from it:

* **Only people's words.** The question is the customer's; the answer is
  typed by someone at the shop. The agent's own replies are never learned -
  a model learning from itself drifts, and repeats its own mistakes as fact.
* **Nothing is live until a person says so.** A reply to one customer can be
  about that customer ("your parcel left today"); it is shown, editable, and
  only taught when approved. Personal details are pointed out before then.
* **Taught answers are ordinary knowledge.** They go through the same
  retrieval as the documents, the same price checks and the same guard, and
  one press takes any of them back out.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import KnowledgeDocument, LearnedAnswer

logger = logging.getLogger(__name__)

SOURCE = "Answers you taught"

#: A taught passage is stored as the question and the answer together, so
#: retrieval finds it from a customer asking the same thing in different
#: words. The framing is for the search, not for the customer - and it was
#: being read out with the answer. In the evidence run of 7 October, "so how
#: much will the kitchen cost me?" came back "When a customer asks: How much
#: will my project cost? ...", and "where is my order?" came back "The answer
#: is: Financing is through GreenSky ...".
#:
#: The same fault as a knowledge base full of `OPEN:` and `DISCOVER:` lines:
#: a passage written for one reader, read aloud to another.
#:
#: Each half is matched on its own, because a long answer is chunked before
#: it is embedded and the two can end up in different chunks. The first fix
#: looked for both together and missed exactly that: the passage behind
#: "The answer is: Financing is through GreenSky..." had been split, so the
#: chunk the customer was shown began at the marker with no question above
#: it. Either half, at the start of a passage, is framing.
_ASKED = re.compile(r"^\s*when a customer asks\s*:[^\n]*\n?", re.IGNORECASE)
_ANSWERED = re.compile(r"^\s*the answer is\s*:\s*", re.IGNORECASE)

#: The same two markers, wherever a line starts with one. Passages are joined
#: before some readers see them - the quote panel reads every chunk of a
#: source as one text, separated by blank lines - so a marker that began a
#: passage ends up in the middle of a string, where an anchored pattern
#: cannot reach it. That is how "The answer is: Financing is through
#: GreenSky..." was still being shown under "Quoted from your terms" after
#: two fixes: both of them could only see the start.
_ASKED_LINE = re.compile(r"^[ \t]*when a customer asks[ \t]*:[^\n]*\n?", re.IGNORECASE | re.MULTILINE)
_ANSWERED_LINE = re.compile(r"^[ \t]*the answer is[ \t]*:[ \t]*", re.IGNORECASE | re.MULTILINE)


def spoken(content: str) -> str:
    """A stored passage as a customer should hear it, without the framing.

    Line-wise rather than anchored, because the callers differ: one hands in
    a single chunk and another hands in every chunk of a document joined
    together. Both are passages a customer may be shown.
    """
    text = _ASKED_LINE.sub("", content or "")
    text = _ANSWERED_LINE.sub("", text)
    return text.strip() or (content or "")


def parts(content: str) -> tuple[str | None, str]:
    """The question this passage was taught against, and the shop's answer.

    Both halves matter and they matter to different readers. The question is
    the half a customer's words actually match - "how much will the kitchen
    cost me?" shares nothing with "we don't publish fixed prices" - so it is
    what the passage should be *found* by. The answer is the only half that
    should be read out.

    Returning them separately is what lets a passage be searched on one and
    quoted from the other. Stripping the framing on its own made every taught
    answer unreachable, which is the same fault the other way round.

    An ordinary passage has no question: `(None, the passage)`. So does a
    chunk that carries only the answer half - there is no question in it to
    be found by - but its marker is still taken off, because the second half
    of the pair is read out exactly like the first.
    """
    text = content or ""
    question = None
    asked = _ASKED.match(text)
    if asked:
        question = asked.group(0).strip()
        text = text[asked.end():]
    answered = _ANSWERED.match(text)
    if answered:
        text = text[answered.end():]
    body = text.strip()
    if not body:
        return None, content or ""
    return question, body

WAITING = "waiting"
SUGGESTED = "suggested"
TAUGHT = "taught"
DISMISSED = "dismissed"

# A reply this long after the question is not an answer to it.
ANSWER_WITHIN = timedelta(hours=48)
# Replies in a row, close together, are one answer written in pieces.
FOLLOW_ON = timedelta(minutes=15)
MAX_ANSWER = 1500

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PHONE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")
_ORDER_REF = re.compile(r"#\s?\d{3,}|\border\s+(?:no\.?|number)?\s*\d{3,}", re.IGNORECASE)
_TODAY_ONLY = re.compile(
    r"\b(your (?:order|parcel|package|appointment|booking|payment)|today|tomorrow|just now|"
    r"shortly|this morning|tonight)\b",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def watch_out(answer: str, customer_name: str | None = None) -> list[str]:
    """What in this answer may be about one customer rather than true for all."""
    notes: list[str] = []
    text = answer or ""
    if _EMAIL.search(text) or _PHONE.search(text):
        notes.append("It contains a phone number or email address.")
    if _ORDER_REF.search(text):
        notes.append("It mentions an order number.")
    if customer_name and len(customer_name) > 2 and customer_name.lower() in text.lower():
        notes.append(f"It uses the customer's name ({customer_name}).")
    if _TODAY_ONLY.search(text):
        notes.append("It may be about this customer's own order or day - check it is true for everyone.")
    return notes


async def record_question(db, organization_id, contact_id, question: str) -> LearnedAnswer | None:
    """Write down a question the agent could not answer. Never raises."""
    try:
        question = (question or "").strip()[:500]
        if not question:
            return None
        recent = (
            await db.execute(
                select(LearnedAnswer).where(
                    LearnedAnswer.organization_id == organization_id,
                    LearnedAnswer.contact_id == contact_id,
                    LearnedAnswer.status == WAITING,
                )
            )
        ).scalars().all()
        for row in recent:
            # The same customer asking again before anyone replied is the same
            # question, not a second one.
            if _aware(row.asked_at) and _now() - _aware(row.asked_at) < timedelta(hours=1):
                return row
        row = LearnedAnswer(
            organization_id=organization_id,
            contact_id=contact_id,
            question=question,
            status=WAITING,
        )
        db.add(row)
        await db.flush()
        return row
    except Exception as exc:  # noqa: BLE001 - never at the cost of a reply
        logger.warning("could not record an unanswered question: %s", exc)
        return None


async def capture_reply(db, organization_id, contact_id, text: str) -> LearnedAnswer | None:
    """Keep what a person at the shop just replied, beside the question it answers.

    The first reply after the question becomes its suggested answer; replies
    that follow within a few minutes are the same answer continued. Never
    raises: a reply is sent whether or not it is learned from.
    """
    try:
        text = (text or "").strip()
        if not text or contact_id is None:
            return None
        rows = (
            await db.execute(
                select(LearnedAnswer)
                .where(
                    LearnedAnswer.organization_id == organization_id,
                    LearnedAnswer.contact_id == contact_id,
                    LearnedAnswer.status.in_((WAITING, SUGGESTED)),
                )
                .order_by(LearnedAnswer.asked_at.desc())
            )
        ).scalars().all()
        now = _now()
        for row in rows:
            if row.status == SUGGESTED:
                answered = _aware(row.answered_at)
                if answered and now - answered <= FOLLOW_ON:
                    row.answer = f"{row.answer}\n{text}"[:MAX_ANSWER]
                    row.answered_at = now
                    await db.flush()
                    return row
                continue
            asked = _aware(row.asked_at)
            if asked and now - asked <= ANSWER_WITHIN:
                row.answer = text[:MAX_ANSWER]
                row.status = SUGGESTED
                row.answered_at = now
                await db.flush()
                return row
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not keep a reply to learn from: %s", exc)
        return None


async def teach(db, row: LearnedAnswer, question: str, answer: str) -> LearnedAnswer:
    """Make an approved answer something the agent knows."""
    from app.services import retrieval

    question = (question or row.question or "").strip()[:500]
    answer = (answer or "").strip()[:MAX_ANSWER]
    if not question or not answer:
        raise ValueError("A question and an answer are both needed.")
    if row.knowledge_id:
        old = await db.get(KnowledgeDocument, row.knowledge_id)
        if old is not None:
            await db.delete(old)
    # Written as the question and the shop's answer, so retrieval finds it
    # from a customer asking the same thing in other words.
    document = await retrieval.index_document(
        db,
        organization_id=row.organization_id,
        title=question[:255],
        content=f"When a customer asks: {question}\nThe answer is: {answer}",
        source=SOURCE,
    )
    row.question = question
    row.answer = answer
    row.status = TAUGHT
    row.knowledge_id = document.id
    row.taught_at = _now()
    await db.flush()
    return row


async def untaught(db, row: LearnedAnswer) -> LearnedAnswer:
    """Take an answer back out of what the agent knows, or set a suggestion aside."""
    if row.knowledge_id:
        old = await db.get(KnowledgeDocument, row.knowledge_id)
        if old is not None:
            await db.delete(old)
    row.knowledge_id = None
    row.status = DISMISSED
    await db.flush()
    return row
