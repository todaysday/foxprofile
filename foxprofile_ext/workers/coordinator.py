"""Route authorized runs through the existing local runtime or their worker."""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import re
import threading
import time
from dataclasses import asdict

from ..capacity import CapacityService
from ..dto import BrowserRunRefDTO, ProcessIdentity, ProfileRefDTO, RuntimeDescriptor
from ..errors import DomainError, ErrorCode
from ..locking import catalog_lock
from ..policy import require_profile_access
from ..run_contracts import RunStatusResult
from ..runtime.coordinator import RuntimeCoordinator
from ..storage.capacity import RunStatus

logger = logging.getLogger(__name__)


class PlacementCapacity(CapacityService):
    """Shared admission for browser launches and local stopped-profile helpers."""

    def __init__(self, ledger, assignments, scheduler, profile_by_id, *, allow_remote=True):
        super().__init__(ledger, assignments)
        self.scheduler = scheduler
        self.profile_by_id = profile_by_id
        self.allow_remote = allow_remote

    def reserve(self, principal, profiles, host_id):
        refs = tuple(profiles)
        for ref in refs:
            require_profile_access(principal, ref, self.assignments.is_assigned)
        return self.scheduler.reserve(
            tuple(self.profile_by_id(ref.profile_id) for ref in refs),
            host_id,
            allow_remote=self.allow_remote,
        )


class _LaunchWaiter:
    """Keep BrowserLauncher readiness semantics for both execution locations."""

    def __init__(self, local, ledger):
        self.local = local
        self.ledger = ledger

    def __getattr__(self, name):
        return getattr(self.local, name)

    def wait_for_launch(self, name, timeout, run_id=None):
        run = next((r for r in self.ledger.active_runs() if r.ref.run_id == run_id), None)
        if run is None or run.worker_id is None:
            return self.local.wait_for_launch(name, timeout, run_id)
        deadline = time.monotonic() + timeout
        while True:
            current = self.ledger.get(run.ref)
            if current is None or current.status in {RunStatus.CLOSED, RunStatus.CANCELLED}:
                return False, "LAUNCH_FAILED"
            if current.status is RunStatus.RUNNING:
                return True, None
            if current.status is RunStatus.UNCERTAIN:
                return False, "RUN_UNCERTAIN"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "timeout"
            time.sleep(min(0.05, remaining))


