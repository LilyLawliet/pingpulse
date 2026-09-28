"""Structured customer memory.

The model is not asked to remember anything. Facts live here, each recorded
with where it came from and how sure we are, so a stated budget outranks an
inferred one. Requirements the customer withdraws are marked inactive rather
than deleted, so "forget Instagram, I only want X" does not silently lose the
fact that Instagram was once wanted.

Shape stored on `crm_contacts.memory`:

    {
      "facts": {
        "budget": {"value": "3000", "source": "customer", "confidence": 1.0,
                    "updated_at": "..."},
        "colour_preference": {...}
      },
      "requirements": {
        "red lawn": {"active": true, "source": "customer", "updated_at": "..."}
      },
      "objections": [{"type": "price", "text": "...", "resolved": false, ...}],
      "commitments": ["wants delivery to Lahore before Eid"]
    }
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

# Confidence attached to a fact by where it came from.
SOURCE_CONFIDENCE = {
    "customer": 1.0,   # they said it in their own words
    "inferred": 0.6,   # the analyzer deduced it
    "operator": 1.0,   # a human typed it into the CRM
}

TRACKED_FACTS = (
    "budget",
    "colour_preference",
    "size",
    "city",
    "category_interest",
    "fabric_preference",
    "occasion",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty() -> dict[str, Any]:
    return {
        "facts": {},
        "requirements": {},
        "objections": [],
        "commitments": [],
        # Things the customer has turned down. Never offered again.
        "rejected_items": [],
    }


def normalise(memory: dict[str, Any] | None) -> dict[str, Any]:
    """Fill in any missing sections so callers can index without checking.

    The sections are deep-copied rather than aliased. Every helper below builds
    on `normalise`, and `contact.memory` is a plain JSON column with no mutation
    tracking: if the returned dict shared its lists and dicts with the one
    SQLAlchemy loaded, appending a rejection would mutate the loaded value too.
    The before and after images would then compare equal, no UPDATE would be
    emitted, and the change would be lost on commit — silently, and only from
    the second message onwards, because the first starts from an empty dict.
    """
    base = empty()
    if not isinstance(memory, dict):
        return base
    for key, default in base.items():
        value = memory.get(key)
        base[key] = deepcopy(value) if isinstance(value, type(default)) else default
    return base


def record_fact(
    memory: dict[str, Any],
    key: str,
    value: str,
    source: str = "customer",
) -> dict[str, Any]:
    """Write a fact, unless a more trustworthy one is already on file.

    An inference must never overwrite something the customer told us directly.
    """
    memory = normalise(memory)
    if not value or not str(value).strip():
        return memory

    confidence = SOURCE_CONFIDENCE.get(source, 0.5)
    existing = memory["facts"].get(key)
    if existing and existing.get("confidence", 0) > confidence:
        return memory

    memory["facts"][key] = {
        "value": str(value).strip(),
        "source": source,
        "confidence": confidence,
        "updated_at": _now(),
    }
    return memory


def add_requirement(memory: dict[str, Any], text: str, source: str = "customer") -> dict[str, Any]:
    memory = normalise(memory)
    key = text.strip().lower()
    if not key:
        return memory
    memory["requirements"][key] = {
        "text": text.strip(),
        "active": True,
        "source": source,
        "updated_at": _now(),
    }
    return memory


def drop_requirement(memory: dict[str, Any], text: str) -> dict[str, Any]:
    """Mark a requirement inactive. The history stays — it is not deleted."""
    memory = normalise(memory)
    key = text.strip().lower()
    entry = memory["requirements"].get(key)
    if entry:
        entry["active"] = False
        entry["updated_at"] = _now()
    return memory


def add_objection(memory: dict[str, Any], objection_type: str, text: str = "") -> dict[str, Any]:
    memory = normalise(memory)
    for existing in memory["objections"]:
        if existing.get("type") == objection_type and not existing.get("resolved"):
            return memory  # already open, do not stack duplicates
    memory["objections"].append(
        {"type": objection_type, "text": text, "resolved": False, "raised_at": _now()}
    )
    return memory


def resolve_objection(memory: dict[str, Any], objection_type: str) -> dict[str, Any]:
    memory = normalise(memory)
    for existing in memory["objections"]:
        if existing.get("type") == objection_type:
            existing["resolved"] = True
            existing["resolved_at"] = _now()
    return memory


def reject_item(memory: dict[str, Any], text: str) -> dict[str, Any]:
    """Record something the customer does not want.

    Stored deduplicated case-insensitively so "Blue" and "blue" are one entry.
    """
    memory = normalise(memory)
    clean = (text or "").strip()
    if not clean:
        return memory
    existing = {item.lower() for item in memory["rejected_items"]}
    if clean.lower() not in existing:
        memory["rejected_items"].append(clean)
    return memory


def rejected_items(memory: dict[str, Any]) -> list[str]:
    return list(normalise(memory)["rejected_items"])


def is_rejected(memory: dict[str, Any], text: str) -> bool:
    """Does this product or attribute match anything they turned down?"""
    lowered = (text or "").lower()
    if not lowered:
        return False
    return any(item.lower() in lowered for item in rejected_items(memory) if item)


def add_commitment(memory: dict[str, Any], text: str) -> dict[str, Any]:
    memory = normalise(memory)
    clean = text.strip()
    if clean and clean not in memory["commitments"]:
        memory["commitments"].append(clean)
    return memory


def open_objections(memory: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in normalise(memory)["objections"] if not o.get("resolved")]


def active_requirements(memory: dict[str, Any]) -> list[str]:
    return [
        entry["text"]
        for entry in normalise(memory)["requirements"].values()
        if entry.get("active")
    ]


def dropped_requirements(memory: dict[str, Any]) -> list[str]:
    return [
        entry["text"]
        for entry in normalise(memory)["requirements"].values()
        if not entry.get("active")
    ]


def apply_analysis(memory: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Fold one analyzer result into memory.

    Everything the analyzer reports is treated as customer-stated, because it
    is extracted from the customer's own words; anything it merely guessed is
    reported under `inferred` and stored at lower confidence.
    """
    memory = normalise(memory)

    for key in TRACKED_FACTS:
        value = analysis.get(key)
        if value:
            memory = record_fact(memory, key, value, source="customer")

    for key, value in (analysis.get("inferred") or {}).items():
        if key in TRACKED_FACTS and value:
            memory = record_fact(memory, key, value, source="inferred")

    for requirement in analysis.get("new_requirements") or []:
        memory = add_requirement(memory, requirement)

    for requirement in analysis.get("dropped_requirements") or []:
        memory = drop_requirement(memory, requirement)

    objection = analysis.get("objection")
    if objection and objection not in ("none", "null"):
        memory = add_objection(memory, objection, analysis.get("objection_text", ""))

    for commitment in analysis.get("commitments") or []:
        memory = add_commitment(memory, commitment)

    for rejected in analysis.get("rejected_items") or []:
        memory = reject_item(memory, rejected)

    return memory


