"""Durable browser-run reservations and process-identity reconciliation."""

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from time import time
from uuid import uuid4

from ..dto import BrowserRunRefDTO, CapacitySettings, ProcessIdentity
from ..errors import DomainError, ErrorCode
from ..policy import require_capacity
from .database import ExtensionDatabase


class RunStatus(str, Enum):
    RESERVED = "reserved"
    TRANSFERRING = "transferring"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    SYNCING = "syncing"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class ProcessLiveness(str, Enum):
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


_ACTIVE_SQL = (
    "'reserved', 'transferring', 'starting', 'running', 'stopping', 'syncing', 'uncertain'"
)
_REMOTE_TRANSITIONS = {
    RunStatus.RESERVED: {RunStatus.TRANSFERRING, RunStatus.UNCERTAIN},
    RunStatus.TRANSFERRING: {RunStatus.STARTING, RunStatus.UNCERTAIN},
    RunStatus.STARTING: {RunStatus.RUNNING, RunStatus.STOPPING, RunStatus.UNCERTAIN},
    RunStatus.RUNNING: {RunStatus.STOPPING, RunStatus.UNCERTAIN},
    RunStatus.STOPPING: {RunStatus.SYNCING, RunStatus.UNCERTAIN},
    RunStatus.SYNCING: {RunStatus.UNCERTAIN},
    RunStatus.UNCERTAIN: {RunStatus.STOPPING, RunStatus.SYNCING},
}


@dataclass(frozen=True, slots=True)
class RunRecord:
    ref: BrowserRunRefDTO
    host_id: str
    status: RunStatus
    process: ProcessIdentity | None = field(default=None, repr=False)
    created_at: float = 0
    worker_id: str | None = None
    worker_boot_id: str | None = None
    connection_generation: int | None = None
    reserved_memory_bytes: int = 0
    base_revision: int | None = None


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    cancelled_unclaimed: int
    confirmed_live: int
    released_dead: int
    uncertain: int


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """Existing runs plus runs created by this admission request only."""

    runs: tuple[RunRecord, ...]
    newly_reserved: tuple[BrowserRunRefDTO, ...]


