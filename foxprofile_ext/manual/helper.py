"""Independent, trusted VNC supervisor, outside the interactive sandbox.

Only this process receives the private control descriptor. Its gated child
receives the RFB descriptor and a credential-free confined environment. The
supervisor survives gateway death long enough to release input and reap its
owned helper. A final receipt is proof of cleanup, not a dead parent PID.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import math
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..dto import ProcessIdentity
from ..runtime.registry import current_host_id, process_identity

_stopping = False


def _request_stop(_number: int, _frame: object) -> None:
    global _stopping
    _stopping = True


def write_receipt(path: Path, receipt: dict) -> None:
    temporary = path.with_suffix(".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _alive(identity: list[int]) -> bool:
    return process_identity(identity[0]) == ProcessIdentity(*identity)


def _signal_child(identity: list[int], number: int) -> None:
    if _alive(identity):
        with contextlib.suppress(ProcessLookupError):
            os.kill(identity[0], number)


def _runtime_alive(config: dict) -> bool:
    return config["host_id"] == current_host_id() and all(
        _alive(identity) for identity in config["runtime_processes"]
    )


def _release(config: dict, deadline: float) -> None:
    if not _runtime_alive(config):
        return
    remaining = min(0.3, deadline - time.time())
    if remaining <= 0:
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            config["cleanup_command"],
            env=config["environment"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
            check=False,
        )


def _spawn_gated(config: dict, rfb: socket.socket, control: socket.socket, receipt: dict) -> int:
    """Persist host child identity before allowing its first X connection."""
    gate_read, gate_write = os.pipe()
    parent = os.getpid()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(gate_write)
            control.close()
            # A dead supervisor cannot leave a gated or executing bwrap behind.
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
                os._exit(1)
            if os.read(gate_read, 3) != b"GO\n":
                os._exit(1)
            os.close(gate_read)
            os.dup2(rfb.fileno(), 0)
            os.dup2(rfb.fileno(), 1)
            if rfb.fileno() > 2:
                rfb.close()
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 2)
            if null > 2:
                os.close(null)
            os.execvpe(config["command"][0], config["command"], config["environment"])
        except BaseException:
            os._exit(1)
    os.close(gate_read)
    try:
        identity = process_identity(pid)
        if identity is None:
            raise RuntimeError("VNC child identity unavailable")
        receipt["child"] = [identity.pid, identity.start_ticks]
        receipt["phase"] = "attached"
        write_receipt(Path(config["receipt"]), receipt)
        os.write(gate_write, b"GO\n")
    except BaseException:
        # This is our unreaped fork child, so its PID cannot have been reused.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise
    finally:
        os.close(gate_write)
    return pid


def _reap(pid: int) -> bool:
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return True


def _reap_descendants() -> None:
    """Subreaper: confirm every orphaned descendant, including PID-init, exits."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid:
            continue
        try:
            children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
        except OSError:
            children = []
        for child in children:
            identity = process_identity(int(child))
            if identity is not None:
                _signal_child([identity.pid, identity.start_ticks], signal.SIGKILL)
        select.select([], [], [], 0.01)


def supervise(control: socket.socket, rfb: socket.socket) -> None:
    # SOCK_SEQPACKET keeps each bounded private control message atomic.
    control.settimeout(5)
    raw = control.recv(65536)
    config = json.loads(raw)
    control.settimeout(None)
    deadline = min(float(config["watchdog_deadline"]), time.time() + 60)
    expires_at = float(config["expires_at"])
    hard_deadline = min(float(config["cleanup_deadline"]), expires_at + 60)
    if not all(math.isfinite(value) for value in (deadline, expires_at, hard_deadline)):
        return
    identity = process_identity(os.getpid())
    receipt = {
        "host_id": current_host_id(),
        "supervisor": [identity.pid, identity.start_ticks],
        "phase": "gated",
        "child": None,
        "watchdog_deadline": deadline,
        "expires_at": expires_at,
    }
    path = Path(config["receipt"])
    write_receipt(path, receipt)
    pid = None
    attached = False
    teardown_budget = min(1.0, max(0.05, (deadline - time.time()) / 4))
    try:
        while True:
            now = time.time()
            if (
                _stopping
                or now >= min(deadline - teardown_budget, expires_at)
                or not _runtime_alive(config)
            ):
                break
            if pid is not None and _reap(pid):
                break
            readable, _, _ = select.select([control], [], [], min(0.1, max(0, deadline - now)))
            if not readable:
                continue
            raw = control.recv(65536)
            if not raw:
                # Parent death is immediate loss; heartbeat grace cannot delay it.
                break
            message = json.loads(raw)
            operation = message.get("op")
            if operation == "stop":
                deadline = min(deadline, float(message["deadline"]), now + 60)
                break
            if operation == "heartbeat":
                proposed = float(message["deadline"])
                if not math.isfinite(proposed):
                    break
                deadline = min(proposed, now + 60, hard_deadline)
                receipt["watchdog_deadline"] = deadline
                write_receipt(path, receipt)
            elif operation == "release" and attached:
                _release(config, min(deadline, now + 0.3))
            elif operation == "attach" and not attached:
                # The caller sends this only after its durable helper claim.
                pid = _spawn_gated(config, rfb, control, receipt)
                attached = True
                rfb.close()
    finally:
        receipt["cleanup_deadline"] = deadline
        receipt["phase"] = "closing"
        with contextlib.suppress(OSError):
            write_receipt(path, receipt)
        if pid is not None:
            _release(config, deadline)
            child = receipt["child"]
            _signal_child(child, signal.SIGTERM)
            graceful_until = min(deadline - 0.1, time.time() + 0.3)
            while time.time() < graceful_until and not _reap(pid):
                select.select([], [], [], 0.01)
            if not _reap(pid):
                _signal_child(child, signal.SIGKILL)
            while not _reap(pid):
                select.select([], [], [], 0.01)
            _reap_descendants()
            _release(config, deadline)
        receipt["phase"] = "dead"
        write_receipt(path, receipt)
        rfb.close()


def main() -> None:
    # Reparented bwrap descendants stay ours until individually reaped. Parent
    # PID disappearance alone cannot produce a successful cleanup receipt.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise RuntimeError("VNC child supervision unavailable")
    signal.signal(signal.SIGTERM, _request_stop)
    control = socket.socket(fileno=int(sys.argv[1]))
    rfb = socket.socket(fileno=int(sys.argv[2]))
    try:
        supervise(control, rfb)
    finally:
        control.close()
        rfb.close()


if __name__ == "__main__":
    main()
