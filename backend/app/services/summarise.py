"""A standing summary of a conversation, and what to do about it next.

An operator opening a thread with sixty messages in it should not have to read
it from the top to find out where things stand. This keeps two short pieces of
text on the contact: what has happened, and the single thing worth doing next.

Three things keep it from becoming noise.

*It only runs when the conversation has moved.* Rewriting the summary after
"ok thanks" spends a model call to produce the same sentence, so it runs on a
cadence rather than every turn.

*It never blocks a reply.* It runs after the customer's message is already
answered and returns nothing on failure. A summary is a convenience; the reply
is the product.

*The next action is for the operator, not the agent.* "Send the quote for the
back roof" is a note to a person. The agent already has the conversation and
does not need telling.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Every few customer messages rather than all of them. A conversation that has
# gained one "ok" since the last summary has not changed enough to spend a
# model call on.
EVERY_N_MESSAGES = 4

MAX_SUMMARY_CHARS = 600
MAX_ACTION_CHARS = 120

PROMPT = """Summarise this WhatsApp conversation between a business and a customer,
for a colleague who is about to open it and has not read it.

Return strict JSON with exactly these keys:
{{"summary": "...", "next_action": "..."}}

Rules:
- summary: at most two sentences. What the customer wants and where things
  stand. No greeting, no preamble, no "the customer asks".
- next_action: the single most useful thing a PERSON at the business should do
  next, as an instruction of a few words. "Send a quote for the back roof."
  If there is genuinely nothing to do, use null.
- State only what the conversation shows. Never invent a price, a date or a
  commitment that was not made.
- Return JSON only, no explanation and no code fences.

CONVERSATION:
{conversation}
"""


def is_due(message_count: int, had_summary: bool) -> bool:
    """Has the conversation moved enough to be worth re-reading?

    The first summary is worth writing as soon as there is anything to say -
    a thread with three messages is already one somebody may open cold.
    """
    if not had_summary:
        return message_count >= 2
    return message_count > 0 and message_count % EVERY_N_MESSAGES == 0


async def write(history, latest_message: str = "") -> dict:
    """Produce {"summary", "next_action"}. Never raises; {} on any failure."""
    from app.services.llm_service import _call_groq, format_history

    conversation = format_history(history)
    if latest_message:
        conversation = f"{conversation}\nCustomer: {latest_message}"

    try:
        raw = await _call_groq(PROMPT.format(conversation=conversation))
    except Exception as exc:  # noqa: BLE001
        logger.info("could not summarise the conversation: %s", exc)
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
        logger.info("summary returned non-JSON")
        return {}

    out: dict[str, str] = {}
    summary = str(parsed.get("summary") or "").strip()
    action = str(parsed.get("next_action") or "").strip()

    if summary and summary.lower() not in ("null", "none"):
        out["summary"] = summary[:MAX_SUMMARY_CHARS]
    if action and action.lower() not in ("null", "none", "nothing"):
        out["next_action"] = action[:MAX_ACTION_CHARS]
    return out
