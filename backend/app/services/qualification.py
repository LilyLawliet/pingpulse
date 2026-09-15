"""What still needs asking before a lead is worth a person's time.

A sales agent that answers questions well and never finds out what the job is
produces conversations nobody can act on. This tracks the handful of facts that
turn "somebody asked about roofs" into a lead an operator can price: what the
job is, where it is, how big, what they will spend, when they need it, and
whether the person talking is the one who can say yes.

Two decisions shape it.

*Which slots matter is a per-tenant question.* A roofer needs the property's
ownership and a photograph; a salon needs a date and a service. So the slots are
configurable, with a general default set, and the agent is told which ones are
still missing rather than being handed a script to march through.

*It never interrogates.* The slots reach the prompt as "ask for at most one of
these, when it fits", because a customer asking a price and receiving six
questions leaves. Filling the form is worth less than the conversation.

Extraction runs after the reply is already on its way, so it costs the customer
nothing, and it only ever adds: a slot the customer answered once is not
re-asked because a later message did not mention it.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# The general set. Deliberately short - every slot here is one more thing the
# agent might ask instead of selling.
DEFAULT_SLOTS = (
    ("job_type", "what they want done or bought"),
    ("location", "the town or area the work is in"),
    ("scope", "how big the job is, in their own words"),
    ("budget", "any figure or ceiling they named"),
    ("timeline", "when they need it"),
    ("decision_maker", "whether this person can approve the work"),
)

MAX_SLOT_CHARS = 200


def slots_for(organization) -> tuple[tuple[str, str], ...]:
    """Which slots this organization wants filled."""
    config = getattr(organization, "agent_config", None) or {}
    custom = config.get("qualification_slots")

    # An explicit empty list is an answer, not an absence: it means this shop
    # has turned qualification off, and the agent should go back to simply
    # answering questions. Only a missing key falls back to the defaults.
    if not isinstance(custom, list):
        return DEFAULT_SLOTS
    if not custom:
        return ()

    out: list[tuple[str, str]] = []
    for entry in custom[:10]:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            asks = str(entry.get("asks") or "").strip()
        else:
            name, asks = str(entry).strip(), ""
        if name:
            out.append((name[:40], asks[:120]))
    return tuple(out)


def missing(organization, collected: dict | None) -> list[tuple[str, str]]:
    """The slots still unanswered, in the order they were configured."""
    have = {k for k, v in (collected or {}).items() if v}
    return [(name, asks) for name, asks in slots_for(organization) if name not in have]


def as_prompt_block(organization, collected: dict | None) -> str:
    """What is known and what is worth finding out, for the prompt.

    The instruction to ask at most one thing is the important half. Without it
    a model handed a list of six gaps asks for all six, and a customer who
    wanted a price receives a form.
    """
    known = {k: v for k, v in (collected or {}).items() if v}
    outstanding = missing(organization, collected)
    if not known and not outstanding:
        return ""

    lines = ["=== WHAT WE KNOW ABOUT THIS JOB ==="]
    if known:
        lines += [f"- {name}: {value}" for name, value in known.items()]
        lines.append("Never ask again for anything listed above.")
    else:
        lines.append("Nothing yet.")

    if outstanding:
        lines.append("Still unknown: " + ", ".join(f"{name} ({asks})" if asks else name for name, asks in outstanding))
        lines.append(
            "Ask for AT MOST ONE of these, and only where it follows naturally from "
            "what they just said. Answer their question first. A customer who asked "
            "a price and got a list of questions leaves."
        )
    return "\n".join(lines)


EXTRACTION_PROMPT = """From the conversation below, extract only what the CUSTOMER
has actually stated about the job they want done.

Return strict JSON with exactly these keys:
{keys}

Rules:
- Use null for anything not clearly stated. Never guess, never infer.
- Use the customer's own words, shortened. Do not rephrase into a category.
- decision_maker: only if they said whether they can approve the work.
- Return JSON only, no explanation and no code fences.

CONVERSATION:
{conversation}

LATEST CUSTOMER MESSAGE:
{latest}
"""


async def extract(organization, history, latest_message: str) -> dict:
    """Pull job facts out of a conversation. Never raises.

    Runs after the reply has gone, so its latency never delays a customer, and
    any failure returns {} — qualification is an enhancement and must never
    break the reply path.
    """
    from app.services.llm_service import _call_groq, format_history

    slots = slots_for(organization)
    keys = "{" + ", ".join(f'"{name}": null' for name, _asks in slots) + "}"
    prompt = EXTRACTION_PROMPT.format(
        keys=keys, conversation=format_history(history), latest=latest_message
    )

    try:
        raw = await _call_groq(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.info("qualification extraction failed: %s", exc)
        return {}

    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.info("qualification extraction returned non-JSON")
        return {}

    allowed = {name for name, _asks in slots}
    found: dict[str, str] = {}
    for key, value in parsed.items():
        if key not in allowed:
            continue
        if isinstance(value, (int, float, bool)):
            value = str(value)
        if isinstance(value, str):
            value = value.strip()
            if value and value.lower() not in ("null", "none", "unknown", "n/a", "not stated"):
                found[key] = value[:MAX_SLOT_CHARS]
    return found


def merge(collected: dict | None, learned: dict) -> dict:
    """Add what was learned without overwriting what was already answered.

    Only ever adds. A customer who named their budget in the first message and
    said nothing about it in the fifth has not withdrawn it, and letting a
    later empty extraction clear the slot would have the agent asking again.
    """
    merged = dict(collected or {})
    for key, value in (learned or {}).items():
        if value and not merged.get(key):
            merged[key] = value
    return merged
