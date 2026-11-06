"""Executable offline backup/migration/restore rehearsals on synthetic installations."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time

import pytest

from foxprofile_ext.locking import catalog_lock
from foxprofile_ext.maintenance import Installation, MaintenanceError, backup, restore
from foxprofile_ext.storage import AssignmentDAO, ExtensionDatabase, SettingsDAO, UserDAO


@pytest.fixture
def installation(tmp_path):
    directories = [tmp_path / name for name in ("profiles", "config", "release")]
    for directory in directories:
        directory.mkdir(mode=0o700)
    (directories[0] / "Sample").mkdir()
    (directories[0] / "Sample" / "fingerprint.json").write_text('{"synthetic": true}')
    (directories[0] / "Sample" / "cookies.sqlite").write_bytes(b"synthetic-cookie-fixture")
    (directories[1] / "service.env").write_text("SYNTHETIC_SECRET=fixture-private-value\n")
    (directories[2] / "start").write_text("#!/bin/sh\nexit 0\n")
    (directories[2] / "start").chmod(0o700)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"Sample": {"name": "Sample", "os_type": "linux"}}))
    dependencies = tmp_path / "dependencies.txt"
    dependencies.write_text("synthetic-package==1.0\n")
    return Installation(
        catalog,
        directories[0],
        tmp_path / "state.sqlite3",
        directories[1],
        directories[2],
        dependencies,
        "1" * 40,
    )


def descriptor(installation):
    path = installation.profiles_file.parent / "installation.json"
    path.write_text(json.dumps({key: str(value) for key, value in vars(installation).items()}))
    return path


def cli(*arguments, code=None):
    command = [sys.executable, "-m", "foxprofile_ext.maintenance", *map(str, arguments)]
    if code:
        command = [sys.executable, "-c", code, *map(str, arguments)]
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("FOXPROFILE_")
    }
    return subprocess.run(command, text=True, capture_output=True, env=environment, timeout=30)


def migrate(installation):
    result = cli("migrate", "--installation", descriptor(installation), "--stopped-and-closed")
    assert result.returncode == 0, result.stderr
    return json.loads(installation.profiles_file.read_text())["Sample"]["profile_id"]


def test_matched_backup_restore_preserves_private_state_and_valid_capacity(installation, tmp_path):
    profile_id = migrate(installation)
    database = ExtensionDatabase(installation.database)
    UserDAO(database).bootstrap_admin("admin-id", "admin0", "synthetic-password-hash")
    UserDAO(database).create("user-a", "userA", "synthetic-password-hash")
    AssignmentDAO(database).grant("user-a", profile_id)
    SettingsDAO(database).set_capacity(12)
    snapshot = tmp_path / "snapshot"
    result = cli(
        "backup",
        "--installation",
        descriptor(installation),
        "--stopped-and-closed",
        "--destination",
        snapshot,
    )
    assert result.returncode == 0, result.stderr
    assert "fixture-private-value" not in result.stdout + result.stderr
    destination = tmp_path / "restored"
    destination.mkdir()
    result = cli("restore", "--snapshot", snapshot, "--empty-disposable-destination", destination)
    assert result.returncode == 0, result.stderr
    restored = Installation.load(destination / "installation.json")
    assert migrate(restored) == profile_id
    assert migrate(restored) == profile_id
    restored_database = ExtensionDatabase(restored.database)
    assert AssignmentDAO(restored_database).is_assigned("user-a", profile_id)
    assert SettingsDAO(restored_database).get_capacity().max_concurrent_profiles == 12
    assert (
        restored.data_dir / "Sample" / "cookies.sqlite"
    ).read_bytes() == b"synthetic-cookie-fixture"
    assert (restored.data_dir / "Sample" / "fingerprint.json").read_bytes() == (
        installation.data_dir / "Sample" / "fingerprint.json"
    ).read_bytes()
    assert (restored.configuration_dir / "service.env").read_bytes() == (
        installation.configuration_dir / "service.env"
    ).read_bytes()
    assert (restored.release_dir / "start").read_bytes() == (
        installation.release_dir / "start"
    ).read_bytes()
    assert restored.dependencies_file.read_bytes() == installation.dependencies_file.read_bytes()
    assert os.access(restored.release_dir / "start", os.X_OK)
    if os.name == "posix":
        for path in (snapshot, destination, *snapshot.rglob("*"), *destination.rglob("*")):
            assert path.stat().st_mode & 0o077 == 0


def test_upstream_snapshot_before_schema_migration(installation, tmp_path):
    snapshot = tmp_path / "before"
    backup(installation, snapshot)
    assert not (snapshot / "state.sqlite3").exists()
    destination = tmp_path / "copy"
    destination.mkdir()
    restore(snapshot, destination)
    restored = Installation.load(destination / "installation.json")
    profile_id = migrate(restored)
    assert migrate(restored) == profile_id
    assert (
        SettingsDAO(ExtensionDatabase(restored.database)).get_capacity().max_concurrent_profiles
        == 10
    )
    assert not installation.database.exists()
    assert "profile_id" not in json.loads(installation.profiles_file.read_text())["Sample"]


@pytest.mark.parametrize("after_save", [False, True])
def test_interrupted_identity_migration_recovers_on_rerun(installation, after_save):
    code = """