class CapacityLedger:
    def __init__(self, database: ExtensionDatabase):
        self.database = database

    def reserve(self, profile_ids: Iterable[str], host_id: str) -> AdmissionResult:
        ids = tuple(dict.fromkeys(profile_ids))
        if not ids or any(not isinstance(item, str) or not item.strip() for item in ids):
            raise ValueError("profile IDs must be non-empty strings")
        if not isinstance(host_id, str) or not host_id.strip():
            raise ValueError("host ID must be a non-empty string")
        if _is_remote_host(host_id):
            raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
        now = time()
        with self.database.transaction() as connection:
            blocked = connection.execute(
                "SELECT blocked FROM capacity_recovery WHERE singleton = 1"
            ).fetchone()[0]
            if blocked:
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)
            existing: dict[str, sqlite3.Row] = {}
            for profile_id in ids:
                row = connection.execute(
                    self._run_select()
                    + " WHERE browser_runs.profile_id = ? AND browser_runs.status IN ("
                    + _ACTIVE_SQL
                    + ")",
                    (profile_id,),
                ).fetchone()
                if row is not None:
                    record = self._record(row)
                    if (
                        record.worker_id is not None or _is_remote_host(record.host_id)
                    ) and record.host_id != host_id:
                        raise DomainError(ErrorCode.PROFILE_BUSY)
                    existing[profile_id] = row
            new_ids = [profile_id for profile_id in ids if profile_id not in existing]
            occupied = connection.execute(
                f"SELECT COUNT(*) FROM browser_runs WHERE status IN ({_ACTIVE_SQL})"
            ).fetchone()[0]
            setting = connection.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'max_concurrent_profiles'"
            ).fetchone()
            capacity = CapacitySettings(int(setting[0]) if setting is not None else 10)
            require_capacity(capacity, occupied, len(new_ids))
            newly_reserved: list[BrowserRunRefDTO] = []
            for profile_id in new_ids:
                run_id = uuid4().hex
                connection.execute(
                    "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
                    "VALUES (?, ?, ?, 'reserved', ?)",
                    (run_id, profile_id, host_id, now),
                )
                record = connection.execute(
                    self._run_select() + " WHERE browser_runs.run_id = ?", (run_id,)
                ).fetchone()
                existing[profile_id] = record
                newly_reserved.append(BrowserRunRefDTO(profile_id, run_id))
            return AdmissionResult(
                tuple(self._record(existing[profile_id]) for profile_id in ids),
                tuple(newly_reserved),
            )

    def get(self, ref: BrowserRunRefDTO) -> RunRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                self._run_select()
                + " WHERE browser_runs.run_id = ? AND browser_runs.profile_id = ?",
                (ref.run_id, ref.profile_id),
            ).fetchone()
        return self._record(row) if row is not None else None

    def active_runs(self) -> tuple[RunRecord, ...]:
        with self.database.read() as connection:
            rows = connection.execute(
                self._run_select()
                + f" WHERE browser_runs.status IN ({_ACTIVE_SQL}) ORDER BY browser_runs.profile_id"
            )
            return tuple(self._record(row) for row in rows)

    def lifecycle_snapshot(self) -> dict[str, tuple[str, str]]:
        """Read active state or latest unsaved outcome in one SQLite snapshot."""
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT browser_runs.profile_id, browser_runs.status, capacity_recovery.blocked, "
                "browser_runs.host_id, browser_run_placements.worker_id "
                "FROM browser_runs CROSS JOIN capacity_recovery "
                "LEFT JOIN browser_run_placements ON browser_run_placements.run_id = browser_runs.run_id "
                f"WHERE capacity_recovery.singleton = 1 AND (browser_runs.status IN ({_ACTIVE_SQL}) "
                "OR (browser_runs.status = 'closed' AND browser_run_placements.storage_outcome = 'unsaved' "
                "AND NOT EXISTS (SELECT 1 FROM browser_runs later LEFT JOIN browser_run_placements p "
                "ON p.run_id = later.run_id WHERE later.profile_id = browser_runs.profile_id "
                "AND later.rowid > browser_runs.rowid "
                f"AND (later.status IN ({_ACTIVE_SQL}) OR (later.status = 'closed' "
                "AND COALESCE(p.storage_outcome, 'saved') != 'not_started')))))"
            )
            states = {}
            for row in rows:
                state = row["status"]
                if state == "closed":
                    state = "unsaved"
                elif row["blocked"]:
                    state = "recovering"
                location = "local"
                if row["worker_id"] is not None or _is_remote_host(row["host_id"]):
                    location = "remote"
                states[row["profile_id"]] = (state, location)
            return states

    def lifecycle_states(self) -> dict[str, str]:
        """Read lifecycle states and terminal save warnings in one snapshot."""
        return {
            profile_id: state
            for profile_id, (state, _location) in self.lifecycle_snapshot().items()
        }

    def claim(self, ref: BrowserRunRefDTO, host_id: str, process: ProcessIdentity) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'starting', pid = ?, start_ticks = ? "
                "WHERE run_id = ? AND profile_id = ? AND host_id = ? AND status = 'reserved' "
                "AND pid IS NULL AND NOT EXISTS (SELECT 1 FROM browser_run_placements "
                "WHERE browser_run_placements.run_id = browser_runs.run_id "
                "AND browser_run_placements.worker_id IS NOT NULL) "
                "AND host_id NOT LIKE 'worker:%' "
                "AND EXISTS (SELECT 1 FROM capacity_recovery WHERE singleton = 1 AND blocked = 0)",
                (process.pid, process.start_ticks, ref.run_id, ref.profile_id, host_id),
            )
            return cursor.rowcount == 1

    def mark_running(self, ref: BrowserRunRefDTO) -> bool:
        return self._transition_local(ref, "starting", "running")

    def mark_stopping(self, ref: BrowserRunRefDTO) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'stopping' WHERE run_id = ? AND profile_id = ? "
                "AND status IN ('starting', 'running') AND NOT EXISTS "
                "(SELECT 1 FROM browser_run_placements WHERE browser_run_placements.run_id = browser_runs.run_id "
                "AND browser_run_placements.worker_id IS NOT NULL) AND host_id NOT LIKE 'worker:%'",
                (ref.run_id, ref.profile_id),
            )
            return cursor.rowcount == 1

    def cancel_unclaimed(self, ref: BrowserRunRefDTO) -> bool:
        """CAS cancellation frees only a never-claimed local reservation."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'cancelled' WHERE run_id = ? AND profile_id = ? "
                "AND status = 'reserved' AND pid IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM browser_run_placements WHERE browser_run_placements.run_id = browser_runs.run_id "
                "AND browser_run_placements.worker_id IS NOT NULL) AND host_id NOT LIKE 'worker:%'",
                (ref.run_id, ref.profile_id),
            )
            return cursor.rowcount == 1

    def release_after_exit(
        self,
        ref: BrowserRunRefDTO,
        process: ProcessIdentity,
        probe: Callable[[str, ProcessIdentity], ProcessLiveness | str],
    ) -> bool:
        """Release only a local run after its exact process identity is confirmed dead."""
        current = self.get(ref)
        if (
            current is None
            or current.worker_id is not None
            or _is_remote_host(current.host_id)
            or current.process != process
        ):
            return False
        try:
            if ProcessLiveness(probe(current.host_id, process)) is not ProcessLiveness.DEAD:
                return False
        except Exception:
            return False
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'closed' WHERE run_id = ? AND profile_id = ? "
                "AND host_id = ? AND pid = ? AND start_ticks = ? "
                "AND status IN ('starting', 'running', 'stopping') AND NOT EXISTS "
                "(SELECT 1 FROM browser_run_placements WHERE browser_run_placements.run_id = browser_runs.run_id "
                "AND browser_run_placements.worker_id IS NOT NULL) AND host_id NOT LIKE 'worker:%'",
                (ref.run_id, ref.profile_id, current.host_id, process.pid, process.start_ticks),
            )
            return cursor.rowcount == 1

    def transition_remote(
        self,
        ref: BrowserRunRefDTO,
        worker_id: str,
        worker_boot_id: str,
        connection_generation: int,
        expected_status: RunStatus | str,
        next_status: RunStatus | str,
    ) -> bool:
        """CAS an exact assigned remote run without releasing its reservation."""
        if not _valid_remote_identity(worker_id, worker_boot_id, connection_generation):
            return False
        expected, target = RunStatus(expected_status), RunStatus(next_status)
        if target not in _REMOTE_TRANSITIONS.get(expected, set()):
            return False
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = ? WHERE run_id = ? AND profile_id = ? "
                "AND status = ? AND EXISTS (SELECT 1 FROM browser_run_placements p "
                "WHERE p.run_id = browser_runs.run_id AND p.worker_id = ? "
                "AND p.worker_boot_id = ? AND p.connection_generation = ?)",
                (
                    target.value,
                    ref.run_id,
                    ref.profile_id,
                    expected.value,
                    worker_id,
                    worker_boot_id,
                    connection_generation,
                ),
            )
            return cursor.rowcount == 1

    def mark_remote_uncertain(
        self,
        ref: BrowserRunRefDTO,
        worker_id: str,
        worker_boot_id: str,
        connection_generation: int,
        expected_status: RunStatus | str | None = None,
    ) -> bool:
        """Keep exact remote ownership and capacity reserved after lost certainty."""
        if not _valid_remote_identity(worker_id, worker_boot_id, connection_generation):
            return False
        expected_clause = ""
        parameters: list[object] = [ref.run_id, ref.profile_id]
        if expected_status is not None:
            expected_clause = " AND browser_runs.status = ?"
            parameters.append(RunStatus(expected_status).value)
        parameters.extend((worker_id, worker_boot_id, connection_generation))
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'uncertain' WHERE run_id = ? AND profile_id = ? "
                f"AND status IN ({_ACTIVE_SQL}){expected_clause} AND EXISTS "
                "(SELECT 1 FROM browser_run_placements p WHERE p.run_id = browser_runs.run_id "
                "AND p.worker_id = ? AND p.worker_boot_id = ? AND p.connection_generation = ?)",
                tuple(parameters),
            )
            return cursor.rowcount == 1

    def complete_remote(
        self,
        ref: BrowserRunRefDTO,
        worker_id: str,
        worker_boot_id: str,
        connection_generation: int,
        *,
        process_stopped: bool,
        sync_verified: bool = False,
        quarantined: bool = False,
        no_process_created: bool = False,
    ) -> bool:
        """Close an exact remote run only after stop and a safe storage outcome."""
        if not _valid_remote_identity(worker_id, worker_boot_id, connection_generation):
            return False
        if type(process_stopped) is not bool or type(sync_verified) is not bool:
            return False
        if (
            type(quarantined) is not bool
            or type(no_process_created) is not bool
            or not process_stopped
            or not (sync_verified or quarantined)
            or (no_process_created and not quarantined)
        ):
            return False
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'closed' WHERE run_id = ? AND profile_id = ? "
                "AND status IN ('syncing', 'uncertain') AND EXISTS "
                "(SELECT 1 FROM browser_run_placements p WHERE p.run_id = browser_runs.run_id "
                "AND p.worker_id = ? AND p.worker_boot_id = ? AND p.connection_generation = ?)",
                (ref.run_id, ref.profile_id, worker_id, worker_boot_id, connection_generation),
            )
            if cursor.rowcount != 1:
                return False
            if no_process_created:
                outcome = "not_started"
            elif sync_verified:
                outcome = "saved"
            else:
                outcome = "unsaved"
            connection.execute(
                "UPDATE browser_run_placements SET storage_outcome = ? WHERE run_id = ?",
                (outcome, ref.run_id),
            )
            return True

    def recover(
        self, probe: Callable[[str, ProcessIdentity], ProcessLiveness | str]
    ) -> RecoveryReport:
        """Probe local processes only; remote uncertainty stays owned per profile."""
        with self.database.transaction() as connection:
            connection.execute("UPDATE capacity_recovery SET blocked = 1 WHERE singleton = 1")
            rows = tuple(
                connection.execute(
                    self._run_select() + f" WHERE browser_runs.status IN ({_ACTIVE_SQL})"
                )
            )

        cancelled = confirmed = released = uncertain = local_uncertain = 0
        decisions: list[tuple[sqlite3.Row, ProcessLiveness]] = []
        for row in rows:
            record = self._record(row)
            if record.worker_id is not None or _is_remote_host(record.host_id):
                decisions.append((row, ProcessLiveness.UNKNOWN))
            elif row["pid"] is None:
                decisions.append((row, ProcessLiveness.DEAD))
            else:
                identity = ProcessIdentity(row["pid"], row["start_ticks"])
                try:
                    liveness = ProcessLiveness(probe(row["host_id"], identity))
                except Exception:
                    liveness = ProcessLiveness.UNKNOWN
                decisions.append((row, liveness))
        with self.database.transaction() as connection:
            for row, liveness in decisions:
                if row["worker_id"] is not None or _is_remote_host(row["host_id"]):
                    if row["worker_id"] is None:
                        cursor = connection.execute(
                            "UPDATE browser_runs SET status = 'uncertain' WHERE run_id = ? "
                            f"AND status IN ({_ACTIVE_SQL})",
                            (row["run_id"],),
                        )
                        uncertain += cursor.rowcount
                        continue
                    cursor = connection.execute(
                        "UPDATE browser_runs SET status = 'uncertain' WHERE run_id = ? "
                        f"AND status IN ({_ACTIVE_SQL}) AND EXISTS "
                        "(SELECT 1 FROM browser_run_placements p WHERE p.run_id = browser_runs.run_id "
                        "AND p.worker_id = ? AND p.worker_boot_id = ? AND p.connection_generation = ?)",
                        (
                            row["run_id"],
                            row["worker_id"],
                            row["worker_boot_id"],
                            row["connection_generation"],
                        ),
                    )
                    uncertain += cursor.rowcount
                elif liveness is ProcessLiveness.UNKNOWN:
                    uncertain += 1
                    local_uncertain += 1
                elif row["pid"] is None:
                    cursor = connection.execute(
                        "UPDATE browser_runs SET status = 'cancelled' WHERE run_id = ? "
                        "AND status = 'reserved' AND pid IS NULL",
                        (row["run_id"],),
                    )
                    cancelled += cursor.rowcount
                elif liveness is ProcessLiveness.DEAD:
                    cursor = connection.execute(
                        "UPDATE browser_runs SET status = 'closed' WHERE run_id = ? "
                        "AND pid = ? AND start_ticks = ? AND status IN ('starting', 'running', 'stopping')",
                        (row["run_id"], row["pid"], row["start_ticks"]),
                    )
                    released += cursor.rowcount
                else:
                    confirmed += 1
            connection.execute(
                "UPDATE capacity_recovery SET blocked = ? WHERE singleton = 1",
                (int(local_uncertain > 0),),
            )
        return RecoveryReport(cancelled, confirmed, released, uncertain)

    def _transition_local(self, ref: BrowserRunRefDTO, old: str, new: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = ? WHERE run_id = ? AND profile_id = ? "
                "AND status = ? AND NOT EXISTS (SELECT 1 FROM browser_run_placements "
                "WHERE browser_run_placements.run_id = browser_runs.run_id "
                "AND browser_run_placements.worker_id IS NOT NULL) AND host_id NOT LIKE 'worker:%'",
                (new, ref.run_id, ref.profile_id, old),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _run_select() -> str:
        return (
            "SELECT browser_runs.*, p.worker_id, p.worker_boot_id, p.connection_generation, "
            "COALESCE(p.reserved_memory_bytes, 0) AS reserved_memory_bytes, p.base_revision "
            "FROM browser_runs LEFT JOIN browser_run_placements p ON p.run_id = browser_runs.run_id"
        )

    def _record(self, row: sqlite3.Row) -> RunRecord:
        keys = set(row.keys())
        process = (
            ProcessIdentity(row["pid"], row["start_ticks"]) if row["pid"] is not None else None
        )
        return RunRecord(
            ref=BrowserRunRefDTO(row["profile_id"], row["run_id"]),
            host_id=row["host_id"],
            status=RunStatus(row["status"]),
            process=process,
            created_at=row["created_at"],
            worker_id=row["worker_id"] if "worker_id" in keys else None,
            worker_boot_id=row["worker_boot_id"] if "worker_boot_id" in keys else None,
            connection_generation=row["connection_generation"]
            if "connection_generation" in keys
            else None,
            reserved_memory_bytes=row["reserved_memory_bytes"]
            if "reserved_memory_bytes" in keys
            else 0,
            base_revision=row["base_revision"] if "base_revision" in keys else None,
        )


def _is_remote_host(host_id: str) -> bool:
    return host_id.startswith("worker:")


def _valid_remote_identity(worker_id: str, worker_boot_id: str, generation: int) -> bool:
    return (
        isinstance(worker_id, str)
        and bool(worker_id.strip())
        and isinstance(worker_boot_id, str)
        and bool(worker_boot_id.strip())
        and type(generation) is int
        and generation > 0
    )
