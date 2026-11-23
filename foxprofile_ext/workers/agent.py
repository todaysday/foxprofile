"""Outbound-only Worker agent for Master-admitted browser generations."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import signal
import ssl
import stat
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.models.profile import ENGINES, Profile
from src.services.browser.launcher import BrowserLauncher
from src.utils.validation import validate_locale, validate_proxy_format, validate_timezone

from ..dto import BrowserRunRefDTO, ProcessIdentity
from ..errors import DomainError, ErrorCode
from ..manual.backend import PrivateVNCBackend
from ..manual.contracts import BackendSession
from ..runtime.confinement import ConfinementPurpose, ConfinementSpec
from ..runtime.processes import RuntimeSupervisor
from ..runtime.registry import current_host_id
from ..storage.capacity import ProcessLiveness, RunStatus
from .execution import WorkerExecutionDatabase
from .manifest import VerifiedManifest, load_manifest
from .metrics import NodeMetrics, read_local_metrics
from .watchdog import WorkerWatchdog

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_CONTROL_PARAMS_BYTES = 1024 * 1024
MAX_COMMAND_CACHE = 512
MAX_COMMAND_CACHE_BYTES = 16 * 1024 * 1024
_COMMAND_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPERATIONS = frozenset(
    {
        "start",
        "stop",
        "cleanup",
        "control",
        "describe",
        "sync",
        "manual_open",
        "manual_activate",
        "manual_heartbeat",
        "manual_release",
        "manual_close",
        "manual_probe",
    }
)


def operating_system_boot_id() -> str:
    """Return kernel boot identity; Worker service restarts do not invent boots."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as error:
        raise RuntimeError("Linux boot identity is unavailable") from error
    if not re.fullmatch(r"[a-f0-9-]{36}", value):
        raise RuntimeError("Linux boot identity is invalid")
    return value


def read_worker_credential(path: str | Path) -> str:
    """Read one private systemd credential; never place it in child settings."""
    candidate = Path(path)
    try:
        info = candidate.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid not in {0, os.getuid()}
            or info.st_size > 1024
        ):
            raise ValueError("Worker credential file must be private and service-owned")
        value = candidate.read_text(encoding="ascii").strip()
    except OSError as error:
        raise ValueError("Worker credential is unavailable") from error
    if not 16 <= len(value) <= 512 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("Worker credential is invalid")
    return value


def secure_worker_directory(path: str | Path) -> Path:
    candidate = Path(path).absolute()
    if candidate.is_symlink():
        raise ValueError("Worker state directory cannot be a symlink")
    candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = candidate.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Worker state directory must be service-owned")
    candidate.chmod(0o700)
    return candidate.resolve()


