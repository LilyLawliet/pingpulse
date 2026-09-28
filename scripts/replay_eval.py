"""Replay scripted conversations against the real agent and score every reply.

    python scripts/replay_eval.py evals/mikus.json --backend https://pingpulse.duckdns.org --token pp_...
    python scripts/replay_eval.py evals/*.json --token pp_... --upload docs/test-stores/Mikus_Stationery_Catalogue.docx

Goes through the Test agent endpoint, so it is the real pipeline and the real
AI providers - nothing is sent to anyone and no lead is created. Run it
before a deploy and after a client uploads new documents. A client's own
suite is the fastest way to find out whether their documents were read.

Each suite is JSON:

    {"name": "...", "turns": [
        {"say": "I want 20 gel pens",
         "expect": ["2,200"],             every one must appear
         "expect_any": ["pack", "packs"], at least one must appear
         "forbid": ["22,000"]}            none may appear
    ]}

Turns run in order as one conversation. Every reply is also checked for what
no reply may ever contain, and the report says how often the AI was not used
and how long replies took - the two numbers a client feels first.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

# Never acceptable in any reply, for any business.
ALWAYS_FORBIDDEN = [
    (r"NEEDS[_ ]TEAM", "leaked the internal no-answer marker"),
    (r"PRICE FACTS|=== ", "leaked prompt text"),
    (r"our team will|get back to you|a representative will", "promised a follow-up itself"),
    (r"\bI(?:'m| am) (?:a )?(?:real|human|live) (?:person|human|agent)", "claimed to be a person"),
    (r"here (?:are|is) (?:the |some )?(?:photos?|pictures?|images?)", "announced photos (the sandbox never sends any)"),
    (r"could you tell me a little more about what you're looking for", "filler instead of an answer"),
]


def run_suite(client: httpx.Client, backend: str, token: str, suite: dict, slow_ms: int) -> dict:
    history: list[dict] = []
    results = []
    for turn in suite["turns"]:
        started = time.perf_counter()
        response = client.post(
            f"{backend}/api/v1/agent/simulate",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": turn["say"], "history": history},
            timeout=90,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        reply = body.get("reply") or ""
        low = reply.lower()
        problems = []
        if response.status_code != 200:
            problems.append(f"HTTP {response.status_code}: {response.text[:120]}")
        if body.get("escalated"):
            problems.append(f"escalated ({body.get('reason')})") if not turn.get("escalates") else None
        for want in turn.get("expect", []):
            if want.lower() not in low:
                problems.append(f"missing {want!r}")
        if turn.get("expect_any") and not any(w.lower() in low for w in turn["expect_any"]):
            problems.append(f"none of {turn['expect_any']!r}")
        for bad in turn.get("forbid", []):
            if bad.lower() in low:
                problems.append(f"contains {bad!r}")
        for pattern, why in ALWAYS_FORBIDDEN:
            if re.search(pattern, reply, re.IGNORECASE):
                problems.append(why)
        if turn.get("needs_team") and not body.get("needs_team"):
            problems.append("should have gone to the team")
        results.append(
            {
                "say": turn["say"],
                "reply": reply,
                "provider": body.get("provider"),
                "ms": body.get("latency_ms") or elapsed,
                "why": body.get("why"),
                "problems": problems,
            }
        )
        history += [
            {"sender": "user", "content": turn["say"]},
            {"sender": "agent", "content": reply},
        ]
    return {"name": suite.get("name", "suite"), "results": results}


def report(runs: list[dict], slow_ms: int) -> int:
    total = failed = no_ai = slow = 0
    times = []
    for run in runs:
        print(f"\n=== {run['name']}")
        for r in run["results"]:
            total += 1
            times.append(r["ms"])
            if r["provider"] == "none":
                no_ai += 1
            if r["ms"] > slow_ms:
                slow += 1
            mark = "PASS" if not r["problems"] else "FAIL"
            failed += bool(r["problems"])
            print(f"[{mark}] {r['say']}  ({r['provider']}, {r['ms']} ms)")
            if r["problems"]:
                print("       reply: " + r["reply"].replace("\n", " ")[:300])
                for p in r["problems"]:
                    print(f"       - {p}")
                if r["why"]:
                    print(f"       why: {r['why'][:200]}")
    if not total:
        print("no turns run")
        return 1
    print(
        f"\n{total - failed}/{total} passed · AI not used on {no_ai} · "
        f"median {int(statistics.median(times))} ms, slowest {max(times)} ms, "
        f"{slow} over {slow_ms} ms"
    )
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("suites", nargs="+", help="suite files (globs allowed)")
    parser.add_argument("--backend", default="http://localhost:8000")
    parser.add_argument("--token", required=True, help="the client's access token")
    parser.add_argument("--upload", help="upload this document to the client first")
    parser.add_argument("--slow-ms", type=int, default=8000, help="a reply slower than this is reported")
    args = parser.parse_args()

    paths = [p for pattern in args.suites for p in sorted(glob.glob(pattern))]
    backend = args.backend.rstrip("/")
    with httpx.Client() as client:
        if args.upload:
            with open(args.upload, "rb") as handle:
                up = client.post(
                    f"{backend}/api/v1/knowledge/upload",
                    headers={"Authorization": f"Bearer {args.token}"},
                    files={"file": (Path(args.upload).name, handle, "application/octet-stream")},
                    timeout=120,
                )
            print(f"uploaded {args.upload}: HTTP {up.status_code} {up.text[:200]}")
        runs = [
            run_suite(client, backend, args.token, json.loads(Path(p).read_text()), args.slow_ms)
            for p in paths
        ]
    return report(runs, args.slow_ms)


if __name__ == "__main__":
    sys.exit(main())
