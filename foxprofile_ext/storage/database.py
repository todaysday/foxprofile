"""SQLite connection and schema migration boundary for extension state."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_VERSION = 4


class ExtensionDatabase:
    """Open short-lived connections to one explicit, local database path.

    Session timestamps and expiry values are Unix seconds in UTC. SQLite uses
    rollback journaling so no less-restricted WAL sidecar is left behind.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _migrate(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported extension schema version: {version}")
            if version < 1:
                for statement in (
                    "CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin', 'user')), active INTEGER NOT NULL CHECK (active IN (0, 1)), auth_version INTEGER NOT NULL DEFAULT 0 CHECK (auth_version >= 0))",
                    "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, token_hash TEXT NOT NULL UNIQUE, auth_version INTEGER NOT NULL CHECK (auth_version >= 0), created_at REAL NOT NULL, expires_at REAL NOT NULL CHECK (expires_at > created_at))",
                    "CREATE INDEX sessions_user_id ON sessions(user_id)",
                    "CREATE TABLE profile_grants (user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, profile_id TEXT NOT NULL, PRIMARY KEY (user_id, profile_id))",
                    "CREATE INDEX profile_grants_profile_id ON profile_grants(profile_id)",
                    "CREATE TABLE settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)",
                    "INSERT INTO settings (setting_key, setting_value) VALUES ('max_concurrent_profiles', '10')",
                    "CREATE TABLE catalog_operations (operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('create', 'import', 'rename', 'delete', 'identity_migration')), profile_id TEXT NOT NULL, source_name TEXT, destination_name TEXT, status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed')), created_at REAL NOT NULL, updated_at REAL NOT NULL)",
                    "CREATE INDEX catalog_operations_status ON catalog_operations(status)",
                ):
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
                version = 1
            if version < 2:
                connection.execute(
                    "ALTER TABLE catalog_operations ADD COLUMN actor_user_id TEXT REFERENCES users(user_id) ON DELETE SET NULL"
                )
                connection.execute(
                    "ALTER TABLE catalog_operations ADD COLUMN target_profile_id TEXT"
                )
                connection.execute(
                    "CREATE TABLE transfer_handles (handle_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, profile_id TEXT NOT NULL, server_path TEXT NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'consumed')))"
                )
                connection.execute(
                    "CREATE INDEX transfer_handles_owner ON transfer_handles(user_id, profile_id, status)"
                )
                connection.execute("PRAGMA user_version = 2")
                version = 2
            if version < 3:
                connection.execute(
                    "CREATE TABLE browser_runs (run_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, host_id TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('reserved', 'starting', 'running', 'stopping', 'cancelled', 'closed')), pid INTEGER, start_ticks INTEGER, created_at REAL NOT NULL, CHECK ((pid IS NULL AND start_ticks IS NULL) OR (pid > 0 AND start_ticks >= 0)))"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX browser_runs_one_active_profile ON browser_runs(profile_id) WHERE status IN ('reserved', 'starting', 'running', 'stopping')"
                )
                connection.execute("CREATE INDEX browser_runs_status ON browser_runs(status)")
                connection.execute(
                    "CREATE TABLE capacity_recovery (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), blocked INTEGER NOT NULL CHECK (blocked IN (0, 1)))"
                )
                connection.execute(
                    "INSERT INTO capacity_recovery (singleton, blocked) VALUES (1, 0)"
                )
                connection.execute("PRAGMA user_version = 3")
                version = 3
            if version < 4:
                connection.execute(
                    "CREATE TABLE manual_leases (session_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, personal_session_id TEXT NOT NULL, profile_id TEXT NOT NULL, browser_run_id TEXT NOT NULL, host_id TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('provisioning', 'active', 'closing', 'recovering', 'closed')), created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL, cleanup_deadline REAL NOT NULL, helper_pid INTEGER, helper_start_ticks INTEGER, CHECK (expires_at > created_at), CHECK (cleanup_deadline > created_at), CHECK ((helper_pid IS NULL AND helper_start_ticks IS NULL) OR (helper_pid > 0 AND helper_start_ticks >= 0)))"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX manual_leases_one_open ON manual_leases((1)) WHERE state IN ('provisioning', 'active', 'closing', 'recovering')"
                )
                connection.execute("CREATE INDEX manual_leases_expiry ON manual_leases(expires_at)")
                connection.execute("PRAGMA user_version = 4")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a short write transaction; callers must not perform I/O inside."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()
