"""Ship a desktop release. One command, start to finish.

    python scripts/make_release.py --version 1.2.4 --notes "What changed"

That sets the version, builds, signs, uploads to the VM, and then fetches the
result back the way a client would to prove it actually works. Nothing else to
run, and no GitHub release involved — the repository is private, and release
assets on a private repository are not publicly downloadable, so the updater
only ever saw a 404 there. Clients poll /updates/latest.json on our own domain.

The verification at the end is not ceremony. This has failed silently twice —
once with a manifest pointing at a URL that 404d, once with the app polling a
different endpoint from the one being published to — and both times it looked
like a clean release from this side while every client quietly stayed put. An
updater that fails silently is worth checking from the outside.

Two versions must agree or clients download the same file forever: the one in
tauri.conf.json and the one in latest.json. Both come from --version, which is
the point of the script.

    --stage-only    build and sign without putting it in front of anyone

Everything is written under Drive D:.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(r"D:\pingpulse")
CONF = ROOT / "src-tauri" / "tauri.conf.json"
BUNDLE = ROOT / "src-tauri" / "target" / "release" / "bundle" / "nsis"
OUT = ROOT / "builds" / "desktop"
KEY = ROOT / ".secrets" / "updater.key"

BACKEND_URL = os.environ.get("PINGPULSE_URL", "https://pingpulse.duckdns.org")
PUBLIC_UPDATES_URL = f"{BACKEND_URL.rstrip('/')}/updates"

# The VM the installers are served from.
VM_NAME = os.environ.get("PINGPULSE_VM", "pingpulse-prod")
VM_ZONE = os.environ.get("PINGPULSE_ZONE", "me-central1-b")
GCP_PROJECT = os.environ.get("PINGPULSE_PROJECT", "pingpulse-508212")
REMOTE_UPDATES = "/opt/pingpulse/data/updates"
GCLOUD = os.environ.get("GCLOUD", r"D:\google-cloud-sdk\bin\gcloud.cmd")


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def set_version(version: str) -> str:
    config = json.loads(CONF.read_text(encoding="utf-8"))
    previous = config["version"]
    config["version"] = version
    CONF.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")

    # Cargo's version is separate and drifts if it is not set alongside.
    cargo = ROOT / "src-tauri" / "Cargo.toml"
    text = cargo.read_text(encoding="utf-8")
    text = text.replace(f'version = "{previous}"', f'version = "{version}"', 1)
    cargo.write_text(text, encoding="utf-8", newline="\n")

    print(f"  version {previous} -> {version}")
    return previous


def build(version: str) -> tuple[pathlib.Path, str]:
    if not KEY.is_file():
        raise SystemExit(
            f"  no signing key at {KEY}\n"
            "  Without it the build produces no signature and no client can update.\n"
            "  Generate one with: tauri signer generate -w .secrets/updater.key"
        )

    env = dict(
        os.environ,
        VITE_API_BASE_URL=BACKEND_URL,
        TAURI_SIGNING_PRIVATE_KEY=str(KEY),
        TAURI_SIGNING_PRIVATE_KEY_PASSWORD="",
        CARGO_TARGET_DIR=str(ROOT / "src-tauri" / "target"),
    )
    print("  building (this takes a few minutes) ...")
    run([str(ROOT / "frontend" / "node_modules" / ".bin" / "tauri.cmd"), "build"], env=env)

    installer = BUNDLE / f"PingPulse_{version}_x64-setup.exe"
    signature = BUNDLE / f"PingPulse_{version}_x64-setup.exe.sig"
    if not installer.is_file():
        raise SystemExit(f"  expected {installer.name}, which was not produced")
    if not signature.is_file():
        raise SystemExit(
            f"  {installer.name} was built but not signed.\n"
            "  Check that bundle.createUpdaterArtifacts is true in tauri.conf.json."
        )
    return installer, signature.read_text(encoding="utf-8").strip()


def write_manifest(version: str, installer: pathlib.Path, signature: str, notes: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # The URL the client downloads from — our own server, not a GitHub
    # release. The repository is private, so release assets there are not
    # publicly downloadable and the updater only ever saw a 404.
    url = f"{BACKEND_URL.rstrip('/')}/updates/{installer.name}"

    manifest = {
        "version": version,
        "notes": notes,
        "pub_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platforms": {
            "windows-x86_64": {"signature": signature, "url": url},
        },
    }

    (OUT / "latest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    shutil.copy2(installer, OUT / installer.name)
    shutil.copy2(installer.with_suffix(".exe.sig"), OUT / f"{installer.name}.sig")
    # Keep the stable filename too, for handing to a brand-new client.
    shutil.copy2(installer, OUT / "PingPulse_Setup.exe")

    print(f"  manifest  {OUT / 'latest.json'}")
    print(f"  installer {OUT / installer.name}")


def publish(version: str, installer: pathlib.Path) -> None:
    """Put the manifest and installer where the clients look.

    Clients poll PUBLIC_UPDATES_URL, served from the VM's data volume rather
    than a GitHub release: the repository is private, and release assets on a
    private repository are not publicly downloadable, so the updater only ever
    saw a 404.

    The installer is copied before the manifest, deliberately. The manifest is
    what advertises the new version, so writing it first would point every
    client at a download that is not there yet.
    """
    print("  uploading to the VM ...")
    run([
        GCLOUD, "compute", "scp",
        str(installer), str(OUT / "latest.json"),
        f"{VM_NAME}:/tmp/", f"--zone={VM_ZONE}", f"--project={GCP_PROJECT}", "--quiet",
    ])

    remote = (
        f"sudo cp /tmp/{installer.name} {REMOTE_UPDATES}/ && "
        f"sudo cp /tmp/latest.json {REMOTE_UPDATES}/ && "
        f"sudo chmod 644 {REMOTE_UPDATES}/{installer.name} {REMOTE_UPDATES}/latest.json"
    )
    run([
        GCLOUD, "compute", "ssh", VM_NAME, f"--zone={VM_ZONE}",
        f"--project={GCP_PROJECT}", "--quiet", "--command", remote,
    ])
    print("  uploaded")


def confirm(version: str, installer: pathlib.Path) -> bool:
    """Fetch what a client would fetch, and check it before trusting it.

    Publishing without this has gone wrong twice: once with a manifest pointing
    at a URL that 404d, once with the app polling a different endpoint from the
    one being published to. Both looked like a clean release from here, and
    both failed silently on the client — which is the worst way for an updater
    to break, because nothing ever reports it.
    """
    import hashlib
    import urllib.request

    print("  verifying as a client would ...")
    try:
        served = json.loads(
            urllib.request.urlopen(f"{PUBLIC_UPDATES_URL}/latest.json", timeout=60).read()
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED to fetch the manifest: {exc}")
        return False

    if served.get("version") != version:
        print(f"  manifest says {served.get('version')}, expected {version}")
        return False

    url = served["platforms"]["windows-x86_64"]["url"]
    try:
        downloaded = urllib.request.urlopen(url, timeout=300).read()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED to download the installer at {url}: {exc}")
        return False

    if hashlib.sha256(downloaded).hexdigest() != hashlib.sha256(installer.read_bytes()).hexdigest():
        print("  the installer being served is not the one that was signed")
        return False

    signature = (installer.parent / f"{installer.name}.sig").read_text(encoding="utf-8").strip()
    if served["platforms"]["windows-x86_64"]["signature"] != signature:
        print("  the signature being served does not match the one just produced")
        return False

    print(f"  verified — clients are being offered {version}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Build, sign and publish a desktop release.")
    parser.add_argument("--version", required=True, help="e.g. 1.1.0")
    parser.add_argument("--notes", default="Improvements and fixes.", help="Shown to clients")
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="Build and sign, but do not put it in front of clients yet",
    )
    args = parser.parse_args()

    if args.version.startswith("v"):
        parser.error("give the bare version, e.g. 1.1.0 — the tag gets the v")

    print(f"\nPreparing PingPulse {args.version}\n")
    set_version(args.version)
    installer, signature = build(args.version)
    write_manifest(args.version, installer, signature, args.notes)

    if args.stage_only:
        print()
        print("  Staged, not published. Clients are still on the previous version.")
        print("  Publish when ready:")
        print(f"      python scripts/make_release.py --version {args.version}")
        print()
        return 0

    publish(args.version, installer)
    if not confirm(args.version, installer):
        print()
        print("  PUBLISH FAILED VERIFICATION — check before saying it shipped.")
        return 1

    print()
    print(f"  PingPulse {args.version} is live.")
    print()
    print("  Existing clients update themselves on their next launch; nobody runs")
    print("  an installer. A brand-new machine gets builds/desktop/PingPulse_Setup.exe")
    print("  once, and never again.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
