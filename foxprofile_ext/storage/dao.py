"""Concrete repositories for account, access, settings, and catalog intent."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from math import isfinite

from ..dto import CapacitySettings, Role
from .database import ExtensionDatabase


class StorageConflict(Exception):
    """A requested state transition conflicts with durable state."""


class LastAdministratorError(StorageConflict):
    """Refusing to remove or disable the final active administrator."""


class BootstrapAlreadyConfigured(StorageConflict):
    """Initial administrator creation is available only before first account."""


@dataclass(frozen=True, slots=True)
class AccountRecord:
    user_id: str
    username: str
    password_hash: str = field(repr=False)
    role: Role
    active: bool
    auth_version: int


@dataclass(frozen=True, slots=True)
class AccountSessionRecord:
    session_id: str
    user_id: str
    token_hash: str = field(repr=False)
    created_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class CatalogOperationRecord:
    operation_id: str
    kind: str
    profile_id: str
    source_name: str | None
    destination_name: str | None
    status: str
    created_at: float
    updated_at: float
    actor_user_id: str | None
    target_profile_id: str | None


@dataclass(frozen=True, slots=True)
class TransferHandleRecord:
    handle_id: str
    user_id: str
    profile_id: str
    server_path: str = field(repr=False)
    expires_at: float
    status: str


def _required_text(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("values must be non-empty strings")


def _epoch(value: float) -> None:
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError("time must be finite UTC Unix seconds")


def _account(row: sqlite3.Row | None) -> AccountRecord | None:
    if row is None:
        return None
    return AccountRecord(
        row["user_id"],
        row["username"],
        row["password_hash"],
        Role(row["role"]),
        bool(row["active"]),
        row["auth_version"],
    )


class UserDAO:
    def __init__(self, database: ExtensionDatabase):
        self.database = database

    def bootstrap_admin(self, user_id: str, username: str, password_hash: str) -> AccountRecord:
        """Atomically create first admin; caller supplies already-hashed password."""
        _required_text(user_id, username, password_hash)
        with self.database.transaction() as connection:
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise BootstrapAlreadyConfigured
            connection.execute(
                "INSERT INTO users (user_id, username, password_hash, role, active) "
                "VALUES (?, ?, ?, 'admin', 1)",
                (user_id, username, password_hash),
            )
            return _account(
                connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            )  # type: ignore[return-value]

    def create(
        self, user_id: str, username: str, password_hash: str, role: Role | str = Role.USER
    ) -> AccountRecord:
        _required_text(user_id, username, password_hash)
        role = Role(role)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users (user_id, username, password_hash, role, active) "
                "VALUES (?, ?, ?, ?, 1)",
                (user_id, username, password_hash, role.value),
            )
            return _account(
                connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            )  # type: ignore[return-value]

    def get(self, user_id: str) -> AccountRecord | None:
        with self.database.read() as connection:
            return _account(
                connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            )

    def get_by_username(self, username: str) -> AccountRecord | None:
        with self.database.read() as connection:
            return _account(
                connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            )

    def list_accounts(self) -> tuple[AccountRecord, ...]:
        with self.database.read() as connection:
            return tuple(
                _account(row)  # type: ignore[misc]
                for row in connection.execute(
                    "SELECT * FROM users ORDER BY username COLLATE NOCASE"
                )
            )

    def set_active(self, user_id: str, active: bool) -> AccountRecord:
        return self.update_state(user_id, active=active)

    def set_role(self, user_id: str, role: Role | str) -> AccountRecord:
        return self.update_state(user_id, role=role)

    def update_state(
        self,
        user_id: str,
        *,
        role: Role | str | None = None,
        active: bool | None = None,
    ) -> AccountRecord:
        if role is None and active is None:
            raise ValueError("role or active must be supplied")
        if role is not None:
            role = Role(role)
        if active is not None and type(active) is not bool:
            raise ValueError("active must be bool")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                raise StorageConflict("account does not exist")
            next_role = role.value if role is not None else row["role"]
            next_active = active if active is not None else bool(row["active"])
            if row["role"] != next_role or bool(row["active"]) != next_active:
                self._protect_last_admin(connection, row, role=next_role, active=next_active)
                connection.execute(
                    "UPDATE users SET role = ?, active = ?, auth_version = auth_version + 1 "
                    "WHERE user_id = ?",
                    (next_role, int(next_active), user_id),
                )
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return _account(
                connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            )  # type: ignore[return-value]

    def set_password_hash(self, user_id: str, password_hash: str) -> AccountRecord:
        _required_text(password_hash)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE users SET password_hash = ?, auth_version = auth_version + 1 WHERE user_id = ?",
                (password_hash, user_id),
            )
            if cursor.rowcount != 1:
                raise StorageConflict("account does not exist")
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return _account(
                connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            )  # type: ignore[return-value]

    @staticmethod
    def _protect_last_admin(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        role: str,
        active: bool,
    ) -> None:
        if (
            row["role"] == Role.ADMIN.value
            and row["active"]
            and (role != Role.ADMIN.value or not active)
        ):
            remaining = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND user_id != ?",
                (row["user_id"],),
            ).fetchone()[0]
            if remaining == 0:
                raise LastAdministratorError


class SessionDAO:
    """Persist opaque session token hashes, never plaintext bearer tokens."""

    def __init__(self, database: ExtensionDatabase):
        self.database = database

    def create(
        self,
        session_id: str,
        user_id: str,
        token_hash: str,
        created_at: float,
        expires_at: float,
        *,
        expected_auth_version: int | None = None,
    ) -> AccountSessionRecord:
        _required_text(session_id, user_id, token_hash)
        _epoch(created_at)
        _epoch(expires_at)
        if expires_at <= created_at:
            raise ValueError("session times must be increasing UTC Unix seconds")
        if expected_auth_version is not None and (
            type(expected_auth_version) is not int or expected_auth_version < 0
        ):
            raise ValueError("expected_auth_version must be a non-negative integer")
        with self.database.transaction() as connection:
            account = connection.execute(
                "SELECT auth_version, active FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if (
                account is None
                or not account["active"]
                or (
                    expected_auth_version is not None
                    and account["auth_version"] != expected_auth_version
                )
            ):
                raise StorageConflict("account changed during session creation")
            connection.execute(
                "INSERT INTO sessions (session_id, user_id, token_hash, auth_version, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, user_id, token_hash, account["auth_version"], created_at, expires_at),
            )
        return AccountSessionRecord(session_id, user_id, token_hash, created_at, expires_at)

    def get_active(self, token_hash: str, now: float) -> AccountSessionRecord | None:
        _epoch(now)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT s.session_id, s.user_id, s.token_hash, s.created_at, s.expires_at "
                "FROM sessions AS s JOIN users AS u ON u.user_id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1 "
                "AND s.auth_version = u.auth_version",
                (token_hash, now),
            ).fetchone()
        return _session(row)

    def get_valid_by_id(self, session_id: str, now: float) -> AccountSessionRecord | None:
        """Resolve an active personal session from its server-side reference."""
        _required_text(session_id)
        _epoch(now)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT s.session_id, s.user_id, s.token_hash, s.created_at, s.expires_at "
                "FROM sessions AS s JOIN users AS u ON u.user_id = s.user_id "
                "WHERE s.session_id = ? AND s.expires_at > ? AND u.active = 1 "
                "AND s.auth_version = u.auth_version",
                (session_id, now),
            ).fetchone()
        return _session(row)

    def revoke(self, session_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def revoke_user(self, user_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def _session(row: sqlite3.Row | None) -> AccountSessionRecord | None:
    return AccountSessionRecord(*row) if row is not None else None


class AssignmentDAO:
    """Full-control user/profile grants keyed by immutable local IDs."""

    def __init__(self, database: ExtensionDatabase):
        self.database = database

    def is_assigned(self, user_id: str, profile_id: str) -> bool:
        with self.database.read() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM profile_grants WHERE user_id = ? AND profile_id = ?",
                    (user_id, profile_id),
                ).fetchone()
                is not None
            )

    def grant(self, user_id: str, profile_id: str) -> None:
        _required_text(user_id, profile_id)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO profile_grants (user_id, profile_id) VALUES (?, ?) "
                "ON CONFLICT (user_id, profile_id) DO NOTHING",
                (user_id, profile_id),
            )

    def revoke(self, user_id: str, profile_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM profile_grants WHERE user_id = ? AND profile_id = ?",
                (user_id, profile_id),
            )

    def revoke_profile(self, profile_id: str) -> None:
        _required_text(profile_id)
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM profile_grants WHERE profile_id = ?", (profile_id,))

    def replace_for_user(self, user_id: str, profile_ids: Iterable[str]) -> None:
        ids = tuple(profile_ids)
        if any(not isinstance(profile_id, str) or not profile_id.strip() for profile_id in ids):
            raise ValueError("profile IDs must be non-empty strings")
        if len(ids) != len(set(ids)):
            raise ValueError("profile IDs must be unique")
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM profile_grants WHERE user_id = ?", (user_id,))
            connection.executemany(
                "INSERT INTO profile_grants (user_id, profile_id) VALUES (?, ?)",
                ((user_id, profile_id) for profile_id in ids),
            )

    def profiles_for_user(self, user_id: str) -> tuple[str, ...]:
        with self.database.read() as connection:
            return tuple(
                row[0]
                for row in connection.execute(
                    "SELECT profile_id FROM profile_grants WHERE user_id = ? ORDER BY profile_id",
                    (user_id,),
                )
            )


class SettingsDAO:
    def __init__(self, database: ExtensionDatabase):
        self.database = database

    def get_capacity(self) -> CapacitySettings:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'max_concurrent_profiles'"
            ).fetchone()
        return CapacitySettings(int(row[0]) if row is not None else 10)

    def set_capacity(self, max_concurrent_profiles: int) -> CapacitySettings:
        settings = CapacitySettings(max_concurrent_profiles)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO settings (setting_key, setting_value) "
                "VALUES ('max_concurrent_profiles', ?) "
                "ON CONFLICT (setting_key) DO UPDATE SET setting_value = excluded.setting_value",
                (str(settings.max_concurrent_profiles),),
            )
        return settings


class CatalogOperationDAO:
    """Finite catalog intent ledger; filesystem work remains a caller concern."""

    _KINDS = {"create", "import", "rename", "delete", "identity_migration"}

    def __init__(self, database: ExtensionDatabase):
        self.database = database

    def begin(
        self,
        operation_id: str,
        kind: str,
        profile_id: str,
        created_at: float,
        source_name: str | None = None,
        destination_name: str | None = None,
        *,
        actor_user_id: str | None = None,
        target_profile_id: str | None = None,
    ) -> CatalogOperationRecord:
        _required_text(operation_id, profile_id)
        _epoch(created_at)
        if actor_user_id is not None:
            _required_text(actor_user_id)
        if target_profile_id is not None:
            _required_text(target_profile_id)
        if kind not in self._KINDS:
            raise ValueError("unsupported catalog operation")
        if (source_name is not None and not isinstance(source_name, str)) or (
            destination_name is not None and not isinstance(destination_name, str)
        ):
            raise ValueError("catalog names must be strings or None")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog_operations "
                "(operation_id, kind, profile_id, source_name, destination_name, status, created_at, updated_at, actor_user_id, target_profile_id) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    operation_id,
                    kind,
                    profile_id,
                    source_name,
                    destination_name,
                    created_at,
                    created_at,
                    actor_user_id,
                    target_profile_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM catalog_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return self._record(row)

    def pending(self) -> tuple[CatalogOperationRecord, ...]:
        with self.database.read() as connection:
            return tuple(
                self._record(row)
                for row in connection.execute(
                    "SELECT * FROM catalog_operations WHERE status = 'pending' ORDER BY created_at"
                )
            )

    def finish(self, operation_id: str, status: str, updated_at: float) -> CatalogOperationRecord:
        _epoch(updated_at)
        if status not in {"complete", "failed"}:
            raise ValueError("terminal status must be complete or failed")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE catalog_operations SET status = ?, updated_at = ? "
                "WHERE operation_id = ? AND status = 'pending'",
                (status, updated_at, operation_id),
            )
            if cursor.rowcount != 1:
                raise StorageConflict("pending operation does not exist")
            row = connection.execute(
                "SELECT * FROM catalog_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> CatalogOperationRecord:
        return CatalogOperationRecord(
            row["operation_id"],
            row["kind"],
            row["profile_id"],
            row["source_name"],
            row["destination_name"],
            row["status"],
            row["created_at"],
            row["updated_at"],
            row["actor_user_id"],
            row["target_profile_id"],
        )


class TransferHandleDAO:
    """Short-lived user/profile-scoped reference to server-side staged data."""

    def __init__(self, database: ExtensionDatabase):
        self.database = database

    def issue(
        self, handle_id: str, user_id: str, profile_id: str, server_path: str, expires_at: float
    ) -> TransferHandleRecord:
        _required_text(handle_id, user_id, profile_id, server_path)
        _epoch(expires_at)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO transfer_handles (handle_id, user_id, profile_id, server_path, expires_at, status) "
                "SELECT ?, ?, ?, ?, ?, 'active' WHERE EXISTS "
                "(SELECT 1 FROM users WHERE user_id = ? AND active = 1)",
                (handle_id, user_id, profile_id, server_path, expires_at, user_id),
            )
            row = connection.execute(
                "SELECT * FROM transfer_handles WHERE handle_id = ?", (handle_id,)
            ).fetchone()
            if row is None:
                raise StorageConflict("active account required")
        return self._record(row)

    def get_valid(
        self, handle_id: str, user_id: str, profile_id: str, now: float
    ) -> TransferHandleRecord | None:
        _epoch(now)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT h.* FROM transfer_handles AS h JOIN users AS u ON u.user_id = h.user_id "
                "WHERE h.handle_id = ? AND h.user_id = ? AND h.profile_id = ? "
                "AND h.status = 'active' AND h.expires_at > ? AND u.active = 1",
                (handle_id, user_id, profile_id, now),
            ).fetchone()
        return self._record(row) if row is not None else None

    def expired(self, now: float) -> tuple[TransferHandleRecord, ...]:
        """List expired transfer rows for checked staging cleanup by the caller."""
        _epoch(now)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM transfer_handles WHERE expires_at <= ? ORDER BY expires_at, handle_id",
                (now,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def remove_expired(self, handle_id: str, now: float) -> TransferHandleRecord | None:
        """Remove an expired row only after the caller has cleaned its staged file."""
        _required_text(handle_id)
        _epoch(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM transfer_handles WHERE handle_id = ? AND expires_at <= ?",
                (handle_id, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM transfer_handles WHERE handle_id = ? AND expires_at <= ?",
                (handle_id, now),
            )
        return self._record(row)

    def revoke(self, handle_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE transfer_handles SET status = 'revoked' "
                "WHERE handle_id = ? AND status = 'active'",
                (handle_id,),
            )

    def revoke_profile(self, profile_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE transfer_handles SET status = 'revoked' "
                "WHERE profile_id = ? AND status = 'active'",
                (profile_id,),
            )

    def consume(
        self, handle_id: str, user_id: str, profile_id: str, now: float
    ) -> TransferHandleRecord | None:
        """Atomically return-and-consume one valid handle; concurrent reuse fails."""
        _epoch(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT h.* FROM transfer_handles AS h JOIN users AS u ON u.user_id = h.user_id "
                "WHERE h.handle_id = ? AND h.user_id = ? AND h.profile_id = ? "
                "AND h.status = 'active' AND h.expires_at > ? AND u.active = 1",
                (handle_id, user_id, profile_id, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE transfer_handles SET status = 'consumed' WHERE handle_id = ?",
                (handle_id,),
            )
        return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> TransferHandleRecord:
        return TransferHandleRecord(
            row["handle_id"],
            row["user_id"],
            row["profile_id"],
            row["server_path"],
            row["expires_at"],
            row["status"],
        )
