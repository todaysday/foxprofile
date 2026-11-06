"""Offline GeoIP preparation, upstream freshness and exact cache-mount checks."""

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import maxminddb
import pytest
from camoufox import geolocation

from foxprofile_ext.runtime import cache
from foxprofile_ext.runtime.confinement import (
    ConfinementUnavailable,
    build_confined_command,
    build_confined_environment,
)
from foxprofile_ext.runtime.processes import RuntimeSupervisor
from foxprofile_ext.storage import ExtensionDatabase
from src.models.profile import Profile
from src.services.browser import runner
from tests.test_ext_confinement import _spec


@pytest.fixture
def geoip(monkeypatch, tmp_path):
    directory = tmp_path / "cache" / "camoufox" / "geoip"
    directory.mkdir(parents=True)
    monkeypatch.setattr(geolocation, "GEOIP_DIR", directory)
    monkeypatch.setattr(geolocation, "MMDB_DIR", directory / "mmdb")
    monkeypatch.setattr(geolocation, "GEOIP_CONFIG", directory / "config.yml")
    state = SimpleNamespace(now=time.time(), build_age=2, downloads=[])

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def metadata(self):
            return SimpleNamespace(build_epoch=state.now - state.build_age * 86400)

        def get(self, ip):
            return {
                "country": {"iso_code": "US"},
                "location": {"longitude": -97, "latitude": 38, "time_zone": "America/Chicago"},
            }

    monkeypatch.setattr(maxminddb, "open_database", lambda *_args: Reader())
    monkeypatch.setattr(geolocation.time, "time", lambda: state.now)

    def downloaded():
        state.downloads.append(os.getpid())
        state.build_age = 2
        for version in ("ipv4", "ipv6"):
            path = geolocation.get_mmdb_path(version)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake-mmdb; native reader replaced in this test")
            os.utime(path, (state.now, state.now))

    monkeypatch.setattr(geolocation, "download_mmdb", downloaded)
    state.download = downloaded
    return state


def test_missing_cache_prepares_with_public_downloader_only_in_parent(geoip):
    with pytest.raises(RuntimeError, match="missing or stale"):
        cache.require_geoip_cache()
    assert geoip.downloads == []

    cache.prepare_geoip_cache()
    assert geoip.downloads == [os.getpid()]
    cache.require_geoip_cache()
    # Actual public upstream geolocation path sees both address families;
    # its downloader spy must remain untouched after parent preparation.
    for ip in ("203.0.113.8", "2001:db8::8"):
        assert geolocation.get_geolocation(ip).timezone == "America/Chicago"
    assert geoip.downloads == [os.getpid()]


def test_existing_working_cache_and_explicit_source_remain_unchanged(geoip):
    geoip.download()
    geolocation.GEOIP_CONFIG.write_text("name: GeoIP AIO by daijro\nexplicit: true\n")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in geolocation.GEOIP_DIR.rglob("*")
        if path.is_file()
    }
    geoip.downloads.clear()

    cache.prepare_geoip_cache()

    assert geoip.downloads == []
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}


def test_concurrent_cold_preparations_write_once_then_preserve_valid_cache(geoip, monkeypatch):
    attempts = []
    both_attempted = threading.Event()
    original_lock = cache.catalog_lock

    @contextmanager
    def observed_lock(path):
        attempts.append(path)
        if len(attempts) == 2:
            both_attempted.set()
        with original_lock(path):
            yield

    def overlapping_download():
        assert both_attempted.wait(5), "second preparation did not reach cache lock"
        geoip.download()

    monkeypatch.setattr(cache, "catalog_lock", observed_lock)
    monkeypatch.setattr(geolocation, "download_mmdb", overlapping_download)

    def prepare_and_read():
        cache.prepare_geoip_cache()
        cache.require_geoip_cache()
        path = geolocation.get_mmdb_path()
        return path.read_bytes(), path.stat().st_mtime_ns

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(prepare_and_read)
        second = executor.submit(prepare_and_read)
        snapshots = (first.result(timeout=10), second.result(timeout=10))
        assert snapshots[0] == snapshots[1]
        assert geoip.downloads == [os.getpid()]
        # Both callers subsequently reuse the same bytes and timestamp.
        results = tuple(executor.map(lambda _: prepare_and_read(), range(2)))
        assert results == snapshots
        assert geoip.downloads == [os.getpid()]
    assert all(path == geolocation.GEOIP_DIR / ".prepare.lock" for path in attempts)


