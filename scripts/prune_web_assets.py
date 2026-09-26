"""Keep the dashboard's asset directory from growing without end.

This runs *on the VM*, against /opt/pingpulse/data/web, right after a new
bundle has been copied into place. Standard library only, host python3.

Why the old assets are there at all
-----------------------------------
`publish_web` copies the new bundle over the top of the old one rather than
replacing the directory, and that is deliberate: asset filenames carry a
content hash, so a browser that loaded the page a second before the switch can
still fetch the files that page was promised. Deleting them on publish would
break exactly the person who was mid-load.

So the old ones have to survive a publish. They just do not have to survive
*every* publish, forever - which is what was happening. Sixteen releases had
left 46 files and 7.3 MB behind, of which two were being served.

Why a manifest and not timestamps
---------------------------------
The obvious approach is to keep the newest files by mtime. It does not work
here: `deploy.sh` copies the whole directory with `cp -r`, which restamps
every file it touches, so after any backend deploy the assets all claim to
have been written at the same second. There is no per-build manifest in a
Vite build either, and each build's index.html is overwritten by the next, so
once a bundle is superseded there is nothing left on disk that says which
.css belonged with which .js.

Hence this file writes down what it saw. Each run records the asset names the
live index.html is asking for, keeps the last few of those records, and
deletes anything not named in them. That survives the restamping because it
does not consult the filesystem's opinion about age at all.

The first run
-------------
With no manifest yet there is nothing to say what the *previous* build was, so
that one transition loses its grace window and every superseded asset goes at
once. That is safe here and was checked rather than assumed: across the whole
of Caddy's retained access log, the only asset anyone had requested was the
current one. Every run after the first keeps KEEP_BUILDS worth of history.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# The current build plus the two before it. Two publishes is a generous grace
# window for a page load that is already in flight - and releases are minutes
# apart at worst, not seconds.
KEEP_BUILDS = 3

# Kept beside the web root rather than inside it. Everything under the web
# root is served, and this file has no business being fetchable - the logs on
# this host are full of scanners walking dotfile paths. It holds nothing
# sensitive, but serving it is not a decision anybody made.
MANIFEST_NAME = ".web-builds.json"
ASSET_PATTERN = re.compile(r"index-[A-Za-z0-9_-]+\.(?:js|css)")


def referenced(index_html: Path) -> list[str]:
    """The asset filenames this index.html actually asks for."""
    if not index_html.is_file():
        return []
    return sorted(set(ASSET_PATTERN.findall(index_html.read_text(encoding="utf-8"))))


def load(manifest: Path) -> list[dict]:
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt manifest must not take the dashboard's assets with it.
        # Starting over costs one grace window, which is recoverable; deleting
        # a live asset is not.
        print(f"  !! unreadable manifest ({exc}) - starting a new one")
        return []
    return data if isinstance(data, list) else []


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "/opt/pingpulse/data/web")
    assets = root / "assets"
    if not assets.is_dir():
        print(f"  !! no {assets} - nothing to prune")
        return 0

    live = referenced(root / "index.html")
    if not live:
        # Refusing to act is the safe branch: with nothing known to be live,
        # every file below would look prunable.
        print("  !! index.html names no hashed assets - pruning nothing")
        return 0

    manifest = root.parent / MANIFEST_NAME
    builds = load(manifest)

    # Only a genuinely new bundle earns an entry. Re-running after a backend
    # deploy, which republishes the same bundle, must not push the history out
    # by repeating what is already at the top of it.
    if not builds or builds[-1].get("assets") != live:
        builds.append(
            {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "assets": live}
        )
    builds = builds[-KEEP_BUILDS:]

    keep = {name for build in builds for name in build.get("assets", [])}
    # Belt and braces: whatever the manifest says, the bundle being served
    # right now is never a candidate for deletion.
    keep.update(live)

    # Written before anything is unlinked, not after. If this write failed
    # once the deleting had already happened, the next run would start from an
    # empty history and take the previous build with it - so a failure here
    # has to stop the run while it is still only a missed cleanup.
    try:
        manifest.write_text(json.dumps(builds, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  !! could not write {manifest} ({exc}) - pruning nothing")
        return 0

    removed, freed = 0, 0
    for path in sorted(assets.iterdir()):
        if not path.is_file() or path.name in keep:
            continue
        # Only ever a candidate if it looks like one of the bundles this file
        # tracks. A Vite build also drops hashed images and fonts in here, and
        # those are referenced from the CSS rather than from index.html - so
        # they are invisible to the manifest and would be swept away on the
        # first publish after somebody adds a logo.
        if not ASSET_PATTERN.fullmatch(path.name):
            continue
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError as exc:
            print(f"  !! could not remove {path.name}: {exc}")
            continue
        removed += 1
        freed += size

    kept = len([p for p in assets.iterdir() if p.is_file()])
    if removed:
        print(f"  pruned {removed} superseded asset(s), {freed / 1048576:.1f} MB")
    print(f"  {kept} asset file(s) kept, covering {len(builds)} build(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
