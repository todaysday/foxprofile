"""Atomic local-first browser admission across the Master and Workers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from math import isfinite
from time import time
from uuid import uuid4

from src.models.profile import Profile

from ..dto import BrowserRunRefDTO, CapacitySettings
from ..errors import DomainError, ErrorCode, ProfileCapacityExceeded
from ..storage.capacity import AdmissionResult, CapacityLedger, RunStatus
from ..storage.database import ExtensionDatabase
from .metrics import NodeMetrics
from .registry import WorkerRecord, WorkerRegistry

_ACTIVE_SQL = (
    "'reserved', 'transferring', 'starting', 'running', 'stopping', 'syncing', 'uncertain'"
)


class PlacementScheduler:
    def __init__(
        self,
        ledger: CapacityLedger,
        registry: WorkerRegistry,
        *,
        local_metrics: Callable[[], NodeMetrics],
        browser_memory_bytes: int,
        system_reserve_bytes: int,
        staging_reserve_bytes: int = 1 << 30,
        clock=time,
    ):
        _byte_setting(browser_memory_bytes, "browser_memory_bytes", minimum=1)
        _byte_setting(system_reserve_bytes, "system_reserve_bytes")
        _byte_setting(staging_reserve_bytes, "staging_reserve_bytes")
        if not callable(local_metrics) or not callable(clock):
            raise ValueError("local_metrics and clock must be callable")
        if ledger.database.path.resolve() != registry.database.path.resolve():
            raise ValueError("ledger and worker registry must use the same database")
        self.ledger = ledger
        self.registry = registry
        self.database: ExtensionDatabase = ledger.database
        self.local_metrics = local_metrics
        self.browser_memory_bytes = browser_memory_bytes
        self.system_reserve_bytes = system_reserve_bytes
        self.staging_reserve_bytes = staging_reserve_bytes
        self.clock = clock

    def reserve(
        self,
        profiles: Sequence[Profile],
        local_host_id: str,
        *,
        allow_remote: bool = True,
    ) -> AdmissionResult:
        """Reserve an authorized batch atomically, preferring local capacity."""
        profile_batch = _profiles(profiles)
        if not isinstance(local_host_id, str) or not local_host_id.strip():
            raise ValueError("local_host_id must be a non-empty string")
        if local_host_id.startswith("worker:"):
            raise ValueError("local_host_id cannot use a Worker identity")
        if type(allow_remote) is not bool:
            raise ValueError("allow_remote must be bool")
        with self.database.read() as connection:
            local_running_before_sample = frozenset(
                row["run_id"]
                for row in connection.execute(
                    "SELECT run_id FROM browser_runs WHERE host_id = ? AND status = 'running'",
                    (local_host_id,),
                )
            )
        try:
            local = self.local_metrics()
            if not isinstance(local, NodeMetrics):
                raise ValueError("local_metrics must return NodeMetrics")
            local = NodeMetrics(
                local.mem_available_bytes,
                local.cgroup_headroom_bytes,
                local.staging_free_bytes,
                local.ready_engines,
            )
        except (OSError, ValueError):
            local = None
        now = self._now()
        with self.database.transaction() as connection:
            blocked = connection.execute(
                "SELECT blocked FROM capacity_recovery WHERE singleton = 1"
            ).fetchone()[0]
            if blocked:
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)

            existing = {}
            for profile in profile_batch:
                row = connection.execute(
                    self.ledger._run_select()
                    + f" WHERE browser_runs.profile_id = ? AND browser_runs.status IN ({_ACTIVE_SQL})",
                    (profile.profile_id,),
                ).fetchone()
                if row is not None:
                    record = self.ledger._record(row)
                    if record.status is RunStatus.UNCERTAIN:
                        raise DomainError(ErrorCode.RUN_UNCERTAIN)
                    existing[profile.profile_id] = row
            new_profiles = [
                profile for profile in profile_batch if profile.profile_id not in existing
            ]

            occupied = connection.execute(
                f"SELECT COUNT(*) FROM browser_runs WHERE status IN ({_ACTIVE_SQL})"
            ).fetchone()[0]
            setting = connection.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'max_concurrent_profiles'"
            ).fetchone()
            capacity = CapacitySettings(int(setting[0]) if setting is not None else 10)
            if occupied + len(new_profiles) > capacity.max_concurrent_profiles:
                raise ProfileCapacityExceeded(capacity.max_concurrent_profiles)

            local_can_stage = self._has_staging_space(local)
            workers = self.registry._records_in_transaction(connection, now)
            active_inventory = self.registry._active_run_inventory_in_transaction(connection, now)
            local_reserved, remote_reserved = self._reserved_memory(
                connection, local_host_id, active_inventory, local_running_before_sample
            )
            local_remaining = self._memory_headroom(local) - local_reserved
            worker_headroom = {
                worker.worker_id: self._worker_headroom(worker, remote_reserved)
                for worker in workers
                if worker.state == "ready"
                and worker.boot_id is not None
                and worker.metrics is not None
            }
            placements: list[tuple[Profile, str, WorkerRecord | None]] = []
            for profile in new_profiles:
                if (
                    local is not None
                    and profile.engine in local.ready_engines
                    and local_can_stage
                    and local_remaining >= self.browser_memory_bytes
                ):
                    placements.append((profile, local_host_id, None))
                    local_remaining -= self.browser_memory_bytes
                    continue
                if not allow_remote:
                    raise DomainError(ErrorCode.NO_WORKER_MEMORY)
                worker = self._choose_worker(profile.engine, workers, worker_headroom)
                if worker is None:
                    self._raise_no_placement(profile.engine, workers, worker_headroom)
                host_id = f"worker:{worker.worker_id}:{worker.boot_id}"
                placements.append((profile, host_id, worker))
                worker_headroom[worker.worker_id] -= self.browser_memory_bytes

            newly_reserved: list[BrowserRunRefDTO] = []
            for profile, host_id, worker in placements:
                run_id = uuid4().hex
                connection.execute(
                    "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
                    "VALUES (?, ?, ?, 'reserved', ?)",
                    (run_id, profile.profile_id, host_id, now),
                )
                revision = getattr(profile, "base_revision", None)
                if revision is not None and (type(revision) is not int or revision < 0):
                    raise ValueError("profile base_revision must be a non-negative integer")
                connection.execute(
                    "INSERT INTO browser_run_placements "
                    "(run_id, worker_id, worker_boot_id, connection_generation, reserved_memory_bytes, base_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        worker.worker_id if worker is not None else None,
                        worker.boot_id if worker is not None else None,
                        worker.connection_generation if worker is not None else None,
                        self.browser_memory_bytes,
                        revision,
                    ),
                )
                existing[profile.profile_id] = connection.execute(
                    self.ledger._run_select() + " WHERE browser_runs.run_id = ?", (run_id,)
                ).fetchone()
                newly_reserved.append(BrowserRunRefDTO(profile.profile_id, run_id))
            return AdmissionResult(
                tuple(
                    self.ledger._record(existing[profile.profile_id]) for profile in profile_batch
                ),
                tuple(newly_reserved),
            )

    def _choose_worker(
        self,
        engine: str,
        workers: tuple[WorkerRecord, ...],
        headroom: dict[str, int],
    ) -> WorkerRecord | None:
        eligible = [
            worker
            for worker in workers
            if worker.worker_id in headroom
            and worker.metrics is not None
            and worker.boot_id is not None
            and engine in worker.metrics.ready_engines
            and self._has_staging_space(worker.metrics)
            and headroom[worker.worker_id] >= self.browser_memory_bytes
        ]
        if not eligible:
            return None
        return min(eligible, key=lambda worker: (-headroom[worker.worker_id], worker.worker_id))

    def _raise_no_placement(
        self,
        engine: str,
        workers: tuple[WorkerRecord, ...],
        headroom: dict[str, int],
    ) -> None:
        fresh = [
            worker for worker in workers if worker.state == "ready" and worker.metrics is not None
        ]
        if not fresh:
            if any(worker.state == "incompatible" for worker in workers):
                raise DomainError(ErrorCode.RUNTIME_INCOMPATIBLE)
            raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
        if not any(engine in worker.metrics.ready_engines for worker in fresh if worker.metrics):
            raise DomainError(ErrorCode.RUNTIME_INCOMPATIBLE)
        raise DomainError(ErrorCode.NO_WORKER_MEMORY)

    def _worker_headroom(self, worker: WorkerRecord, reserved: dict[str, int]) -> int:
        if worker.metrics is None or worker.boot_id is None:
            return 0
        return self._memory_headroom(worker.metrics) - reserved.get(worker.worker_id, 0)

    def _reserved_memory(
        self, connection, local_host_id, active_inventory, local_running_before_sample
    ):
        local_reserved = 0
        remote_reserved: dict[str, int] = {}
        rows = connection.execute(
            "SELECT browser_runs.run_id, browser_runs.host_id, browser_runs.status, "
            "p.worker_id, p.worker_boot_id, p.connection_generation, "
            "COALESCE(p.reserved_memory_bytes, 0) AS memory_bytes "
            "FROM browser_runs LEFT JOIN browser_run_placements p ON p.run_id = browser_runs.run_id "
            f"WHERE browser_runs.status IN ({_ACTIVE_SQL})"
        )
        for row in rows:
            if row["worker_id"] is None:
                if row["host_id"] == local_host_id:
                    reflected = (
                        row["status"] == RunStatus.RUNNING.value
                        and row["run_id"] in local_running_before_sample
                    )
                    if not reflected:
                        local_reserved += row["memory_bytes"]
                continue
            generation = (
                row["worker_id"],
                row["worker_boot_id"],
                row["connection_generation"],
            )
            reflected = row["status"] == RunStatus.RUNNING.value and row[
                "run_id"
            ] in active_inventory.get(generation, frozenset())
            if not reflected:
                remote_reserved[row["worker_id"]] = (
                    remote_reserved.get(row["worker_id"], 0) + row["memory_bytes"]
                )
        return local_reserved, remote_reserved

    def _memory_headroom(self, metrics: NodeMetrics | None) -> int:
        if metrics is None:
            return 0
        available = metrics.mem_available_bytes
        if metrics.cgroup_headroom_bytes is not None:
            available = min(available, metrics.cgroup_headroom_bytes)
        return max(0, available - self.system_reserve_bytes)

    def _has_staging_space(self, metrics: NodeMetrics | None) -> bool:
        return (
            metrics is not None
            and metrics.staging_free_bytes > 0
            and metrics.staging_free_bytes >= self.staging_reserve_bytes
        )

    def _now(self) -> float:
        now = self.clock()
        if type(now) not in (int, float) or not isfinite(now):
            raise ValueError("clock must return a finite Unix timestamp")
        return float(now)


def _byte_setting(value: object, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= (1 << 60):
        raise ValueError(f"{name} must be an integer byte count")


def _profiles(profiles: Sequence[Profile]) -> tuple[Profile, ...]:
    if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Sequence) or not profiles:
        raise ValueError("profiles must be a non-empty sequence")
    unique: dict[str, Profile] = {}
    for profile in profiles:
        if not isinstance(profile, Profile):
            raise ValueError("profiles must contain Profile objects")
        if not isinstance(profile.profile_id, str) or not profile.profile_id.strip():
            raise ValueError("profiles must have a profile_id")
        if profile.engine not in {"camoufox", "chrome"}:
            raise DomainError(ErrorCode.RUNTIME_INCOMPATIBLE)
        existing = unique.get(profile.profile_id)
        if existing is not None and existing.engine != profile.engine:
            raise ValueError("duplicate profile IDs must use the same engine")
        unique.setdefault(profile.profile_id, profile)
    return tuple(unique.values())
