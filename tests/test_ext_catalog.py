import json
import multiprocessing
import zipfile

import pytest

from foxprofile_ext.catalog import CatalogOperationError, ProfileCatalog
from foxprofile_ext.locking import catalog_lock
from foxprofile_ext.storage import (
    AssignmentDAO,
    CatalogOperationDAO,
    ExtensionDatabase,
    UserDAO,
)
from src.models.profile import Profile
from src.services.profile import manager as manager_module
from src.services.profile.manager import ProfileManager
from src.services.profile.transfer import export_to_zip


def _catalog(tmp_path, monkeypatch):
    data_dir = tmp_path / "browserdata"
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(manager_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(manager_module, "PROFILES_FILE", profiles_file)
    manager = ProfileManager()
    db = ExtensionDatabase(tmp_path / "extension.sqlite3")
    users = UserDAO(db)
    users.bootstrap_admin("admin", "admin", "hash")
    users.create("alice", "alice", "hash")
    users.create("bob", "bob", "hash")
    assignments = AssignmentDAO(db)
    operations = CatalogOperationDAO(db)
    catalog = ProfileCatalog(
        manager,
        operations,
        assignments,
        tmp_path / "catalog.lock",
    )
    return catalog, manager, assignments, operations


def _create_in_process(data_dir, profiles_file, database_path, lock_path, name, barrier):
    manager_module.DATA_DIR = data_dir
    manager_module.PROFILES_FILE = profiles_file
    manager = ProfileManager()
    database = ExtensionDatabase(database_path)
    catalog = ProfileCatalog(
        manager,
        CatalogOperationDAO(database),
        AssignmentDAO(database),
        lock_path,
    )
    barrier.wait()
    catalog.create("alice", name=name, os_type="windows")


def test_profile_ids_migrate_once_and_survive_reload_rename(tmp_path, monkeypatch):
    (tmp_path / "profiles.json").write_text(
        json.dumps({"old": {"name": "old", "os_type": "windows"}}), encoding="utf-8"
    )
    catalog, manager, _, _ = _catalog(tmp_path, monkeypatch)
    identity = manager.profiles["old"].profile_id
    assert identity
    assert json.loads((tmp_path / "profiles.json").read_text())["old"]["profile_id"] == identity

    manager.reload_profiles()
    assert manager.profiles["old"].profile_id == identity
    renamed = catalog.rename("alice", identity, "renamed")
    assert renamed.profile_id == identity
    assert manager.profiles["renamed"].profile_id == identity


def test_create_assigns_fresh_identity_and_does_not_reuse_deleted_data(tmp_path, monkeypatch):
    catalog, manager, assignments, _ = _catalog(tmp_path, monkeypatch)
    created = catalog.create("alice", name="alpha", os_type="windows")
    assert assignments.is_assigned("alice", created.profile_id)
    (manager.data_dir / "alpha" / "Cookies").write_text("private", encoding="utf-8")

    original_rmtree = manager_module.shutil.rmtree
    monkeypatch.setattr(
        manager_module.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup interrupted")),
    )
    with pytest.raises(OSError):
        catalog.delete("alice", created.profile_id)
    monkeypatch.setattr(manager_module.shutil, "rmtree", original_rmtree)
    catalog.recover()
    assert not assignments.is_assigned("alice", created.profile_id)
    quarantine = next(manager.data_dir.glob(".alpha.deleted-*"))
    assert (quarantine / "Cookies").read_text(encoding="utf-8") == "private"
    recreated = catalog.create("alice", name="alpha", os_type="windows")
    assert recreated.profile_id != created.profile_id
    assert not list((manager.data_dir / "alpha").iterdir())


def test_import_ignores_forged_id_and_overwrite_keeps_target_identity_and_name(
    tmp_path, monkeypatch
):
    catalog, manager, assignments, _ = _catalog(tmp_path, monkeypatch)
    target = catalog.create("alice", name="target", os_type="windows")
    (manager.data_dir / "target" / "Cookies").write_text("old", encoding="utf-8")
    archive = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(
            "profile.json",
            json.dumps({"name": "TARGET", "profile_id": "forged", "engine": "camoufox"}),
        )
        zipped.writestr("data/fingerprint.json", "{}")

    replaced = catalog.import_archive("alice", str(archive), target_profile_id=target.profile_id)
    assert replaced.name == "target"
    assert replaced.profile_id == target.profile_id
    assert assignments.is_assigned("alice", target.profile_id)
    assert not (manager.data_dir / "target" / "Cookies").exists()
    assert (manager.data_dir / "target" / "fingerprint.json").exists()

    fresh_archive = tmp_path / "fresh.zip"
    with zipfile.ZipFile(fresh_archive, "w") as zipped:
        zipped.writestr(
            "profile.json",
            json.dumps({"name": "archive-name", "profile_id": "forged", "engine": "camoufox"}),
        )
    imported = catalog.import_archive("alice", str(fresh_archive))
    assert imported.name == "archive-name"
    assert imported.profile_id != "forged"
    assert imported.profile_id != target.profile_id
    assert manager.profiles["archive-name"].profile_id == imported.profile_id


def test_export_is_portable_and_excludes_symlinked_data(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    profile_data = tmp_path / "data"
    profile_data.mkdir()
    (profile_data / "safe.json").write_text("ok", encoding="utf-8")
    (profile_data / "leak.txt").symlink_to(outside)
    ok, archive_path = export_to_zip(
        Profile(name="p", profile_id="local-id"), str(profile_data), str(tmp_path)
    )
    assert ok
    with zipfile.ZipFile(archive_path) as archive:
        assert "local-id" not in archive.read("profile.json").decode()
        assert "data/safe.json" in archive.namelist()
        assert "data/leak.txt" not in archive.namelist()


def test_failed_catalog_save_leaves_operation_pending_and_recovery_fail_closed(
    tmp_path, monkeypatch
):
    catalog, manager, assignments, operations = _catalog(tmp_path, monkeypatch)
    original_save = manager.save_profiles
    manager.save_profiles = lambda: (_ for _ in ()).throw(OSError("disk full"))
    with pytest.raises(OSError):
        catalog.create("alice", name="broken", os_type="windows")
    manager.save_profiles = original_save

    pending = operations.pending()
    assert len(pending) == 1
    assert pending[0].profile_id not in {profile.profile_id for profile in catalog.list_profiles()}
    assert not assignments.is_assigned("alice", pending[0].profile_id)
    catalog.recover()
    assert operations.pending() == ()
    assert not assignments.is_assigned("alice", pending[0].profile_id)


def test_assignment_failure_recovers_without_exposing_pending_profile(tmp_path, monkeypatch):
    catalog, manager, assignments, operations = _catalog(tmp_path, monkeypatch)
    original_grant = assignments.grant
    assignments.grant = lambda *_: (_ for _ in ()).throw(OSError("assignment store unavailable"))
    with pytest.raises(OSError):
        catalog.create("alice", name="pending", os_type="windows")
    assignments.grant = original_grant

    profile = manager.profiles["pending"]
    assert profile not in catalog.list_profiles()
    assert operations.pending()[0].profile_id == profile.profile_id
    catalog.recover()
    assert assignments.is_assigned("alice", profile.profile_id)
    assert operations.pending() == ()
    assert profile in catalog.list_profiles()


def test_overwrite_recovery_does_not_grant_actor_or_drop_existing_grants(tmp_path, monkeypatch):
    catalog, _, assignments, operations = _catalog(tmp_path, monkeypatch)
    target = catalog.create("alice", name="target", os_type="windows")
    assignments.grant("admin", target.profile_id)
    archive = tmp_path / "overwrite.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("profile.json", json.dumps({"name": "foreign", "profile_id": "forged"}))

    finish = operations.finish
    operations.finish = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("finalize failed"))
    with pytest.raises(OSError):
        catalog.import_archive("bob", str(archive), target_profile_id=target.profile_id)
    operations.finish = finish
    assert operations.pending()[0].target_profile_id == target.profile_id

    catalog.recover()
    assert assignments.is_assigned("alice", target.profile_id)
    assert assignments.is_assigned("admin", target.profile_id)
    assert not assignments.is_assigned("bob", target.profile_id)


def test_pending_profile_id_is_rejected_by_public_resolver(tmp_path, monkeypatch):
    catalog, _, _, operations = _catalog(tmp_path, monkeypatch)
    profile = catalog.create("alice", name="visible", os_type="windows")
    operations.begin("pending-op", "rename", profile.profile_id, 1, "visible", "next")
    assert profile not in catalog.list_profiles()
    with pytest.raises(CatalogOperationError):
        catalog.get_profile(profile.profile_id)


def test_catalog_lock_is_reentrant_within_thread(tmp_path):
    path = tmp_path / "catalog.lock"
    with catalog_lock(path), catalog_lock(path):
        assert path.exists()


def test_two_processes_reload_before_mutating_shared_catalog(tmp_path):
    data_dir = tmp_path / "browserdata"
    profiles_file = tmp_path / "profiles.json"
    database_path = tmp_path / "extension.sqlite3"
    lock_path = tmp_path / "catalog.lock"
    database = ExtensionDatabase(database_path)
    users = UserDAO(database)
    users.bootstrap_admin("admin", "admin", "hash")
    users.create("alice", "alice", "hash")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes = [
        context.Process(
            target=_create_in_process,
            args=(data_dir, profiles_file, database_path, lock_path, name, barrier),
        )
        for name in ("first", "second")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
    assert all(process.exitcode == 0 for process in processes)
    persisted = json.loads(profiles_file.read_text(encoding="utf-8"))
    assert set(persisted) == {"first", "second"}
