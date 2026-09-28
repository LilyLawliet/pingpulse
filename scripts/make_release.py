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
import re
import shutil
import subprocess
import sys
import tarfile
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

# The browser dashboard at /app, which is the same bundle the desktop app was
# just built around.
#
# It used to be published by deploy.sh alone, which copies from frontend/dist
# *on the VM* — and frontend/dist is in .gitignore, so it never arrived there.
# The desktop app bakes the bundle in at build time and kept moving; /app sat
# on whatever had been copied across by hand once, months earlier, and nothing
# reported the gap because both looked fine from their own side.
#
# Publishing it from here rather than from deploy.sh is what makes main.py's
# claim true: one build, shipped to both places in the same step, so they
# cannot drift apart again.
DIST = ROOT / "frontend" / "dist"
REMOTE_WEB = "/opt/pingpulse/data/web"
REMOTE_DIST = "/opt/pingpulse/app/frontend/dist"
PUBLIC_APP_URL = f"{BACKEND_URL.rstrip('/')}/app"


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

    # Merge rather than overwrite. One manifest serves every platform, and the
    # builds are produced on different machines — a macOS bundle cannot be made
    # on Windows — so writing this file from scratch on a Windows release would
    # silently delete the macOS entry and strand every Mac client on whatever
    # version they happened to have.
    existing = {}
    manifest_path = OUT / "latest.json"
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if previous.get("version") == version:
                existing = previous.get("platforms", {})
        except (ValueError, OSError):
            existing = {}

    manifest = {
        "version": version,
        "notes": notes,
        "pub_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platforms": {**existing, "windows-x86_64": {"signature": signature, "url": url}},
    }

    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    shutil.copy2(installer, OUT / installer.name)
    shutil.copy2(installer.with_suffix(".exe.sig"), OUT / f"{installer.name}.sig")
    # Keep the stable filename too, for handing to a brand-new client. The
    # signature is copied with it and never separately: this file pair has gone
    # out mismatched twice now — a 1.3.0 installer beside a 1.2.3 signature —
    # because only the executable was refreshed. A signature that belongs to a
    # different build is worse than none, since it looks like it was checked.
    shutil.copy2(installer, OUT / "PingPulse_Setup.exe")
    shutil.copy2(installer.with_suffix(".exe.sig"), OUT / "PingPulse_Setup.exe.sig")

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


def bundled_assets() -> list[str]:
    """The asset filenames the freshly built index.html actually asks for.

    Vite puts a content hash in every filename, so this is the one string that
    distinguishes this build from the one before it — and the only honest way
    to check which build a server is really handing out.
    """
    index = (DIST / "index.html").read_text(encoding="utf-8")
    return re.findall(r"assets/index-[A-Za-z0-9_-]+\.(?:js|css)", index)


def publish_web() -> bool:
    """Put the same bundle behind /app.

    Copied over the top rather than replacing the directory: asset filenames
    carry a content hash, so the old ones are inert, and a browser that loaded
    the page a second before the switch can still fetch the assets that page
    was promised. deploy.sh does the same thing for the same reason.

    It also lands in the VM's checkout of frontend/dist, so a later deploy.sh
    republishes this bundle instead of reverting /app to an older one.

    Not fatal. The desktop app is the product; the browser copy is the
    convenience, and a release that reached every desktop client should not be
    reported as failed because one copy step did not.
    """
    if not (DIST / "index.html").is_file():
        print("  !! no frontend/dist — /app keeps the bundle it already has")
        return False

    print("  publishing the browser dashboard ...")
    archive = OUT / "dashboard.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(DIST)).replace("\\", "/"))

    # Copied over the top, then pruned - in that order. The prune reads the
    # index.html that was just put in place to decide what is live, and keeps
    # the builds before it so a page load already in flight can still fetch
    # what it was promised. Without it this directory only ever grew: sixteen
    # releases had left 46 files and 7.3 MB behind, two of them being served.
    remote = (
        f"sudo mkdir -p {REMOTE_WEB} {REMOTE_DIST} && "
        f"sudo tar -xzf /tmp/dashboard.tar.gz -C {REMOTE_DIST} && "
        f"sudo cp -r {REMOTE_DIST}/. {REMOTE_WEB}/ && "
        f"sudo chmod -R a+rX {REMOTE_WEB} && "
        f"sudo python3 /tmp/prune_web_assets.py {REMOTE_WEB} && "
        # And the checkout it was copied from. The tarball is unpacked over
        # the top of that too, so it keeps every bundle it has ever held - and
        # deploy.sh copies it back over the served directory, which quietly
        # undoes the prune above on the next backend deploy.
        f"sudo python3 /tmp/prune_web_assets.py {REMOTE_DIST} && "
        "rm -f /tmp/dashboard.tar.gz /tmp/prune_web_assets.py"
    )
    try:
        run([
            GCLOUD, "compute", "scp", str(archive),
            str(ROOT / "scripts" / "prune_web_assets.py"),
            f"{VM_NAME}:/tmp/",
            f"--zone={VM_ZONE}", f"--project={GCP_PROJECT}", "--quiet",
        ])
        run([
            GCLOUD, "compute", "ssh", VM_NAME, f"--zone={VM_ZONE}",
            f"--project={GCP_PROJECT}", "--quiet", "--command", remote,
        ])
    except subprocess.CalledProcessError as exc:
        print(f"  !! could not publish /app: {exc}")
        return False
    finally:
        archive.unlink(missing_ok=True)
    return True


def confirm_web() -> bool:
    """Ask /app for its page and check it names this build's assets.

    """
    import urllib.request

    wanted = bundled_assets()
    if not wanted:
        print("  !! the built index.html references no hashed assets")
        return False

    try:
        request = urllib.request.Request(f"{PUBLIC_APP_URL}/")
        page = urllib.request.urlopen(request, timeout=60).read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! could not fetch {PUBLIC_APP_URL}/: {exc}")
        return False

    missing = [name for name in wanted if name not in page]
    if missing:
        print(f"  !! /app is serving an older bundle — it does not reference {missing}")
        return False

    print(f"  verified — {PUBLIC_APP_URL}/ is serving this build")
    return True


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

    # The browser copy, from the same dist the installer was just built
    # around. Reported separately and never fatal: the desktop release is
    # already verified in front of clients by this point, and saying it failed
    # because /app did not copy would be a worse lie than the one this whole
    # step exists to prevent.
    web = publish_web() and confirm_web()

    print()
    print(f"  PingPulse {args.version} is live.")
    print()
    print("  Existing clients update themselves on their next launch; nobody runs")
    print("  an installer. A brand-new machine gets builds/desktop/PingPulse_Setup.exe")
    print("  once, and never again.")
    print()
    if web:
        print(f"  In a browser: {PUBLIC_APP_URL}/")
    else:
        print(f"  {PUBLIC_APP_URL}/ was NOT updated — it is still serving an older")
        print("  bundle. The desktop release above is fine; only the browser copy")
        print("  needs another run.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
