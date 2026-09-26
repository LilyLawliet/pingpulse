"""The thing that deletes files off the production dashboard.

It lives in scripts/ and runs on the VM, so nothing else in this suite would
ever load it - but it is the only code in the repository whose job is to
unlink files from a directory the public is being served out of. The failure
that matters is not "an old file survived"; it is "the bundle being served
right now was deleted", which white-screens the dashboard for everybody until
somebody notices and republishes.

So the cases below are mostly about what it must refuse to do.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "prune_web_assets.py"
_spec = importlib.util.spec_from_file_location("prune_web_assets", _SOURCE)
prune = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune)


def _web(tmp_path: Path, live: list[str], present: list[str]) -> Path:
    """A dashboard directory: an index.html naming `live`, and `present` on disk."""
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    refs = "\n".join(f'    <script src="/app/assets/{n}"></script>' for n in live)
    (root / "index.html").write_text(f"<html>\n{refs}\n</html>", encoding="utf-8")
    for name in present:
        (root / "assets" / name).write_text("x" * 100, encoding="utf-8")
    return root


def _names(root: Path) -> set[str]:
    return {p.name for p in (root / "assets").iterdir() if p.is_file()}


def _builds(root: Path) -> list[dict]:
    return json.loads((root.parent / prune.MANIFEST_NAME).read_text(encoding="utf-8"))


A = ["index-AAAAAAAA.js", "index-AAAAAAAA.css"]
B = ["index-BBBBBBBB.js", "index-BBBBBBBB.css"]
C = ["index-CCCCCCCC.js", "index-CCCCCCCC.css"]
D = ["index-DDDDDDDD.js", "index-DDDDDDDD.css"]


# ===================================================== what it must not do
def test_the_bundle_being_served_is_never_deleted(tmp_path):
    """The whole point. Everything else is housekeeping."""
    root = _web(tmp_path, live=A, present=A + B + C + D)
    prune.main(["prune", str(root)])
    assert set(A) <= _names(root)


def test_an_index_naming_no_assets_prunes_nothing(tmp_path):
    """If index.html cannot be parsed, every file looks unreferenced - which
    is precisely when deleting would take the live bundle with it."""
    root = _web(tmp_path, live=[], present=A + B)
    prune.main(["prune", str(root)])
    assert _names(root) == set(A + B)


def test_a_missing_index_prunes_nothing(tmp_path):
    root = _web(tmp_path, live=A, present=A + B)
    (root / "index.html").unlink()
    prune.main(["prune", str(root)])
    assert _names(root) == set(A + B)


def test_a_corrupt_manifest_does_not_take_the_live_bundle_with_it(tmp_path):
    root = _web(tmp_path, live=A, present=A + B)
    (root.parent / prune.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    prune.main(["prune", str(root)])
    assert set(A) <= _names(root)
    assert _builds(root)[-1]["assets"] == sorted(A)


def test_a_missing_assets_directory_is_not_an_error(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    assert prune.main(["prune", str(root)]) == 0


# ========================================================= the grace window
def test_the_previous_build_survives_one_publish(tmp_path):
    """A browser that loaded the page a second before the switch still has to
    be able to fetch the assets that page was promised."""
    root = _web(tmp_path, live=A, present=A)
    prune.main(["prune", str(root)])

    # B ships.
    for name in B:
        (root / "assets" / name).write_text("x" * 100, encoding="utf-8")
    (root / "index.html").write_text(
        "".join(f'<script src="/app/assets/{n}"></script>' for n in B), encoding="utf-8"
    )
    prune.main(["prune", str(root)])

    assert set(B) <= _names(root), "the new build must be served"
    assert set(A) <= _names(root), "the one it replaced must still be fetchable"


def test_a_build_falls_out_of_the_window_eventually(tmp_path):
    """Otherwise this does not solve the problem it exists for."""
    root = _web(tmp_path, live=A, present=A)
    prune.main(["prune", str(root)])

    for build in (B, C, D):
        for name in build:
            (root / "assets" / name).write_text("x" * 100, encoding="utf-8")
        (root / "index.html").write_text(
            "".join(f'<script src="/app/assets/{n}"></script>' for n in build),
            encoding="utf-8",
        )
        prune.main(["prune", str(root)])

    kept = _names(root)
    assert set(D) <= kept and set(C) <= kept and set(B) <= kept
    assert not (set(A) & kept), "the fourth build should have pushed A out"
    assert len(_builds(root)) == prune.KEEP_BUILDS


def test_republishing_the_same_bundle_does_not_consume_the_window(tmp_path):
    """deploy.sh recopies whatever bundle is already there. If that counted as
    a new build, two backend deploys would quietly evict the real previous
    build and break the grace window."""
    root = _web(tmp_path, live=A, present=A)
    prune.main(["prune", str(root)])

    for name in B:
        (root / "assets" / name).write_text("x" * 100, encoding="utf-8")
    (root / "index.html").write_text(
        "".join(f'<script src="/app/assets/{n}"></script>' for n in B), encoding="utf-8"
    )
    for _ in range(5):  # five backend deploys, same bundle each time
        prune.main(["prune", str(root)])

    assert len(_builds(root)) == 2
    assert set(A) <= _names(root), "A must not have been evicted by repeats"


# ============================================================ the first run
def test_the_first_run_clears_the_backlog(tmp_path):
    """Sixteen releases of accumulation, two files actually being served."""
    backlog = [f"index-{chr(65 + i) * 8}.js" for i in range(16)]
    root = _web(tmp_path, live=A, present=A + backlog)
    prune.main(["prune", str(root)])
    assert _names(root) == set(A)


def test_the_manifest_is_not_written_where_it_would_be_served(tmp_path):
    """Everything under the web root is served to the public. The logs on that
    host are full of scanners walking dotfile paths, and this file being
    fetchable is not a decision anybody made."""
    root = _web(tmp_path, live=A, present=A)
    prune.main(["prune", str(root)])
    assert not (root / prune.MANIFEST_NAME).exists()
    assert (root.parent / prune.MANIFEST_NAME).is_file()


def test_the_manifest_records_what_was_seen(tmp_path):
    root = _web(tmp_path, live=A, present=A)
    prune.main(["prune", str(root)])
    builds = _builds(root)
    assert len(builds) == 1
    assert builds[0]["assets"] == sorted(A)
    assert builds[0]["at"].startswith("20")


# ================================================================ parsing
@pytest.mark.parametrize(
    "name",
    ["index-CEAGKZda.js", "index-CpthLvBs.css", "index--FBT6yJX.css", "index-tZv-pph-.js"],
)
def test_the_real_production_filenames_are_recognised(tmp_path, name):
    """Hyphens and underscores appear in real Vite hashes, including leading
    and trailing ones - these four are copied off the live server."""
    root = _web(tmp_path, live=[name], present=[name, "index-OTHER123.js"])
    prune.main(["prune", str(root)])
    assert name in _names(root)
    assert "index-OTHER123.js" not in _names(root)


def test_files_that_are_not_hashed_bundles_are_left_alone(tmp_path):
    """A font or an image dropped in assets/ is not a build artefact and must
    not be swept up with them."""
    root = _web(tmp_path, live=A, present=A)
    # The realistic shape: Vite hashes these too, and index.html never names
    # them - the CSS does. An earlier version of this script deleted them.
    for name in ("logo.png", "logo-Ba7xQ12.png", "inter-D9k2Lm.woff2"):
        (root / "assets" / name).write_text("binary", encoding="utf-8")
    prune.main(["prune", str(root)])
    kept = _names(root)
    for name in ("logo.png", "logo-Ba7xQ12.png", "inter-D9k2Lm.woff2"):
        assert name in kept, name
