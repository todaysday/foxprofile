"""Pinned Ubuntu 24.04 runtime manifest and per-host native evidence checks."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FORMAT_VERSION = 1
_REQUIRED_NATIVE_CHECKS = frozenset(
    {
        "browser_geometry",
        "webgl_renderer",
        "rendering_backend",
        "font_fingerprint",
        "identity_roundtrip",
        "sandbox",
        "network_proxy",
    }
)
_PINNED_BINARIES = ("bwrap", "xvfb", "xauth")
_ENGINE_BINARIES = {"camoufox": "camoufox", "chrome": "chrome"}
_MAX_EVIDENCE_BYTES = 256 * 1024
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ManifestError(ValueError):
    """A configured runtime is absent, mismatched, or lacks native evidence."""


@dataclass(frozen=True, slots=True)
class VerifiedManifest:
    digest: str
    ready_engines: tuple[str, ...]
    runtime: dict[str, Any]


def canonical_digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def architecture_name(value: str | None = None) -> str:
    raw = (value or platform.machine()).lower()
    aliases = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}
    return aliases.get(raw, raw)


def load_manifest(
    manifest_path: str | Path,
    native_evidence_path: str | Path,
    expected_digest: str,
    *,
    observed: dict[str, Any] | None = None,
) -> VerifiedManifest:
    """Verify pinned runtime and browser-measured host evidence before hello."""
    if not _SHA256.fullmatch(expected_digest or ""):
        raise ManifestError("configured manifest digest is invalid")
    manifest = _read_json(manifest_path, _MAX_MANIFEST_BYTES, "runtime manifest")
    if not isinstance(manifest, dict) or canonical_digest(manifest) != expected_digest:
        raise ManifestError("runtime manifest digest does not match configuration")
    if manifest.get("format_version") != _FORMAT_VERSION:
        raise ManifestError("unsupported runtime manifest format")
    if manifest.get("distribution") != {"id": "ubuntu", "version": "24.04"}:
        raise ManifestError("runtime manifest must pin Ubuntu 24.04")
    native_policy = manifest.get("native_evidence")
    required_checks = (
        native_policy.get("required_checks") if isinstance(native_policy, dict) else None
    )
    if (
        not isinstance(required_checks, list)
        or any(not isinstance(name, str) for name in required_checks)
        or set(required_checks) != _REQUIRED_NATIVE_CHECKS
    ):
        raise ManifestError("runtime manifest lacks required native evidence checks")

    architecture = architecture_name()
    runtimes = manifest.get("runtimes")
    runtime = runtimes.get(architecture) if isinstance(runtimes, dict) else None
    if not isinstance(runtime, dict):
        raise ManifestError("runtime manifest does not include this CPU architecture")
    binaries = runtime.get("binaries")
    if not isinstance(binaries, dict) or not set(_PINNED_BINARIES).issubset(binaries):
        raise ManifestError("runtime manifest lacks required sandbox or display binaries")
    if not any(name in binaries for name in _ENGINE_BINARIES.values()):
        raise ManifestError("runtime manifest contains no supported browser engine")
    observed = observed or observe_host(runtime)
    _verify_observed(runtime, observed)

    evidence = _read_json(native_evidence_path, _MAX_EVIDENCE_BYTES, "native evidence")
    _verify_native_evidence(runtime, architecture, evidence)
    ready_engines = tuple(
        engine
        for engine, binary_name in _ENGINE_BINARIES.items()
        if binary_name in runtime.get("binaries", {})
    )
    if not ready_engines:
        raise ManifestError("runtime manifest contains no supported browser engine")
    return VerifiedManifest(expected_digest, ready_engines, runtime)


def _read_json(path: str | Path, maximum: int, label: str) -> Any:
    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise ManifestError(f"{label} file is unavailable")
        if candidate.stat().st_size > maximum:
            raise ManifestError(f"{label} file exceeds its limit")
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"{label} file is invalid") from error
    return value


def _verify_observed(runtime: dict[str, Any], observed: dict[str, Any]) -> None:
    if observed.get("architecture") != architecture_name():
        raise ManifestError("host CPU architecture does not match runtime manifest")
    if observed.get("python_version") != runtime.get("python_version"):
        raise ManifestError("Python version does not match runtime manifest")
    for key in ("binaries", "packages"):
        pins = runtime.get(key)
        actual = observed.get(key)
        if not isinstance(pins, dict) or not isinstance(actual, dict):
            raise ManifestError(f"runtime manifest is missing {key}")
        if pins != actual:
            raise ManifestError(f"installed {key} do not match runtime manifest")
    if observed.get("fonts_sha256") != runtime.get("fonts_sha256"):
        raise ManifestError("installed fonts do not match runtime manifest")


def _verify_native_evidence(runtime: dict[str, Any], architecture: str, evidence: Any) -> None:
    if not isinstance(evidence, dict) or evidence.get("format_version") != 1:
        raise ManifestError("native browser evidence is missing or malformed")
    if evidence.get("status") != "PASS" or evidence.get("architecture") != architecture:
        raise ManifestError("native browser evidence did not pass on this architecture")
    if evidence.get("runtime_pins_sha256") != runtime_pins_digest(runtime):
        raise ManifestError("native browser evidence is not bound to these runtime pins")
    expected = runtime.get("native")
    observed = evidence.get("engines")
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        raise ManifestError("native per-engine observations are missing")
    expected_by_engine = expected.get("engines")
    if not isinstance(expected_by_engine, dict):
        raise ManifestError("native per-engine pins are missing")
    expected_engines = {
        engine
        for engine, binary in _ENGINE_BINARIES.items()
        if binary in runtime.get("binaries", {})
    }
    if set(expected_by_engine) != expected_engines or set(observed) != expected_engines:
        raise ManifestError("native evidence must cover every configured browser engine")
    for engine in expected_engines:
        actual = observed.get(engine)
        pinned = expected_by_engine.get(engine)
        if not isinstance(actual, dict) or not isinstance(pinned, dict):
            raise ManifestError(f"native observations are incomplete for {engine}")
        checks = actual.get("checks")
        if not isinstance(checks, dict) or any(
            checks.get(name) is not True for name in _REQUIRED_NATIVE_CHECKS
        ):
            raise ManifestError(f"native evidence lacks passing checks for {engine}")
        for key in (
            "geometry",
            "webgl_renderer",
            "rendering_backend",
            "fonts_sha256",
            "identity",
            "sandbox",
            "network_proxy",
        ):
            if actual.get(key) != pinned.get(key):
                raise ManifestError(f"measured {key} does not match runtime manifest for {engine}")
        if actual.get("fonts_sha256") != runtime.get("fonts_sha256"):
            raise ManifestError(f"browser-measured fonts do not match for {engine}")
        if not _complete_engine_evidence(actual):
            raise ManifestError(f"native observations are incomplete for {engine}")


def runtime_pins_digest(runtime: dict[str, Any]) -> str:
    """Bind native browser evidence to exact binaries and Python artifacts."""
    return canonical_digest(
        {
            "python_version": runtime.get("python_version"),
            "binaries": runtime.get("binaries"),
            "packages": runtime.get("packages"),
            "fonts_sha256": runtime.get("fonts_sha256"),
        }
    )


def _complete_engine_evidence(value: dict[str, Any]) -> bool:
    geometry = value.get("geometry")
    renderer = value.get("webgl_renderer")
    backend = value.get("rendering_backend")
    identity = value.get("identity")
    sandbox = value.get("sandbox")
    network = value.get("network_proxy")
    fonts_digest = value.get("fonts_sha256")
    return (
        isinstance(geometry, dict)
        and all(
            type(geometry.get(key)) is int and geometry[key] > 0
            for key in ("screen_width", "screen_height")
        )
        and isinstance(renderer, str)
        and bool(renderer.strip())
        and isinstance(backend, dict)
        and all(
            isinstance(backend.get(key), str) and backend[key].strip()
            for key in ("api", "vendor", "renderer", "version")
        )
        and type(backend.get("software")) is bool
        and isinstance(fonts_digest, str)
        and _SHA256.fullmatch(fonts_digest) is not None
        and isinstance(identity, dict)
        and identity.get("stable") is True
        and identity.get("same_bytes_after_roundtrip") is True
        and isinstance(sandbox, dict)
        and all(
            sandbox.get(key) is True
            for key in ("bubblewrap", "user_namespace", "pid_namespace", "profile_mount_isolated")
        )
        and isinstance(network, dict)
        and network.get("profile_proxy_honored") is True
        and network.get("proxy_failure_no_direct_fallback") is True
        and isinstance(network.get("webrtc_policy"), str)
        and bool(network["webrtc_policy"].strip())
        and isinstance(network.get("dns_policy"), str)
        and bool(network["dns_policy"].strip())
    )


def observe_host(runtime: dict[str, Any]) -> dict[str, Any]:
    """Measure facts used by manifest verification; never writes a passing proof."""
    distribution = _ubuntu_release()
    if distribution != {"id": "ubuntu", "version": "24.04"}:
        raise ManifestError("Worker host must run Ubuntu 24.04")
    binaries: dict[str, dict[str, str]] = {}
    pins = runtime.get("binaries")
    if not isinstance(pins, dict):
        raise ManifestError("runtime binary pins are missing")
    for name, pin in pins.items():
        if not isinstance(pin, dict):
            raise ManifestError("invalid binary pin")
        path = pin.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ManifestError("binary pin path must be absolute")
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ManifestError(f"pinned binary is unavailable: {name}")
        digest = _file_digest(candidate)
        if not _SHA256.fullmatch(pin.get("sha256", "")):
            raise ManifestError(f"binary SHA-256 pin is invalid: {name}")
        binaries[name] = {"path": str(candidate.resolve()), "sha256": digest}
    packages = _installed_packages(runtime.get("packages"))
    fonts = _font_digest()
    return {
        "architecture": architecture_name(),
        "python_version": platform.python_version(),
        "binaries": binaries,
        "packages": packages,
        "fonts_sha256": fonts,
    }


def _ubuntu_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ID", "VERSION_ID"}:
                values[key] = value.strip().strip('"')
    except OSError:
        return {}
    return {"id": values.get("ID", ""), "version": values.get("VERSION_ID", "")}


def _installed_packages(pins: Any) -> dict[str, str]:
    if not isinstance(pins, dict) or not pins:
        raise ManifestError("runtime Python package pins are missing")
    actual = {}
    for name in pins:
        if not isinstance(name, str) or not name:
            raise ManifestError("runtime package pin is invalid")
        try:
            actual[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise ManifestError(f"runtime package is unavailable: {name}") from None
    return actual


def _font_digest() -> str:
    command = shutil.which("fc-list")
    if command is None:
        raise ManifestError("fontconfig is unavailable")
    try:
        result = subprocess.run(
            [command, "--format", "%{file}\\n"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise ManifestError("installed font inventory is unavailable") from None
    files = sorted({Path(line).resolve() for line in result.stdout.splitlines() if line})
    if not files:
        raise ManifestError("no installed font files were found")
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_digest(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_private_json(destination: str | Path, document: dict[str, Any]) -> None:
    path = Path(destination)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    if path.is_symlink() or temporary.is_symlink():
        raise ManifestError("runtime manifest output cannot be a symlink")
    payload = json.dumps(document, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def measure_runtime_pins(
    *,
    binary_paths: dict[str, str] | None = None,
    packages: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Measure runtime inputs for native tests without asserting native PASS."""
    if _ubuntu_release() != {"id": "ubuntu", "version": "24.04"}:
        raise ManifestError("Worker host must run Ubuntu 24.04")
    bins: dict[str, dict[str, str]] = {}
    paths = dict(binary_paths or _discover_binaries())
    for name in (*_PINNED_BINARIES, *_ENGINE_BINARIES.values()):
        path = paths.get(name)
        if path is None:
            if name in _PINNED_BINARIES:
                raise ManifestError(f"required binary is unavailable: {name}")
            continue
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ManifestError(f"binary is unavailable: {name}")
        bins[name] = {"path": str(candidate.resolve()), "sha256": _file_digest(candidate)}
    package_pins = packages or {
        name: ""
        for name in ("camoufox", "playwright")
        if (name == "camoufox" and "camoufox" in bins)
        or (name == "playwright" and "chrome" in bins)
    }
    return {
        "python_version": platform.python_version(),
        "binaries": bins,
        "packages": _installed_packages(package_pins),
        "fonts_sha256": _font_digest(),
    }


