"""Distributed mode is explicit and never puts secrets into transfer origins."""

import pytest

from foxprofile_ext.workers.config import DistributedSettings


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXPROFILE_WORKERS_ENABLED", "true")
    monkeypatch.setenv("FOXPROFILE_MASTER_URL", "https://master.example.test")
    monkeypatch.setenv("FOXPROFILE_RUNTIME_MANIFEST_SHA256", "a" * 64)
    monkeypatch.setenv("FOXPROFILE_RUNTIME_MANIFEST", str(tmp_path / "manifest.json"))
    monkeypatch.setenv("FOXPROFILE_NATIVE_EVIDENCE", str(tmp_path / "native-evidence.json"))


def test_workers_require_explicit_enablement(monkeypatch):
    monkeypatch.delenv("FOXPROFILE_WORKERS_ENABLED", raising=False)
    assert DistributedSettings.from_environment() is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://master.example.test",
        "https://user:password@master.example.test",
        "https://master.example.test/api",
        "https://master.example.test?secret=value",
        "https://master.example.test#fragment",
        "https://master.example.test:wrong",
        "https://master.example.test:65536",
        "https://master.example.test:-1",
    ],
)
def test_unsafe_or_ambiguous_master_origin_fails_closed(configured, monkeypatch, origin):
    monkeypatch.setenv("FOXPROFILE_MASTER_URL", origin)
    with pytest.raises(ValueError, match="HTTPS origin"):
        DistributedSettings.from_environment()


def test_master_origin_accepts_valid_custom_port(configured, monkeypatch):
    monkeypatch.setenv("FOXPROFILE_MASTER_URL", "https://master.example.test:8443")
    assert DistributedSettings.from_environment().public_url == "https://master.example.test:8443"


@pytest.mark.parametrize("digest", ["", "a" * 63, "g" * 64, "A" * 64])
def test_manifest_must_be_explicit_canonical_sha256(configured, monkeypatch, digest):
    monkeypatch.setenv("FOXPROFILE_RUNTIME_MANIFEST_SHA256", digest)
    with pytest.raises(ValueError, match="verified"):
        DistributedSettings.from_environment()


def test_budget_defaults_and_invalid_browser_budget(configured, monkeypatch):
    settings = DistributedSettings.from_environment()
    assert settings.browser_memory_bytes == 1024**3
    assert settings.system_reserve_bytes == 512 * 1024**2
    assert settings.staging_reserve_bytes == 2 * 1024**3
    monkeypatch.setenv("FOXPROFILE_BROWSER_MEMORY_BYTES", "0")
    with pytest.raises(ValueError, match="positive byte budget"):
        DistributedSettings.from_environment()


@pytest.mark.parametrize("setting", ["FOXPROFILE_RUNTIME_MANIFEST", "FOXPROFILE_NATIVE_EVIDENCE"])
def test_local_master_requires_preflight_paths(configured, monkeypatch, setting):
    monkeypatch.delenv(setting)
    with pytest.raises(ValueError, match="absolute preflight"):
        DistributedSettings.from_environment()


def test_master_fails_before_admission_when_manifest_is_missing(configured):
    from foxprofile_ext.workers.config import build_distributed_coordinator
    from foxprofile_ext.workers.manifest import ManifestError

    with pytest.raises(ManifestError):
        build_distributed_coordinator(
            None, None, None, None, DistributedSettings.from_environment()
        )


def test_master_engine_path_must_match_verified_runtime(configured, monkeypatch):
    from types import SimpleNamespace

    from foxprofile_ext.workers import manifest
    from foxprofile_ext.workers.config import build_distributed_coordinator

    monkeypatch.setattr(
        manifest,
        "load_manifest",
        lambda *_: SimpleNamespace(
            ready_engines=("camoufox",),
            runtime={"binaries": {"camoufox": {"path": "/opt/pinned/browser"}}},
        ),
    )
    local = SimpleNamespace(
        runtime=SimpleNamespace(settings={"FOXPROFILE_CAMOUFOX_EXECUTABLE": "/opt/other/browser"})
    )
    with pytest.raises(ValueError, match="verified runtime manifest"):
        build_distributed_coordinator(
            local, None, None, None, DistributedSettings.from_environment()
        )