def test_stale_cache_fails_in_child_and_refreshes_in_parent_using_upstream_clock(geoip):
    geoip.download()
    geoip.downloads.clear()
    geoip.build_age = geolocation.UPDATE_DAYS + 1
    checked = geoip.now - (geolocation.RECHECK_DAYS + 1) * 86400
    path = geolocation.get_mmdb_path()
    os.utime(path, (checked, checked))
    assert geolocation.needs_update()

    with pytest.raises(RuntimeError, match="missing or stale"):
        cache.require_geoip_cache()
    assert geoip.downloads == []

    cache.prepare_geoip_cache()
    assert geoip.downloads == [os.getpid()]
    assert not geolocation.needs_update()


def test_missing_ipv6_is_prepared_even_when_upstream_ipv4_is_fresh(geoip, monkeypatch):
    geolocation.GEOIP_CONFIG.write_text("name: MaxMind GeoLite2\nexplicit: true\n")
    geoip.download()
    geolocation.get_mmdb_path("ipv6").unlink()
    geoip.downloads.clear()
    assert not geolocation.needs_update()

    with pytest.raises(RuntimeError, match="missing or stale"):
        cache.require_geoip_cache()
    cache.prepare_geoip_cache()
    assert geoip.downloads == [os.getpid()]


def test_failed_refresh_does_not_admit_an_empty_cache(geoip, monkeypatch):
    monkeypatch.setattr(geolocation, "download_mmdb", lambda: None)
    with pytest.raises(RuntimeError, match="missing or stale"):
        cache.prepare_geoip_cache()


@pytest.mark.parametrize("kind", ["config-symlink", "mmdb-symlink", "source-path"])
def test_cache_aliases_and_source_path_traversal_fail_before_downloading(
    geoip, monkeypatch, tmp_path, kind
):
    target = tmp_path / "private"
    target.mkdir()
    if kind == "config-symlink":
        secret = target / "secret.yml"
        secret.write_text("name: GeoIP AIO by daijro\n")
        geolocation.GEOIP_CONFIG.symlink_to(secret)
    elif kind == "mmdb-symlink":
        geolocation.MMDB_DIR.symlink_to(target, target_is_directory=True)
    else:
        monkeypatch.setattr(geolocation, "load_geoip_config", lambda: {"name": "../../private"})
    with pytest.raises(RuntimeError, match="unsafe path|outside its cache"):
        cache.prepare_geoip_cache()
    assert geoip.downloads == []


