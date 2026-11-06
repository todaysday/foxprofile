#!/usr/bin/env python3
"""Check a reviewed candidate; optionally rehearse drift on a disposable copy."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def check(root: Path) -> int:
    sys.path.insert(0, str(root))
    os.environ["FOXPROFILE_EXT_ENABLED"] = "true"
    from foxprofile_ext.compatibility import check_compatibility, load_register
    from src.api.app import create_app

    problems = check_compatibility(root, create_app(object()))
    if problems:
        print("\n".join(problems))
        return 1
    register = load_register(root)
    print(
        f"PASS: {len(register['routes'])} route/method entries, "
        f"{len(register['hooks'])} hooks, {len(register['launch_calls'])} launch callsites"
    )
    return 0


def rehearse(root: Path) -> int:
    """Never modify the supplied checkout, its index, branches, or worktrees."""
    script = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(prefix="foxprofile-compatibility-") as directory:
        candidate = Path(directory)
        for package in ("src", "chrome_engine", "foxprofile_ext"):
            shutil.copytree(
                root / package,
                candidate / package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(script), "--root", str(candidate)],
                cwd=candidate,
                capture_output=True,
                text=True,
                check=False,
            )

        baseline = run()
        if baseline.returncode:
            print(baseline.stdout + baseline.stderr)
            return 1
        cases = [
            (
                "unclassified profile-data/export/screenshot/browser route",
                "src/api/app.py",
                '    logger.info("FastAPI application created")',
                """    @app.get("/api/v1/unreviewed/export-screenshot-browser-data")
    def unreviewed_surface():
        return {"unreviewed": True}

    logger.info("FastAPI application created")""",
                "unclassified route: HTTP GET /api/v1/unreviewed/export-screenshot-browser-data",
            ),
            (
                "missing shared launch admission hook",
                "foxprofile_ext/runtime/coordinator.py",
                "self.capacity.reserve(current_actor, refs, self.host_id)",
                "self.capacity.unreviewed_reserve(current_actor, refs, self.host_id)",
                "missing required call: foxprofile_ext/runtime/coordinator.py:RuntimeCoordinator.reserve_and_start -> self.capacity.reserve",
            ),
            (
                "runtime adapter signature drift",
                "foxprofile_ext/runtime/contracts.py",
                "def spawn_browser(self, profile: Profile, profile_id: str, run_id: str)",
                "def spawn_browser(self, profile: Profile, profile_id: str)",
                "signature drift: foxprofile_ext/runtime/contracts.py:BrowserRuntimeAdapter.spawn_browser",
            ),
        ]
        for label, relative, original, replacement, expected in cases:
            path = candidate / relative
            source = path.read_text(encoding="utf-8")
            if source.count(original) != 1:
                print(f"FAIL: rehearsal fixture needs adaptation: {label}")
                return 1
            path.write_text(source.replace(original, replacement), encoding="utf-8")
            result = run()
            path.write_text(source, encoding="utf-8")
            if result.returncode != 1 or expected not in result.stdout:
                print(f"FAIL: {label}\n{result.stdout}{result.stderr}")
                return 1
            print(f"REJECTED: {label}: {expected}")
        path = candidate / "src/services/browser/unreviewed.py"
        path.write_text(
            "import subprocess\ndef create_browser():\n    return subprocess.Popen(['unreviewed-browser'])\n",
            encoding="utf-8",
        )
        result = run()
        path.unlink()
        expected = "unclassified launch_calls: src/services/browser/unreviewed.py:create_browser:subprocess.Popen"
        if result.returncode != 1 or expected not in result.stdout:
            print(f"FAIL: new browser factory\n{result.stdout}{result.stderr}")
            return 1
        print(f"REJECTED: new browser factory: {expected}")
        restored = run()
        if restored.returncode:
            print(restored.stdout + restored.stderr)
            return 1
        print("PASS: restored disposable candidate; original checkout unchanged")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--rehearse", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    return rehearse(root) if args.rehearse else check(root)


if __name__ == "__main__":
    raise SystemExit(main())
