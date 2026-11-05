import io
import json
import stat
import threading
import time
import uuid
import zipfile

import pytest

from foxprofile_ext import transfer as transfer_module
from foxprofile_ext.catalog import CatalogOperationError, ProfileCatalog
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind, Role
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage import (
    AssignmentDAO,
    CatalogOperationDAO,
    ExtensionDatabase,
    TransferHandleDAO,
    UserDAO,
)
from foxprofile_ext.transfer import ManagedProfileTransfer
from src.services.profile import manager as manager_module
from src.services.profile.manager import ProfileManager


def _services(tmp_path, monkeypatch):
    data_dir = tmp_path / "browserdata"
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(manager_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(manager_module, "PROFILES_FILE", profiles_file)
    manager = ProfileManager()
    database = ExtensionDatabase(tmp_path / "extension.sqlite3")
    users = UserDAO(database)
    users.bootstrap_admin("admin", "admin", "hash")
    users.create("alice", "alice", "hash")
    users.create("bob", "bob", "hash")
    assignments = AssignmentDAO(database)
    handles = TransferHandleDAO(database)
    catalog = ProfileCatalog(
        manager,
        CatalogOperationDAO(database),
        assignments,
        tmp_path / "catalog.lock",
        handles,
    )

    def resolve(principal, profile_id):
        profile = next(
            (item for item in manager.list_profiles() if item.profile_id == profile_id), None
        )
        if profile is None or not assignments.is_assigned(principal.subject_id, profile_id):
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return profile

    transfer = ManagedProfileTransfer(catalog, handles, tmp_path / "staging", resolve)
    return catalog, manager, assignments, handles, transfer


def _principal(user_id):
    return PrincipalDTO(user_id, PrincipalKind.PERSONAL, Role.USER, f"session-{user_id}")


def _archive(name, extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        zipped.writestr("profile.json", json.dumps({"name": name, "profile_id": "forged-id"}))
        for path, value in (extra or {}).items():
            zipped.writestr(path, value)
    output.seek(0)
    return output


def test_managed_import_is_one_use_actor_bound_and_assigns_fresh_id(tmp_path, monkeypatch):
    catalog, _, assignments, handles, transfer = _services(tmp_path, monkeypatch)
    handle, profile_id = transfer.stage_import(
        _principal("alice"), _archive("imported", {"data/fingerprint.json": "{}"})
    )
    imported = transfer.import_staged(_principal("alice"), handle, profile_id)
    assert imported.profile_id == profile_id
    assert imported.profile_id != "forged-id"
    assert assignments.is_assigned("alice", profile_id)
    assert handles.get_valid(handle, "alice", profile_id, 0) is None
    with pytest.raises(DomainError):
        transfer.import_staged(_principal("bob"), handle, profile_id)


def test_managed_overwrite_requires_access_and_keeps_target(tmp_path, monkeypatch):
    catalog, _, assignments, _, transfer = _services(tmp_path, monkeypatch)
    target = catalog.create("alice", name="target", os_type="windows")
    handle, _ = transfer.stage_import(
        _principal("alice"),
        _archive("different-name", {"data/fingerprint.json": "new"}),
        target_profile_id=target.profile_id,
    )
    with pytest.raises(DomainError):
        transfer.import_staged(_principal("bob"), handle, target.profile_id, overwrite=True)
    replaced = transfer.import_staged(
        _principal("alice"), handle, target.profile_id, overwrite=True
    )
    assert replaced.name == "target"
    assert replaced.profile_id == target.profile_id
    assert assignments.is_assigned("alice", target.profile_id)


def test_managed_download_is_profile_and_actor_scoped(tmp_path, monkeypatch):
    catalog, _, assignments, handles, transfer = _services(tmp_path, monkeypatch)
    profile = catalog.create("alice", name="download", os_type="windows")
    with pytest.raises(DomainError):
        transfer.stage_export("alice", profile.profile_id)
    handle_id = transfer.stage_export(_principal("alice"), profile.profile_id)
    handle = handles.get_valid(handle_id, "alice", profile.profile_id, 0)
    assert handle is not None
    with (
        pytest.raises(DomainError),
        transfer.download(_principal("bob"), handle_id, profile.profile_id),
    ):
        pass
    with transfer.download(_principal("alice"), handle_id, profile.profile_id) as content:
        assert zipfile.is_zipfile(io.BytesIO(content.read()))
    assert not (transfer.staging_dir / f"{handle_id}.zip").exists()

    revoked_handle = transfer.stage_export(_principal("alice"), profile.profile_id)
    assignments.revoke("alice", profile.profile_id)
    with (
        pytest.raises(DomainError),
        transfer.download(_principal("alice"), revoked_handle, profile.profile_id),
    ):
        pass


def test_export_holds_catalog_lock_against_delete_and_name_reuse(tmp_path, monkeypatch):
    catalog, manager, _, _, transfer = _services(tmp_path, monkeypatch)
    profile = catalog.create("alice", name="shared", os_type="windows")
    (manager.data_dir / "shared" / "Cookies").write_text("old", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    deleted = threading.Event()
    real_export = transfer_module.export_to_zip
    handle_ids = []

    def blocked_export(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_export(*args, **kwargs)

    monkeypatch.setattr(transfer_module, "export_to_zip", blocked_export)

    def export():
        handle_ids.append(transfer.stage_export(_principal("alice"), profile.profile_id))

    def delete_and_reuse():
        catalog.delete("alice", profile.profile_id)
        replacement = catalog.create("alice", name="shared", os_type="windows")
        (manager.data_dir / "shared" / "Cookies").write_text("new", encoding="utf-8")
        assert replacement.profile_id != profile.profile_id
        deleted.set()

    export_thread = threading.Thread(target=export)
    export_thread.start()
    assert entered.wait(5)
    delete_thread = threading.Thread(target=delete_and_reuse)
    delete_thread.start()
    assert not deleted.wait(0.1)
    release.set()
    export_thread.join(5)
    delete_thread.join(5)
    assert not export_thread.is_alive() and not delete_thread.is_alive()

    archive_path = transfer.staging_dir / f"{handle_ids[0]}.zip"
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("data/Cookies") == b"old"


def test_managed_import_rejects_zip_traversal_without_writing_outside(tmp_path, monkeypatch):
    _, manager, _, _, transfer = _services(tmp_path, monkeypatch)
    handle, profile_id = transfer.stage_import(
        _principal("alice"), _archive("escape", {"data/../../escaped.txt": "private"})
    )
    with pytest.raises(CatalogOperationError):
        transfer.import_staged(_principal("alice"), handle, profile_id)
    assert not (tmp_path / "escaped.txt").exists()
    assert not (manager.data_dir / "escape").exists()


def test_import_rejects_archive_symlink(tmp_path, monkeypatch):
    _, manager, _, _, transfer = _services(tmp_path, monkeypatch)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        zipped.writestr("profile.json", json.dumps({"name": "linked"}))
        symlink = zipfile.ZipInfo("data/escape")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(symlink, "../../outside")
    output.seek(0)
    handle, profile_id = transfer.stage_import(_principal("alice"), output)
    with pytest.raises(CatalogOperationError):
        transfer.import_staged(_principal("alice"), handle, profile_id)
    assert not (manager.data_dir / "linked").exists()


def test_expired_staging_cleanup_deletes_file_before_expired_row(tmp_path, monkeypatch):
    _, _, _, handles, transfer = _services(tmp_path, monkeypatch)
    stale_id = str(uuid.uuid4())
    stale_path = transfer.staging_dir / f"{stale_id}.zip"
    stale_path.write_bytes(b"expired")
    handles.issue(stale_id, "alice", "unimported-profile", str(stale_path), time.time() - 1)
    live_id, live_profile_id = transfer.stage_import(_principal("alice"), _archive("live"))
    live_path = transfer.staging_dir / f"{live_id}.zip"

    transfer.stage_import(_principal("alice"), _archive("another"))

    assert not stale_path.exists()
    assert handles.expired(time.time()) == ()
    assert live_path.is_file()
    assert handles.get_valid(live_id, "alice", live_profile_id, time.time()) is not None
