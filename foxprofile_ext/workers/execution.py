"""Worker-local execution state with no Master admission or account tables."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..dto import BrowserRunRefDTO
from ..storage.capacity import RunStatus

_WORKER_TABLES = frozenset({"browser_runs", "capacity_recovery", "runtime_descriptors"})
_MAX_STATE_JSON_BYTES = 4 * 1024 * 1024
_MAX_RETAINED_PROOFS = 2048
_MAX_RETAINED_ACKNOWLEDGED = 32
_ACTIVE = ("reserved", "starting", "running", "stopping")


class WorkerExecutionDatabase:
    """Small process gate used by the existing protected RuntimeSupervisor.

    This database never assigns a run ID and has no user, profile catalog,
    grants, settings, or API state. ``reserve_exact`` only records an ID
    already issued by the authenticated Master connection.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("Worker execution database cannot be a symlink")
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
            tables = frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            )
            if tables - _WORKER_TABLES:
                raise RuntimeError("Worker database contains non-execution state")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS browser_runs ("
                "run_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, host_id TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK (status IN ('reserved', 'transferring', 'starting', 'running', 'stopping', 'syncing', 'uncertain', 'cancelled', 'closed')), "
                "pid INTEGER, start_ticks INTEGER, created_at REAL NOT NULL, "
                "CHECK ((pid IS NULL AND start_ticks IS NULL) OR (pid > 0 AND start_ticks >= 0)))"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS browser_runs_one_active_profile "
                "ON browser_runs(profile_id) WHERE status IN ('reserved', 'starting', 'running', 'stopping')"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS browser_runs_status ON browser_runs(status)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS capacity_recovery ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "blocked INTEGER NOT NULL CHECK (blocked IN (0, 1)), "
                "run_context_json TEXT NOT NULL DEFAULT '{}', "
                "stopped_proofs_json TEXT NOT NULL DEFAULT '{}')"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(capacity_recovery)")}
            if "run_context_json" not in columns:
                connection.execute(
                    "ALTER TABLE capacity_recovery ADD COLUMN run_context_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "stopped_proofs_json" not in columns:
                connection.execute(
                    "ALTER TABLE capacity_recovery ADD COLUMN stopped_proofs_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute(
                "INSERT OR IGNORE INTO capacity_recovery (singleton, blocked) VALUES (1, 0)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS runtime_descriptors ("
                "run_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, host_id TEXT NOT NULL, "
                "supervisor_pid INTEGER NOT NULL, supervisor_start_ticks INTEGER NOT NULL, "
                "browser_pid INTEGER, browser_start_ticks INTEGER, boundary_pid INTEGER, "
                "boundary_start_ticks INTEGER, display TEXT, namespace TEXT, tmp_path TEXT, "
                "xauthority_path TEXT, control_port INTEGER, control_token TEXT, updated_at REAL NOT NULL, "
                "CHECK (supervisor_pid > 0 AND supervisor_start_ticks >= 0), "
                "CHECK ((browser_pid IS NULL AND browser_start_ticks IS NULL) OR (browser_pid > 0 AND browser_start_ticks >= 0)), "
                "CHECK ((boundary_pid IS NULL AND boundary_start_ticks IS NULL) OR (boundary_pid > 0 AND boundary_start_ticks >= 0)), "
                "CHECK ((control_port IS NULL AND control_token IS NULL) OR (control_port BETWEEN 1 AND 65535 AND control_token IS NOT NULL)))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS runtime_descriptors_profile ON runtime_descriptors(profile_id)"
            )
            connection.execute("DROP VIEW IF EXISTS browser_run_placements")
            connection.execute(
                "CREATE VIEW browser_run_placements AS "
                "SELECT run_id, NULL AS worker_id, NULL AS worker_boot_id, "
                "NULL AS connection_generation, 0 AS reserved_memory_bytes, "
                "NULL AS base_revision, NULL AS storage_outcome FROM browser_runs"
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
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

    def reserve_exact(self, ref: BrowserRunRefDTO, host_id: str) -> bool:
        """Insert only an authenticated Master-issued generation; never mint IDs."""
        if (
            not isinstance(ref, BrowserRunRefDTO)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref.run_id)
            or not isinstance(host_id, str)
            or not host_id.strip()
        ):
            raise ValueError("invalid exact worker run identity")
        now = time.time()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT profile_id, host_id FROM browser_runs WHERE run_id = ?",
                (ref.run_id,),
            ).fetchone()
            if existing is not None:
                return False
            active = connection.execute(
                "SELECT 1 FROM browser_runs WHERE profile_id = ? "
                "AND status IN ('reserved', 'starting', 'running', 'stopping')",
                (ref.profile_id,),
            ).fetchone()
            if active is not None:
                raise RuntimeError("profile already has an active worker execution")
            connection.execute(
                "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
                "VALUES (?, ?, ?, 'reserved', ?)",
                (ref.run_id, ref.profile_id, host_id, now),
            )
        return True

    def record_cancelled_exact(self, ref: BrowserRunRefDTO, host_id: str) -> bool:
        """Tombstone an exact Master run that cannot spawn because a local slot is busy."""
        if (
            not isinstance(ref, BrowserRunRefDTO)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref.run_id)
            or not isinstance(host_id, str)
            or not host_id.strip()
        ):
            raise ValueError("invalid exact worker run identity")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT profile_id FROM browser_runs WHERE run_id = ?", (ref.run_id,)
            ).fetchone()
            if existing is not None:
                return False
            connection.execute(
                "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
                "VALUES (?, ?, ?, 'cancelled', ?)",
                (ref.run_id, ref.profile_id, host_id, time.time()),
            )
        return True

    def run_status(self, ref: BrowserRunRefDTO) -> RunStatus | None:
        with self.read() as connection:
            row = connection.execute(
                "SELECT status FROM browser_runs WHERE run_id = ? AND profile_id = ?",
                (ref.run_id, ref.profile_id),
            ).fetchone()
        return RunStatus(row["status"]) if row is not None else None

    def active_runs(self) -> tuple[dict[str, object], ...]:
        with self.read() as connection:
            rows = connection.execute(
                "SELECT profile_id, run_id, status FROM browser_runs "
                "WHERE status IN ('reserved', 'starting', 'running', 'stopping') "
                "ORDER BY profile_id"
            )
            return tuple(dict(row) for row in rows)

    def put_run_context(self, run_id: str, value: dict[str, object]) -> None:
        self._update_json_map("run_context_json", run_id, value)

    def run_context(self, run_id: str) -> dict[str, object] | None:
        return self._read_json_map("run_context_json").get(run_id)

    def record_stop_proof(
        self, run_id: str, proof: dict[str, object], *, context: dict[str, object] | None = None
    ) -> None:
        if len(json.dumps(proof, separators=(",", ":")).encode()) > 65536:
            raise ValueError("worker stop proof is too large")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT run_context_json, stopped_proofs_json FROM capacity_recovery WHERE singleton = 1"
            ).fetchone()
            contexts = json.loads(row["run_context_json"])
            proofs = json.loads(row["stopped_proofs_json"])
            # A stop proof is a generation-scoped fact, not a mutable status
            # slot. First proof wins so a late callback cannot downgrade or
            # replace the exact archive digest already issued to Master.
            if run_id in proofs:
                return
            if context is not None:
                contexts[run_id] = context
            proofs[run_id] = proof
            _prune_acknowledged(contexts, proofs)
            values = (contexts, proofs)
            encoded = tuple(json.dumps(item, separators=(",", ":")) for item in values)
            if len(proofs) > _MAX_RETAINED_PROOFS or any(
                len(item.encode()) > _MAX_STATE_JSON_BYTES for item in encoded
            ):
                raise ValueError("worker run state exceeds its size limit")
            connection.execute(
                "UPDATE capacity_recovery SET run_context_json = ?, stopped_proofs_json = ? "
                "WHERE singleton = 1",
                encoded,
            )

    def update_run_context(self, run_id: str, **updates: object) -> dict[str, object] | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT run_context_json FROM capacity_recovery WHERE singleton = 1"
            ).fetchone()
            contexts = json.loads(row["run_context_json"])
            value = contexts.get(run_id)
            if not isinstance(value, dict):
                return None
            value.update(updates)
            encoded = json.dumps(contexts, separators=(",", ":"))
            if len(encoded.encode()) > _MAX_STATE_JSON_BYTES:
                raise ValueError("worker run state exceeds its size limit")
            connection.execute(
                "UPDATE capacity_recovery SET run_context_json = ? WHERE singleton = 1",
                (encoded,),
            )
            return value

    def acknowledge_terminal_run(self, run_id: str) -> bool:
        """Retain only bounded proof/receipt metadata after Master closes a run.

        The terminal browser_runs row remains as the exact-run tombstone. Only
        Master-acknowledged terminal records are pruned, with the newest 32
        retained for proof and commit-receipt replay. Active runs and clean
        archives awaiting sync are never pruned here.
        """
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT status FROM browser_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run["status"] not in {"cancelled", "closed"}:
                return False
            row = connection.execute(
                "SELECT run_context_json, stopped_proofs_json FROM capacity_recovery WHERE singleton = 1"
            ).fetchone()
            contexts = json.loads(row["run_context_json"])
            proofs = json.loads(row["stopped_proofs_json"])
            context = contexts.get(run_id)
            proof = proofs.get(run_id)
            if not isinstance(context, dict) or not isinstance(proof, dict):
                return False
            compact = {
                key: context[key]
                for key in (
                    "profile_id",
                    "run_id",
                    "worker_id",
                    "worker_boot_id",
                    "connection_generation",
                    "data_cleaned",
                    "sync_completed",
                    "sync_receipt",
                    "sync_archive_digest",
                    "sync_base_revision",
                    "sync_worker_id",
                    "sync_worker_boot_id",
                )
                if key in context
            }
            compact.update(
                {
                    "terminal_acknowledged": True,
                    "terminal_disposition": "closed",
                    "terminal_acknowledged_at": time.time(),
                    "data_cleaned": True,
                }
            )
            contexts[run_id] = compact
            _prune_acknowledged(contexts, proofs)
            encoded = (
                json.dumps(contexts, separators=(",", ":")),
                json.dumps(proofs, separators=(",", ":")),
            )
            if len(proofs) > _MAX_RETAINED_PROOFS or any(
                len(item.encode()) > _MAX_STATE_JSON_BYTES for item in encoded
            ):
                raise ValueError("worker run state exceeds its size limit")
            connection.execute(
                "UPDATE capacity_recovery SET run_context_json = ?, stopped_proofs_json = ? "
                "WHERE singleton = 1",
                encoded,
            )
            return True

    def stopped_proofs(self, *, include_acknowledged: bool = True) -> tuple[dict[str, object], ...]:
        values = self._read_json_map("stopped_proofs_json")
        if not include_acknowledged:
            contexts = self._read_json_map("run_context_json")
            values = {
                run_id: proof
                for run_id, proof in values.items()
                if (contexts.get(run_id) or {}).get("terminal_acknowledged") is not True
            }
        return tuple(values.values())

    def _update_json_map(self, column: str, key: str, value: dict[str, object]) -> None:
        if column not in {"run_context_json", "stopped_proofs_json"}:
            raise ValueError("invalid worker state column")
        encoded_value = json.dumps(value, separators=(",", ":"))
        if len(encoded_value.encode()) > 65536:
            raise ValueError("worker run state item is too large")
        with self.transaction() as connection:
            row = connection.execute(
                f"SELECT {column} FROM capacity_recovery WHERE singleton = 1"
            ).fetchone()
            values = json.loads(row[column])
            values[key] = value
            encoded = json.dumps(values, separators=(",", ":"))
            if len(encoded.encode()) > _MAX_STATE_JSON_BYTES:
                raise ValueError("worker run state exceeds its size limit")
            connection.execute(
                f"UPDATE capacity_recovery SET {column} = ? WHERE singleton = 1", (encoded,)
            )

    def _read_json_map(self, column: str) -> dict[str, dict[str, object]]:
        if column not in {"run_context_json", "stopped_proofs_json"}:
            raise ValueError("invalid worker state column")
        with self.read() as connection:
            row = connection.execute(
                f"SELECT {column} FROM capacity_recovery WHERE singleton = 1"
            ).fetchone()
        value = json.loads(row[column])
        if not isinstance(value, dict) or any(
            not isinstance(item, dict) for item in value.values()
        ):
            raise RuntimeError("Worker execution state is corrupt")
        return value