def _https_url(value: object, *, web_socket: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("transfer URL is invalid")
    parsed = urlsplit(value)
    allowed = {"wss"} if web_socket else {"https"}
    if (
        parsed.scheme not in allowed
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(char in value for char in "\r\n\x00")
    ):
        raise ValueError("Worker transport requires an authenticated TLS URL")
    return value


def _strict_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    if len(json.dumps(value, separators=(",", ":")).encode()) > MAX_CONTROL_PARAMS_BYTES:
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    return value


def _redacted_error_code(error: BaseException) -> str:
    if isinstance(error, DomainError):
        return error.code.value
    if isinstance(error, (ValueError, TypeError, KeyError, json.JSONDecodeError)):
        return ErrorCode.VALIDATION_ERROR.value
    name = getattr(error, "code", None)
    try:
        return ErrorCode(name).value
    except (TypeError, ValueError):
        return ErrorCode.WORKER_UNAVAILABLE.value


@dataclass
class _ManualGate:
    session_id: str
    run: BrowserRunRefDTO
    descriptor: object
    relay: dict[str, Any]
    expires_at: float
    cleanup_deadline: float
    watchdog_deadline: float
    prepared: threading.Event = field(default_factory=threading.Event)
    activate: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    helper: ProcessIdentity | None = None
    backend_session: BackendSession | None = None
    error: BaseException | None = None
    relay_task: asyncio.Task | None = None
    closing: bool = False


class WorkerRuntime:
    """RuntimeSupervisor plus the existing loopback PageController adapter."""

    def __init__(
        self,
        database: WorkerExecutionDatabase,
        state_dir: Path,
        manifest: VerifiedManifest,
        *,
        prepare_launch: Callable[[Profile], None] | None = None,
        supervisor: RuntimeSupervisor | None = None,
        launcher: BrowserLauncher | None = None,
    ) -> None:
        os.environ["FOXPROFILE_EXT_ENABLED"] = "true"
        self.database = database
        self.state_dir = state_dir
        self.data_dir = secure_worker_directory(state_dir / "profiles")
        self.code_root = Path(__file__).resolve().parents[2]
        self.manifest = manifest
        if supervisor is None:
            from ..runtime_factory import _installed_executable

            runtime_paths = [
                Path(item)
                for item in (
                    "/usr",
                    "/bin",
                    "/lib",
                    "/lib64",
                    "/etc/fonts",
                    "/etc/ssl/certs",
                    "/etc/resolv.conf",
                    "/etc/hosts",
                    "/etc/nsswitch.conf",
                    sys.prefix,
                    sys.base_prefix,
                )
            ]
            settings = {
                name: os.environ[name]
                for name in (
                    "LANG",
                    "LC_ALL",
                    "TZ",
                    "FOXPROFILE_LANG",
                    "FOXPROFILE_RESTORE_TABS",
                    "FOXPROFILE_PROXY_TIMEOUT",
                )
                if name in os.environ
            }
            settings["FOXPROFILE_LOG_DIR"] = "/tmp/logs"
            for engine in manifest.ready_engines:
                executable = _installed_executable(engine)
                if executable is None:
                    raise RuntimeError("Pinned browser executable is unavailable")
                runtime_paths.append(executable.parent)
                settings[f"FOXPROFILE_{engine.upper()}_EXECUTABLE"] = str(executable)
                if engine == "camoufox":
                    from ..runtime.cache import geoip_cache_directory

                    cache = geoip_cache_directory()
                    cache.mkdir(parents=True, exist_ok=True)
                    runtime_paths.append(cache)
                    settings["XDG_CACHE_HOME"] = str(cache.parent.parent)
            self.supervisor = RuntimeSupervisor(
                database,
                data_dir=self.data_dir,
                code_root=self.code_root,
                runtime_paths=tuple(dict.fromkeys(path for path in runtime_paths if path.exists())),
                settings=settings,
                prepare_launch=prepare_launch,
                worker_execution=True,
            )
            self.supervisor.recover()
        else:
            self.supervisor = supervisor
        self.ledger = self.supervisor.ledger
        self.launcher = launcher or BrowserLauncher(runtime_adapter=self.supervisor)

    def profile_directory(self, run_id: str) -> Path:
        return self.data_dir / run_id

    def descriptor(self, ref: BrowserRunRefDTO):
        return self.supervisor.verified_descriptor(ref)

    def tree_is_dead(self, ref: BrowserRunRefDTO) -> bool:
        run = self.ledger.get(ref)
        if run is None:
            return False
        if run.process is None:
            return run.status in {RunStatus.CANCELLED, RunStatus.CLOSED}
        return self.supervisor._probe_run(run.host_id, run.process) is ProcessLiveness.DEAD

    async def start(
        self,
        profile: Profile,
        ref: BrowserRunRefDTO,
        *,
        on_ready: Callable[[], None],
        on_stop: Callable[[], None],
        timeout: float,
    ) -> str:
        current = self.ledger.get(ref)
        if current is None or current.host_id != self.supervisor.host_id:
            raise DomainError(ErrorCode.STALE_RUN)
        if current.status is RunStatus.RUNNING:
            return "running"
        if current.status in {RunStatus.STARTING, RunStatus.STOPPING}:
            return "starting"
        if current.status is not RunStatus.RESERVED or current.process is not None:
            raise DomainError(ErrorCode.STALE_RUN)
        self.supervisor.prepare_browser(profile, ref.profile_id, ref.run_id)
        if not self.launcher.start_thread(
            profile,
            lambda _line: None,
            on_ready=lambda: (self.ledger.mark_running(ref), on_ready()),
            on_stop=on_stop,
            run_id=ref.run_id,
            profile_id=ref.profile_id,
        ):
            self.ledger.cancel_unclaimed(ref)
            raise DomainError(ErrorCode.PROFILE_BUSY)
        ok, error = await asyncio.to_thread(
            self.launcher.wait_for_launch, profile.name, timeout, ref.run_id
        )
        if ok:
            return "running"
        if error == "timeout":
            return "starting"
        raise DomainError(ErrorCode.WORKER_UNAVAILABLE)

    def stop(self, ref: BrowserRunRefDTO, timeout: float) -> bool:
        return self.supervisor.request_stop(ref.profile_id, ref.run_id, timeout)

    def control(self, ref: BrowserRunRefDTO, action: str, params: dict[str, Any], timeout: float):
        return self.supervisor.control(ref.profile_id, ref.run_id, action, params, timeout)

    def shutdown(self, timeout: float = 5) -> None:
        self.supervisor.shutdown(timeout)


class WorkerAgent:
    """Correlated command client. The Master remains the only admission authority."""

    def __init__(
        self,
        *,
        worker_id: str,
        credential: str,
        master_url: str,
        state_dir: str | Path,
        database: WorkerExecutionDatabase,
        manifest: VerifiedManifest,
        runtime: WorkerRuntime,
        watchdog: WorkerWatchdog,
        boot_id: str | None = None,
        session: object | None = None,
        manual_backend: PrivateVNCBackend | None = None,
        loss_timeout: float = 30.0,
        startup_timeout: float = 20.0,
        memory_reserve_bytes: int = 0,
        staging_reserve_bytes: int = 0,
    ) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("Worker ID is invalid")
        if (
            not isinstance(credential, str)
            or not 16 <= len(credential) <= 512
            or any(ord(char) < 33 or ord(char) > 126 for char in credential)
        ):
            raise ValueError("Worker credential is invalid")
        parsed = urlsplit(master_url)
        if (
            parsed.scheme != "wss"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Worker master URL must be a WSS origin")
        for value in (loss_timeout, startup_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Worker timeouts must be finite and positive")
        if loss_timeout > 30:
            raise ValueError("Worker loss timeout cannot exceed 30 seconds")
        for value in (memory_reserve_bytes, staging_reserve_bytes):
            if type(value) is not int or value < 0:
                raise ValueError("Worker reserves must be non-negative bytes")
        self.worker_id = worker_id
        self.credential = credential
        self.master_url = master_url.rstrip("/")
        self.state_dir = secure_worker_directory(state_dir)
        self.database = database
        self.manifest = manifest
        self.runtime = runtime
        self.watchdog = watchdog
        self.boot_id = boot_id or operating_system_boot_id()
        self.session = session
        self.manual_backend = manual_backend
        self.loss_timeout = float(loss_timeout)
        self._watchdog_loss_cap = float(loss_timeout)
        self.startup_timeout = min(60.0, float(startup_timeout))
        self.memory_reserve_bytes = memory_reserve_bytes
        self.staging_reserve_bytes = staging_reserve_bytes
        self.connection_generation: int | None = None
        self.heartbeat_interval = 10.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._events: asyncio.Queue[dict[str, Any]] | None = None
        self._send_lock: asyncio.Lock | None = None
        self._commands: dict[str, asyncio.Task] = {}
        self._command_cache: OrderedDict[str, tuple[str, dict[str, Any], int]] = OrderedDict()
        self._command_cache_bytes = 0
        self._profile_by_run: dict[str, Profile] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._finalize_locks: dict[str, asyncio.Lock] = {}
        self._manual: dict[str, _ManualGate] = {}
        self._manual_lock = threading.RLock()
        self._manual_backend = manual_backend
        self._shutdown = asyncio.Event()
        self._archive_limit = MAX_ARCHIVE_BYTES
        self._http_timeout = 120.0

    @property
    def connect_url(self) -> str:
        return self.master_url + "/api/v1/workers/connect"

    def start_watchdog(self) -> None:
        self.watchdog.start()

    async def run(self) -> None:
        import aiohttp

        self._loop = asyncio.get_running_loop()
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=self._http_timeout)
        if self.session is None:
            self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)
            owns_session = True
        else:
            owns_session = False
        delay = 1.0
        try:
            while not self._shutdown.is_set():
                try:
                    await self._connect_once()
                    delay = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._shutdown.is_set():
                        break
                    await asyncio.sleep(delay)
                    delay = min(30.0, delay * 2)
        finally:
            active_before_shutdown = ()
            with contextlib.suppress(Exception):
                active_before_shutdown = self.runtime.ledger.active_runs()
            try:
                if owns_session:
                    await self.session.close()
                await self._close_all_manual_sessions()
            finally:
                try:
                    await asyncio.to_thread(self.runtime.shutdown, 5)
                finally:
                    try:
                        # Service shutdown can stop runs outside an explicit
                        # Master stop command. Persist loss proofs after the
                        # runtime confirms each exact tree dead; do not attempt
                        # an unbounded archive pack while the unit is stopping.
                        for row in active_before_shutdown:
                            ref = getattr(row, "ref", None)
                            if ref is None:
                                ref = BrowserRunRefDTO(row["profile_id"], row["run_id"])
                            with contextlib.suppress(Exception):
                                await self._finalize_stopped(ref, reason="loss", event=False)
                    finally:
                        await asyncio.to_thread(self.watchdog.close, 65)

    async def stop(self) -> None:
        self._shutdown.set()

    async def _connect_once(self) -> None:
        import aiohttp

        headers = {"Authorization": f"Bearer {self.credential}"}
        async with self.session.ws_connect(
            self.connect_url,
            headers=headers,
            heartbeat=10,
            receive_timeout=40,
            max_msg_size=MAX_MESSAGE_BYTES,
            autoping=True,
            autoclose=True,
        ) as websocket:
            await websocket.send_json(
                {
                    "type": "hello",
                    "worker_id": self.worker_id,
                    "boot_id": self.boot_id,
                    "manifest_digest": self.manifest.digest,
                    "preflight_ok": True,
                }
            )
            welcome = await websocket.receive(timeout=10)
            if welcome.type is not aiohttp.WSMsgType.TEXT:
                raise ConnectionError("Master did not welcome Worker")
            if len(welcome.data.encode()) > 65536:
                raise ConnectionError("Master welcome exceeds limit")
            message = json.loads(welcome.data)
            generation = message.get("connection_generation")
            if (
                message.get("type") != "welcome"
                or message.get("worker_id") != self.worker_id
                or type(generation) is not int
                or generation < 1
            ):
                raise ConnectionError("Master welcome is invalid")
            interval = message.get("heartbeat_interval", 10)
            loss_timeout = message.get("loss_timeout", self.loss_timeout)
            if type(interval) not in (int, float) or not 1 <= interval <= 10:
                raise ConnectionError("Master heartbeat interval is invalid")
            if (
                type(loss_timeout) not in (int, float)
                or not 10 <= loss_timeout <= 30
                or interval >= loss_timeout
            ):
                raise ConnectionError("Master loss timeout is invalid")
            self.connection_generation = generation
            self.heartbeat_interval = float(interval)
            self.loss_timeout = float(loss_timeout)
            self.watchdog.accept_generation(
                generation, min(self._watchdog_loss_cap, float(loss_timeout))
            )
            await self._serve_connection(websocket, generation)
        self.connection_generation = None

    async def _serve_connection(self, websocket, generation: int) -> None:
        self._events = asyncio.Queue(maxsize=512)
        self._send_lock = asyncio.Lock()
        heartbeat = asyncio.create_task(self._heartbeat_loop(websocket, generation))
        event_sender = asyncio.create_task(self._event_loop(websocket, generation))
        try:
            async for frame in websocket:
                if frame.type == 2:
                    raise ConnectionError("Unexpected binary frame on Worker control channel")
                if frame.type == 8 or frame.type == 257:
                    break
                if frame.type != 1 or len(frame.data.encode()) > MAX_MESSAGE_BYTES:
                    raise ConnectionError("Invalid Worker control frame")
                message = json.loads(frame.data)
                if not isinstance(message, dict):
                    raise ConnectionError("Invalid Worker control message")
                if message.get("type") == "heartbeat_ack":
                    if any(
                        message.get(key) != expected
                        for key, expected in (
                            ("worker_id", self.worker_id),
                            ("boot_id", self.boot_id),
                            ("connection_generation", generation),
                        )
                    ):
                        raise ConnectionError("Stale Master heartbeat acknowledgment")
                    # Only a current authenticated Master ack renews loss lease.
                    self.watchdog.renew(generation)
                    continue
                if message.get("type") != "command":
                    raise ConnectionError("Unexpected Master control message")
                self._validate_command(message, generation)
                command_id = message["command_id"]
                signature = _request_signature(message)
                cached = self._command_cache.get(command_id)
                if cached is not None:
                    if cached[0] != signature:
                        await self._send_result(
                            websocket,
                            generation,
                            message,
                            ok=False,
                            error_code=ErrorCode.STALE_RUN.value,
                        )
                    else:
                        self._command_cache.move_to_end(command_id)
                        await self._send_json(websocket, cached[1])
                    continue
                if command_id in self._commands:
                    continue
                if len(self._commands) >= 128:
                    await self._send_result(
                        websocket,
                        generation,
                        message,
                        ok=False,
                        error_code=ErrorCode.WORKER_UNAVAILABLE.value,
                    )
                    continue
                task = asyncio.create_task(
                    self._dispatch_and_reply(websocket, generation, message, signature)
                )
                self._commands[command_id] = task
                task.add_done_callback(lambda _task, key=command_id: self._commands.pop(key, None))
        finally:
            heartbeat.cancel()
            event_sender.cancel()
            await asyncio.gather(heartbeat, event_sender, return_exceptions=True)
            # In-flight operations may finish, but their old generation replies
            # cannot be reused on a new connection.
            for task in tuple(self._commands.values()):
                task.cancel()
            await asyncio.gather(*tuple(self._commands.values()), return_exceptions=True)
            self._commands.clear()
            self._events = None
            self._send_lock = None

    def _validate_command(self, message: dict[str, Any], generation: int) -> None:
        if any(
            message.get(key) != expected
            for key, expected in (
                ("worker_id", self.worker_id),
                ("boot_id", self.boot_id),
                ("connection_generation", generation),
            )
        ):
            raise ConnectionError("Master command targets another Worker generation")
        if (
            not isinstance(message.get("command_id"), str)
            or not _COMMAND_ID.fullmatch(message["command_id"])
            or not isinstance(message.get("profile_id"), str)
            or not message["profile_id"].strip()
            or not isinstance(message.get("run_id"), str)
            or not _RUN_ID.fullmatch(message["run_id"])
            or message.get("operation") not in _OPERATIONS
        ):
            raise ConnectionError("Master command identity is invalid")
        _strict_object(message.get("payload", {}), "payload")

    async def _dispatch_and_reply(self, websocket, generation, message, signature) -> None:
        try:
            result = await self._dispatch(message, generation)
            response = self._result_message(message, generation, ok=True, result=result)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            response = self._result_message(
                message,
                generation,
                ok=False,
                error_code=_redacted_error_code(error),
            )
        encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode()) > MAX_MESSAGE_BYTES:
            response = self._result_message(
                message, generation, ok=False, error_code=ErrorCode.VALIDATION_ERROR.value
            )
        self._cache_result(message["command_id"], signature, response)
        if self.connection_generation == generation:
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                await self._send_json(websocket, response)

    async def _dispatch(self, message: dict[str, Any], generation: int) -> dict[str, Any]:
        operation = message["operation"]
        payload = message.get("payload") or {}
        ref = BrowserRunRefDTO(message["profile_id"], message["run_id"])
        if operation == "start":
            return await self._start_command(ref, payload, generation)
        if operation == "stop":
            return await self._stop_command(ref, payload)
        if operation == "cleanup":
            return self._cleanup_command(ref, payload)
        if operation == "control":
            return await self._control_command(ref, payload)
        if operation == "describe":
            return self._describe_command(ref)
        if operation == "sync":
            return await self._sync_command(ref, payload)
        if operation == "manual_open":
            return await self._manual_open_command(ref, payload)
        if operation == "manual_activate":
            return await self._manual_activate_command(ref, payload)
        if operation.startswith("manual_"):
            return await self._manual_command(operation, ref, payload)
        raise DomainError(ErrorCode.VALIDATION_ERROR)

    async def _start_command(
        self, ref: BrowserRunRefDTO, payload: dict[str, Any], generation: int
    ) -> dict[str, Any]:
        lock = self._run_locks.setdefault(ref.run_id, asyncio.Lock())
        async with lock:
            return await self._start_command_serialized(ref, payload, generation)

    async def _start_command_serialized(
        self, ref: BrowserRunRefDTO, payload: dict[str, Any], generation: int
    ) -> dict[str, Any]:
        existing = self.database.run_status(ref)
        if existing in {RunStatus.STARTING, RunStatus.RUNNING, RunStatus.STOPPING}:
            return {"state": "running" if existing is RunStatus.RUNNING else "starting"}
        if existing is not None or self.database.run_context(ref.run_id) is not None:
            raise DomainError(ErrorCode.STALE_RUN)
        try:
            host_id = getattr(
                getattr(self.runtime, "supervisor", None), "host_id", current_host_id()
            )
            reserved = self.database.reserve_exact(ref, host_id)
        except RuntimeError:
            # A different local generation of this profile is present. Do not
            # infer that it belongs to the new Master run or stop it here. The
            # exact ID has no worker row, so the child-side reservation gate
            # could not have admitted this generation; report that fact.
            if self.database.run_status(ref) is None:
                if not self.database.record_cancelled_exact(ref, host_id):
                    raise DomainError(ErrorCode.STALE_RUN) from None
                context = {
                    "profile_id": ref.profile_id,
                    "run_id": ref.run_id,
                    "worker_id": self.worker_id,
                    "worker_boot_id": self.boot_id,
                    "connection_generation": generation,
                    "manual_sessions": {},
                }
                self.database.put_run_context(ref.run_id, context)
                proof = _no_process_proof(self.worker_id, self.boot_id, context, ref)
                self.database.record_stop_proof(ref.run_id, proof)
                self._emit_event({"type": "stopped", "proof": proof})
            raise DomainError(ErrorCode.PROFILE_BUSY) from None
        if not reserved:
            raise DomainError(ErrorCode.STALE_RUN)

        # Record the exact Master generation before doing capacity or transfer
        # work. Any pre-spawn rejection can then return durable proof that this
        # run never claimed a supervisor process.
        context: dict[str, Any] = {
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "worker_id": self.worker_id,
            "worker_boot_id": self.boot_id,
            "connection_generation": generation,
            "manual_sessions": {},
        }
        self.database.put_run_context(ref.run_id, context)
        staged_path = self.runtime.profile_directory(ref.run_id)
        archive = self.state_dir / "transfers" / f"{ref.run_id}.zip"
        try:
            profile = _profile_from_payload(payload.get("profile"), ref.profile_id, ref.run_id)
            if profile.engine not in self.manifest.ready_engines:
                raise DomainError(ErrorCode.RUNTIME_INCOMPATIBLE)
            budget = payload.get("memory_budget_bytes")
            if type(budget) is not int or not 0 < budget <= (1 << 60):
                raise DomainError(ErrorCode.VALIDATION_ERROR)
            self._check_local_capacity(profile.engine, budget)
            snapshot = _strict_object(payload.get("snapshot"), "snapshot")
            expected_size = snapshot.get("size_bytes")
            base_revision = snapshot.get("base_revision")
            expected_digest = snapshot.get("archive_digest")
            identity_digest = snapshot.get("identity_digest")
            token = snapshot.get("token")
            url = _https_url(snapshot.get("url"))
            if (
                type(expected_size) is not int
                or not 1 <= expected_size <= self._archive_limit
                or type(base_revision) is not int
                or base_revision < 1
                or not _valid_digest(expected_digest)
                or not _valid_digest(identity_digest)
                or not isinstance(token, str)
                or not 16 <= len(token) <= 512
            ):
                raise DomainError(ErrorCode.TRANSFER_INVALID)
            context.update(
                {
                    "engine": profile.engine,
                    "identity_digest": identity_digest,
                    "base_revision": base_revision,
                    "memory_budget_bytes": budget,
                    "data_dir": str(staged_path),
                }
            )
            self.database.put_run_context(ref.run_id, context)
            archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            headers = self._transfer_headers(token, generation)
            await self._download_snapshot(url, headers, archive, expected_size, expected_digest)
            try:
                from .profile_store import unpack_browser_data

                unpacked = await asyncio.to_thread(
                    unpack_browser_data,
                    archive,
                    staged_path,
                    expected_digest,
                    profile.engine,
                    identity_digest,
                    min_free_bytes=self.staging_reserve_bytes,
                )
                if unpacked.identity_digest != identity_digest:
                    raise DomainError(ErrorCode.TRANSFER_INVALID)
            except BaseException:
                with contextlib.suppress(OSError):
                    shutil.rmtree(staged_path)
                raise
            finally:
                archive.unlink(missing_ok=True)
            self.database.put_run_context(ref.run_id, context)
            self._profile_by_run[ref.run_id] = profile
            state = await self.runtime.start(
                profile,
                ref,
                on_ready=lambda: self._browser_ready(ref),
                on_stop=lambda: self._browser_exited(ref),
                timeout=self.startup_timeout,
            )
            return {"state": state}
        except asyncio.CancelledError:
            await asyncio.shield(self._finish_failed_start(ref))
            raise
        except BaseException:
            await self._finish_failed_start(ref)
            raise

    async def _finish_failed_start(self, ref: BrowserRunRefDTO) -> None:
        run = self.runtime.ledger.get(ref)
        if (
            run is not None
            and run.status is RunStatus.RESERVED
            and self.runtime.ledger.cancel_unclaimed(ref)
        ):
            context = self.database.run_context(ref.run_id) or {}
            proof = _no_process_proof(self.worker_id, self.boot_id, context, ref)
            self.database.record_stop_proof(ref.run_id, proof)
            self._remove_run_data(ref, context)
            self._emit_event({"type": "stopped", "proof": proof})
            return
        try:
            await self._finalize_stopped(ref, reason="loss", event=True)
        except Exception:
            # No proof is sent while a supervisor or namespace remains live or
            # unknown. Master keeps the run blocked for recovery or host fencing.
            return

    def _check_local_capacity(self, engine: str, memory_budget: int) -> None:
        if engine not in self.manifest.ready_engines:
            raise DomainError(ErrorCode.RUNTIME_INCOMPATIBLE)
        metrics = self._metrics()
        headroom = metrics.mem_available_bytes
        if metrics.cgroup_headroom_bytes is not None:
            headroom = min(headroom, metrics.cgroup_headroom_bytes)
        if headroom - self.memory_reserve_bytes < memory_budget:
            raise DomainError(ErrorCode.NO_WORKER_MEMORY)
        if metrics.staging_free_bytes - self.staging_reserve_bytes <= 0:
            raise DomainError(ErrorCode.TRANSFER_INVALID)

    def _metrics(self) -> NodeMetrics:
        return read_local_metrics(self.state_dir, self.manifest.ready_engines)

    async def _download_snapshot(
        self, url, headers, destination, expected_size, expected_digest
    ) -> None:
        hasher = hashlib.sha256()
        count = 0
        complete = False
        if destination.exists() or destination.is_symlink():
            raise DomainError(ErrorCode.TRANSFER_CONFLICT)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            async with self.session.get(
                url,
                headers=headers,
                timeout=self._request_timeout(),
                allow_redirects=False,
                max_line_size=8190,
                max_field_size=8190,
            ) as response:
                if response.status != 200:
                    raise DomainError(ErrorCode.TRANSFER_INVALID)
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) != expected_size:
                    raise DomainError(ErrorCode.TRANSFER_INVALID)
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        count += len(chunk)
                        if count > expected_size or count > self._archive_limit:
                            raise DomainError(ErrorCode.TRANSFER_INVALID)
                        hasher.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if count != expected_size or hasher.hexdigest() != expected_digest:
                raise DomainError(ErrorCode.TRANSFER_INVALID)
            complete = True
        except (OSError, ValueError) as error:
            raise DomainError(ErrorCode.TRANSFER_INVALID) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not complete:
                destination.unlink(missing_ok=True)

    async def _stop_command(self, ref: BrowserRunRefDTO, payload: dict[str, Any]) -> dict[str, Any]:
        lock = self._run_locks.setdefault(ref.run_id, asyncio.Lock())
        async with lock:
            return await self._stop_command_serialized(ref, payload)

    async def _stop_command_serialized(
        self, ref: BrowserRunRefDTO, payload: dict[str, Any]
    ) -> dict[str, Any]:
        timeout = payload.get("timeout", 30.0)
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 <= timeout <= 60
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        context = self.database.run_context(ref.run_id)
        if context is None or context.get("profile_id") != ref.profile_id:
            raise DomainError(ErrorCode.STALE_RUN)
        proof = self._proof_for_run(ref.run_id)
        if proof is None:
            if not await self._close_manual_for_run(ref, float(timeout)):
                raise DomainError(ErrorCode.RUN_UNCERTAIN)
            stopped = await asyncio.to_thread(self.runtime.stop, ref, float(timeout))
            if not stopped or not await asyncio.to_thread(self.runtime.tree_is_dead, ref):
                raise DomainError(ErrorCode.RUN_UNCERTAIN)
            proof = await self._finalize_stopped(ref, reason="clean", event=False)
        if proof.get("reason") == "clean" and not _valid_digest(proof.get("archive_digest")):
            raise DomainError(ErrorCode.SYNC_FAILED)
        return {"stopped": True, "proof": proof}

    async def _finalize_stopped(
        self, ref: BrowserRunRefDTO, *, reason: str, event: bool
    ) -> dict[str, Any]:
        lock = self._finalize_locks.setdefault(ref.run_id, asyncio.Lock())
        async with lock:
            # An explicit stop and BrowserLauncher callback can arrive at the
            # same time. Recheck the durable proof while serialized so only one
            # immutable archive/proof is produced for this run generation.
            existing = self._proof_for_run(ref.run_id)
            if existing is not None:
                if event:
                    self._emit_event({"type": "stopped", "proof": existing})
                return existing
            return await self._finalize_stopped_locked(ref, reason=reason, event=event)

    async def _finalize_stopped_locked(
        self, ref: BrowserRunRefDTO, *, reason: str, event: bool
    ) -> dict[str, Any]:
        context = self.database.run_context(ref.run_id)
        if context is None or context.get("profile_id") != ref.profile_id:
            raise DomainError(ErrorCode.STALE_RUN)
        existing = self._proof_for_run(ref.run_id)
        if existing is not None:
            if event:
                self._emit_event({"type": "stopped", "proof": existing})
            return existing
        if not await asyncio.to_thread(self.runtime.tree_is_dead, ref):
            raise DomainError(ErrorCode.RUN_UNCERTAIN)
        manual_ok = await self._close_manual_for_run(ref, 5.0)
        proof = {
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "worker_id": context["worker_id"],
            "worker_boot_id": context["worker_boot_id"],
            "connection_generation": context["connection_generation"],
            "terminated": True,
            "process_tree_dead": True,
            "manual_helpers_stopped": manual_ok,
            "reason": reason,
        }
        if not manual_ok and reason == "clean":
            reason = "loss"
            proof["reason"] = reason
        archive_path = self.state_dir / "sync" / f"{ref.run_id}.zip"
        if reason == "clean":
            try:
                from .profile_store import pack_browser_data

                archive_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if archive_path.exists() or archive_path.is_symlink():
                    raise DomainError(ErrorCode.SYNC_FAILED)
                info = await asyncio.to_thread(
                    pack_browser_data,
                    context["data_dir"],
                    context["engine"],
                    archive_path,
                )
                if info.identity_digest != context["identity_digest"]:
                    raise DomainError(ErrorCode.TRANSFER_INVALID)
                proof["archive_digest"] = info.archive_digest
                proof["archive_size_bytes"] = info.size_bytes
                context = (
                    self.database.update_run_context(
                        ref.run_id,
                        archive_path=str(archive_path),
                        archive_digest=info.archive_digest,
                        archive_size_bytes=info.size_bytes,
                        stopped=True,
                    )
                    or context
                )
            except Exception:
                reason = "loss"
                proof["reason"] = reason
                with contextlib.suppress(OSError):
                    archive_path.unlink()
        self.database.record_stop_proof(ref.run_id, proof, context=context)
        proof = self._proof_for_run(ref.run_id) or proof
        if event:
            self._emit_event({"type": "stopped", "proof": proof})
        return proof

    def _remove_run_data(self, ref: BrowserRunRefDTO, context: dict[str, Any]) -> bool:
        expected = self.runtime.profile_directory(ref.run_id)
        try:
            candidate = Path(str(context.get("data_dir", expected)))
            if candidate != expected or candidate.is_symlink():
                return False
            shutil.rmtree(candidate, ignore_errors=True)
            return not candidate.exists()
        except (OSError, TypeError, ValueError):
            return False

    def _proof_for_run(self, run_id: str) -> dict[str, Any] | None:
        for proof in self.database.stopped_proofs():
            if proof.get("run_id") == run_id:
                return proof
        return None

    async def _sync_command(self, ref: BrowserRunRefDTO, payload: dict[str, Any]) -> dict[str, Any]:
        context = self.database.run_context(ref.run_id)
        proof = self._proof_for_run(ref.run_id)
        if (
            context is None
            or context.get("profile_id") != ref.profile_id
            or proof is None
            or proof.get("reason") != "clean"
            or not _valid_digest(proof.get("archive_digest"))
        ):
            raise DomainError(ErrorCode.SYNC_FAILED)
        upload = _strict_object(payload.get("upload"), "upload")
        url = _https_url(upload.get("url"))
        token = upload.get("token")
        base_revision = upload.get("base_revision")
        supplied_digest = upload.get("archive_digest")
        if (
            not isinstance(token, str)
            or not 16 <= len(token) <= 512
            or type(base_revision) is not int
            or base_revision != context.get("base_revision")
            or supplied_digest != proof["archive_digest"]
        ):
            raise DomainError(ErrorCode.TRANSFER_INVALID)
        if context.get("sync_completed") is True:
            if (
                context.get("sync_archive_digest") != proof["archive_digest"]
                or context.get("sync_base_revision") != base_revision
                or context.get("sync_worker_id") != self.worker_id
                or context.get("sync_worker_boot_id") != self.boot_id
                or not isinstance(context.get("sync_receipt"), dict)
            ):
                raise DomainError(ErrorCode.TRANSFER_CONFLICT)
            return {"receipt": context["sync_receipt"]}
        archive_path = Path(str(context.get("archive_path", "")))
        expected_path = self.state_dir / "sync" / f"{ref.run_id}.zip"
        if (
            archive_path != expected_path
            or archive_path.is_symlink()
            or not archive_path.is_file()
            or archive_path.stat().st_size != proof.get("archive_size_bytes")
            or _file_digest(archive_path) != proof["archive_digest"]
        ):
            raise DomainError(ErrorCode.TRANSFER_INVALID)
        headers = self._transfer_headers(token, int(context["connection_generation"]))
        headers.update(
            {
                "Content-Type": "application/zip",
                "Content-Length": str(archive_path.stat().st_size),
                "X-Archive-Digest": str(proof["archive_digest"]),
            }
        )
        try:
            with archive_path.open("rb") as archive_stream:
                async with self.session.post(
                    url,
                    data=archive_stream,
                    headers=headers,
                    timeout=self._request_timeout(120),
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise DomainError(ErrorCode.SYNC_FAILED)
                    raw_body = await response.content.read(1024 * 1024 + 1)
                    if len(raw_body) > 1024 * 1024:
                        raise DomainError(ErrorCode.SYNC_FAILED)
                    body = json.loads(raw_body)
                    if not isinstance(body, dict):
                        raise DomainError(ErrorCode.SYNC_FAILED)
        except DomainError:
            raise
        except Exception as error:
            raise DomainError(ErrorCode.SYNC_FAILED) from error
        self.database.update_run_context(
            ref.run_id,
            sync_receipt=body,
            sync_completed=True,
            sync_archive_digest=proof["archive_digest"],
            sync_base_revision=base_revision,
            sync_worker_id=self.worker_id,
            sync_worker_boot_id=self.boot_id,
        )
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)
        data_dir = Path(str(context.get("data_dir", "")))
        if data_dir == self.runtime.profile_directory(ref.run_id) and not data_dir.is_symlink():
            with contextlib.suppress(OSError):
                shutil.rmtree(data_dir)
        return {"receipt": body}

    def _cleanup_command(self, ref: BrowserRunRefDTO, payload: dict[str, Any]) -> dict[str, Any]:
        context = self.database.run_context(ref.run_id)
        proof = self._proof_for_run(ref.run_id)
        submitted_proof = payload.get("proof")
        disposition = payload.get("disposition")
        if (
            context is None
            or context.get("profile_id") != ref.profile_id
            or proof is None
            or submitted_proof != proof
            or disposition != "closed"
            or proof.get("profile_id") != ref.profile_id
            or proof.get("run_id") != ref.run_id
            or proof.get("worker_id") != self.worker_id
            or context.get("worker_id") != self.worker_id
            or proof.get("worker_boot_id") != context.get("worker_boot_id")
            or proof.get("connection_generation") != context.get("connection_generation")
            or proof.get("terminated") is not True
            or proof.get("process_tree_dead") is not True
            or proof.get("manual_helpers_stopped") is not True
        ):
            raise DomainError(ErrorCode.STALE_RUN)
        run = self.runtime.ledger.get(ref)
        if (
            run is None
            or run.status not in {RunStatus.CANCELLED, RunStatus.CLOSED}
            or not self.runtime.tree_is_dead(ref)
        ):
            raise DomainError(ErrorCode.RUN_UNCERTAIN)
        if not self._remove_run_data(ref, context):
            raise DomainError(ErrorCode.TRANSFER_INVALID)
        for archive in (
            self.state_dir / "transfers" / f"{ref.run_id}.zip",
            self.state_dir / "sync" / f"{ref.run_id}.zip",
        ):
            if archive.is_symlink():
                raise DomainError(ErrorCode.TRANSFER_INVALID)
            with contextlib.suppress(FileNotFoundError):
                archive.unlink()
        if not self.database.acknowledge_terminal_run(ref.run_id):
            raise DomainError(ErrorCode.STALE_RUN)
        return {"cleaned": True, "disposition": disposition}

    async def _control_command(self, ref: BrowserRunRefDTO, payload: dict[str, Any]):
        action = payload.get("action")
        params = payload.get("params", {})
        timeout = payload.get("timeout", 60.0)
        if (
            not isinstance(action, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", action)
            or not isinstance(params, dict)
            or len(json.dumps(params, separators=(",", ":")).encode()) > MAX_CONTROL_PARAMS_BYTES
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        run = self.runtime.ledger.get(ref)
        if run is None or run.status is not RunStatus.RUNNING:
            raise DomainError(ErrorCode.STALE_RUN)
        result = await asyncio.to_thread(self.runtime.control, ref, action, params, float(timeout))
        if (
            len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode())
            > MAX_MESSAGE_BYTES
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        return result

    def _describe_command(self, ref: BrowserRunRefDTO) -> dict[str, Any]:
        descriptor = self.runtime.descriptor(ref)
        if descriptor is None:
            raise DomainError(ErrorCode.STALE_BROWSER_RUN)
        return {"descriptor": asdict(descriptor)}

    def _browser_ready(self, ref: BrowserRunRefDTO) -> None:
        self._emit_event({"type": "event", "event": "running", **asdict(ref)})

    def _browser_exited(self, ref: BrowserRunRefDTO) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(
            self._finalize_stopped(ref, reason="clean", event=True), loop
        )
        # callback thread must not block BrowserLauncher monitor thread
        future.add_done_callback(
            lambda completed: completed.exception() if not completed.cancelled() else None
        )

    def _emit_event(self, message: dict[str, Any]) -> None:
        loop, queue = self._loop, self._events
        if loop is None or queue is None or loop.is_closed():
            return

        def enqueue() -> None:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                return

        loop.call_soon_threadsafe(enqueue)

    async def _stop_command_event(self, websocket, generation, message):
        await self._send_json(websocket, message)

    async def _heartbeat_loop(self, websocket, generation: int) -> None:
        while self.connection_generation == generation:
            await asyncio.sleep(self.heartbeat_interval)
            metrics = asdict(self._metrics())
            payload = {
                "type": "heartbeat",
                "worker_id": self.worker_id,
                "boot_id": self.boot_id,
                "connection_generation": generation,
                "metrics": metrics,
                "active_runs": [
                    {"profile_id": row["profile_id"], "run_id": row["run_id"]}
                    for row in self.database.active_runs()
                ],
                "stopped_runs": list(self.database.stopped_proofs(include_acknowledged=False)),
            }
            if len(json.dumps(payload, separators=(",", ":")).encode()) > MAX_MESSAGE_BYTES:
                raise ConnectionError("Worker heartbeat exceeds protocol limit")
            await self._send_json(websocket, payload)

    async def _event_loop(self, websocket, generation: int) -> None:
        assert self._events is not None
        while self.connection_generation == generation:
            event = await self._events.get()
            if event.get("type") not in {"event", "stopped"}:
                continue
            event.setdefault("worker_id", self.worker_id)
            event.setdefault("boot_id", self.boot_id)
            event.setdefault("connection_generation", generation)
            await self._send_json(websocket, event)

    async def _send_result(
        self, websocket, generation, request, *, ok, result=None, error_code=None
    ):
        await self._send_json(
            websocket,
            self._result_message(request, generation, ok=ok, result=result, error_code=error_code),
        )

    def _result_message(self, request, generation, *, ok, result=None, error_code=None):
        response = {
            "type": "result",
            "command_id": request["command_id"],
            "worker_id": self.worker_id,
            "boot_id": self.boot_id,
            "connection_generation": generation,
            "profile_id": request["profile_id"],
            "run_id": request["run_id"],
            "ok": bool(ok),
        }
        if ok:
            response["result"] = {} if result is None else result
        else:
            response["error_code"] = error_code or ErrorCode.WORKER_UNAVAILABLE.value
        return response

    async def _send_json(self, websocket, message) -> None:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode()) > MAX_MESSAGE_BYTES:
            raise ConnectionError("Worker response exceeds protocol limit")
        lock = self._send_lock
        if lock is None:
            raise ConnectionError("Worker transport is closed")
        async with lock:
            await websocket.send_str(encoded)

    def _cache_result(self, command_id: str, signature: str, result: dict[str, Any]) -> None:
        size = len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode())
        if size > MAX_COMMAND_CACHE_BYTES:
            return
        previous = self._command_cache.pop(command_id, None)
        if previous is not None:
            self._command_cache_bytes -= previous[2]
        self._command_cache[command_id] = (signature, result, size)
        self._command_cache_bytes += size
        self._command_cache.move_to_end(command_id)
        while (
            len(self._command_cache) > MAX_COMMAND_CACHE
            or self._command_cache_bytes > MAX_COMMAND_CACHE_BYTES
        ):
            _old_id, (_old_signature, _old_result, old_size) = self._command_cache.popitem(
                last=False
            )
            self._command_cache_bytes -= old_size

    def _transfer_headers(self, token: str, generation: int) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "X-Worker-ID": self.worker_id,
            "X-Worker-Boot-ID": self.boot_id,
            "X-Connection-Generation": str(generation),
        }

    def _request_timeout(self, total: float | None = None):
        import aiohttp

        return aiohttp.ClientTimeout(
            total=total or self._http_timeout, connect=10, sock_read=self._http_timeout
        )

    async def _close_manual_for_run(self, ref: BrowserRunRefDTO, timeout: float) -> bool:
        context = self.database.run_context(ref.run_id) or {}
        persisted = context.get("manual_sessions", {})
        if not isinstance(persisted, dict):
            persisted = {}
        with self._manual_lock:
            gates = {gate.session_id: gate for gate in self._manual.values() if gate.run == ref}
        session_ids = set(persisted) | set(gates)
        success = True
        remaining = dict(persisted)
        for session_id in session_ids:
            gate = gates.get(session_id)
            helper = gate.helper if gate is not None else None
            if gate is not None:
                gate.closing = True
                gate.activate.set()
                if gate.relay_task is not None:
                    gate.relay_task.cancel()
                    await asyncio.gather(gate.relay_task, return_exceptions=True)
            stored = persisted.get(session_id)
            if helper is None and isinstance(stored, dict):
                try:
                    helper = ProcessIdentity(stored["pid"], stored["start_ticks"])
                except (KeyError, TypeError, ValueError):
                    helper = None
            if self._manual_backend is None or helper is None:
                success = False
                continue
            if gate is not None and gate.backend_session is not None:
                with contextlib.suppress(Exception):
                    self._manual_backend.release_input(session_id)
                with contextlib.suppress(Exception):
                    self._manual_backend.revoke(session_id)
            stored_deadline = stored.get("cleanup_deadline") if isinstance(stored, dict) else None
            deadline = time.time() + max(0.1, timeout)
            if isinstance(stored_deadline, (int, float)) and math.isfinite(stored_deadline):
                deadline = min(deadline, float(stored_deadline))
            if gate is not None:
                deadline = min(deadline, gate.cleanup_deadline)
            await asyncio.to_thread(
                self._manual_backend.stop,
                session_id,
                current_host_id(),
                helper,
                deadline,
            )
            liveness = await asyncio.to_thread(
                self._manual_backend.probe,
                session_id,
                current_host_id(),
                helper,
            )
            if str(getattr(liveness, "value", liveness)) != "dead":
                success = False
            else:
                self._manual.pop(session_id, None)
                remaining.pop(session_id, None)
        self.database.update_run_context(ref.run_id, manual_sessions=remaining)
        return success

    async def _manual_open_command(self, ref: BrowserRunRefDTO, payload: dict[str, Any]):
        if self._manual_backend is None:
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not 8 <= len(session_id) <= 128:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        context = self.database.run_context(ref.run_id)
        profile = self._profile_by_run.get(ref.run_id)
        descriptor = self.runtime.descriptor(ref)
        if (
            context is None
            or context.get("profile_id") != ref.profile_id
            or profile is None
            or profile.engine != "camoufox"
            or descriptor is None
            or any(
                value is None
                for value in (
                    descriptor.browser,
                    descriptor.display,
                    descriptor.namespace,
                    descriptor.tmp_path,
                    descriptor.xauthority_path,
                )
            )
        ):
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        relay = _strict_object(payload.get("relay"), "relay")
        relay_url = _https_url(relay.get("url"), web_socket=True)
        relay_token = relay.get("token")
        if not isinstance(relay_token, str) or not 16 <= len(relay_token) <= 512:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        expires_at = _finite_deadline(payload.get("expires_at"))
        cleanup_deadline = _finite_deadline(payload.get("cleanup_deadline"))
        watchdog_deadline = _finite_deadline(payload.get("watchdog_deadline"))
        if not (time.time() < min(expires_at, cleanup_deadline, watchdog_deadline)):
            raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
        gate = _ManualGate(
            session_id,
            ref,
            descriptor,
            {"url": relay_url, "token": relay_token},
            expires_at,
            cleanup_deadline,
            watchdog_deadline,
        )
        with self._manual_lock:
            if session_id in self._manual:
                raise DomainError(ErrorCode.MANUAL_SESSION_BUSY)
            self._manual[session_id] = gate

        def claim_helper(helper: ProcessIdentity) -> bool:
            gate.helper = helper
            receipt = self._manual_backend._receipt_path(session_id)
            current = self.database.run_context(ref.run_id) or {}
            sessions = dict(current.get("manual_sessions", {}))
            sessions[session_id] = {
                "pid": helper.pid,
                "start_ticks": helper.start_ticks,
                "receipt_path": str(receipt),
                "cleanup_deadline": cleanup_deadline,
            }
            self.database.update_run_context(ref.run_id, manual_sessions=sessions)
            gate.prepared.set()
            if not gate.activate.wait(timeout=max(0.1, min(60, cleanup_deadline - time.time()))):
                return False
            return not gate.closing and gate.helper == helper

        def run_backend() -> None:
            try:
                gate.backend_session = self._manual_backend.start(
                    session_id,
                    descriptor,
                    claim_helper,
                    expires_at=expires_at,
                    cleanup_deadline=cleanup_deadline,
                    watchdog_deadline=watchdog_deadline,
                )
            except BaseException as error:
                gate.error = error
            finally:
                gate.done.set()

        thread = threading.Thread(target=run_backend, name="worker-manual-open", daemon=True)
        thread.start()
        prepared = await asyncio.to_thread(
            gate.prepared.wait, min(10, max(0.1, cleanup_deadline - time.time()))
        )
        if not prepared or gate.helper is None:
            gate.closing = True
            gate.activate.set()
            await asyncio.to_thread(gate.done.wait, 5)
            self._manual.pop(session_id, None)
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        return {"helper": asdict(gate.helper)}

    async def _manual_activate_command(self, ref: BrowserRunRefDTO, payload: dict[str, Any]):
        session_id = payload.get("session_id")
        gate = self._get_manual_gate(session_id, ref)
        gate.activate.set()
        completed = await asyncio.to_thread(gate.done.wait, 12)
        if not completed or gate.error is not None or gate.backend_session is None:
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        gate.relay_task = asyncio.create_task(self._relay_manual(gate))
        # Let the outbound relay complete its authenticated handshake.
        await asyncio.sleep(0)
        if gate.relay_task.done():
            await gate.relay_task
            raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
        return {"opened": True}

    async def _manual_command(self, operation: str, ref: BrowserRunRefDTO, payload: dict[str, Any]):
        if self._manual_backend is None:
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        session_id = payload.get("session_id")
        if not isinstance(session_id, str):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        gate = self._manual.get(session_id)
        if gate is not None and gate.run != ref:
            raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
        if operation == "manual_heartbeat":
            gate = self._get_manual_gate(session_id, ref)
            deadline = _finite_deadline(payload.get("watchdog_deadline"))
            await asyncio.to_thread(self._manual_backend.heartbeat, session_id, deadline)
            return {"renewed": True}
        if operation == "manual_release":
            gate = self._get_manual_gate(session_id, ref)
            await asyncio.to_thread(self._manual_backend.release_input, session_id)
            return {"released": True}
        if operation == "manual_close":
            cleanup_deadline = _finite_deadline(payload.get("cleanup_deadline"))
            gate = self._manual.get(session_id)
            if gate is not None:
                gate.closing = True
                gate.activate.set()
                if gate.relay_task is not None:
                    gate.relay_task.cancel()
                    await asyncio.gather(gate.relay_task, return_exceptions=True)
            await self._close_manual_for_run(ref, max(0.1, cleanup_deadline - time.time()))
            probe = await asyncio.to_thread(
                self._manual_backend.probe,
                session_id,
                current_host_id(),
                gate.helper if gate is not None else self._manual_helper(ref, session_id),
            )
            stopped = str(getattr(probe, "value", probe)) == "dead"
            return {"stopped": stopped}
        if operation == "manual_probe":
            gate = self._manual.get(session_id)
            probe = await asyncio.to_thread(
                self._manual_backend.probe,
                session_id,
                current_host_id(),
                gate.helper if gate is not None else self._manual_helper(ref, session_id),
            )
            return {"liveness": str(getattr(probe, "value", probe))}
        raise DomainError(ErrorCode.VALIDATION_ERROR)

    def _get_manual_gate(self, session_id, ref) -> _ManualGate:
        if not isinstance(session_id, str):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with self._manual_lock:
            gate = self._manual.get(session_id)
        if gate is None or gate.run != ref or gate.closing:
            raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
        return gate

    def _manual_helper(self, ref: BrowserRunRefDTO, session_id: str) -> ProcessIdentity | None:
        context = self.database.run_context(ref.run_id) or {}
        sessions = context.get("manual_sessions", {})
        stored = sessions.get(session_id) if isinstance(sessions, dict) else None
        if not isinstance(stored, dict):
            return None
        try:
            return ProcessIdentity(stored["pid"], stored["start_ticks"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _relay_manual(self, gate: _ManualGate) -> None:
        import aiohttp

        channel = gate.backend_session.channel
        sock = getattr(channel, "socket", None)
        if sock is None:
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        sock.setblocking(False)
        try:
            async with self.session.ws_connect(
                gate.relay["url"],
                headers={"Authorization": f"Bearer {gate.relay['token']}"},
                heartbeat=10,
                receive_timeout=60,
                max_msg_size=65536,
                autoping=True,
                autoclose=True,
            ) as websocket:
                loop = asyncio.get_running_loop()

                async def send_rfb():
                    while not gate.closing:
                        data = await loop.sock_recv(sock, 65536)
                        if not data:
                            return
                        await websocket.send_bytes(data)

                async def receive_rfb():
                    async for frame in websocket:
                        if frame.type == aiohttp.WSMsgType.BINARY:
                            data = frame.data
                            if len(data) > 65536:
                                raise DomainError(ErrorCode.VALIDATION_ERROR)
                            await loop.sock_sendall(sock, data)
                        elif frame.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                            return
                        elif frame.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError("Manual relay disconnected")
                        else:
                            raise DomainError(ErrorCode.VALIDATION_ERROR)

                readers = [asyncio.create_task(send_rfb()), asyncio.create_task(receive_rfb())]
                try:
                    await asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for reader in readers:
                        reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
        finally:
            if self._manual_backend is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._manual_backend.release_input, gate.session_id)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._manual_backend.revoke, gate.session_id)
            if not gate.closing:
                gate.closing = True
                with contextlib.suppress(Exception):
                    await self._close_manual_for_run(gate.run, 5)

    def _stop_manual_relays(self) -> None:
        with self._manual_lock:
            gates = tuple(self._manual.values())
        for gate in gates:
            gate.closing = True
            gate.activate.set()
            if gate.relay_task is not None:
                gate.relay_task.cancel()

    async def _close_all_manual_sessions(self) -> None:
        refs = {gate.run for gate in self._manual.values()}
        for run in self.database.active_runs():
            refs.add(BrowserRunRefDTO(run["profile_id"], run["run_id"]))
        for ref in refs:
            with contextlib.suppress(Exception):
                await self._close_manual_for_run(ref, 5)


