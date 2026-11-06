"""Replaceable x11vnc adapter for one verified, already running X display."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..dto import ProcessIdentity, RuntimeDescriptor
from ..runtime.confinement import ConfinementPurpose, ConfinementSpec, build_confined_command
from ..runtime.registry import current_host_id, process_identity
from ..storage.manual import HelperLiveness
from .contracts import BackendSession
from .private_channel import PrivateChannel


class VNCBackendUnavailable(RuntimeError):
    """Redacted failure; native diagnostics never share the binary RFB stream."""


@dataclass(slots=True)
class _Session:
    channel: PrivateChannel
    control: socket.socket
    process: subprocess.Popen | None = None
    helper: ProcessIdentity | None = None
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class PrivateVNCBackend:
    """Private inetd channel with durable, independently supervised cleanup.

    ``confinement_for`` resolves trusted server paths after checking that the
    descriptor is still the current verified run. ``state_root`` stays outside
    every interactive mount. ``executable_sha256`` pins the deployed distro
    binary; binaries and GPL code are not bundled into this MIT package.
    """

    def __init__(
        self,
        *,
        state_root: str | Path,
        confinement_for: Callable[[RuntimeDescriptor], ConfinementSpec],
        executable_sha256: str,
        executable: str | Path = "x11vnc",
        library_paths: Sequence[str | Path] = (),
    ) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", executable_sha256):
            raise ValueError("a pinned executable SHA-256 is required")
        self.state_root = Path(state_root).absolute()
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_root.lstat()
        if (
            self.state_root.is_symlink()
            or not self.state_root.is_dir()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("VNC state directory must be service-owned and private")
        self.state_root = self.state_root.resolve()
        self._confinement_for = confinement_for
        self._executable = str(executable)
        self._sha256 = executable_sha256
        self._library_paths = tuple(Path(path).resolve() for path in library_paths)
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self.host_id = current_host_id()

    def _receipt_path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("manual session identity is required")
        return self.state_root / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")

    def _commands(self, runtime: RuntimeDescriptor) -> tuple[list[str], list[str], dict[str, str]]:
        if (
            runtime.host_id != self.host_id
            or self.host_id.endswith(":unknown-boot")
            or runtime.browser is None
            or not runtime.namespace
            or not re.fullmatch(r"pid:\[\d+\]", runtime.namespace)
            or not runtime.display
            or not re.fullmatch(r":\d+(?:\.\d+)?", runtime.display)
            or not runtime.tmp_path
            or not runtime.xauthority_path
            or process_identity(runtime.supervisor.pid) != runtime.supervisor
            or process_identity(runtime.browser.pid) != runtime.browser
        ):
            raise VNCBackendUnavailable("Verified virtual runtime is unavailable")
        spec = replace(self._confinement_for(runtime), purpose=ConfinementPurpose.VNC_HELPER)
        temporary = Path(runtime.tmp_path).resolve(strict=True)
        authority = Path(runtime.xauthority_path)
        if (
            spec.private_tmp_path.resolve(strict=True) != temporary
            or authority.is_symlink()
            or not authority.is_file()
            or temporary not in authority.resolve().parents
            or authority.stat().st_uid != os.getuid()
            or authority.stat().st_mode & 0o077
            or any(
                self.state_root == path.resolve()
                or self.state_root in path.resolve().parents
                or path.resolve() in self.state_root.parents
                for path in (
                    spec.private_tmp_path,
                    spec.profile_path,
                    *spec.runtime_paths,
                    *(spec.code_root / name for name in ("src", "chrome_engine", "foxprofile_ext")),
                )
            )
        ):
            raise VNCBackendUnavailable("Private runtime context is unavailable")
        executable = shutil.which(self._executable)
        if executable is None:
            raise VNCBackendUnavailable("Pinned x11vnc executable is unavailable")
        binary = Path(executable).resolve(strict=True)
        if hashlib.sha256(binary.read_bytes()).hexdigest() != self._sha256:
            raise VNCBackendUnavailable("Pinned x11vnc executable does not match")
        settings = {
            "DISPLAY": runtime.display,
            "XAUTHORITY": str(Path("/tmp") / authority.resolve().relative_to(temporary)),
        }
        command, environment = build_confined_command(
            spec,
            (
                str(binary),
                "-norc",
                "-inetd",
                "-q",
                "-display",
                runtime.display,
                "-auth",
                settings["XAUTHORITY"],
                "-rfbport",
                "0",
                "-no6",
                "-safer",
                "-nocmds",
                "-nosel",
                "-noxrandr",
                "-clear_keys",
                "-noshm",
                "-noxrecord",
                "-desktop",
                "FoxProfile",
            ),
            settings,
        )
        cleanup, _ = build_confined_command(
            spec, (sys.executable, "-m", "foxprofile_ext.manual.x11cleanup"), settings
        )
        for library_path in self._library_paths:
            if not any(
                library_path == path.resolve() or path.resolve() in library_path.parents
                for path in spec.runtime_paths
            ):
                raise VNCBackendUnavailable("Native libraries are outside trusted runtime mounts")
        if self._library_paths:
            environment["LD_LIBRARY_PATH"] = ":".join(map(str, self._library_paths))
        return command, cleanup, environment

    def start(
        self,
        session_id: str,
        runtime: RuntimeDescriptor,
        claim_helper: Callable[[ProcessIdentity], bool],
        *,
        expires_at: float,
        cleanup_deadline: float,
        watchdog_deadline: float,
    ) -> BackendSession:
        now = time.time()
        if not all(
            math.isfinite(value) for value in (expires_at, cleanup_deadline, watchdog_deadline)
        ):
            raise ValueError("finite deadlines are required")
        deadline = min(watchdog_deadline, cleanup_deadline, now + 60, expires_at + 60)
        if min(deadline, expires_at) <= now:
            raise VNCBackendUnavailable("Manual session has expired")
        receipt = self._receipt_path(session_id)
        gateway, native = socket.socketpair()
        control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        entry = _Session(PrivateChannel(gateway), control)
        with self._lock:
            if session_id in self._sessions or receipt.exists():
                gateway.close()
                native.close()
                control.close()
                child_control.close()
                raise VNCBackendUnavailable("Manual session cannot be reopened")
            self._sessions[session_id] = entry
        try:
            command, cleanup, environment = self._commands(runtime)
            with entry.lock:
                if entry.cancelled:
                    raise VNCBackendUnavailable("Manual session was cancelled")
                entry.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "foxprofile_ext.manual.helper",
                        str(child_control.fileno()),
                        str(native.fileno()),
                    ],
                    pass_fds=(child_control.fileno(), native.fileno()),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=Path(__file__).resolve().parents[2],
                    env={
                        "PATH": "/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    start_new_session=True,
                )
                entry.helper = process_identity(entry.process.pid)
                if entry.helper is None:
                    raise VNCBackendUnavailable("VNC supervisor identity is unavailable")
                self._send(
                    entry,
                    {
                        "command": command,
                        "cleanup_command": cleanup,
                        "environment": environment,
                        "receipt": str(receipt),
                        "host_id": runtime.host_id,
                        "runtime_processes": [
                            [value.pid, value.start_ticks]
                            for value in (runtime.supervisor, runtime.browser)
                        ],
                        "watchdog_deadline": deadline,
                        "expires_at": expires_at,
                        "cleanup_deadline": cleanup_deadline,
                    },
                )
            native.close()
            child_control.close()
            if not claim_helper(entry.helper):
                raise VNCBackendUnavailable("VNC helper claim was cancelled")
            with entry.lock:
                if entry.cancelled:
                    raise VNCBackendUnavailable("Manual session was cancelled")
                self._send(entry, {"op": "attach"})
            ready_deadline = min(deadline - 0.1, time.time() + 10)
            while time.time() < ready_deadline:
                if entry.cancelled or entry.process.poll() is not None:
                    break
                readable, _, _ = select.select([gateway], [], [], 0.05)
                if readable:
                    banner = gateway.recv(12, socket.MSG_PEEK)
                    if len(banner) == 12 and re.fullmatch(rb"RFB \d{3}\.\d{3}\n", banner):
                        with entry.lock:
                            if entry.cancelled:
                                break
                            return BackendSession(entry.helper, entry.channel)
                    if not banner:
                        break
            raise VNCBackendUnavailable("Private VNC helper did not become ready")
        except BaseException:
            self.stop(session_id, runtime.host_id, entry.helper, min(deadline, time.time() + 2))
            raise
        finally:
            native.close()
            child_control.close()

    @staticmethod
    def _send(entry: _Session, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        if len(data) > 65535:
            raise VNCBackendUnavailable("Private VNC configuration is too large")
        try:
            entry.control.sendall(data)
        except OSError as error:
            raise VNCBackendUnavailable("Private VNC supervisor is unavailable") from error

    def connect(self, session_id: str, helper: ProcessIdentity) -> PrivateChannel:
        with self._lock:
            entry = self._sessions.get(session_id)
        if (
            entry is None
            or entry.helper != helper
            or entry.cancelled
            or entry.channel.revoked
            or self.probe(session_id, self.host_id, helper) is not HelperLiveness.LIVE
        ):
            raise VNCBackendUnavailable("Private VNC channel is unavailable")
        return entry.channel

    def heartbeat(self, session_id: str, watchdog_deadline: float) -> None:
        if not math.isfinite(watchdog_deadline):
            raise ValueError("finite watchdog deadline is required")
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is None:
            raise VNCBackendUnavailable("Private VNC supervisor is unavailable")
        with entry.lock:
            if entry.cancelled:
                raise VNCBackendUnavailable("Private VNC supervisor is closing")
            self._send(
                entry, {"op": "heartbeat", "deadline": min(watchdog_deadline, time.time() + 60)}
            )

    def revoke(self, session_id: str) -> None:
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is not None:
            entry.channel.revoke()

    def release_input(self, session_id: str) -> None:
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is not None:
            with entry.lock:
                if not entry.cancelled:
                    self._send(entry, {"op": "release"})

    def stop(
        self, session_id: str, host_id: str, helper: ProcessIdentity | None, cleanup_deadline: float
    ) -> None:
        if host_id != self.host_id:
            return
        if not math.isfinite(cleanup_deadline):
            raise ValueError("finite cleanup deadline is required")
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is not None:
            with entry.lock:
                if helper is not None and helper != entry.helper:
                    return
                entry.cancelled = True
                entry.channel.revoke()
                with contextlib.suppress(VNCBackendUnavailable):
                    self._send(entry, {"op": "stop", "deadline": cleanup_deadline})
                entry.control.close()
            if entry.process is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    entry.process.wait(timeout=max(0, min(2, cleanup_deadline - time.time())))
            return
        # A recovered adapter can request its exact supervisor's cleanup. Never
        # kill a numeric PID alone or equate its disappearance with tree exit.
        receipt = self._read_receipt(session_id)
        if (
            receipt is not None
            and helper is not None
            and receipt.get("supervisor") == [helper.pid, helper.start_ticks]
            and process_identity(helper.pid) == helper
        ):
            with contextlib.suppress(ProcessLookupError):
                os.kill(helper.pid, signal.SIGTERM)

    def _read_receipt(self, session_id: str) -> dict | None:
        try:
            value = json.loads(self._receipt_path(session_id).read_text(encoding="utf-8"))
            return (
                value if isinstance(value, dict) and value.get("host_id") == self.host_id else None
            )
        except (OSError, ValueError):
            return None

    def probe(
        self, session_id: str, host_id: str, helper: ProcessIdentity | None
    ) -> HelperLiveness:
        if host_id != self.host_id or host_id.endswith(":unknown-boot"):
            return HelperLiveness.UNKNOWN
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is not None and helper is not None and entry.helper != helper:
            return HelperLiveness.UNKNOWN
        receipt = self._read_receipt(session_id)
        if receipt is not None and (
            helper is None or receipt.get("supervisor") == [helper.pid, helper.start_ticks]
        ):
            if receipt.get("phase") == "dead":
                if entry is not None and entry.process is not None:
                    entry.process.poll()
                return HelperLiveness.DEAD
            identity = receipt.get("supervisor")
            if (
                isinstance(identity, list)
                and len(identity) == 2
                and process_identity(identity[0]) == ProcessIdentity(*identity)
            ):
                return HelperLiveness.LIVE
        if entry is not None and entry.process is None and entry.cancelled:
            return HelperLiveness.DEAD
        return HelperLiveness.UNKNOWN