import sys
from pathlib import Path
from foxprofile_ext import maintenance
installation = maintenance.Installation.load(Path(sys.argv[1]))
import os
os.environ["FOXPROFILE_DATA_DIR"] = str(installation.data_dir)
os.environ["FOXPROFILE_PROFILES_FILE"] = str(installation.profiles_file)
from src.services.profile.manager import ProfileManager
original = ProfileManager.save_profiles
def interrupted(self):
    if sys.argv[2] == "True":
        original(self)
    raise RuntimeError("synthetic interruption")
ProfileManager.save_profiles = interrupted
maintenance.migrate(installation)
"""
    result = cli(descriptor(installation), str(after_save), code=code)
    assert result.returncode != 0
    with sqlite3.connect(installation.database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM catalog_operations WHERE status = 'pending'"
            ).fetchone()[0]
            == 1
        )
    saved = json.loads(installation.profiles_file.read_text())["Sample"].get("profile_id")
    profile_id = migrate(installation)
    if after_save:
        assert profile_id == saved
    assert migrate(installation) == profile_id
    with sqlite3.connect(installation.database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM catalog_operations WHERE status = 'pending'"
            ).fetchone()[0]
            == 0
        )


def test_interrupted_schema_transaction_rolls_back_then_reruns(installation, monkeypatch):
    class InterruptedConnection(sqlite3.Connection):
        def commit(self):
            raise RuntimeError("synthetic schema interruption")

    def interrupted_connect(self):
        return sqlite3.connect(self.path, isolation_level=None, factory=InterruptedConnection)

    with monkeypatch.context() as patch:
        patch.setattr(ExtensionDatabase, "_connect", interrupted_connect)
        with pytest.raises(RuntimeError, match="synthetic schema interruption"):
            ExtensionDatabase(installation.database)
    with sqlite3.connect(installation.database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
            == 0
        )
    assert migrate(installation)


def test_migration_does_not_follow_planted_catalog_temporary_symlink(installation, tmp_path):
    external = tmp_path / "external.json"
    external.write_bytes(b"external bytes must survive")
    planted = installation.profiles_file.with_suffix(".json.tmp")
    planted.symlink_to(external)

    profile_id = migrate(installation)

    assert external.read_bytes() == b"external bytes must survive"
    assert planted.is_symlink()
    assert not installation.profiles_file.is_symlink()
    assert json.loads(installation.profiles_file.read_text())["Sample"]["profile_id"] == profile_id


@pytest.mark.parametrize("command", ["backup", "migrate"])
def test_maintenance_rejects_lock_symlink_without_changing_target(installation, tmp_path, command):
    external = tmp_path / "external-lock"
    external.write_bytes(b"external lock bytes")
    external.chmod(0o644)
    installation.lock_path.symlink_to(external)
    catalog_before = installation.profiles_file.read_bytes()
    arguments = [command, "--installation", descriptor(installation), "--stopped-and-closed"]
    if command == "backup":
        arguments.extend(["--destination", tmp_path / "snapshot"])

    result = cli(*arguments)

    assert result.returncode == 1
    assert external.read_bytes() == b"external lock bytes"
    if os.name == "posix":
        assert stat.S_IMODE(external.stat().st_mode) == 0o644
    assert installation.profiles_file.read_bytes() == catalog_before
    assert not installation.database.exists()
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO browser_runs VALUES ('run', 'profile', 'host', 'running', 42, 1, 0)",
        "INSERT INTO browser_runs VALUES ('run', 'profile', 'host', 'reserved', NULL, NULL, 0)",
        "INSERT INTO browser_runs VALUES ('run', 'profile', 'host', 'cancelled', 42, 1, 0)",
        "UPDATE capacity_recovery SET blocked = 1",
        "INSERT INTO manual_leases VALUES ('lease', 'owner', 'personal', 'profile', 'run', 'host', 'recovering', 0, 0, 10, 20, NULL, NULL)",
        "INSERT INTO runtime_descriptors VALUES ('run', 'profile', 'host', 42, 1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0)",
    ],
)
def test_active_or_uncertain_runtime_blocks_backup_and_migrate(installation, tmp_path, sql):
    migrate(installation)
    with sqlite3.connect(installation.database) as connection:
        connection.execute(sql)
    with pytest.raises(MaintenanceError, match="active or uncertain"):
        backup(installation, tmp_path / "snapshot")
    result = cli("migrate", "--installation", descriptor(installation), "--stopped-and-closed")
    assert result.returncode == 1
    assert "fixture-private-value" not in result.stdout + result.stderr


def test_invalid_saved_capacity_fails_without_reset(installation):
    migrate(installation)
    with sqlite3.connect(installation.database) as connection:
        connection.execute(
            "UPDATE settings SET setting_value = '0' WHERE setting_key = 'max_concurrent_profiles'"
        )
    result = cli("migrate", "--installation", descriptor(installation), "--stopped-and-closed")
    assert result.returncode == 1
    with sqlite3.connect(installation.database) as connection:
        assert connection.execute("SELECT setting_value FROM settings").fetchone()[0] == "0"


@pytest.mark.parametrize("kind", ["parent.lock", "SingletonLock", "symlink", "fifo"])
def test_browser_markers_and_special_files_fail_closed(installation, tmp_path, kind):
    if kind == "fifo" and os.name != "posix":
        pytest.skip("POSIX FIFO fixture")
    path = installation.data_dir / "Sample" / kind
    if kind == "symlink":
        path.symlink_to(installation.configuration_dir / "service.env")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.touch()
    with pytest.raises(MaintenanceError):
        backup(installation, tmp_path / "snapshot")


@pytest.mark.parametrize("damage", ["content", "traversal", "extra", "symlink"])
def test_restore_rejects_tampered_or_traversal_snapshot(installation, tmp_path, damage):
    snapshot = tmp_path / "snapshot"
    backup(installation, snapshot)
    if damage == "content":
        (snapshot / "catalog.json").write_text("{}")
    elif damage == "traversal":
        manifest = json.loads((snapshot / "manifest.json").read_text())
        manifest["files"]["../../escape"] = "0" * 64
        (snapshot / "manifest.json").write_text(json.dumps(manifest))
    elif damage == "extra":
        (snapshot / "unexpected").write_text("unexpected")
    else:
        (snapshot / "escape").symlink_to(tmp_path)
    destination = tmp_path / "copy"
    destination.mkdir()
    with pytest.raises(MaintenanceError):
        restore(snapshot, destination)
    assert list(destination.iterdir()) == []
    assert not (tmp_path / "escape").exists()


def test_restore_never_overwrites_nonempty_or_symlink_destination(installation, tmp_path):
    snapshot = tmp_path / "snapshot"
    backup(installation, snapshot)
    destination = tmp_path / "copy"
    destination.mkdir()
    (destination / "keep").write_text("keep")
    with pytest.raises(MaintenanceError):
        restore(snapshot, destination)
    assert (destination / "keep").read_text() == "keep"
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(MaintenanceError):
        restore(snapshot, link)


def test_failed_backup_never_publishes_partial_snapshot(installation, tmp_path, monkeypatch):
    import foxprofile_ext.maintenance as maintenance

    def fail_copy(*args):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(maintenance, "_copy", fail_copy)
    with pytest.raises(OSError):
        backup(installation, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()
    assert not list(tmp_path.glob(".foxprofile-backup-*"))


def test_failed_restore_preserves_empty_destination(installation, tmp_path, monkeypatch):
    import foxprofile_ext.maintenance as maintenance

    snapshot = tmp_path / "snapshot"
    backup(installation, snapshot)
    destination = tmp_path / "copy"
    destination.mkdir()

    def fail_copy(*args):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(maintenance, "_copy", fail_copy)
    with pytest.raises(OSError):
        restore(snapshot, destination)
    assert list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".foxprofile-restore-*"))


def test_future_schema_blocks_maintenance_without_modification(installation, tmp_path):
    migrate(installation)
    with sqlite3.connect(installation.database) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(MaintenanceError, match="unsupported extension schema"):
        backup(installation, tmp_path / "snapshot")
    result = cli("migrate", "--installation", descriptor(installation), "--stopped-and-closed")
    assert result.returncode == 1
    with sqlite3.connect(installation.database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999


def test_descriptor_rejects_relative_and_overlapping_roots(installation):
    path = descriptor(installation)
    original = json.loads(path.read_text())
    for changed in (
        {**original, "data_dir": "relative/profiles"},
        {**original, "data_dir": original["release_dir"]},
        {**original, "data_dir": str(installation.release_dir / "profiles")},
    ):
        path.write_text(json.dumps(changed))
        with pytest.raises(MaintenanceError):
            Installation.load(path)


def test_snapshot_rejects_parent_path_traversal(installation, tmp_path):
    snapshot = tmp_path / "snapshot"
    backup(installation, snapshot)
    destination = tmp_path / "copy"
    destination.mkdir()
    with pytest.raises(MaintenanceError, match="parent path traversal"):
        restore(snapshot, destination / ".." / "copy")


def test_cli_requires_shutdown_attestation(installation, tmp_path):
    result = cli(
        "backup", "--installation", descriptor(installation), "--destination", tmp_path / "snapshot"
    )
    assert result.returncode == 2
    assert not (tmp_path / "snapshot").exists()


def test_catalog_lock_timeout_and_reentrance(tmp_path):
    path = tmp_path / "profiles.lock"
    acquired, release = threading.Event(), threading.Event()

    def holder():
        with catalog_lock(path):
            acquired.set()
            release.wait(3)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert acquired.wait(3)
        start = time.monotonic()
        with pytest.raises(TimeoutError), catalog_lock(path, timeout=0.05):
            pytest.fail("lock unexpectedly acquired")
        assert time.monotonic() - start < 1
    finally:
        release.set()
        thread.join(3)
    with catalog_lock(path, timeout=0.1), catalog_lock(path, timeout=0):
        pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX special-file fixtures and permissions")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_catalog_lock_rejects_unsafe_files_before_chmod_or_locking(tmp_path, monkeypatch, kind):
    path = tmp_path / "profiles.lock"
    external = tmp_path / "external"
    external.write_bytes(b"external bytes")
    external.chmod(0o644)
    if kind == "symlink":
        path.symlink_to(external)
    elif kind == "hardlink":
        os.link(external, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()

    def unexpected_effect(*args, **kwargs):
        pytest.fail("unsafe lock reached chmod or file locking")

    import fcntl

    monkeypatch.setattr(os, "fchmod", unexpected_effect)
    monkeypatch.setattr(fcntl, "flock", unexpected_effect)
    with pytest.raises(OSError), catalog_lock(path):
        pytest.fail("unsafe lock acquired")
    assert external.read_bytes() == b"external bytes"
    assert stat.S_IMODE(external.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX special-file fixtures and permissions")
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
@pytest.mark.parametrize("nofollow", [False, True])
def test_catalog_lock_validates_descriptor_after_path_swap(tmp_path, monkeypatch, kind, nofollow):
    path = tmp_path / "profiles.lock"
    path.touch()
    external = tmp_path / "external"
    external.write_bytes(b"external bytes")
    external.chmod(0o644)
    original_open = os.open
    if not nofollow:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    def swapped_open(candidate, flags, mode=0o777):
        if candidate == path:
            path.unlink()
            if kind == "symlink":
                path.symlink_to(external)
            else:
                os.mkfifo(path)
        return original_open(candidate, flags, mode)

    def unexpected_effect(*args, **kwargs):
        pytest.fail("unsafe descriptor reached chmod or file locking")

    import fcntl

    monkeypatch.setattr(os, "open", swapped_open)
    monkeypatch.setattr(os, "fchmod", unexpected_effect)
    monkeypatch.setattr(fcntl, "flock", unexpected_effect)
    with pytest.raises(OSError), catalog_lock(path):
        pytest.fail("unsafe lock acquired")
    assert external.read_bytes() == b"external bytes"
    assert stat.S_IMODE(external.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX cross-process flock")
def test_catalog_lock_process_deadline(tmp_path):
    path = tmp_path / "profiles.lock"
    code = """
import sys
from foxprofile_ext.locking import catalog_lock
try:
    with catalog_lock(sys.argv[1], timeout=0.05):
        raise AssertionError("unexpected acquisition")
except TimeoutError:
    print("timed out")
"""
    with catalog_lock(path):
        result = cli(path, code=code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "timed out"
