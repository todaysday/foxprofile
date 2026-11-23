"""Independent Worker lease watchdog and bounded process-tree cleanup."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..dto import BrowserRunRefDTO, ProcessIdentity
from ..runtime.processes import _runtime_liveness, _signal_group
from ..runtime.registry import current_host_id, probe_process, process_identity
from ..storage.capacity import CapacityLedger, ProcessLiveness, RunStatus
from .execution import WorkerExecutionDatabase

_MAX_CONTROL_LINE = 65536
_MAX_LOSS_TIMEOUT = 30.0
_MAX_TERM_GRACE = 5.0
_MAX_KILL_GRACE = 5.0
_MAX_CLEANUP_TIME = 60.0
_MAX_MANUAL_CLEANUP_TIME = 20.0


class WorkerWatchdog:
    """Own a separate interpreter which cannot be blocked by the agent loop."""

    def __init__(
        self,
        database: WorkerExecutionDatabase,
        *,
        loss_timeout: float = 30.0,
        term_grace: float = 2.0,
        kill_grace: float = 2.0,
        parent_pid: int | None = None,
        code_root: str | Path | None = None,
    ) -> None:
        if (
            type(loss_timeout) not in (int, float)
            or not 0 < loss_timeout <= _MAX_LOSS_TIMEOUT
            or type(term_grace) not in (int, float)
            or not 0 < term_grace <= _MAX_TERM_GRACE
            or type(kill_grace) not in (int, float)
            or not 0 < kill_grace <= _MAX_KILL_GRACE
        ):
            raise ValueError("watchdog time budgets must be positive and bounded")
        self.database = database
        self.loss_timeout = float(loss_timeout)
        self.term_grace = float(term_grace)
        self.kill_grace = float(kill_grace)
        self.parent_pid = parent_pid
        self.code_root = Path(code_root or Path(__file__).resolve().parents[2]).resolve()
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        config = {
            "database_path": str(self.database.path),
            "host_id": current_host_id(),
            "loss_timeout": self.loss_timeout,
            "term_grace": self.term_grace,
            "kill_grace": self.kill_grace,
            "parent_pid": self.parent_pid,
        }
        env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(self.code_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
            if name in os.environ:
                env[name] = os.environ[name]
        self._process = subprocess.Popen(
            [sys.executable, "-m", "foxprofile_ext.workers.watchdog", "--watchdog"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            cwd=self.code_root,
            env=env,
            start_new_session=True,
        )
        assert self._process.stdin is not None
        self._send(
            {
                "op": "configure",
                **config,
            }
        )

    def accept_generation(self, generation: int, loss_timeout: float | None = None) -> None:
        if type(generation) is not int or generation < 1:
            raise ValueError("connection generation must be positive")
        message: dict[str, object] = {"op": "welcome", "generation": generation}
        if loss_timeout is not None:
            if type(loss_timeout) not in (int, float) or not 0 < loss_timeout <= _MAX_LOSS_TIMEOUT:
                raise ValueError("watchdog loss timeout is invalid")
            message["loss_timeout"] = loss_timeout
        self._send(message)

    def renew(self, generation: int) -> None:
        self._send_generation("renew", generation)

    def close(self, timeout: float = 65.0) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            with contextlib.suppress(RuntimeError):
                self._send({"op": "stop"})
            try:
                process.wait(timeout=max(0.1, min(timeout, 70)))
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        self._process = None

    def _send_generation(self, operation: str, generation: int) -> None:
        if type(generation) is not int or generation < 1:
            raise ValueError("connection generation must be positive")
        self._send({"op": operation, "generation": generation})

    def _send(self, message: dict[str, object]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("Worker watchdog is not running")
        data = json.dumps(message, separators=(",", ":"))
        if len(data.encode()) > _MAX_CONTROL_LINE:
            raise ValueError("watchdog control message is too large")
        try:
            process.stdin.write(data + "\n")
            process.stdin.flush()
        except OSError as error:
            raise RuntimeError("Worker watchdog is unavailable") from error


def _notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    import socket

    if address.startswith("@"):
        address = "\0" + address[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(address)
        sock.sendall(message.encode("ascii", "strict"))
    except OSError:
        pass
    finally:
        sock.close()


def _tree_liveness(
    database: WorkerExecutionDatabase,
    host_id: str,
    ref: BrowserRunRefDTO,
    supervisor: ProcessIdentity,
) -> ProcessLiveness:
    supervisor_state = probe_process(host_id, supervisor)
    with database.read() as connection:
        row = connection.execute(
            "SELECT * FROM runtime_descriptors WHERE run_id = ? AND profile_id = ?",
            (ref.run_id, ref.profile_id),
        ).fetchone()
    if row is None:
        return ProcessLiveness.UNKNOWN
    boundary = (
        ProcessIdentity(row["boundary_pid"], row["boundary_start_ticks"])
        if row["boundary_pid"] is not None
        else None
    )
    namespace = row["namespace"]
    if boundary is None:
        return ProcessLiveness.UNKNOWN
    boundary_state = _runtime_liveness(boundary, namespace)
    if ProcessLiveness.LIVE in (supervisor_state, boundary_state):
        return ProcessLiveness.LIVE
    if ProcessLiveness.UNKNOWN in (supervisor_state, boundary_state):
        return ProcessLiveness.UNKNOWN
    return ProcessLiveness.DEAD


def _signal_exact(identity: ProcessIdentity | None, signum: int) -> bool:
    if identity is None or process_identity(identity.pid) != identity:
        return False
    try:
        os.kill(identity.pid, signum)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def _run_cleanup(
    database: WorkerExecutionDatabase,
    host_id: str,
    *,
    term_grace: float,
    kill_grace: float,
    reason: str,
) -> None:
    """Stop every exact owned tree; unknown process state never becomes proof."""
    cleanup_deadline = time.monotonic() + _MAX_CLEANUP_TIME
    manual_deadline = min(cleanup_deadline, time.monotonic() + _MAX_MANUAL_CLEANUP_TIME)
    ledger = CapacityLedger(database)
    rows = ledger.active_runs()
    active: dict[str, tuple[object, ProcessIdentity, ProcessIdentity | None, str | None]] = {}
    manual_stopped: dict[str, bool] = {}
    for run in rows:
        context = database.run_context(run.ref.run_id) or {}
        manual_stopped[run.ref.run_id] = _stop_manual_helpers(
            database,
            context,
            host_id,
            term_grace=term_grace,
            kill_grace=kill_grace,
            deadline=manual_deadline,
        )
        if run.process is None:
            if run.status is RunStatus.RESERVED and ledger.cancel_unclaimed(run.ref):
                proof = _loss_proof(context, run.ref, reason, manual_stopped[run.ref.run_id])
                if proof is not None:
                    database.record_stop_proof(run.ref.run_id, proof)
            continue
        with database.read() as connection:
            descriptor = connection.execute(
                "SELECT * FROM runtime_descriptors WHERE run_id = ? AND profile_id = ?",
                (run.ref.run_id, run.ref.profile_id),
            ).fetchone()
        if descriptor is None:
            continue
        supervisor = ProcessIdentity(
            descriptor["supervisor_pid"], descriptor["supervisor_start_ticks"]
        )
        boundary = (
            ProcessIdentity(descriptor["boundary_pid"], descriptor["boundary_start_ticks"])
            if descriptor["boundary_pid"] is not None
            else None
        )
        active[run.ref.run_id] = (run, supervisor, boundary, descriptor["namespace"])
        _signal_exact(supervisor, signal.SIGTERM)
        if boundary is not None:
            _signal_group(boundary, signal.SIGTERM)

    term_deadline = min(time.monotonic() + term_grace, cleanup_deadline)
    while active and time.monotonic() < term_deadline:
        for run_id, (run, supervisor, _boundary, _namespace) in tuple(active.items()):
            if _tree_liveness(database, host_id, run.ref, supervisor) is ProcessLiveness.DEAD:
                _record_dead_proof(
                    database, ledger, run, supervisor, reason, manual_stopped[run_id]
                )
                active.pop(run_id, None)
        if active:
            time.sleep(min(0.05, max(0.0, term_deadline - time.monotonic())))

    for _run_id, (_run, supervisor, boundary, _namespace) in active.items():
        _signal_exact(supervisor, signal.SIGKILL)
        if boundary is not None:
            _signal_group(boundary, signal.SIGKILL)

    kill_deadline = cleanup_deadline
    while active and time.monotonic() < kill_deadline:
        for run_id, (run, supervisor, _boundary, _namespace) in tuple(active.items()):
            if _tree_liveness(database, host_id, run.ref, supervisor) is ProcessLiveness.DEAD:
                _record_dead_proof(
                    database, ledger, run, supervisor, reason, manual_stopped[run_id]
                )
                active.pop(run_id, None)
        if active:
            time.sleep(min(0.05, max(0.0, kill_deadline - time.monotonic())))


def _loss_proof(
    context: dict[str, object],
    ref: BrowserRunRefDTO,
    reason: str,
    manual_helpers_stopped: bool,
) -> dict[str, object] | None:
    required = ("worker_id", "worker_boot_id", "connection_generation")
    if any(key not in context for key in required):
        return None
    return {
        "profile_id": ref.profile_id,
        "run_id": ref.run_id,
        "worker_id": context["worker_id"],
        "worker_boot_id": context["worker_boot_id"],
        "connection_generation": context["connection_generation"],
        "terminated": True,
        "process_tree_dead": True,
        "manual_helpers_stopped": manual_helpers_stopped,
        "reason": reason,
        "shutdown_kind": "watchdog",
    }


def _record_dead_proof(database, ledger, run, supervisor, reason, manual_helpers_stopped):
    context = database.run_context(run.ref.run_id) or {}
    proof = _loss_proof(context, run.ref, reason, manual_helpers_stopped)
    if proof is None:
        return
    if ledger.release_after_exit(
        run.ref,
        supervisor,
        lambda _host, _process: ProcessLiveness.DEAD,
    ):
        database.record_stop_proof(run.ref.run_id, proof)


def _stop_manual_helpers(
    database: WorkerExecutionDatabase,
    context: dict[str, object],
    host_id: str,
    *,
    term_grace: float,
    kill_grace: float,
    deadline: float,
) -> bool:
    sessions = context.get("manual_sessions", {})
    if not isinstance(sessions, dict) or not sessions:
        return True
    active = {}
    for session_id, stored in sessions.items():
        if not isinstance(session_id, str) or not isinstance(stored, dict):
            return False
        try:
            identity = ProcessIdentity(stored["pid"], stored["start_ticks"])
        except (KeyError, TypeError, ValueError):
            return False
        receipt = _manual_receipt(database, session_id, stored, host_id, identity)
        if receipt is None:
            return False
        if (
            receipt.get("phase") == "dead"
            and probe_process(host_id, identity) is ProcessLiveness.DEAD
        ):
            continue
        _signal_exact(identity, signal.SIGTERM)
        active[session_id] = (identity, receipt["path"])

    term_deadline = min(time.monotonic() + term_grace, deadline)
    while active and time.monotonic() < term_deadline:
        for session_id, (identity, receipt_path) in tuple(active.items()):
            receipt = _read_manual_receipt(receipt_path)
            if (
                receipt is not None
                and receipt.get("phase") == "dead"
                and probe_process(host_id, identity) is ProcessLiveness.DEAD
            ):
                active.pop(session_id, None)
        if active:
            time.sleep(min(0.05, max(0.0, term_deadline - time.monotonic())))

    for identity, _receipt_path in active.values():
        _signal_exact(identity, signal.SIGKILL)
    kill_deadline = min(time.monotonic() + kill_grace, deadline)
    while active and time.monotonic() < kill_deadline:
        for session_id, (identity, receipt_path) in tuple(active.items()):
            receipt = _read_manual_receipt(receipt_path)
            if (
                receipt is not None
                and receipt.get("phase") == "dead"
                and probe_process(host_id, identity) is ProcessLiveness.DEAD
            ):
                active.pop(session_id, None)
        if active:
            time.sleep(min(0.05, max(0.0, kill_deadline - time.monotonic())))
    return not active


def _manual_receipt(database, session_id, stored, host_id, identity):
    expected = (
        database.path.parent / "manual" / f"{hashlib.sha256(session_id.encode()).hexdigest()}.json"
    )
    try:
        candidate = Path(stored["receipt_path"])
        if candidate != expected or candidate.is_symlink() or candidate.stat().st_size > 65536:
            return None
        receipt = _read_manual_receipt(candidate)
    except (OSError, TypeError, KeyError):
        return None
    if (
        receipt is None
        or receipt.get("host_id") != host_id
        or receipt.get("supervisor") != [identity.pid, identity.start_ticks]
    ):
        return None
    receipt["path"] = expected
    return receipt


def _read_manual_receipt(path: Path):
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _read_control_line(stream) -> dict[str, object] | None:
    raw = stream.readline(_MAX_CONTROL_LINE + 1)
    if not raw:
        return None
    if len(raw.encode()) > _MAX_CONTROL_LINE or not raw.endswith("\n"):
        raise ValueError("watchdog control line exceeds limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("watchdog control must be an object")
    return value


def _watchdog_loop(stream) -> int:
    first = _read_control_line(stream)
    if first is None or first.get("op") != "configure":
        return 2
    try:
        database = WorkerExecutionDatabase(first["database_path"])
        host_id = str(first["host_id"])
        timeout = float(first["loss_timeout"])
        term_grace = float(first["term_grace"])
        kill_grace = float(first["kill_grace"])
        parent_pid = first.get("parent_pid")
        parent_identity = process_identity(parent_pid) if type(parent_pid) is int else None
        if (
            not host_id
            or not (0 < timeout <= _MAX_LOSS_TIMEOUT)
            or not (0 < term_grace <= _MAX_TERM_GRACE)
            or not (0 < kill_grace <= _MAX_KILL_GRACE)
        ):
            return 2
    except (KeyError, TypeError, ValueError, OSError):
        return 2

    shutdown_requested = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: shutdown_requested.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: shutdown_requested.set())
    generation: int | None = None
    last_ack = time.monotonic()
    next_notify = time.monotonic()
    max_timeout = timeout
    while True:
        if shutdown_requested.is_set():
            break
        remaining = timeout - (time.monotonic() - last_ack)
        if remaining <= 0:
            break
        if time.monotonic() >= next_notify:
            _notify_systemd("WATCHDOG=1")
            next_notify = time.monotonic() + max(0.25, min(10.0, timeout / 3))
        ready, _, _ = select.select([stream], [], [], min(0.25, remaining))
        if not ready:
            continue
        try:
            message = _read_control_line(stream)
        except (ValueError, json.JSONDecodeError):
            break
        if message is None:
            break
        operation = message.get("op")
        candidate = message.get("generation")
        if operation == "welcome" and type(candidate) is int and candidate > 0:
            if generation is None or candidate > generation:
                requested_timeout = message.get("loss_timeout", timeout)
                if (
                    type(requested_timeout) not in (int, float)
                    or not 0 < requested_timeout <= max_timeout
                ):
                    break
                timeout = float(requested_timeout)
                generation = candidate
        elif operation == "renew" and type(candidate) is int and candidate == generation:
            last_ack = time.monotonic()
        elif operation == "stop":
            _run_cleanup(
                database,
                host_id,
                term_grace=term_grace,
                kill_grace=kill_grace,
                reason="loss",
            )
            return 0
        else:
            continue

    _run_cleanup(
        database,
        host_id,
        term_grace=term_grace,
        kill_grace=kill_grace,
        reason="loss",
    )
    if parent_identity is not None and process_identity(parent_identity.pid) == parent_identity:
        with contextlib.suppress(ProcessLookupError):
            os.kill(parent_identity.pid, signal.SIGTERM)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--watchdog", action="store_true")
    args = parser.parse_args()
    if not args.watchdog:
        return 2
    return _watchdog_loop(sys.stdin)


if __name__ == "__main__":
    raise SystemExit(main())
