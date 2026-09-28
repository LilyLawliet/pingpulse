"""Write every live access token to .secrets/tokens.md on Drive D:.

A token is shown once, when it is minted, and then it is a long random string
in a terminal that scrolls away. The database has it, but "ssh to the VM and
run psql" is not a way to find your own key — so this pulls them out and keeps
a readable copy where they can be found.

The production database is the source of truth, not an append-only log, so the
file is rewritten from scratch every run. That means it tells the truth about
what is revoked and what has expired, rather than accumulating tokens that
stopped working months ago.

    python scripts/save_tokens.py
    python scripts/save_tokens.py --print     also show them in the terminal

.secrets/ is gitignored and already holds the updater signing key. Nothing is
written outside Drive D:, and full tokens are never printed unless asked for —
a terminal is shared and scrollback is forever.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VAULT = REPO / ".secrets" / "tokens.md"

VM = "pingpulse-prod"
ZONE = "me-central1-b"
PROJECT = "pingpulse-508212"
# The bare `gcloud` on this path is a shell script, which Windows cannot
# execute directly — CreateProcess wants the .cmd wrapper beside it.
GCLOUD = (
    r"D:\google-cloud-sdk\bin\gcloud.cmd"
    if os.name == "nt"
    else "/d/google-cloud-sdk/bin/gcloud"
)

SEPARATOR = "\t"

# One line, and not a single quote character anywhere in it, both deliberately.
#
# On Windows gcloud is a .cmd, so whatever is handed to --command is re-parsed
# by cmd.exe on the way out. A quote inside it ends the argument early and the
# rest of the query is read as commands; a newline truncates it. So the
# separator is chr(9) rather than a quoted string, and a missing organization
# becomes chr(45) rather than the empty string — concat_ws drops NULLs
# silently, which would shift every column after it.
#
# Passing the query over stdin would avoid all of this, except that plink
# swallows stdin on the way to the VM and psql receives a stray keystroke.
QUERY = (
    "select concat_ws(chr(9), t.token, t.client_name, t.expires_at, t.is_active,"
    " coalesce(o.name, chr(45)), t.created_at,"
    " coalesce(t.last_used_at::text, chr(45)))"
    " from access_tokens t"
    " left join users u on u.id = t.user_id"
    " left join organizations o on o.id = u.active_organization_id"
    " order by t.created_at;"
)


class Token:
    def __init__(self, row: list[str]) -> None:
        (self.token, self.client, self.expires, active,
         self.organization, self.created, self.last_used) = row
        self.active = active == "t"

    @property
    def expired(self) -> bool:
        try:
            return datetime.fromisoformat(self.expires) < datetime.now(timezone.utc)
        except ValueError:
            return False

    @property
    def state(self) -> str:
        if not self.active:
            return "revoked"
        if self.expired:
            return "expired"
        return "active"

    @property
    def masked(self) -> str:
        return f"{self.token[:12]}...{self.token[-6:]}"

    @staticmethod
    def day(value: str) -> str:
        return (value or "")[:10] or "—"


def fetch() -> list[Token]:
    """Read the tokens off the production database."""
    remote = (
        f'sudo docker exec pingpulse-db psql -U pingpulse -d pingpulse -t -A -c "{QUERY}"'
    )
    gcloud = GCLOUD if Path(GCLOUD).exists() else "gcloud"

    result = subprocess.run(
        [gcloud, "compute", "ssh", VM, f"--zone={ZONE}", f"--project={PROJECT}",
         "--quiet", "--command", remote],
        capture_output=True,
        text=True,
        # gcloud on Windows needs its bundled Python; push.sh sets the same.
        env={**os.environ, "CLOUDSDK_PYTHON": r"D:\google-cloud-sdk\platform\bundledpython\python.exe"},
    )
    if result.returncode != 0:
        print(result.stderr.strip()[-400:], file=sys.stderr)
        raise SystemExit("  could not reach the production database")

    tokens = []
    for line in result.stdout.splitlines():
        row = line.strip().split(SEPARATOR)
        if len(row) == 9 and row[0].startswith("pp_live_"):
            tokens.append(Token(row))
    return tokens


def render(tokens: list[Token]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# PingPulse access tokens",
        "",
        f"Rewritten from the production database on {stamp}.",
        "Re-run `python scripts/save_tokens.py` after issuing or revoking one.",
        "",
        "**These are live credentials.** Anyone holding one can read and reply to",
        "that tenant's WhatsApp conversations. This file is gitignored; keep it that",
        "way, and send a token to a client over something better than email.",
        "",
        "To revoke one: `update access_tokens set is_active = false where token = '…';`",
        "",
        "| Client | Workspace | State | Expires | Last used |",
        "| --- | --- | --- | --- | --- |",
    ]
    for t in tokens:
        lines.append(
            f"| {t.client} | {t.organization or '—'} | {t.state} "
            f"| {t.day(t.expires)} | {t.day(t.last_used)} |"
        )

    lines += ["", "---", ""]
    for t in tokens:
        lines += [
            f"## {t.client}",
            "",
            "```",
            t.token,
            "```",
            "",
            f"- Workspace: {t.organization or 'none'}",
            f"- State: **{t.state}**",
            f"- Issued: {t.day(t.created)}   Expires: {t.day(t.expires)}",
            "",
        ]
        if t.state != "active":
            lines += [f"> This token no longer works ({t.state}).", ""]

    if not tokens:
        lines += ["No tokens exist yet. Issue one with `scripts/onboard_client.py`.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--print", dest="show", action="store_true",
        help="print the tokens in full as well as writing them",
    )
    args = parser.parse_args()

    tokens = fetch()
    VAULT.parent.mkdir(parents=True, exist_ok=True)
    VAULT.write_text(render(tokens), encoding="utf-8")

    active = sum(1 for t in tokens if t.state == "active")
    print(f"  {VAULT}")
    print(f"  {len(tokens)} token(s), {active} active\n")
    for t in tokens:
        value = t.token if args.show else t.masked
        print(f"    {t.client:<16} {t.state:<8} {value}")
    if not args.show and tokens:
        print("\n  Full values are in the file above (--print to show them here).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
