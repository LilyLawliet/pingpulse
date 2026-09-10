"""Build the desktop app and prepare a release clients will auto-update to.

The updater works by polling one small JSON file. This builds the signed
bundle, reads the signature Tauri produced, and writes that manifest next to
the installer so the whole set can be uploaded to a GitHub Release together.

    python scripts/make_release.py --version 1.1.0 --notes "Twilio settings"

Then publish (needs the GitHub CLI, authenticated):

    gh release create v1.1.0 --title "PingPulse 1.1.0" --notes-file notes.md \\
        builds/desktop/latest.json \\
        builds/desktop/PingPulse_1.1.0_x64-setup.exe \\
        builds/desktop/PingPulse_1.1.0_x64-setup.exe.sig

Three things must agree or clients get an update loop, downloading the same
version forever: the version in tauri.conf.json, the git tag, and the version
in latest.json. This script sets all three from one argument, which is the
point of it.

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and stage a desktop release.")
    parser.add_argument("--version", required=True, help="e.g. 1.1.0")
    parser.add_argument("--notes", default="Improvements and fixes.", help="Shown to clients")
    args = parser.parse_args()

    if args.version.startswith("v"):
        parser.error("give the bare version, e.g. 1.1.0 — the tag gets the v")

    print(f"\nPreparing PingPulse {args.version}\n")
    set_version(args.version)
    installer, signature = build(args.version)
    write_manifest(args.version, installer, signature, args.notes)

    print(f"""
  Publish it:

      cd D:\\pingpulse
      git add -A && git commit -m "release {args.version}" && git push
      gh release create v{args.version} \\
          --title "PingPulse {args.version}" --notes "{args.notes}" \\
          builds/desktop/latest.json \\
          builds/desktop/{installer.name} \\
          builds/desktop/{installer.name}.sig

  Existing clients pick it up the next time they open the app.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
