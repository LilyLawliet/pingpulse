"""Put each fixed booking bug back, one at a time, and see which tests notice.

    cd backend && python tests/mutate_booking.py tests/test_readiness_replay.py

A test suite that passes proves nothing about what it would catch. This
breaks booking.py in each of the ways it used to be broken - the faults the
September 30 readiness test found, and the ones its replay found after - and
runs the given test files against each. Every row should read RED. A row
that reads green is a fault the tests would let back in.

It was used to prove the move of the replay's expectations into JSON lost
nothing: the hand-written version caught 10 of these, the JSON one all 14.

Each mutation must match booking.py exactly once, or the run stops: one that
no longer matches would silently test nothing. When booking.py is changed,
update the strings here. The file is restored afterwards whatever happens.
"""
import subprocess, sys, pathlib

SRC = pathlib.Path("app/services/booking.py")

# Read and write the bytes ourselves. read_text() decodes with the platform's
# encoding, which is cp1252 on Windows and cannot read this file at all; and
# the mutation strings below are written with \n, so the text has to be
# normalised to match them. Whatever line ending the file actually uses is
# put back, so a checkout with CRLF is restored as it was rather than
# rewritten wholesale.
_RAW = SRC.read_bytes().decode("utf-8")
_ENDING = "\r\n" if "\r\n" in _RAW else "\n"
ORIGINAL = _RAW.replace("\r\n", "\n")


def _put(text: str) -> None:
    SRC.write_bytes(text.replace("\n", _ENDING).encode("utf-8"))

MUTATIONS = {
    "holding_off ignored": (
        'def holding_off(text: str) -> bool:\n    """They named something and said not to do it yet."""\n    return bool(_HOLDING_OFF.search(text or ""))',
        'def holding_off(text: str) -> bool:\n    """They named something and said not to do it yet."""\n    return False',
    ),
    "bare digit picks a slot anywhere": (
        "        if word.isdigit():\n",
        "        if False:\n",
    ),
    "only the first place is read": (
        '    return "; ".join(places) or None',
        '    return places[0] if places else None',
    ),
    "claim guard not widened": (
        '    r"|(consultation|estimate|visit|appointment|booking|meeting|inspection)\\b[^.!?]{0,80}?\\b"\n'
        '    r"(is|are|has been|have been) (now )?(confirmed|booked|scheduled|reserved|set for)\\b"\n'
        '    r"|(we|i)(\'ve| have| has)? (got |now )?(a|an|the|your)\\b[^.!?]{0,50}?\\b"\n'
        '    r"(scheduled|booked|confirmed|reserved) (for|on)\\b"\n',
        "",
    ),
    "removed is not a cancel claim": (
        '    r"|(removed|taken off|deleted) (that|your|the|it)\\b"\n'
        '    r"|(has|have|is|was|were) (now )?(been )?(removed|deleted|taken off)"\n'
        '    r"|i(\'ve| have)? ?(now )?(removed|deleted)"',
        '    r"|(removed|taken off) (that|your) (booking|appointment)"',
    ),
    "refused reschedule reads as a move": (
        "def _without_refusals(text: str) -> str:\n    return _NOT_DOING.sub(\" \", text or \"\")",
        "def _without_refusals(text: str) -> str:\n    return text or \"\"",
    ),
    "cancel naming its own time is a move": (
        "        or (wants_cancel(text) and names_other_time)\n",
        "        or (wants_cancel(text) and (moment is not None or bool(named.days)))\n",
    ),
    "past date rolls to next year": (
        "        if (ahead - today).days <= MAX_DAYS_AHEAD:\n            return ahead",
        "        return ahead",
    ),
    "time zone ignored": (
        "            if their_zone.key == getattr(zone, \"key\", str(zone)):\n                their_zone = None",
        "            their_zone = None",
    ),
    "no address needed": (
        '    return kind == "onsite" and does_site_visits(organization)',
        "    return False",
    ),
    "areas not checked": (
        '    areas = service_areas(organization)\n    if not areas or not where:\n        return None',
        "    return None",
    ),
    "past days not refused up front": (
        "    gone = [day for day in named.days if day < today]",
        "    gone = []",
    ),
    "move to its own time allowed": (
        "    if _aware(appointment.starts_at) == starts_at:",
        "    if False:",
    ),
    "invalid contact details ignored": (
        "def contact_problems(text: str) -> list[str]:\n",
        "def contact_problems(text: str) -> list[str]:\n    return []\n",
    ),
}

files = sys.argv[1:]
results = {}
try:
    for name, (old, new) in MUTATIONS.items():
        count = ORIGINAL.count(old)
        if count != 1:
            sys.exit(f"mutation {name!r} matches {count} times; fix the harness")
        _put(ORIGINAL.replace(old, new))
        row = {}
        for f in files:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", f],
                capture_output=True, text=True,
            )
            row[f] = "RED" if proc.returncode else "green"
        results[name] = row
finally:
    _put(ORIGINAL)

width = max(len(n) for n in results)
print(" " * width, " | ".join(pathlib.Path(f).stem for f in files))
for name, row in results.items():
    print(name.ljust(width), " | ".join(row[f].ljust(len(pathlib.Path(f).stem)) for f in files))
