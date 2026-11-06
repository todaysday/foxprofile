"""Offline integrity and dependency checks; these do not exercise an RFB session."""

import hashlib
import json
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "foxprofile_ext" / "assets" / "novnc"
IMPORT = re.compile(
    r"^\s*(?:import|export)\s+(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)


def test_novnc_source_pin_and_integrity():
    provenance = json.loads((ASSETS / "provenance.json").read_text())
    assert provenance["tag"] == "v1.7.0"
    assert provenance["commit"] == "63107bd06d9e1f6136ff21aeda8cd62cbf0d433e"
    assert provenance["archive_sha256"] == (
        "06dac239be3973ed79a00e357a68aab996e6ed2fd496ee2cc6e6a34020344ac9"
    )
    assert provenance["modifications"] == []
    files = {str(p.relative_to(ASSETS)): p for p in ASSETS.rglob("*") if p.is_file()}
    assert set(files) == set(provenance["files"]) | {"provenance.json"}
    for name, digest in provenance["files"].items():
        assert not files[name].is_symlink()
        assert hashlib.sha256(files[name].read_bytes()).hexdigest() == digest, name
    for name in ("LICENSE.txt", "AUTHORS", "docs/LICENSE.MPL-2.0", "vendor/pako/LICENSE"):
        assert files[name].stat().st_size > 0


def test_novnc_module_graph_resolves_offline():
    """Walk the consumer entrypoint, including the vendored pako dependency."""
    pending = [ASSETS / "core" / "rfb.js"]
    visited = set()
    while pending:
        module = pending.pop().resolve()
        if module in visited:
            continue
        visited.add(module)
        assert module.is_relative_to(ASSETS.resolve())
        source = module.read_text()
        for dependency in IMPORT.findall(source):
            assert dependency.startswith("."), (module, dependency)
            target = (module.parent / dependency).resolve()
            assert target.is_file(), (module, dependency)
            pending.append(target)
        # Only the caller-supplied WebSocket transport is needed; no loader/CDN.
        assert not re.search(r"\b(?:fetch|XMLHttpRequest|importScripts)\s*\(", source)
        assert not re.search(r"\bimport\s*\(", source)
        assert not re.search(r"['\"](?:https?:)?//", source)
    assert ASSETS / "vendor/pako/lib/zlib/inflate.js" in visited
    assert ASSETS / "vendor/pako/lib/zlib/deflate.js" in visited
