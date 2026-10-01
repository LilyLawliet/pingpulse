"""Put each fixed booking bug back, one at a time, and see which tests notice.

    cd backend && python tests/mutate_booking.py tests/test_readiness_replay.py

A test suite that passes proves nothing about what it would catch. This
breaks booking.py in each of the ways it used to be broken - the faults the
September 30 readiness test found, and the ones its replay found after - and
runs the given test files against each. Every row should read RED. A row
that reads green is a fault the tests would let back in.

It was used to prove the move of the replay's expectations into JSON lost
nothing: the hand-written version caught 10 of the first 14, the JSON one all
14. Entries naming llm_service.py break the reply step rather than booking.

Each mutation must match booking.py exactly once, or the run stops: one that
no longer matches would silently test nothing. When booking.py is changed,
update the strings here. The file is restored afterwards whatever happens.

It exits non-zero when any fault survives every file it was run with, so CI
can run it (cloudbuild.yaml does). Two things would make that lie, and both
are refused rather than reported:

- A file already failing before anything is broken reads RED for every
  fault. The files are run once untouched first, and must pass.
- Only a test failure counts as catching a fault (pytest exit 1). A file that
  collects nothing or cannot be imported also exits non-zero, and is an
  error, not a catch.

    python tests/mutate_booking.py --together tests/a.py tests/b.py

runs the files as one pytest call per fault: the question CI asks is whether
anything catches each one, not which file does, and it is six times faster.
"""
import subprocess, sys, pathlib

SRC = pathlib.Path("app/services/booking.py")
LLM = pathlib.Path("app/services/llm_service.py")


# Read and write the bytes ourselves. read_text() decodes with the platform's
# encoding, which is cp1252 on Windows and cannot read booking.py at all: the
# harness died before mutating anything, so nobody on Windows could check what
# the tests catch. The mutation strings below are written with \n, so the text
# is normalised to match them, and whatever ending the file actually uses is
# put back - restoring a CRLF checkout as plain \n rewrites every line of it,
# and a harness that hands your source back changed is worse than one that
# will not run.
def _read(path: pathlib.Path) -> tuple[str, str]:
    raw = path.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


SOURCES: dict[pathlib.Path, str] = {}
ENDINGS: dict[pathlib.Path, str] = {}
for _path in (SRC, LLM):
    SOURCES[_path], ENDINGS[_path] = _read(_path)


def _write(path: pathlib.Path, text: str) -> None:
    path.write_bytes(text.replace("\n", ENDINGS[path]).encode("utf-8"))

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
    "any reply to a read-back counts as yes": (
        "    if held is not None and agreed_to_it(text):",
        "    if held is not None:",
    ),
    "a yes naming another time counts as the yes": (
        "    return bool(one and other) and all(one.get(k) == other.get(k) for k in keys)",
        "    return bool(one and other)",
    ),
    "a no is not heard as a no": (
        "    if held is not None and _NO.match(text or \"\"):",
        "    if False:",
    ),
    "analyzer meeting flag trusted": (
        "    asked_meeting = is_meeting(text) or (wants_meeting and not does_site_visits(organization))",
        "    asked_meeting = is_meeting(text) or wants_meeting",
    ),
    # Entries for another file name it first.
    "Roman Urdu matched inside words": (LLM,
        '    hits = sum(1 for marker in _ROMAN_URDU_WORDS if marker.search(lowered))',
        '    hits = sum(1 for marker in ROMAN_URDU_MARKERS if marker in f" {lowered} ")',
    ),
    "a booking need not be confirmed": (LLM,
        "    done = \"booked\" if did_book else \"moved\" if did_move else \"cancelled\" if did_cancel else None",
        "    done = None",
    ),
}

args = sys.argv[1:]
together = "--together" in args
files = [a for a in args if a != "--together"]
if not files:
    sys.exit("name the test files to run against each fault")
# One column per file, or one column for all of them.
columns = {" + ".join(pathlib.Path(f).stem for f in files): files} if together else {f: [f] for f in files}


def _run(paths):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *paths],
        capture_output=True, text=True,
    )


for column, paths in columns.items():
    baseline = _run(paths)
    if baseline.returncode != 0:
        sys.exit(
            f"{column} fails with nothing broken (pytest exit {baseline.returncode}); "
            "every fault would read as caught. Fix it first.\n" + baseline.stdout[-2000:]
        )

results = {}
try:
    for name, mutation in MUTATIONS.items():
        path, old, new = mutation if len(mutation) == 3 else (SRC, *mutation)
        original = SOURCES[path]
        count = original.count(old)
        if count != 1:
            sys.exit(f"mutation {name!r} matches {count} times in {path}; fix the harness")
        for other, text in SOURCES.items():
            _write(other, text)
        _write(path, original.replace(old, new))
        row = {}
        for column, paths in columns.items():
            proc = _run(paths)
            if proc.returncode not in (0, 1):
                raise SystemExit(
                    f"{column} could not run with {name!r} applied (pytest exit {proc.returncode}); "
                    "that is a broken mutation, not a caught one.\n" + proc.stdout[-2000:]
                )
            row[column] = "RED" if proc.returncode == 1 else "green"
        results[name] = row
finally:
    for path, text in SOURCES.items():
        _write(path, text)

width = max(len(n) for n in results)
heads = {c: (c if together else pathlib.Path(c).stem) for c in columns}
print(" " * width, " | ".join(heads.values()))
for name, row in results.items():
    print(name.ljust(width), " | ".join(row[c].ljust(len(heads[c])) for c in columns))

survivors = [name for name, row in results.items() if "RED" not in row.values()]
if survivors:
    print(f"\n{len(survivors)} fault(s) no test catches:")
    for name in survivors:
        print(f"  - {name}")
    sys.exit(1)
print(f"\nAll {len(results)} faults caught.")
