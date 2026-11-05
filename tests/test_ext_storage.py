"""Real SQLite persistence and transaction checks for extension DAOs."""

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from foxprofile_ext.dto import Role
from foxprofile_ext.errors import DomainError
from foxprofile_ext.storage import (
    AssignmentDAO,
    BootstrapAlreadyConfigured,
    CatalogOperationDAO,
    ExtensionDatabase,
    LastAdministratorError,
    SessionDAO,
    SettingsDAO,
    TransferHandleDAO,
    UserDAO,
)


def _database(tmp_path):
    return ExtensionDatabase(tmp_path / "state" / "extension.sqlite3")


def test_migration_is_restart_safe_and_database_file_is_private(tmp_path):
    database_path = tmp_path / "state" / "extension.sqlite3"
    database = ExtensionDatabase(database_path)
    assert SettingsDAO(database).get_capacity().max_concurrent_profiles == 10

    with database.read() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        setting_count = connection.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    assert version == 4
    assert setting_count == 1

    reopened = ExtensionDatabase(database_path)
    assert SettingsDAO(reopened).get_capacity().max_concurrent_profiles == 10
    if os.name == "posix":
        assert database_path.stat().st_mode & 0o777 == 0o600


def test_v1_database_upgrades_catalog_metadata_and_transfer_handles_atomically(tmp_path):
    path = tmp_path / "prior.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY);"
            "CREATE TABLE catalog_operations (operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "profile_id TEXT NOT NULL, source_name TEXT, destination_name TEXT, status TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL);"
            "PRAGMA user_version = 1;"
        )

    ExtensionDatabase(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(catalog_operations)")}
        transfer_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transfer_handles'"
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"actor_user_id", "target_profile_id"} <= columns
    assert transfer_table is not None
    assert version == 4
    ExtensionDatabase(path)


def test_bootstrap_is_single_use_under_concurrent_process_style_writes(tmp_path):
    path = tmp_path / "shared.sqlite3"
    ExtensionDatabase(path)

    def bootstrap(index):
        users = UserDAO(ExtensionDatabase(path))
        try:
            return users.bootstrap_admin(f"admin-{index}", f"admin-{index}", "hash")
        except BootstrapAlreadyConfigured:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bootstrap, (1, 2)))
    assert sum(result is not None for result in results) == 1
    assert len(UserDAO(ExtensionDatabase(path)).list_accounts()) == 1


def test_final_active_admin_cannot_be_disabled_or_demoted_concurrently(tmp_path):
    database = _database(tmp_path)
    users = UserDAO(database)
    users.bootstrap_admin("admin-a", "admin-a", "hash")
    users.create("admin-b", "admin-b", "hash", Role.ADMIN)

    def demote(user_id):
        try:
            UserDAO(ExtensionDatabase(database.path)).set_role(user_id, Role.USER)
            return "demoted"
        except LastAdministratorError:
            return "protected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, ("admin-a", "admin-b")))
    assert sorted(outcomes) == ["demoted", "protected"]
    assert sum(user.role is Role.ADMIN and user.active for user in users.list_accounts()) == 1


def test_password_role_active_changes_revoke_sessions_and_preserve_hashed_only_token(tmp_path):
    database = _database(tmp_path)
    users = UserDAO(database)
    users.bootstrap_admin("admin-a", "admin-a", "password-hash")
    users.create("user-a", "user-a", "password-hash")
    sessions = SessionDAO(database)
    record = sessions.create("session-a", "user-a", "sha256-token-digest", 100, 200)
    assert sessions.get_active(record.token_hash, 150) == record
    assert sessions.get_active(record.token_hash, 200) is None
    assert sessions.get_active("plaintext-bearer", 150) is None

    users.set_password_hash("user-a", "new-password-hash")
    assert sessions.get_active(record.token_hash, 150) is None
    record = sessions.create("session-b", "user-a", "another-digest", 100, 200)
    users.set_role("user-a", Role.ADMIN)
    assert sessions.get_active(record.token_hash, 150) is None

    for created, expiry in ((100, float("inf")), (float("nan"), 200)):
        with pytest.raises(ValueError):
            sessions.create("bad-session", "user-a", "invalid", created, expiry)


def test_grant_replacement_rolls_back_whole_set_on_storage_failure(tmp_path):
    database = _database(tmp_path)
    users = UserDAO(database)
    users.bootstrap_admin("admin-a", "admin-a", "hash")
    users.create("user-a", "user-a", "hash")
    assignments = AssignmentDAO(database)
    assignments.replace_for_user("user-a", ["profile-a", "profile-b"])

    with pytest.raises(sqlite3.IntegrityError):
        assignments.replace_for_user("missing-user", ["profile-c"])
    assert assignments.profiles_for_user("user-a") == ("profile-a", "profile-b")
    assert not assignments.is_assigned("user-a", "profile-c")


def test_settings_and_catalog_intent_survive_restart_and_use_finite_states(tmp_path):
    database = _database(tmp_path)
    UserDAO(database).bootstrap_admin("admin-a", "admin-a", "hash")
    settings = SettingsDAO(database)
    settings.set_capacity(3)
    operations = CatalogOperationDAO(database)
    pending = operations.begin(
        "op-a",
        "rename",
        "profile-a",
        10,
        "Old",
        "New",
        actor_user_id="admin-a",
        target_profile_id="profile-b",
    )
    assert pending.status == "pending"
    assert pending.actor_user_id == "admin-a"
    assert pending.target_profile_id == "profile-b"

    reopened = ExtensionDatabase(database.path)
    assert SettingsDAO(reopened).get_capacity().max_concurrent_profiles == 3
    catalog = CatalogOperationDAO(reopened)
    assert catalog.pending() == (pending,)
    assert catalog.finish("op-a", "complete", 11).status == "complete"
    assert catalog.pending() == ()
    with pytest.raises(ValueError):
        catalog.begin("op-b", "arbitrary", "profile-a", 12)


def test_transfer_handles_are_scoped_expiring_revocable_and_single_use(tmp_path):
    database = _database(tmp_path)
    users = UserDAO(database)
    users.bootstrap_admin("admin-a", "admin-a", "hash")
    users.create("user-a", "user-a", "hash")
    handles = TransferHandleDAO(database)
    handle = handles.issue("handle-a", "user-a", "profile-a", "/private/staging/upload", 200)

    assert handles.get_valid("handle-a", "user-a", "profile-a", 150) == handle
    assert handles.get_valid("handle-a", "user-b", "profile-a", 150) is None
    assert handles.get_valid("handle-a", "user-a", "profile-b", 150) is None
    assert handles.get_valid("handle-a", "user-a", "profile-a", 200) is None
    consumed = handles.consume("handle-a", "user-a", "profile-a", 150)
    assert consumed == handle
    assert consumed.status == "active"
    assert handles.consume("handle-a", "user-a", "profile-a", 150) is None

    handles.issue("handle-b", "user-a", "profile-a", "/private/staging/download", 300)
    handles.revoke_profile("profile-a")
    assert handles.get_valid("handle-b", "user-a", "profile-a", 150) is None
    with pytest.raises(ValueError):
        handles.issue("handle-c", "user-a", "profile-a", "/private/file", float("inf"))


def test_invalid_capacity_rejected_without_changing_saved_value(tmp_path):
    settings = SettingsDAO(_database(tmp_path))
    settings.set_capacity(4)
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(DomainError):
            settings.set_capacity(invalid)
    assert settings.get_capacity().max_concurrent_profiles == 4
