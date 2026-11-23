"""Worker runtime pins require complete host native evidence."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from foxprofile_ext.workers import manifest as worker_manifest


def _measurement(fonts_digest: str) -> dict:
    return {
        "geometry": {"screen_width": 1920, "screen_height": 1080},
        "webgl_renderer": "Mesa DRI Intel(R) UHD Graphics",
        "rendering_backend": {
            "api": "OpenGL",
            "vendor": "Mesa",
            "renderer": "llvmpipe (LLVM 17.0.6, 256 bits)",
            "version": "4.5 (Core Profile) Mesa 24.0.0",
            "software": True,
        },
        "fonts_sha256": fonts_digest,
        "identity": {"stable": True, "same_bytes_after_roundtrip": True},
        "sandbox": {
            "bubblewrap": True,
            "user_namespace": True,
            "pid_namespace": True,
            "profile_mount_isolated": True,
        },
        "network_proxy": {
            "profile_proxy_honored": True,
            "proxy_failure_no_direct_fallback": True,
            "webrtc_policy": "disabled",
            "dns_policy": "proxy-resolved",
        },
    }


@pytest.fixture
def evidence_files(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_manifest, "architecture_name", lambda value=None: "x86_64")
    fonts = "f" * 64
    pins = {
        "python_version": "3.12.3",
        "binaries": {
            "bwrap": {"path": "/usr/bin/bwrap", "sha256": "1" * 64},
            "xvfb": {"path": "/usr/bin/Xvfb", "sha256": "2" * 64},
            "xauth": {"path": "/usr/bin/xauth", "sha256": "3" * 64},
            "camoufox": {"path": "/opt/camoufox/camoufox", "sha256": "4" * 64},
        },
        "packages": {"camoufox": "0.4.11"},
        "fonts_sha256": fonts,
    }
    expected = _measurement(fonts)
    pins["native"] = {"engines": {"camoufox": expected}}
    checks = {
        key: True
        for key in (
            "browser_geometry",
            "webgl_renderer",
            "rendering_backend",
            "font_fingerprint",
            "identity_roundtrip",
            "sandbox",
            "network_proxy",
        )
    }
    native = {
        "format_version": 1,
        "status": "PASS",
        "architecture": "x86_64",
        "runtime_pins_sha256": worker_manifest.runtime_pins_digest(pins),
        "engines": {"camoufox": {**copy.deepcopy(expected), "checks": checks}},
    }
    document = {
        "format_version": 1,
        "distribution": {"id": "ubuntu", "version": "24.04"},
        "native_evidence": {"required_checks": sorted(worker_manifest._REQUIRED_NATIVE_CHECKS)},
        "runtimes": {"x86_64": pins},
    }
    observed = {
        "architecture": "x86_64",
        "python_version": pins["python_version"],
        "binaries": pins["binaries"],
        "packages": pins["packages"],
        "fonts_sha256": fonts,
    }
    manifest_path = tmp_path / "runtime.json"
    evidence_path = tmp_path / "native.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    evidence_path.write_text(json.dumps(native), encoding="utf-8")
    return tmp_path, document, native, observed


def _write_files(tmp_path: Path, document: dict, evidence: dict):
    manifest_path = tmp_path / "runtime.json"
    evidence_path = tmp_path / "native.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return manifest_path, evidence_path


def test_manifest_exposes_only_fully_pinned_native_runtime(evidence_files):
    tmp_path, document, _native, observed = evidence_files
    manifest_path, evidence_path = _write_files(tmp_path, document, _native)
    result = worker_manifest.load_manifest(
        manifest_path,
        evidence_path,
        worker_manifest.canonical_digest(document),
        observed=observed,
    )
    assert result.ready_engines == ("camoufox",)
    assert result.runtime["native"]["engines"]["camoufox"]["rendering_backend"]["api"] == "OpenGL"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(status="NOT_RUN"),
        lambda value: value["engines"]["camoufox"].pop("rendering_backend"),
        lambda value: value["engines"]["camoufox"]["checks"].update(rendering_backend=False),
        lambda value: value["engines"]["camoufox"]["rendering_backend"].update(software="true"),
        lambda value: value["engines"]["camoufox"]["sandbox"].update(pid_namespace=False),
        lambda value: value["engines"]["camoufox"]["network_proxy"].update(
            proxy_failure_no_direct_fallback=False
        ),
        lambda value: value["engines"]["camoufox"].update(webgl_renderer="different renderer"),
        lambda value: value.update(runtime_pins_sha256="0" * 64),
    ],
)
def test_manifest_rejects_incomplete_old_or_mismatched_native_evidence(evidence_files, mutate):
    tmp_path, document, native, observed = evidence_files
    candidate = copy.deepcopy(native)
    mutate(candidate)
    manifest_path, evidence_path = _write_files(tmp_path, document, candidate)
    with pytest.raises(worker_manifest.ManifestError):
        worker_manifest.load_manifest(
            manifest_path,
            evidence_path,
            worker_manifest.canonical_digest(document),
            observed=observed,
        )


def test_manifest_rejects_incomplete_policy_and_digest(evidence_files):
    tmp_path, document, native, observed = evidence_files
    wrong_policy = copy.deepcopy(document)
    wrong_policy["native_evidence"]["required_checks"].remove("network_proxy")
    manifest_path, evidence_path = _write_files(tmp_path, wrong_policy, native)
    with pytest.raises(worker_manifest.ManifestError, match="required native evidence"):
        worker_manifest.load_manifest(
            manifest_path,
            evidence_path,
            worker_manifest.canonical_digest(wrong_policy),
            observed=observed,
        )
    with pytest.raises(worker_manifest.ManifestError, match="digest does not match"):
        worker_manifest.load_manifest(
            manifest_path,
            evidence_path,
            "a" * 64,
            observed=observed,
        )


def test_runtime_pin_measurement_never_claims_native_pass(tmp_path, monkeypatch):
    binaries = {}
    for name in ("bwrap", "xvfb", "xauth", "camoufox"):
        path = tmp_path / name
        path.write_text("measured binary", encoding="utf-8")
        path.chmod(0o700)
        binaries[name] = str(path)
    monkeypatch.setattr(
        worker_manifest, "_ubuntu_release", lambda: {"id": "ubuntu", "version": "24.04"}
    )
    monkeypatch.setattr(worker_manifest, "_installed_packages", lambda _pins: {"camoufox": "0.4"})
    monkeypatch.setattr(worker_manifest, "_font_digest", lambda: "f" * 64)
    monkeypatch.setattr(worker_manifest.platform, "python_version", lambda: "3.12.3")
    monkeypatch.setattr(worker_manifest, "_discover_binaries", lambda: binaries)
    destination = tmp_path / "measured-pins.json"
    digest = worker_manifest.write_runtime_pin_measurement(destination)
    result = json.loads(destination.read_text(encoding="utf-8"))
    assert result["status"] == "MEASURED_ONLY"
    assert "engines" not in result
    assert result["runtime_pins_sha256"] == digest
    assert destination.stat().st_mode & 0o077 == 0