def test_runner_missing_cache_fails_before_ip_lookup_display_or_browser(
    geoip, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.setenv("FOXPROFILE_CAMOUFOX_EXECUTABLE", sys.executable)
    monkeypatch.setattr(runner, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runner, "load_or_create", lambda *_: {})
    for name in ("exit_ip", "start_virtual_display", "AsyncCamoufox"):
        monkeypatch.setattr(
            runner, name, lambda *_a, **_k: pytest.fail("started before cache guard")
        )

    assert asyncio.run(runner.run_browser("p", "None", "linux")) == 1
    assert "GeoIP cache is missing or stale" in capsys.readouterr().out
    assert geoip.downloads == []


def test_parent_preparation_runs_after_admission_before_spawn_and_skips_cookie_helper(
    tmp_path, monkeypatch
):
    profile = Profile("p")
    data = tmp_path / "data"
    (data / profile.name).mkdir(parents=True)
    prepared = []
    spawned = []
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    runtime = RuntimeSupervisor(
        database,
        data_dir=data,
        code_root=Path.cwd(),
        runtime_paths=(Path(sys.prefix),),
        prepare_launch=lambda p: prepared.append(p.profile_id),
    )

    def refuse_spawn(*args, **kwargs):
        spawned.append(args)
        raise OSError("controlled spawn stop")

    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    try:
        with pytest.raises(RuntimeError, match="reserved browser run"):
            runtime.spawn_browser(profile, profile.profile_id, "unreserved")
        assert prepared == []
        ref = runtime.ledger.reserve((profile.profile_id,), runtime.host_id).newly_reserved[0]
        with pytest.raises(OSError, match="controlled spawn stop"):
            runtime.spawn_browser(profile, profile.profile_id, ref.run_id)
        assert prepared == [profile.profile_id]
        prepared.clear()
        with pytest.raises(OSError, match="controlled spawn stop"):
            runtime._spawn(
                profile, profile.profile_id, ref.run_id, [sys.executable], helper_stdin="[]"
            )
        assert prepared == []

        def failed_preparation(_profile):
            raise RuntimeError("GeoIP preparation failed")

        runtime.prepare_launch = failed_preparation
        with pytest.raises(RuntimeError, match="GeoIP preparation failed"):
            runtime.spawn_browser(profile, profile.profile_id, ref.run_id)
        assert len(spawned) == 2
    finally:
        runtime.shutdown(0)


def test_factory_creates_only_empty_exact_mount_and_downloads_on_camoufox_launch(
    geoip, monkeypatch, tmp_path
):
    from foxprofile_ext import runtime_factory

    captured = {}

    def supervisor(database, **options):
        captured.update(options)
        return SimpleNamespace(host_id="offline-host", recover=lambda: None)

    monkeypatch.setattr(runtime_factory, "RuntimeSupervisor", supervisor)
    monkeypatch.setattr(
        runtime_factory, "_installed_executable", lambda _engine: Path(sys.executable)
    )
    shutil.rmtree(geolocation.GEOIP_DIR)
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    catalog = SimpleNamespace(
        manager=SimpleNamespace(data_dir=tmp_path / "data"), lock_path=tmp_path / "catalog.lock"
    )
    runtime_factory.build_run_coordinator(database, catalog, None, None, None)

    assert geoip.downloads == []
    assert tuple(geolocation.GEOIP_DIR.iterdir()) == ()
    assert geolocation.GEOIP_DIR in captured["runtime_paths"]
    home = geolocation.GEOIP_DIR.parents[1]
    assert home not in captured["runtime_paths"]
    assert captured["settings"]["XDG_CACHE_HOME"] == str(home)
    captured["prepare_launch"](Profile("chrome-profile", engine="chrome"))
    assert geoip.downloads == []
    captured["prepare_launch"](Profile("camoufox-profile"))
    assert geoip.downloads == [os.getpid()]


def _cache_spec(tmp_path):
    spec = _spec(tmp_path)
    home = spec.code_root / ".venv" / "cache"
    directory = home / "camoufox" / "geoip"
    directory.mkdir(parents=True)
    (directory / "canary").write_text("geoip-only")
    (home / "private-cache").write_text("private-cache-canary")
    return replace(spec, runtime_paths=(*spec.runtime_paths, directory)), home


def test_cache_home_requires_exact_readonly_mount_and_maps_without_parent_mount(
    tmp_path, monkeypatch
):
    spec, home = _cache_spec(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/true")
    command, environment = build_confined_command(
        spec, (sys.executable, "-c", "pass"), {"XDG_CACHE_HOME": str(home)}
    )
    assert environment["XDG_CACHE_HOME"] == "/opt/foxprofile/.venv/cache"
    assert str(home) not in command
    source = str(home / "camoufox" / "geoip")
    assert command[command.index(source) - 1] == "--ro-bind"
    assert "XDG_CACHE_HOME" not in build_confined_environment({"XDG_CACHE_HOME": str(home)})
    for value in ("relative", str(home.parent), str(home / ".." / "cache"), "bad\x00path"):
        with pytest.raises(ConfinementUnavailable):
            build_confined_command(spec, (sys.executable, "-c", "pass"), {"XDG_CACHE_HOME": value})


def test_cache_home_symlink_alias_cannot_select_another_mount(tmp_path, monkeypatch):
    spec, home = _cache_spec(tmp_path)
    alias = home.parent / "alias"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/true")
    with pytest.raises(ConfinementUnavailable):
        build_confined_command(spec, (sys.executable, "-c", "pass"), {"XDG_CACHE_HOME": str(alias)})


def test_actual_namespace_exposes_only_readonly_geoip_cache(tmp_path):
    spec, home = _cache_spec(tmp_path)
    # Match the factory's native library mounts, not only the Python ELF
    # dependencies used by the smaller stdlib-only confinement fixture.
    spec = replace(
        spec,
        runtime_paths=(
            *(
                p
                for p in spec.runtime_paths
                if not str(p).startswith(("/usr/", "/lib/", "/lib64/"))
            ),
            *(Path(p) for p in ("/usr", "/lib", "/lib64") if Path(p).exists()),
        ),
    )
    script = "\n".join(
        (
            "import os,pathlib",
            "from camoufox.geolocation import GEOIP_DIR",
            "assert str(GEOIP_DIR) == os.environ['XDG_CACHE_HOME'] + '/camoufox/geoip'",
            "assert (GEOIP_DIR / 'canary').read_text() == 'geoip-only'",
            "assert not (GEOIP_DIR.parent.parent / 'private-cache').exists()",
            "assert os.environ['HOME'] == '/tmp'",
            "try: (GEOIP_DIR / 'forbidden-write').write_text('no')",
            "except OSError: pass",
            "else: raise AssertionError('GeoIP cache is writable')",
        )
    )
    argv, environment = build_confined_command(
        spec, (sys.executable, "-c", script), {"XDG_CACHE_HOME": str(home)}
    )
    result = subprocess.run(argv, env=environment, capture_output=True, timeout=10)
    if result.returncode != 0 and b"No permissions" in result.stderr:
        pytest.skip(result.stderr.decode())
    assert result.returncode == 0, result.stderr.decode()
