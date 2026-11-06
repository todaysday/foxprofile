"""Private, restart-safe runtime metadata for exact browser-run generations."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path

from ..dto import BrowserRunRefDTO, ProcessIdentity, RuntimeDescriptor
from ..storage.capacity import CapacityLedger, ProcessLiveness, RunStatus
from ..storage.database import ExtensionDatabase


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    descriptor: RuntimeDescriptor
    updated_at: float


def current_host_id() -> str:
    """Identify this host boot, never a copied database file."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        boot_id = "unknown-boot"
    return f"{socket.gethostname()}:{boot_id}"


def process_identity(pid: int) -> ProcessIdentity | None:
    """Read Linux PID start ticks, handling spaces and parentheses in comm."""
    if type(pid) is not int or pid < 1:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = stat[stat.rfind(")") + 2 :].split()
        return ProcessIdentity(pid, int(fields[19]))
    except (OSError, ValueError, IndexError):
        return None


def probe_process(host_id: str, identity: ProcessIdentity) -> ProcessLiveness:
    """Prove exact host/PID generation liveness; uncertainty is retained."""
    local_host = current_host_id()
    if host_id != local_host or local_host.endswith(":unknown-boot"):
        return ProcessLiveness.UNKNOWN
    try:
        stat = Path(f"/proc/{identity.pid}/stat").read_text(encoding="ascii")
        fields = stat[stat.rfind(")") + 2 :].split()
        current = ProcessIdentity(identity.pid, int(fields[19]))
    except FileNotFoundError:
        return ProcessLiveness.DEAD
    except (OSError, ValueError, IndexError):
        return ProcessLiveness.UNKNOWN
    if fields[0] in {"Z", "X"}:
        return ProcessLiveness.DEAD
    return ProcessLiveness.LIVE if current == identity else ProcessLiveness.DEAD


class RuntimeRegistry:
    """Persist trusted process/display/control bindings outside the browser."""

    def __init__(self, database: ExtensionDatabase, ledger: CapacityLedger):
        self.database = database
        self.ledger = ledger

    def register_supervisor(
        self,
        ref: BrowserRunRefDTO,
        host_id: str,
        supervisor: ProcessIdentity,
    ) -> bool:
        """Durably claim a reserved generation before its supervisor may launch."""
        now = time.time()
        with self.database.transaction() as connection:
            # Claim and metadata creation are one durable gate. A cancelled or
            # reclaimed generation cannot get past this transaction and create
            # Xvfb/browser side effects afterwards.
            cursor = connection.execute(
                "UPDATE browser_runs SET status = 'starting', pid = ?, start_ticks = ? "
                "WHERE run_id = ? AND profile_id = ? AND host_id = ? "
                "AND status = 'reserved' AND pid IS NULL "
                "AND EXISTS (SELECT 1 FROM capacity_recovery "
                "WHERE singleton = 1 AND blocked = 0)",
                (supervisor.pid, supervisor.start_ticks, ref.run_id, ref.profile_id, host_id),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO runtime_descriptors "
                "(run_id, profile_id, host_id, supervisor_pid, supervisor_start_ticks, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ref.run_id, ref.profile_id, host_id, supervisor.pid, supervisor.start_ticks, now),
            )
        return True

    def publish_boundary(
        self,
        ref: BrowserRunRefDTO,
        supervisor: ProcessIdentity,
        boundary: ProcessIdentity,
    ) -> bool:
        return self._update(
            ref,
            supervisor,
            "boundary_pid = ?, boundary_start_ticks = ?, updated_at = ?",
            (boundary.pid, boundary.start_ticks, time.time()),
        )

    def publish_browser(
        self,
        ref: BrowserRunRefDTO,
        supervisor: ProcessIdentity,
        browser: ProcessIdentity,
        *,
        display: str | None = None,
        namespace: str | None = None,
        tmp_path: str | None = None,
        xauthority_path: str | None = None,
    ) -> bool:
        return self._update(
            ref,
            supervisor,
            "browser_pid = ?, browser_start_ticks = ?, display = ?, namespace = ?, "
            "tmp_path = ?, xauthority_path = ?, updated_at = ?",
            (
                browser.pid,
                browser.start_ticks,
                display,
                namespace,
                tmp_path,
                xauthority_path,
                time.time(),
            ),
        )

    def publish_runtime_details(
        self,
        ref: BrowserRunRefDTO,
        supervisor: ProcessIdentity,
        *,
        display: str | None,
        namespace: str | None,
        tmp_path: str,
        xauthority_path: str | None,
    ) -> bool:
        """Persist only runner-reported, supervisor-validated display context."""
        return self._update(
            ref,
            supervisor,
            "display = ?, namespace = ?, tmp_path = ?, xauthority_path = ?, updated_at = ?",
            (display, namespace, tmp_path, xauthority_path, time.time()),
        )

    def publish_control(
        self,
        ref: BrowserRunRefDTO,
        supervisor: ProcessIdentity,
        port: int,
        token: str,
    ) -> bool:
        if type(port) is not int or not 1 <= port <= 65535 or not token:
            return False
        return self._update(
            ref,
            supervisor,
            "control_port = ?, control_token = ?, updated_at = ?",
            (port, token, time.time()),
        )

    def get(self, ref: BrowserRunRefDTO) -> RuntimeRecord | None:
        run = self.ledger.get(ref)
        if run is None or run.status not in {
            RunStatus.STARTING,
            RunStatus.RUNNING,
            RunStatus.STOPPING,
        }:
            return None
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_descriptors WHERE run_id = ? AND profile_id = ?",
                (ref.run_id, ref.profile_id),
            ).fetchone()
        if row is None or row["host_id"] != run.host_id:
            return None
        browser = (
            ProcessIdentity(row["browser_pid"], row["browser_start_ticks"])
            if row["browser_pid"] is not None
            else None
        )
        descriptor = RuntimeDescriptor(
            ref,
            row["host_id"],
            ProcessIdentity(row["supervisor_pid"], row["supervisor_start_ticks"]),
            browser,
            row["display"],
            row["namespace"],
            row["tmp_path"],
            row["xauthority_path"],
            row["control_port"],
            row["control_token"],
            ProcessIdentity(row["boundary_pid"], row["boundary_start_ticks"])
            if row["boundary_pid"] is not None
            else None,
        )
        return RuntimeRecord(descriptor, row["updated_at"])

    def _update(
        self,
        ref: BrowserRunRefDTO,
        supervisor: ProcessIdentity,
        assignments: str,
        values: tuple[object, ...],
    ) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE runtime_descriptors SET {assignments} WHERE run_id = ? "
                "AND profile_id = ? AND supervisor_pid = ? AND supervisor_start_ticks = ? "
                "AND EXISTS (SELECT 1 FROM browser_runs WHERE browser_runs.run_id = runtime_descriptors.run_id "
                "AND browser_runs.profile_id = runtime_descriptors.profile_id "
                "AND browser_runs.status IN ('starting', 'running', 'stopping'))",
                (*values, ref.run_id, ref.profile_id, supervisor.pid, supervisor.start_ticks),
            )
            return cursor.rowcount == 1