def assert_worker_only_database(path: str | Path) -> None:
    """Raise if a worker DB contains any Master-only table."""
    with sqlite3.connect(path) as connection:
        tables = frozenset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
    if tables != _WORKER_TABLES:
        raise RuntimeError("Worker database contains unexpected tables")


def _prune_acknowledged(
    contexts: dict[str, dict[str, object]], proofs: dict[str, dict[str, object]]
) -> None:
    """Drop oldest compact terminal records only when bounded storage requires it."""
    acknowledged = sorted(
        (
            (context.get("terminal_acknowledged_at", 0), run_id)
            for run_id, context in contexts.items()
            if context.get("terminal_acknowledged") is True and run_id in proofs
        ),
        key=lambda item: item[0] if type(item[0]) in (int, float) else 0,
    )
    while acknowledged:
        context_bytes = len(json.dumps(contexts, separators=(",", ":")).encode())
        proof_bytes = len(json.dumps(proofs, separators=(",", ":")).encode())
        if (
            len(acknowledged) <= _MAX_RETAINED_ACKNOWLEDGED
            and len(proofs) <= _MAX_RETAINED_PROOFS
            and max(context_bytes, proof_bytes) <= _MAX_STATE_JSON_BYTES
        ):
            return
        _created, run_id = acknowledged.pop(0)
        contexts.pop(run_id, None)
        proofs.pop(run_id, None)
