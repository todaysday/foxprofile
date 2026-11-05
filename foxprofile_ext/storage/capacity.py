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
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class ProcessLiveness(str, Enum):
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunRecord:
    ref: BrowserRunRefDTO
    host_id: str
    status: RunStatus
    process: ProcessIdentity | None = field(default=None, repr=False)
    created_at: float = 0


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
                    "SELECT * FROM browser_runs WHERE profile_id = ? "
                    "AND status IN ('reserved', 'starting', 'running', 'stopping')",
                    (profile_id,),
                ).fetchone()
                if row is not None:
                    existing[profile_id] = row
            new_ids = [profile_id for profile_id in ids if profile_id not in existing]
            occupied = connection.execute(
                "SELECT COUNT(*) FROM browser_runs WHERE status IN "
                "('reserved', 'starting', 'running', 'stopping')"
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
                    "SELECT * FROM browser_runs WHERE run_id = ?", (run_id,)
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
                "SELECT * FROM browser_runs WHERE run_id = ? AND profile_id = ?",
                (ref.run_id, ref.profile_id),
            ).fetchone()
        return self._record(row) if row is not None else None

    def active_runs(self) -> tuple[RunRecord, ...]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM browser_runs WHERE status IN "
                "('reserved', 'starting', 'running', 'stopping') ORDER BY profile_id"
            )
            return tuple(self._record(row) for row in rows)

    def claim(self, ref: BrowserRunRefDTO, host_id: str, process: ProcessIdentity) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'starting', pid = ?, start_ticks = ? "
                "WHERE run_id = ? AND profile_id = ? AND host_id = ? AND status = 'reserved' "
                "AND pid IS NULL AND EXISTS (SELECT 1 FROM capacity_recovery WHERE singleton = 1 AND blocked = 0)",
                (process.pid, process.start_ticks, ref.run_id, ref.profile_id, host_id),
            )
            return cursor.rowcount == 1

    def mark_running(self, ref: BrowserRunRefDTO) -> bool:
        return self._transition(ref, "starting", "running")

    def mark_stopping(self, ref: BrowserRunRefDTO) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'stopping' WHERE run_id = ? AND profile_id = ? "
                "AND status IN ('starting', 'running')",
                (ref.run_id, ref.profile_id),
            )
            return cursor.rowcount == 1

    def cancel_unclaimed(self, ref: BrowserRunRefDTO) -> bool:
        """CAS cancellation frees only a never-claimed reservation."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'cancelled' WHERE run_id = ? AND profile_id = ? "
                "AND status = 'reserved' AND pid IS NULL",
                (ref.run_id, ref.profile_id),
            )
            return cursor.rowcount == 1

    def release_after_exit(
        self,
        ref: BrowserRunRefDTO,
        process: ProcessIdentity,
        probe: Callable[[str, ProcessIdentity], ProcessLiveness | str],
    ) -> bool:
        """Release only after host probe confirms exact process identity is dead."""
        current = self.get(ref)
        if current is None or current.process != process:
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
                "AND status IN ('starting', 'running', 'stopping')",
                (ref.run_id, ref.profile_id, current.host_id, process.pid, process.start_ticks),
            )
            return cursor.rowcount == 1

    def recover(
        self, probe: Callable[[str, ProcessIdentity], ProcessLiveness | str]
    ) -> RecoveryReport:
        """Probe outside transactions; unknown liveness keeps admission blocked."""
        with self.database.transaction() as connection:
            connection.execute("UPDATE capacity_recovery SET blocked = 1 WHERE singleton = 1")
            rows = tuple(
                connection.execute(
                    "SELECT * FROM browser_runs WHERE status IN "
                    "('reserved', 'starting', 'running', 'stopping')"
                )
            )

        cancelled = confirmed = released = uncertain = 0
        decisions: list[tuple[sqlite3.Row, ProcessLiveness]] = []
        for row in rows:
            if row["pid"] is None:
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
                if liveness is ProcessLiveness.UNKNOWN:
                    uncertain += 1
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
                (int(uncertain > 0),),
            )
        return RecoveryReport(cancelled, confirmed, released, uncertain)

    def _transition(self, ref: BrowserRunRefDTO, old: str, new: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE browser_runs SET status = ? WHERE run_id = ? AND profile_id = ? AND status = ?",
                (new, ref.run_id, ref.profile_id, old),
            )
            return cursor.rowcount == 1

    def _record(self, row: sqlite3.Row) -> RunRecord:
        process = (
            ProcessIdentity(row["pid"], row["start_ticks"]) if row["pid"] is not None else None
        )
        return RunRecord(
            BrowserRunRefDTO(row["profile_id"], row["run_id"]),
            row["host_id"],
            RunStatus(row["status"]),
            process,
            row["created_at"],
        )