def as_prompt_block(memory: dict[str, Any], contact: Any = None, greeting: bool = False) -> str:
    """Render memory for the prompt. Empty string when there is nothing to say.

    A message that only says hello gets none of it. "Hi" was answered with
    "since your wedding pair is secured, what are you looking for next?" -
    an old purchase, brought up unasked, from a catalogue the business had
    since replaced.
    """
    memory = normalise(memory)
    lines: list[str] = []
    if greeting:
        return ""

    facts = memory["facts"]
    if facts:
        lines.append("Known about this customer (do NOT ask for any of these again):")
        for key, entry in facts.items():
            label = key.replace("_", " ").capitalize()
            hedge = "" if entry.get("confidence", 0) >= 1.0 else " (unconfirmed)"
            lines.append(f"- {label}: {entry['value']}{hedge}")

    active = active_requirements(memory)
    if active:
        lines.append("They are looking for: " + "; ".join(active))

    dropped = dropped_requirements(memory)
    if dropped:
        lines.append(
            "No longer wanted (do not offer these again): " + "; ".join(dropped)
        )

    unresolved = open_objections(memory)
    if unresolved:
        lines.append(
            "Unresolved objection(s) — address before selling further: "
            + "; ".join(o["type"] for o in unresolved)
        )

    rejected = rejected_items(memory)
    if rejected:
        lines.append(
            "REJECTED — never recommend, suggest or mention these again: "
            + "; ".join(rejected)
        )

    if memory["commitments"]:
        lines.append("Commitments made: " + "; ".join(memory["commitments"]))

    if lines:
        lines.append(
            "Use the above only where it bears on what they are asking now. Do not open a "
            "reply by bringing up an earlier purchase, order or conversation."
        )
    return "\n".join(lines)