def _profile_from_payload(value: object, profile_id: str, run_id: str) -> Profile:
    profile = _strict_object(value, "profile")
    supplied_id = profile.get("profile_id")
    original_name = profile.get("name")
    engine = profile.get("engine", "camoufox")
    proxy = profile.get("proxy")
    os_type = profile.get("os_type", "windows")
    timezone = profile.get("timezone")
    locale = profile.get("locale")
    if (
        supplied_id != profile_id
        or not isinstance(original_name, str)
        or not original_name.strip()
        or engine not in ENGINES
        or not isinstance(os_type, str)
        or os_type not in {"windows", "macos", "linux"}
        or (
            proxy is not None
            and (not isinstance(proxy, str) or not validate_proxy_format(proxy)[0])
        )
        or (
            timezone is not None
            and (not isinstance(timezone, str) or not validate_timezone(timezone)[0])
        )
        or (locale is not None and (not isinstance(locale, str) or not validate_locale(locale)[0]))
        or not _RUN_ID.fullmatch(run_id)
    ):
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    # The run ID is a private per-run data-directory alias. The canonical
    # profile_id remains unchanged for identity and Master authorization.
    return Profile(run_id, proxy, os_type, timezone, locale, engine, profile_id)


def _request_signature(message: dict[str, Any]) -> str:
    data = json.dumps(
        {
            key: message.get(key)
            for key in (
                "worker_id",
                "boot_id",
                "connection_generation",
                "profile_id",
                "run_id",
                "operation",
                "payload",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))


def _no_process_proof(
    worker_id: str,
    boot_id: str,
    context: dict[str, Any],
    ref: BrowserRunRefDTO,
) -> dict[str, Any]:
    generation = context.get("connection_generation")
    if type(generation) is not int or generation < 1:
        raise ValueError("Master run generation is unavailable")
    return {
        "profile_id": ref.profile_id,
        "run_id": ref.run_id,
        "worker_id": worker_id,
        "worker_boot_id": boot_id,
        "connection_generation": generation,
        "terminated": True,
        "process_tree_dead": True,
        "manual_helpers_stopped": True,
        "reason": "loss",
        "shutdown_kind": "pre-spawn",
    }


def _finite_deadline(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    return float(value)


def _systemd_notify(message: str) -> None:
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


def _load_manifest_and_agent() -> WorkerAgent:
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not credentials_dir:
        raise RuntimeError("systemd Worker credentials are unavailable")
    credential = read_worker_credential(Path(credentials_dir) / "worker-token")
    worker_id = os.environ.get("FOXPROFILE_WORKER_ID", "")
    master_url = os.environ.get("FOXPROFILE_WORKER_MASTER_URL", "")
    state_dir = secure_worker_directory(
        os.environ.get("FOXPROFILE_WORKER_STATE_DIR", "/var/lib/foxprofile-worker")
    )
    manifest_path = os.environ.get("FOXPROFILE_WORKER_MANIFEST", "")
    evidence_path = os.environ.get("FOXPROFILE_WORKER_NATIVE_EVIDENCE", "")
    expected_digest = os.environ.get("FOXPROFILE_WORKER_EXPECTED_MANIFEST_SHA256", "")
    if not manifest_path or not evidence_path:
        raise RuntimeError("Worker runtime manifest and native evidence are required")
    manifest = load_manifest(manifest_path, evidence_path, expected_digest)
    database = WorkerExecutionDatabase(state_dir / "runtime.sqlite3")
    watchdog = WorkerWatchdog(
        database,
        loss_timeout=float(os.environ.get("FOXPROFILE_WORKER_LOSS_TIMEOUT", "30")),
        term_grace=float(os.environ.get("FOXPROFILE_WORKER_TERM_GRACE", "2")),
        kill_grace=float(os.environ.get("FOXPROFILE_WORKER_KILL_GRACE", "2")),
        parent_pid=os.getpid(),
    )
    watchdog.start()
    _systemd_notify("READY=1\nSTATUS=Connecting to Master")
    runtime = WorkerRuntime(database, state_dir, manifest)
    manual_backend = _configured_manual_backend(state_dir, runtime)
    agent = WorkerAgent(
        worker_id=worker_id,
        credential=credential,
        master_url=master_url,
        state_dir=state_dir,
        database=database,
        manifest=manifest,
        runtime=runtime,
        watchdog=watchdog,
        manual_backend=manual_backend,
        loss_timeout=float(os.environ.get("FOXPROFILE_WORKER_LOSS_TIMEOUT", "30")),
        memory_reserve_bytes=int(
            os.environ.get("FOXPROFILE_WORKER_SYSTEM_RESERVE_BYTES", "536870912")
        ),
        staging_reserve_bytes=int(
            os.environ.get("FOXPROFILE_WORKER_STAGING_RESERVE_BYTES", str(2 * 1024**3))
        ),
    )
    return agent


def _configured_manual_backend(state_dir: Path, runtime: WorkerRuntime) -> PrivateVNCBackend | None:
    executable = os.environ.get("FOXPROFILE_X11VNC_EXECUTABLE", "")
    digest = os.environ.get("FOXPROFILE_X11VNC_SHA256", "")
    if not executable or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return None

    def confinement_for(descriptor):
        run_id = descriptor.run.run_id
        return ConfinementSpec(
            profile_path=runtime.profile_directory(run_id),
            private_tmp_path=Path(descriptor.tmp_path),
            code_root=runtime.code_root,
            runtime_paths=tuple(
                Path(item)
                for item in (
                    "/usr",
                    "/bin",
                    "/lib",
                    "/lib64",
                    sys.prefix,
                    sys.base_prefix,
                    executable,
                )
                if Path(item).exists()
            ),
            purpose=ConfinementPurpose.VNC_HELPER,
        )

    return PrivateVNCBackend(
        state_root=state_dir / "manual",
        confinement_for=confinement_for,
        executable=executable,
        executable_sha256=digest,
    )


async def _run_cli() -> int:
    agent = _load_manifest_and_agent()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    task = asyncio.create_task(agent.run())
    await stop.wait()
    await agent.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Outbound FoxProfile browser Worker")
    parser.parse_args()
    try:
        return asyncio.run(_run_cli())
    except (ValueError, RuntimeError, OSError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