class DistributedCoordinator(RuntimeCoordinator):
    """The Master owns admission, final data commits and every public action."""

    def __init__(self, local, capacity, registry, hub, profiles, public_url):
        super().__init__(
            capacity,
            local.ledger,
            local.runtime,
            _LaunchWaiter(local.launcher, local.ledger),
            profile_by_id=local.profile_by_id,
            authorize=local.authorize,
            lock_path=local.lock_path,
        )
        self.workers = registry
        self.hub = hub
        self.profiles = profiles
        self.public_url = public_url.rstrip("/")
        from .manual import ManualRelay

        self.manual_relay = ManualRelay(self)
        self._descriptors = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()
        self._finishing: set[str] = set()
        self._cleaning: set[str] = set()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="worker-run"
        )
        self._closed = False
        hub.on_disconnect = self._disconnected
        hub.on_event = self._event
        with self.ledger.database.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS worker_stop_proofs "
                "(run_id TEXT PRIMARY KEY, archive_digest TEXT NOT NULL, confirmed_at REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS worker_cleanup_proofs "
                "(run_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, worker_boot_id TEXT NOT NULL, "
                "connection_generation INTEGER NOT NULL, confirmed_at REAL NOT NULL)"
            )
        profiles.recover()
        self._recover_saved_runs()
        with self.ledger.database.read() as db:
            finished = tuple(
                db.execute(
                    "SELECT s.run_id FROM profile_sync_snapshots s JOIN browser_runs r ON r.run_id = s.run_id "
                    "WHERE r.status IN ('closed', 'cancelled')"
                )
            )
        for row in finished:
            self._cleanup_snapshot(row["run_id"])

    def _recover_saved_runs(self):
        # A crash between the durable receipt and ledger release must not lose
        # the earlier exact stop proof or require a live worker to save again.
        with self.ledger.database.read() as db:
            receipts = tuple(
                db.execute(
                    "SELECT c.profile_id, c.run_id, c.worker_id, c.worker_boot_id, c.connection_generation "
                    "FROM profile_sync_commits c JOIN worker_stop_proofs p ON p.run_id = c.run_id "
                    "WHERE c.status = 'committed' AND p.archive_digest = c.archive_digest"
                )
            )
        for receipt in receipts:
            run = self.ledger.get(BrowserRunRefDTO(receipt["profile_id"], receipt["run_id"]))
            if run and self._owner(run)[1:] == (
                receipt["worker_id"],
                receipt["worker_boot_id"],
                receipt["connection_generation"],
            ):
                self.ledger.complete_remote(
                    *self._owner(run), process_stopped=True, sync_verified=True
                )

    def _run_lock(self, run_id):
        with self._guard:
            return self._locks.setdefault(run_id, threading.RLock())

    @staticmethod
    def _owner(run):
        return (run.ref, run.worker_id, run.worker_boot_id, run.connection_generation)

    def _phase(self, run, expected, next_state):
        return self.ledger.transition_remote(*self._owner(run), expected, next_state)

    def _uncertain(self, run):
        self.ledger.mark_remote_uncertain(*self._owner(run))

    def remote_helpers_stopped(self, run):
        """Only exact-run cleanup or an explicit host fence proves helper death."""
        with self.ledger.database.read() as db:
            return (
                db.execute(
                    "SELECT 1 FROM worker_cleanup_proofs WHERE run_id = ? AND worker_id = ? "
                    "AND worker_boot_id = ? AND connection_generation = ?",
                    (run.ref.run_id, run.worker_id, run.worker_boot_id, run.connection_generation),
                ).fetchone()
                is not None
            )

    @staticmethod
    def _record_cleanup_proof(db, run):
        db.execute(
            "INSERT OR IGNORE INTO worker_cleanup_proofs VALUES (?, ?, ?, ?, ?)",
            (
                run.ref.run_id,
                run.worker_id,
                run.worker_boot_id,
                run.connection_generation,
                time.time(),
            ),
        )

    def _discard_unstarted(self, run):
        self._uncertain(run)
        closed = self.ledger.complete_remote(
            *self._owner(run),
            process_stopped=True,
            quarantined=True,
            no_process_created=True,
        )
        if closed:
            self._cleanup_snapshot(run.ref.run_id)
        return closed

    def _cleanup_snapshot(self, run_id):
        try:
            self.profiles.cleanup_run(run_id)
        except Exception:
            # Keep a successful save successful; startup retries retained
            # finished snapshot records after an interrupted disk cleanup.
            logger.warning("Worker snapshot cleanup is incomplete")

    def _prepare_and_start(self, principal, profile, ref, expected_name):
        run = self.ledger.get(ref)
        if run is None:
            return
        if run.worker_id is None:
            return super()._prepare_and_start(principal, profile, ref, expected_name)
        with self._guard:
            if self._closed:
                self._discard_unstarted(run)
                return
            self._executor.submit(self._start_remote, principal, profile, run, expected_name)

    def _start_remote(self, principal, profile, run, expected_name):
        dispatched = False
        with self._run_lock(run.ref.run_id):
            try:
                if not self._phase(run, RunStatus.RESERVED, RunStatus.TRANSFERRING):
                    return
                with catalog_lock(self.lock_path):
                    current, active = self._current_run(
                        principal, ProfileRefDTO(profile.profile_id, profile.name)
                    )
                    if current.name != expected_name or active is None or active.ref != run.ref:
                        raise DomainError(ErrorCode.STALE_RUN)
                    profile = self.profile_by_id(current.profile_id)
                    snapshot = self.profiles.capture(profile, self.ledger.get(run.ref))
                    profile_settings = profile.to_dict()
                self.authorize(principal, current)
                run = self.ledger.get(run.ref)
                grant = self.profiles.issue_grant(run, "download", snapshot=snapshot)
                if not self._phase(run, RunStatus.TRANSFERRING, RunStatus.STARTING):
                    return
                payload = {
                    "profile": profile_settings,
                    "memory_budget_bytes": run.reserved_memory_bytes,
                    "snapshot": {
                        "url": f"{self.public_url}/api/v1/workers/runs/{run.ref.run_id}/snapshot",
                        "token": grant.token,
                        "archive_digest": snapshot.archive_digest,
                        "base_revision": snapshot.base_revision,
                        "size_bytes": snapshot.size_bytes,
                        "identity_digest": snapshot.identity_digest,
                    },
                }
                # A sent start may succeed after a timeout: never free its claim.
                dispatched = True
                result = self.hub.command(run, "start", payload, timeout=120)
                if isinstance(result, dict) and result.get("state") == "running":
                    self._phase(run, RunStatus.STARTING, RunStatus.RUNNING)
                elif not isinstance(result, dict) or result.get("state") != "starting":
                    self._uncertain(run)
            except Exception:
                if dispatched:
                    self._uncertain(run)
                else:
                    # No start command left this process; staging has no browser.
                    self._discard_unstarted(run)

    def status(self, principal, profile):
        current, run = self._current_run(principal, profile)
        if run is None:
            return None
        return RunStatusResult(
            current.profile_id,
            current.name,
            run.ref.run_id,
            run.status.value,
            execution_location="remote" if run.worker_id else "local",
        )

    def control(self, principal, profile, action, params=None):
        current, run = self._current_run(principal, profile)
        if run is None:
            raise DomainError(ErrorCode.STALE_BROWSER_RUN)
        if run.worker_id is None:
            return super().control(principal, current, action, params)
        with self._run_lock(run.ref.run_id):
            current, latest = self._current_run(principal, current)
            if latest is None or latest.ref != run.ref or latest.status is not RunStatus.RUNNING:
                raise DomainError(ErrorCode.STALE_RUN)
            result = self.hub.command(
                run, "control", {"action": action, "params": params or {}}, timeout=60
            )
            self.authorize(principal, current)
            latest = self.ledger.get(run.ref)
            if latest is None or latest.status is not RunStatus.RUNNING:
                raise DomainError(ErrorCode.STALE_RUN)
            return result

    def stop(self, principal, profile, *, timeout=30.0):
        current, run = self._current_run(principal, profile)
        if run is None:
            return False
        if run.worker_id is None:
            stopped = super().stop(principal, current, timeout=timeout)
            if stopped:
                self._record_local_stop(run)
            return stopped
        with self._run_lock(run.ref.run_id):
            self.authorize(principal, current)
            return self._stop_remote(run, timeout)

    def _stop_remote(self, run, timeout=30.0):
        run = self.ledger.get(run.ref)
        if run is None or run.status in {RunStatus.CANCELLED, RunStatus.CLOSED}:
            return True
        if run.status in {RunStatus.RESERVED, RunStatus.TRANSFERRING}:
            return self._discard_unstarted(run)
        if run.status is RunStatus.UNCERTAIN:
            # A lost result need not mean a lost connection. An explicit Stop
            # may still terminate this exact assignment; ordinary control and
            # replacement admission remain disabled until death is proved.
            try:
                self.hub.connection_for(run)
            except DomainError:
                raise DomainError(ErrorCode.RUN_UNCERTAIN) from None
        if run.status is not RunStatus.SYNCING:
            self._phase(run, run.status, RunStatus.STOPPING)
            try:
                result = self.hub.command(
                    run, "stop", {"timeout": timeout}, timeout=max(1, timeout) + 5
                )
            except DomainError:
                self._uncertain(run)
                raise
            if not isinstance(result, dict) or result.get("stopped") is not True:
                self._uncertain(run)
                raise DomainError(ErrorCode.RUN_UNCERTAIN)
            self.accept_stop_proof(run, result.get("proof"))
        run = self.ledger.get(run.ref)
        if run.status is RunStatus.CLOSED:
            return True
        with self.ledger.database.read() as db:
            proof = db.execute(
                "SELECT archive_digest FROM worker_stop_proofs WHERE run_id = ?", (run.ref.run_id,)
            ).fetchone()
        if proof is None:
            raise DomainError(ErrorCode.RUN_UNCERTAIN)
        grant = self.profiles.issue_grant(
            run,
            "upload",
            base_revision=run.base_revision,
            archive_digest=proof["archive_digest"],
        )
        try:
            self.hub.command(
                run,
                "sync",
                {
                    "upload": {
                        "url": f"{self.public_url}/api/v1/workers/runs/{run.ref.run_id}/upload",
                        "token": grant.token,
                        "base_revision": run.base_revision,
                        "archive_digest": proof["archive_digest"],
                    }
                },
                timeout=120,
            )
        except DomainError:
            # A lost acknowledgment may hide a successful, durable commit.
            if self.ledger.get(run.ref).status is not RunStatus.CLOSED:
                raise DomainError(ErrorCode.SYNC_FAILED) from None
        return self.ledger.get(run.ref).status is RunStatus.CLOSED

    def accept_stop_proof(self, run, proof, connection=None):
        if (
            not isinstance(proof, dict)
            or proof.get("terminated") is not True
            or proof.get("reason") not in {"clean", "loss"}
            or any(
                proof.get(key) != expected
                for key, expected in (
                    ("profile_id", run.ref.profile_id),
                    ("run_id", run.ref.run_id),
                    ("worker_id", run.worker_id),
                    ("worker_boot_id", run.worker_boot_id),
                    ("connection_generation", run.connection_generation),
                )
            )
        ):
            raise DomainError(ErrorCode.STALE_RUN)
        if connection is not None and (
            connection.worker_id != run.worker_id or connection.boot_id != run.worker_boot_id
        ):
            raise DomainError(ErrorCode.STALE_RUN)
        if proof.get("manual_helpers_stopped") is True:
            with self.ledger.database.transaction() as db:
                self._record_cleanup_proof(db, run)
        latest = self.ledger.get(run.ref)
        if latest.status in {RunStatus.CLOSED, RunStatus.CANCELLED}:
            return
        clean_current_run = proof.get("reason") == "clean"
        if clean_current_run:
            try:
                self.hub.connection_for(run)
            except DomainError:
                clean_current_run = False
        if not clean_current_run:
            self._uncertain(run)
            closed = self.ledger.complete_remote(
                *self._owner(run),
                process_stopped=True,
                quarantined=True,
                no_process_created=proof.get("shutdown_kind") == "pre-spawn"
                and proof.get("process_tree_dead") is True,
            )
            if closed:
                self._cleanup_snapshot(run.ref.run_id)
        else:
            digest = proof.get("archive_digest", "")
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise DomainError(ErrorCode.TRANSFER_INVALID)
            with self.ledger.database.transaction() as db:
                prior = db.execute(
                    "SELECT archive_digest FROM worker_stop_proofs WHERE run_id = ?",
                    (run.ref.run_id,),
                ).fetchone()
                if prior is not None and prior["archive_digest"] != digest:
                    raise DomainError(ErrorCode.TRANSFER_CONFLICT)
                db.execute(
                    "INSERT OR IGNORE INTO worker_stop_proofs VALUES (?, ?, ?)",
                    (run.ref.run_id, digest, time.time()),
                )
            if latest.status is not RunStatus.SYNCING:
                if latest.status in {RunStatus.STARTING, RunStatus.RUNNING}:
                    self._phase(run, latest.status, RunStatus.STOPPING)
                    latest = self.ledger.get(run.ref)
                self._phase(run, latest.status, RunStatus.SYNCING)

    def commit_upload(self, run, archive, digest):
        # The stop caller is waiting on the worker's HTTPS upload while holding
        # its operation lock. The durable syncing phase owns exclusivity here.
        with catalog_lock(self.lock_path):
            profile = self.profile_by_id(run.ref.profile_id)
            receipt = self.profiles.commit(profile, run, archive, run.base_revision, digest)
            if (
                not self.ledger.complete_remote(
                    *self._owner(run),
                    process_stopped=True,
                    sync_verified=True,
                )
                and self.ledger.get(run.ref).status is not RunStatus.CLOSED
            ):
                raise DomainError(ErrorCode.RUN_UNCERTAIN)
            self._cleanup_snapshot(run.ref.run_id)
            return asdict(receipt)

    def verified_remote_descriptor(self, run):
        if run.worker_id is None or run.status is not RunStatus.RUNNING:
            return None
        self.hub.connection_for(run)
        if run.ref.run_id in self._descriptors:
            return self._descriptors[run.ref.run_id]
        data = self.hub.command(run, "describe", timeout=5)
        if not isinstance(data, dict):
            raise DomainError(ErrorCode.STALE_RUN)
        data = data.get("descriptor", data)
        if data.get("run") != asdict(run.ref):
            raise DomainError(ErrorCode.STALE_RUN)
        # PIDs are private worker-local proof. Master never signals them.
        descriptor = RuntimeDescriptor(
            run.ref,
            run.host_id,
            ProcessIdentity(**data["supervisor"]),
            browser=ProcessIdentity(**data["browser"]),
            display=data.get("display"),
            namespace=data.get("namespace"),
            tmp_path=data.get("tmp_path"),
            xauthority_path=data.get("xauthority_path"),
            boundary=ProcessIdentity(**data["boundary"]) if data.get("boundary") else None,
        )
        self._descriptors[run.ref.run_id] = descriptor
        return descriptor

    def _disconnected(self, connection):
        for run in self.ledger.active_runs():
            if (run.worker_id, run.worker_boot_id, run.connection_generation) == (
                connection.worker_id,
                connection.boot_id,
                connection.generation,
            ):
                self._uncertain(run)

    def _event(self, connection, message):
        proofs = message.get("stopped_runs", [])
        if message.get("type") == "stopped":
            proofs = [message.get("proof")]
        if message.get("type") == "event" and message.get("event") == "running":
            ref = BrowserRunRefDTO(message["profile_id"], message["run_id"])
            run = self.ledger.get(ref)
            if run and (run.worker_id, run.worker_boot_id, run.connection_generation) == (
                connection.worker_id,
                connection.boot_id,
                connection.generation,
            ):
                self._phase(run, RunStatus.STARTING, RunStatus.RUNNING)
        if not isinstance(proofs, list) or len(proofs) > 10000:
            return
        for proof in proofs:
            if not isinstance(proof, dict):
                continue
            try:
                run = self.ledger.get(BrowserRunRefDTO(proof["profile_id"], proof["run_id"]))
                if run is None:
                    continue
                self.accept_stop_proof(run, proof, connection)
                state = self.ledger.get(run.ref).status
                if state is RunStatus.CLOSED:
                    with self._guard:
                        if not self._closed and run.ref.run_id not in self._cleaning:
                            self._cleaning.add(run.ref.run_id)
                            self._executor.submit(self._cleanup_worker, run, connection, proof)
                    continue
                if state is not RunStatus.SYNCING:
                    continue
                with self._guard:
                    if not self._closed and run.ref.run_id not in self._finishing:
                        self._finishing.add(run.ref.run_id)
                        self._executor.submit(self._finish_event, run)
            except (DomainError, KeyError, ValueError):
                continue

    def _cleanup_worker(self, run, connection, proof):
        try:
            with self._run_lock(run.ref.run_id):
                if self.ledger.get(run.ref).status is RunStatus.CLOSED:
                    self.hub.cleanup_closed_run(run, connection, proof)
        except DomainError:
            # The next authenticated proof inventory retries a lost terminal ACK.
            pass
        finally:
            with self._guard:
                self._cleaning.discard(run.ref.run_id)

    def _finish_event(self, run):
        try:
            with self._run_lock(run.ref.run_id):
                self._stop_remote(run)
        except DomainError:
            pass
        finally:
            with self._guard:
                self._finishing.discard(run.ref.run_id)

    def _record_local_stop(self, run):
        with catalog_lock(self.lock_path):
            if any(r.ref.profile_id == run.ref.profile_id for r in self.ledger.active_runs()):
                return
            self.profiles.record_local_stop(self.profile_by_id(run.ref.profile_id), run)

    def fence(self, run, evidence: str):
        """Called only after an admin attests verified out-of-band host fencing."""
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2048:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with self._run_lock(run.ref.run_id):
            if run.worker_id is None or self.ledger.get(run.ref).status not in {
                RunStatus.UNCERTAIN,
                RunStatus.CLOSED,
            }:
                raise DomainError(ErrorCode.STALE_RUN)
            with self.ledger.database.transaction() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS worker_fence_evidence "
                    "(run_id TEXT PRIMARY KEY, evidence TEXT NOT NULL, created_at REAL NOT NULL)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO worker_fence_evidence VALUES (?, ?, ?)",
                    (run.ref.run_id, evidence, time.time()),
                )
                self._record_cleanup_proof(db, run)
            closed = (
                self.ledger.complete_remote(
                    *self._owner(run),
                    process_stopped=True,
                    quarantined=True,
                )
                or self.ledger.get(run.ref).status is RunStatus.CLOSED
            )
            if closed:
                self._cleanup_snapshot(run.ref.run_id)
            return closed

    def shutdown(self, *, timeout=30.0):
        with self._guard:
            self._closed = True
        for run in self.ledger.active_runs():
            if run.worker_id:
                with contextlib.suppress(DomainError), self._run_lock(run.ref.run_id):
                    self._stop_remote(run, min(timeout, 10))
        self.hub.shutdown()
        self._executor.shutdown(wait=False, cancel_futures=True)
        super().shutdown(timeout=timeout)
