"""What happens when the agent does not have the answer.

A customer who probes - "do you ship to Dubai?", "is this BPA-free?" - asks
things no document covers. Before this, the model was forbidden to say "our
team will get back to you" and had nothing else to say, so it tried, was
refused, tried again, fell through to the second provider and out the other
side thirty seconds later with "could you tell me a little more?".

Now the model says it does not know (see `llm_service.needs_team`), and this
module does the honest thing: a person is alerted, and the customer is told
exactly what happened - "passed to the team" only when somebody really was
told, otherwise where they can reach the business, taken from its own
documents.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from app.models import KnowledgeDocument
from app.services import notifications

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w])\+?\d[\d\s-]{7,}\d(?![\w])")


def passed_on(organization) -> str:
    return (
        "That's a good question, and I don't have the answer to hand. I've passed it to "
        "the team and they'll reply to you here."
    )


def reach_us(contact_line: str | None) -> str:
    if contact_line:
        return f"I'm sorry, I don't have that information. You can reach the team at {contact_line}."
    return "I'm sorry, I don't have that information. Is there anything else I can help with?"


async def contact_line(db, organization_id) -> str | None:
    """An email or phone number the business wrote in its own documents."""
    rows = (
        await db.execute(
            select(KnowledgeDocument.content).where(
                KnowledgeDocument.organization_id == organization_id,
                KnowledgeDocument.doc_type != "product",
            )
        )
    ).scalars().all()
    for content in rows:
        found = _EMAIL.search(content or "")
        if found and not found.group(0).lower().endswith((".png", ".jpg")):
            return found.group(0)
    for content in rows:
        found = _PHONE.search(content or "")
        if found:
            return found.group(0).strip()
    return None


async def handle(db, organization, contact, question: str, asked: str) -> str:
    """Alert a person about a question the agent could not answer; say so.

    Returns the reply to send. The alert is raised first and its destination
    checked, so "passed to the team" is never said to nobody.
    """
    reachable = await notifications.can_reach(db, organization)
    # Written down, so the reply a person gives can be taught to the agent.
    from app.services import taught

    await taught.record_question(db, organization.id, getattr(contact, "id", None), asked)
    who = getattr(contact, "name", None) or getattr(contact, "phone_number", None) or "A customer"
    await notifications.raise_and_send(
        db,
        organization,
        "unanswered",
        f"{who} asked something your agent couldn't answer",
        f"{who} asked:\n\n\u201c{asked.strip()[:300]}\u201d\n\n"
        + (
            "Your documents don't answer this, so they were told the team will reply. "
            "Open the conversation to answer them."
            if reachable
            else "Your documents don't answer this. They were given your contact details, "
            "not told the team would reply - no alert email or device was set up then."
        )
        + "\n\nNext time: once you've answered, your agent can learn that answer - "
        "you'll find it waiting in Setup \u2192 Learning to approve.",
        contact_id=getattr(contact, "id", None),
    )
    if reachable:
        return passed_on(organization)
    return reach_us(await contact_line(db, organization.id))
