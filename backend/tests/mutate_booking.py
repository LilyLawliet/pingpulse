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
update the strings here.

Nothing is broken in place. The backend is copied to a private directory
first and every mutation, run and restore happens there, so this can be run
alongside anything else - a deploy, an editor, another test run - and can be
killed at any point without leaving a mutation behind.

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

Faults run side by side, one per CPU core, each in its own copy of the
backend; --jobs=N sets how many.
"""
import os, shutil, subprocess, sys, pathlib, tempfile

# ---------------------------------------------------------------- the sandbox
# The harness breaks real source files and restores them in a finally. That is
# fine until something else reads them in the meantime. On October 6 it was
# started in the background and booking.py was scp'd to production mid-run:
# the deploy happened to catch the file between two mutations, and could as
# easily have shipped a deliberately broken guard to two live clients. Killing
# the run skipped the finally and left a mutation in the working tree twice.
#
# So it no longer has the working tree to break. Everything below runs on a
# private copy, and the tree the rest of the machine is using is never written
# to at all - not during a run, not during a restore, not if this process is
# killed halfway through.
def _sandbox() -> pathlib.Path:
    here = pathlib.Path.cwd()
    if not (here / "app" / "services" / "booking.py").exists():
        sys.exit("run this from the backend directory")
    root = pathlib.Path(tempfile.mkdtemp(prefix="mutate-booking-"))
    shutil.copytree(
        here,
        root / "backend",
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".venv",
            "node_modules", ".git", "htmlcov",
        ),
    )
    return root


_ROOT = _sandbox()
os.chdir(_ROOT / "backend")

SRC = pathlib.Path("app/services/booking.py")
LLM = pathlib.Path("app/services/llm_service.py")
HANDOVER = pathlib.Path("app/services/handover_question.py")
ANALYZER = pathlib.Path("app/services/analyzer.py")
TASKS = pathlib.Path("app/tasks.py")
ORDERS = pathlib.Path("app/services/orders.py")
SCOPE = pathlib.Path("app/services/scope.py")
TAUGHT = pathlib.Path("app/services/taught.py")
OPS = pathlib.Path("app/api/operations.py")
WEBHOOK = pathlib.Path("app/api/webhook.py")
ROUTES = pathlib.Path("app/api/routes.py")
LANGS = pathlib.Path("app/services/languages.py")


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
for _path in (SRC, LLM, HANDOVER, ANALYZER, TASKS, ORDERS, SCOPE, TAUGHT, OPS, WEBHOOK, ROUTES, LANGS):
    SOURCES[_path], ENDINGS[_path] = _read(_path)


def _write(path: pathlib.Path, text: str, base: pathlib.Path = pathlib.Path(".")) -> None:
    (base / path).write_bytes(text.replace("\n", ENDINGS[path]).encode("utf-8"))

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
    # "cancel naming its own time is a move" lived here. Its guard is still in
    # place, but "a cancellation naming its own time is a move to it" now
    # covers everything it did and more (the 9 October retest), so no test can
    # tell it apart any longer.
    "past date rolls to next year": (
        "        if (ahead - today).days <= MAX_DAYS_AHEAD:\n            return ahead",
        "        return ahead",
    ),
    "time zone ignored": (
        "            if their_zone.key == getattr(zone, \"key\", str(zone)):\n                their_zone = None",
        "            their_zone = None",
    ),
    "no address needed": (
        '    if kind != "onsite":\n        return False\n',
        '    return False\n',
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
    "a booking read as a person is asked about": (
        "    return bool(analysis.get(\"wants_person\")) and not wants_booking(text)",
        "    return bool(analysis.get(\"wants_person\"))",
    ),
    "a yes to the handover question is ignored": (HANDOVER,
        "    return datetime.now(timezone.utc) - made <= timedelta(minutes=VALID_MINUTES)",
        "    return False",
    ),
    "address needed only once site visits are set up": (
        '    return config.get("require_address") is not False',
        '    return config.get("require_address") is not False and does_site_visits(organization)',
    ),
    "scope says not a service we do - ignored": (
        "    if verdict.service_fits is False and verdict.job:\n        forget_offer(contact)",
        "    if False:\n        forget_offer(contact)",
    ),
    "scope says not an area we serve - ignored": (
        "        and (verdict.area_fits is False or elsewhere)",
        "        and elsewhere",
    ),
    "scope not checked before dates": (
        "    if existing is None and (about_times or first_ask):",
        "    if False:",
    ),
    "state backstop off": (
        "        and (verdict.area_fits is False or elsewhere)",
        "        and verdict.area_fits is False",
    ),
    "phone 123 accepted": (
        "    return len(digits) < 7 or len(digits) > 15 or len(set(digits)) == 1",
        "    return len(digits) < 3",
    ),
    "bad details not raised while offering times": (
        "    _flag_bad_details(contact, turn)",
        "    pass",
    ),
    "a corrected phone clears the bad email too": (
        "            held.pop(field, None)",
        "            held.clear()",
    ),
    "a reading may drop a not": (ANALYZER,
        "    if not keeps_what_they_ruled_out(message, text):",
        "    if False:",
    ),
    "analyzer meeting flag trusted": (
        "    asked_meeting = is_meeting(text)\n",
        "    asked_meeting = is_meeting(text) or wants_meeting\n",
    ),
    "work the business does not do is handed to a person instead": (
        "    if verdict.service_fits is not False or not verdict.job:\n        return None\n",
        "    return None\n",
    ),
    "a yes books on details that cannot be used": (
        "        bad = unusable_details(contact)\n        if bad:\n",
        "        bad = []\n        if bad:\n",
    ),
    # Entries for another file name it first.
    "a time is said to be theirs with nothing booked": (
        "    booked = booked or (asserts_a_time_is_theirs(text) if appointment is None else None)\n",
        "    booked = booked\n",
    ),
    "a refusal expires out of the message window": (SCOPE,
        "    standing = refused_job(contact)\n",
        "    standing = None\n",
    ),
    "a valid email is checked and thrown away": (
        "    _keep_the_good_ones(contact, text, given)\n",
        "    pass\n",
    ),
    "the shop's own list does not settle what it does": (SCOPE,
        "        service_fits=flag(\"service_fits\") if settled is None else settled,\n",
        "        service_fits=flag(\"service_fits\"),\n",
    ),
    "a hard stop is handed back to the model to phrase": (
        "    if turn.reply is None and turn.refusal is not None and turn.refusal.reason in HARD_STOPS:\n",
        "    if False:\n",
    ),
    "the scope check waits until they ask about times": (
        "    if existing is None and (about_times or first_ask):\n",
        "    if existing is None and about_times:\n",
    ),
    "an appointment the customer invents is not checked": (
        "    if existing is None and asks_about_their_booking(text):\n",
        "    if False:\n",
    ),
    "an unknown appointment kind becomes the default again": (
        "        return Refusal(\n            \"unknown_kind\",\n            \"That isn't a kind of appointment this business books.\",\n        )\n",
        "        chosen_kind = default_kind(organization)\n",
    ),
    # The scope question asked twice in one turn, by a model that does not
    # have to answer it the same way twice. Five bugs came out of this.
    "the scope question is asked twice in one turn": (
        "        verdict = await scope.for_contact(\n"
        "            organization,\n"
        "            contact,\n"
        "            said,\n"
        "            await _business_documents(db, organization),\n"
        "            message=said,\n"
        "        )\n",
        "        verdict = await scope.check(\n"
        "            organization, said, await _business_documents(db, organization)\n"
        "        )\n",
    ),
    "a time is offered for work nobody named": (
        "        and scope.offered_services(organization)\n",
        "        and False\n",
    ),
    # The yes trusting what was read back instead of deciding again from the
    # record - which is where a standing refusal is checked at the yes now.
    "a standing refusal is not re-checked at the yes": (
        "        now = scope.what_is_being_booked(organization, contact)\n",
        "        now = scope.Bookable(service=held.get(\"service\"))\n",
    ),
    "an order the customer says they have is believed": (ORDERS,
        "    if not _ASKS_ABOUT_AN_ORDER.search(text or \"\"):\n        return None\n",
        "    return None\n",
    ),
    "a redelivered follow-up is sent again": (TASKS,
        "    claims = (metadata or {}).get(SENT_CLAIMS_KEY)\n"
        "    return isinstance(claims, list) and claim_key(token, attempt) in claims",
        "    return False",
    ),
    "Roman Urdu matched inside words": (LLM,
        '    hits = sum(1 for marker in _ROMAN_URDU_WORDS if marker.search(lowered))',
        '    hits = sum(1 for marker in ROMAN_URDU_MARKERS if marker in f" {lowered} ")',
    ),
    # "a booking need not be confirmed" lived here: it weakened the check that
    # the model's confirmation states the booked time. A booking's
    # confirmation is no longer written by the model at all - it is rendered
    # from the row (booking.handle_turn) - so no test can reach that check on
    # a booking any more, and the fault it stood for is covered by "a booking
    # that succeeded is left to the model to phrase".
    # The three the evidence run of 7 October found. Each one reproduces with
    # the model answering normally, so each one is here rather than in a note.
    "a refusal can never be lifted": (
        "    first_ask = scope.worth_checking(said) and (\n",
        "    first_ask = scope.refused_job(contact) is None and scope.worth_checking(said) and (\n",
    ),
    "the words that are not work are not stemmed": (SCOPE,
        "_NOT_WORK = frozenset(\n"
        "    {word for word in _ARRANGING | _ORDINARY}\n"
        "    | {_stem(word) for word in _ARRANGING | _ORDINARY}\n"
        ")\n",
        "_NOT_WORK = _ARRANGING\n",
    ),
    # Both halves of the taught framing, separately. A long answer is chunked
    # before it is embedded, so the question and the answer can land in
    # different chunks and each marker has to come off on its own.
    "a taught question is read out as an answer": (TAUGHT,
        "    asked = _ASKED.match(text)\n",
        "    asked = None\n",
    ),
    "a split taught answer keeps its marker": (TAUGHT,
        "    answered = _ANSWERED.match(text)\n",
        "    answered = None\n",
    ),
    # The one that survived two fixes: both anchored to the start of the
    # string, and the reader that showed it to the customer never gets a
    # string that starts there.
    "the framing is only taken off the start of a passage": (TAUGHT,
        '    text = _ANSWERED_LINE.sub("", text)\n',
        "    text = text\n",
    ),
    "a standing refusal answers every turn": (
        "        (asks_for_work(said) and names_ours) or scope.remembered(contact) is None\n",
        "        asks_for_work(said) or scope.remembered(contact) is None\n",
    ),
    # The client's report of 7 October: dog grooming, then details and a
    # Miami address, read back as a construction site visit and booked on
    # "yes". Each of these is one of the things that now stands in the way,
    # put back on its own.
    "a model's yes counts without the customer naming the work": (SCOPE,
        "    if verdict.service_fits is True and not named_by_them(\n",
        "    if False and not named_by_them(\n",
    ),
    "a booking is read back with no listed service": (
        "    if being_booked.service is None and (\n"
        "        being_booked.refused or (being_booked.needs_job and not meeting)\n"
        "    ):\n",
        "    if False:\n",
    ),
    "the yes does not check what was read back": (
        "        if (held.get(\"service\") or None) != now.service:\n",
        "        if False:\n",
    ),
    "the yes does not check the service again": (
        "        if now.service is None and (now.refused or (now.needs_job and not meeting)):\n",
        "        if False:\n",
    ),
    "a refusal leaves the read-back waiting on a yes": (SCOPE,
        "    metadata.pop(PENDING_KEY, None)\n",
        "",
    ),
    "a place reads as a trade": (SCOPE,
        "    return wanted - _place_words(organization, text) - _business_places(organization)\n",
        "    return wanted\n",
    ),
    "the first listed service is read back, not the closest": (SCOPE,
        "            if ranked > score:\n",
        "            if best is None:\n",
    ),
    "a reply may say yes to refused work": (
        "    refused = says_yes_to_refused(contact, reply)\n"
        "    if refused:\n"
        "        return not_our_trade_reply(refused)\n",
        "",
    ),
    "a reply may say yes to unlisted work while the model is busy": (
        "    if scope.agrees_to(reply, unlisted):\n",
        "    if False:\n",
    ),
    # The live WhatsApp test of 8 October.
    "a redelivered message is answered again": (WEBHOOK,
        "    if await claim_delivery(organization.id, payload.message_sid) is False:\n",
        "    if False:\n",
    ),
    "a second business on one handset answers too": (WEBHOOK,
        "        holder = await handset_held_elsewhere(db, channel, body.get(\"to\"))\n",
        "        holder = None\n",
    ),
    "a booking that succeeded is left to the model to phrase": (
        "    if turn.reply is None and turn.performed and turn.appointment is not None:\n",
        "    if False:\n",
    ),
    "a cancelled booking leaves the lead booked": (WEBHOOK,
        "    if appointment_turn.cancelled:\n"
        "        back = await pipelines.stage_after_cancel(db, organization.id, contact)\n",
        "    if False:\n"
        "        back = None\n",
    ),
    "the inbox shows the oldest messages, not the newest": (ROUTES,
        "    return list(reversed(result.scalars().all()))\n",
        "    return list(result.scalars().all())\n",
    ),
    "what work they need is asked forever": (
        "    if asked and scope._work_words(organization, text or \"\"):\n",
        "    if False:\n",
    ),
    "what the record holds is still asked for": (
        "    if learned:\n        contact.qualification = qualification.merge(",
        "    if False:\n        contact.qualification = qualification.merge(",
    ),
    "automatic nudges continue after a handover": (TASKS,
        "    if not manual and getattr(contact, \"ai_enabled\", True) is False:\n",
        "    if False:\n",
    ),
    # The Constrivo retest of 9 October.
    "a refusal covers only the first action in its list": (
        "    + r\"(?:\\s*(?:,|\\bor\\b|\\band\\b|\\bnor\\b)+\\s*(?:to\\s+)?\" + _REFUSED_ACTION + r\")*\",\n",
        "    ,\n",
    ),
    "a cancellation naming its own time is a move to it": (
        "    identifies_it = wants_cancel(text) and not names_other_time\n",
        "    identifies_it = False\n",
    ),
    "refusing a move holds a cancellation": (
        "    if stopping and wants_cancel(text) and not holding_off(_without_refusals(text)):\n",
        "    if False:\n",
    ),
    "a message in another language is read only as written": (
        "    return meaning\n\n\nasync def handle_turn(",
        "    return text\n\n\nasync def handle_turn(",
    ),
    "a yes after a refusal is left to the model": (
        "        if being_booked.service is None and being_booked.refused:\n            return TurnResult(\n                reply=(\n                    f\"There's nothing to confirm",
        "        if False:\n            return TurnResult(\n                reply=(\n                    f\"There's nothing to confirm",
    ),
    "unlisted work is asked what work it is": (
        "            named=scope.names_unmatched_work(organization, text),\n",
        "            named=asks_for_work(text) and scope.names_unmatched_work(organization, text),\n",
    ),
    "a translated fixed reply is not kept": (LANGS,
        "    kept = await _remembered(language, text)\n",
        "    kept = None\n",
    ),
    "the simulator checks scope on a contact with no memory": (OPS,
        "    # memory - so the sandbox forgot the conversation where WhatsApp would not.\n"
        "    pretend.contact_metadata = dict(state)\n",
        "",
    ),
}

args = sys.argv[1:]
together = "--together" in args
# One fault per worker at a time, each worker in its own copy of the backend.
# Run one after another, sixty-odd faults against the booking tests took an
# hour on a slow laptop and left seven of Cloud Build's eight cores idle.
jobs = os.cpu_count() or 1
for arg in list(args):
    if arg.startswith("--jobs="):
        jobs = max(1, int(arg.split("=", 1)[1]))
files = [a for a in args if a != "--together" and not a.startswith("--jobs=")]
if not files:
    sys.exit("name the test files to run against each fault")
# One column per file, or one column for all of them.
columns = {" + ".join(pathlib.Path(f).stem for f in files): files} if together else {f: [f] for f in files}


def _run(paths, cwd: pathlib.Path = pathlib.Path(".")):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *paths],
        capture_output=True, text=True, cwd=cwd,
    )


for column, paths in columns.items():
    baseline = _run(paths)
    if baseline.returncode != 0:
        sys.exit(
            f"{column} fails with nothing broken (pytest exit {baseline.returncode}); "
            "every fault would read as caught. Fix it first.\n" + baseline.stdout[-2000:]
        )

for name, mutation in MUTATIONS.items():
    path, old, new = mutation if len(mutation) == 3 else (SRC, *mutation)
    count = SOURCES[path].count(old)
    if count != 1:
        sys.exit(f"mutation {name!r} matches {count} times in {path}; fix the harness")

import concurrent.futures
import queue
import threading

# Each worker owns one copy for the whole run, and only ever writes inside it.
workers: "queue.Queue[pathlib.Path]" = queue.Queue()
for index in range(min(jobs, len(MUTATIONS))):
    copy = _ROOT / f"worker-{index}" / "backend"
    shutil.copytree(pathlib.Path("."), copy)
    workers.put(copy)
_broken: list[str] = []
_lock = threading.Lock()


def _try(name: str) -> dict:
    mutation = MUTATIONS[name]
    path, old, new = mutation if len(mutation) == 3 else (SRC, *mutation)
    base = workers.get()
    try:
        for other, text in SOURCES.items():
            _write(other, text, base)
        _write(path, SOURCES[path].replace(old, new), base)
        row = {}
        for column, paths in columns.items():
            proc = _run(paths, base)
            if proc.returncode not in (0, 1):
                with _lock:
                    _broken.append(
                        f"{column} could not run with {name!r} applied (pytest exit {proc.returncode}); "
                        "that is a broken mutation, not a caught one.\n" + proc.stdout[-2000:]
                    )
                row[column] = "broken"
                continue
            row[column] = "RED" if proc.returncode == 1 else "green"
        return row
    finally:
        workers.put(base)


results = {}
try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers.qsize()) as pool:
        rows = dict(zip(MUTATIONS, pool.map(_try, MUTATIONS)))
    # In the order they are written, whatever order they finished in.
    results = {name: rows[name] for name in MUTATIONS}
    if _broken:
        raise SystemExit("\n\n".join(_broken))
finally:
    # Nothing here is the working tree, so this is tidiness rather than repair.
    os.chdir(pathlib.Path.home())
    shutil.rmtree(_ROOT, ignore_errors=True)

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
