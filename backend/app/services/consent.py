"""Who may be messaged, and who may never be again.

Two separate reasons a conversation goes quiet on our side, kept apart because
they mean different things and are reversed differently.

*Opt-out* is the customer's decision. Somebody replied STOP. Nothing outbound
may reach them again — not an agent reply, not a scheduled follow-up, not a
pick-up of an old conversation — until they themselves ask to resume. This is
the one that carries legal weight, and it is enforced at the point of sending
rather than at each of the places that decide to send, because there are five
of those and there will be more.

*Takeover* is the shop's decision. A person has stepped into this conversation
and the agent should stay out of it. Messages still arrive, are stored, and
appear on the dashboard; nothing is generated. It is per contact, reversible in
one click, and says nothing about what a human may send.

The asymmetry matters: an opted-out customer must not receive a message a human
typed either, whereas a taken-over conversation is precisely one a human is
typing in.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# What counts as "stop messaging me". Matched as whole words against the whole
# message, because "stop" inside "don't stop sending me pictures" is not an
# opt-out and unsubscribing somebody who did not ask is its own failure.
#
# Kept to unambiguous, widely-recognised keywords. A customer writing "please
# take me off your list" is a real opt-out too, but catching that needs
# judgement rather than a word list, so it reaches a person instead.
OPT_OUT_KEYWORDS = (
    "stop",
    "unsubscribe",
    "opt out",
    "optout",
    "remove me",
    "cancel subscription",
    "do not message",
    "dont message",
)

# The narrow set that turns it back on. Deliberately narrower than the opt-out
# list: the cost of missing one is a customer who has to say it twice, and the
# cost of a false positive is messaging somebody who told us not to.
OPT_IN_KEYWORDS = ("start", "unstop", "resume", "subscribe")

_WORDS = re.compile(r"[a-z]+")


def _normalised(text: str) -> str:
    return " ".join(_WORDS.findall((text or "").lower()))


def _says(text: str, keywords: tuple[str, ...]) -> bool:
    normalised = _normalised(text)
    if not normalised:
        return False
    padded = f" {normalised} "
    return any(f" {keyword} " in padded for keyword in keywords)


def is_opt_out(text: str) -> bool:
    """Did this message ask us to stop?

    A message that is *only* the keyword, or contains it as a standalone word,
    counts. Anything longer than a short instruction does not: a paragraph
    mentioning "stop" in passing is a conversation, not an instruction, and
    treating it as one silently ends a customer relationship.
    """
    normalised = _normalised(text)
    if not normalised:
        return False
    if len(normalised.split()) > 6:
        return False
    return _says(text, OPT_OUT_KEYWORDS)


def is_opt_in(text: str) -> bool:
    normalised = _normalised(text)
    if not normalised or len(normalised.split()) > 4:
        return False
    return _says(text, OPT_IN_KEYWORDS)


def record_opt_out(contact) -> None:
    """Mark a contact as having asked us to stop, and stop any pending work.

    The follow-up token is cleared here as well as the flag being set. The flag
    is what blocks a send; clearing the token is what stops two nudges sitting
    in the broker waiting to try.
    """
    contact.opt_out = True
    contact.opt_out_at = datetime.now(timezone.utc)

    metadata = dict(contact.contact_metadata or {})
    metadata.pop("followup_token", None)
    metadata["opted_out_at"] = contact.opt_out_at.isoformat()
    contact.contact_metadata = metadata
    logger.info("contact %s opted out", contact.id)


def record_opt_in(contact) -> None:
    """They asked to hear from us again. Consent is timestamped, not assumed."""
    contact.opt_out = False
    contact.opt_out_at = None
    contact.consent_at = datetime.now(timezone.utc)

    metadata = dict(contact.contact_metadata or {})
    metadata["consented_at"] = contact.consent_at.isoformat()
    contact.contact_metadata = metadata
    logger.info("contact %s opted back in", contact.id)


def may_send(contact) -> bool:
    """May anything at all be sent to this contact?

    The single question every outbound path asks. Returns False only for an
    opt-out: a taken-over conversation is one a person is deliberately typing
    in, so it is not blocked here.
    """
    return not bool(getattr(contact, "opt_out", False))


def agent_may_reply(contact) -> bool:
    """May the *agent* generate a reply here?

    False when a person has taken the conversation over, and false when the
    customer has opted out — an opt-out stops the automation first of all.
    """
    if not may_send(contact):
        return False
    return bool(getattr(contact, "ai_enabled", True))