def write_runtime_pin_measurement(output_path: str | Path) -> str:
    """Write actual pins and digest with a non-PASS status for evidence authoring."""
    runtime = measure_runtime_pins()
    document = {
        "format_version": 1,
        "status": "MEASURED_ONLY",
        "architecture": architecture_name(),
        "runtime_pins_sha256": runtime_pins_digest(runtime),
        "runtime": runtime,
    }
    _write_private_json(output_path, document)
    return document["runtime_pins_sha256"]


def generate_runtime_manifest(
    native_evidence_path: str | Path,
    output_path: str | Path,
    *,
    binary_paths: dict[str, str] | None = None,
    packages: dict[str, str] | None = None,
) -> str:
    """Add measured architecture pins after external native browser acceptance."""
    evidence = _read_json(native_evidence_path, _MAX_EVIDENCE_BYTES, "native evidence")
    architecture = architecture_name()
    if (
        not isinstance(evidence, dict)
        or evidence.get("status") != "PASS"
        or evidence.get("architecture") != architecture
    ):
        raise ManifestError("passing native evidence for this architecture is required")
    existing_path = Path(output_path)
    if existing_path.exists():
        existing = _read_json(existing_path, _MAX_MANIFEST_BYTES, "runtime manifest")
        if (
            not isinstance(existing, dict)
            or existing.get("format_version") != _FORMAT_VERSION
            or existing.get("distribution") != {"id": "ubuntu", "version": "24.04"}
        ):
            raise ManifestError("existing runtime manifest has unsupported format")
        manifest = existing
    else:
        manifest = {
            "format_version": _FORMAT_VERSION,
            "distribution": {"id": "ubuntu", "version": "24.04"},
            "native_evidence": {"required_checks": sorted(_REQUIRED_NATIVE_CHECKS)},
            "runtimes": {},
        }
    if _ubuntu_release() != manifest["distribution"]:
        raise ManifestError("Worker host must run Ubuntu 24.04")
    runtime_entry = measure_runtime_pins(binary_paths=binary_paths, packages=packages)
    bins = runtime_entry["binaries"]
    fonts_digest = runtime_entry["fonts_sha256"]
    if evidence.get("runtime_pins_sha256") != runtime_pins_digest(runtime_entry):
        raise ManifestError("native evidence does not match measured runtime pins")
    engines = {engine for engine, binary in _ENGINE_BINARIES.items() if binary in bins}
    observed_engines = evidence.get("engines")
    if not isinstance(observed_engines, dict) or set(observed_engines) != engines:
        raise ManifestError("native evidence must cover every installed browser engine")
    native_pins = {}
    for engine in engines:
        actual = observed_engines[engine]
        if not isinstance(actual, dict) or not _complete_engine_evidence(actual):
            raise ManifestError(f"native evidence is incomplete for {engine}")
        checks = actual.get("checks")
        if not isinstance(checks, dict) or any(
            checks.get(name) is not True for name in _REQUIRED_NATIVE_CHECKS
        ):
            raise ManifestError(f"native evidence checks did not pass for {engine}")
        if actual["fonts_sha256"] != fonts_digest:
            raise ManifestError(f"browser font measurement differs for {engine}")
        native_pins[engine] = {
            key: actual[key]
            for key in (
                "geometry",
                "webgl_renderer",
                "rendering_backend",
                "fonts_sha256",
                "identity",
                "sandbox",
                "network_proxy",
            )
        }
    runtime_entry["native"] = {"engines": native_pins}
    for name, existing_runtime in manifest["runtimes"].items():
        if not isinstance(existing_runtime, dict):
            raise ManifestError("existing architecture pin is malformed")
        if name == architecture and existing_runtime != runtime_entry:
            raise ManifestError("measured runtime differs from its existing architecture pin")
        if name != architecture and (
            existing_runtime.get("packages") != runtime_entry["packages"]
            or existing_runtime.get("fonts_sha256") != runtime_entry["fonts_sha256"]
            or existing_runtime.get("native") != runtime_entry["native"]
        ):
            raise ManifestError("native browser outputs differ across CPU architectures")
    manifest["runtimes"][architecture] = runtime_entry
    manifest["runtimes"] = dict(sorted(manifest["runtimes"].items()))
    digest = canonical_digest(manifest)
    _write_private_json(output_path, manifest)
    return digest


def _discover_binaries() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, executable in (("bwrap", "bwrap"), ("xvfb", "Xvfb"), ("xauth", "xauth")):
        path = shutil.which(executable)
        if path:
            result[name] = path
    try:
        from foxprofile_ext.runtime_factory import _installed_executable

        for engine in _ENGINE_BINARIES:
            installed = _installed_executable(engine)
            if installed is not None:
                result[_ENGINE_BINARIES[engine]] = str(installed)
    except Exception:
        pass
    return result
