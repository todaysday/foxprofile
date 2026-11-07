"""Host-owned launch, recovery, stop, and control operations for browser runs."""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from src.models.profile import Profile
from src.services.browser.control import TOKEN_HEADER
from src.services.browser.process import helper_command, runner_command

from ..capacity import CapacityService
from ..dto import BrowserRunRefDTO, PrincipalDTO, ProcessIdentity, ProfileRefDTO, RuntimeDescriptor
from ..errors import DomainError, ErrorCode
from ..locking import catalog_lock
from ..request_context import current_principal
from ..storage.capacity import CapacityLedger, ProcessLiveness, RunStatus
from ..storage.database import ExtensionDatabase
from .confinement import build_confined_environment
from .registry import RuntimeRegistry, current_host_id, probe_process, process_identity


class RuntimeSupervisor:
    """Start one durable supervisor process per reserved browser generation."""

    def __init__(
        self,
        database: ExtensionDatabase,
        *,
        data_dir: str | Path,
        code_root: str | Path,
        runtime_paths: Sequence[str | Path],
        settings: Mapping[str, str] | None = None,
        capacity: CapacityService | None = None,
        profile_by_name: Callable[[str], Profile] | None = None,
        authorize: Callable[[PrincipalDTO, ProfileRefDTO], PrincipalDTO] | None = None,
        lock_path: str | None = None,
        prepare_launch: Callable[[Profile], None] | None = None,
    ) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("Protected runtime requires Linux process namespaces")
        import fcntl

        self.database = database
        lock_pathname = database.path.with_suffix(database.path.suffix + ".runtime.lock")
        if lock_pathname.is_symlink():
            raise RuntimeError("Runtime manager lock is unavailable")
        self._manager_file = lock_pathname.open("a+b")
        os.chmod(lock_pathname, 0o600)
        try:
            fcntl.flock(self._manager_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._manager_file.close()
            raise RuntimeError(
                "Another lifecycle manager is already active for this installation"
            ) from None
        self.ledger = CapacityLedger(database)
        self.registry = RuntimeRegistry(database, self.ledger)
        self.host_id = current_host_id()
        self.data_dir = Path(data_dir).resolve()
        self.code_root = Path(code_root).resolve()
        self.runtime_paths = tuple(os.path.abspath(path) for path in runtime_paths)
        self.settings = dict(settings or {})
        self.capacity = capacity
        self.profile_by_name = profile_by_name
        self.authorize = authorize
        self.lock_path = lock_path
        self.prepare_launch = prepare_launch
        self._handles: dict[str, subprocess.Popen[str]] = {}
        self._owned_runs: dict[str, BrowserRunRefDTO] = {}
        self._prepared_runs: set[BrowserRunRefDTO] = set()
        self._lock = threading.RLock()
        self._closed = False

    def spawn_browser(self, profile: Profile, profile_id: str, run_id: str) -> object:
        """Return a Popen-compatible supervisor handle for BrowserLauncher."""
        return self._spawn(profile, profile_id, run_id, runner_command(profile))

    def prepare_browser(self, profile: Profile, profile_id: str, run_id: str) -> None:
        """Prepare one admitted browser launch before the catalog lock is reacquired."""
        if not profile_id or profile.profile_id != profile_id or not run_id:
            raise ValueError("profile and reserved run identity must match")
        ref = BrowserRunRefDTO(profile_id, run_id)
        self._require_reserved(ref)
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime lifecycle manager has shut down")
            self._owned_runs[run_id] = ref
        try:
            if self.prepare_launch is not None:
                self.prepare_launch(profile)
            self._require_reserved(ref)
            with self._lock:
                if self._closed:
                    raise RuntimeError("Runtime lifecycle manager has shut down")
                self._prepared_runs.add(ref)
        except BaseException:
            self.ledger.cancel_unclaimed(ref)
            self._forget_terminal_run(ref)
            raise

    def discard_prepared(self, ref: BrowserRunRefDTO) -> None:
        with self._lock:
            self._prepared_runs.discard(ref)
        self._forget_terminal_run(ref)

    def _spawn(
        self,
        profile: Profile,
        profile_id: str,
        run_id: str,
        command: list[str],
        *,
        helper_stdin: str | None = None,
    ) -> subprocess.Popen[str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime lifecycle manager has shut down")
        if not profile_id or profile.profile_id != profile_id or not run_id:
            raise ValueError("profile and reserved run identity must match")
        ref = BrowserRunRefDTO(profile_id, run_id)
        current = self.ledger.get(ref)
        if (
            current is None
            or current.host_id != self.host_id
            or current.status is not RunStatus.RESERVED
        ):
            raise RuntimeError("reserved browser run is unavailable")
        profile_path = self.data_dir / profile.name
        if (
            profile_path.is_symlink()
            or not profile_path.is_dir()
            or profile_path.resolve().parent != self.data_dir
        ):
            raise RuntimeError("profile data directory is unavailable")
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime lifecycle manager has shut down")
            # Shutdown must also cancel admitted runs still preparing to spawn.
            self._owned_runs[run_id] = ref
            prepared = helper_stdin is None and ref in self._prepared_runs
            self._prepared_runs.discard(ref)
        process = None
        try:
            if helper_stdin is None and self.prepare_launch is not None and not prepared:
                self.prepare_launch(profile)
            self._require_open_and_reserved(ref)
            temp_root = self.data_dir / ".foxprofile-runtime"
            if temp_root.is_symlink():
                raise RuntimeError("private runtime directory is unavailable")
            temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            temp_root.chmod(0o700)
            private_tmp = temp_root / run_id
            if private_tmp.is_symlink():
                raise RuntimeError("private runtime directory is unavailable")
            private_tmp.mkdir(mode=0o700, exist_ok=True)
            private_tmp.chmod(0o700)
            settings = {
                **self.settings,
                "FOXPROFILE_HEADLESS": "virtual",
                "FOXPROFILE_DATA_DIR": "/profile",
            }
            config = {
                "database_path": str(self.database.path),
                "host_id": self.host_id,
                "profile_id": profile_id,
                "run_id": run_id,
                "profile_path": str(profile_path),
                "private_tmp_path": str(private_tmp),
                "code_root": str(self.code_root),
                "runtime_paths": self.runtime_paths,
                "settings": settings,
                "command": command,
                "helper_stdin": helper_stdin,
                "engine": profile.engine,
            }
            with self._lock:
                if self._closed:
                    raise RuntimeError("Runtime lifecycle manager has shut down")
                process = subprocess.Popen(
                    [sys.executable, "-m", "foxprofile_ext.runtime.supervisor"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=self.code_root,
                    env={
                        **build_confined_environment(settings),
                        "PYTHONPATH": str(self.code_root),
                    },
                    start_new_session=True,
                )
                assert process.stdin is not None
                process.stdin.write(json.dumps(config, separators=(",", ":")))
                process.stdin.close()
                process.stdin = None
                self._handles[run_id] = process
            watcher = threading.Thread(target=self._watch, args=(ref, process), daemon=True)
            watcher.start()
            return process
        except BaseException:
            if process is None:
                self.ledger.cancel_unclaimed(ref)
                self._forget_terminal_run(ref)
            else:
                self._stop_without_watcher(process)
                if process.poll() is not None:
                    self._watch(ref, process)
            raise

    def _require_reserved(self, ref: BrowserRunRefDTO) -> None:
        run = self.ledger.get(ref)
        if run is None or run.host_id != self.host_id or run.status is not RunStatus.RESERVED:
            raise RuntimeError("reserved browser run is unavailable")

    def _require_open_and_reserved(self, ref: BrowserRunRefDTO) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime lifecycle manager has shut down")
        self._require_reserved(ref)

    def _stop_without_watcher(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return

    def request_stop(self, profile_id: str, run_id: str, timeout: float) -> bool:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            return False
        ref = BrowserRunRefDTO(profile_id, run_id)
        run = self.ledger.get(ref)
        if run is None or run.host_id != self.host_id:
            return False
        if run.status in {RunStatus.CANCELLED, RunStatus.CLOSED}:
            self._forget_terminal_run(ref)
            return True
        if run.status not in {
            RunStatus.STARTING,
            RunStatus.RUNNING,
            RunStatus.STOPPING,
        }:
            return False
        self.ledger.mark_stopping(ref)
        record = self.registry.get(ref)
        if record is None or record.descriptor.host_id != self.host_id:
            return False
        supervisor = record.descriptor.supervisor
        supervisor_state = probe_process(self.host_id, supervisor)
        if supervisor_state is ProcessLiveness.UNKNOWN:
            return False
        if supervisor_state is ProcessLiveness.DEAD:
            browser = record.descriptor.boundary
            if browser is not None:
                group = _runtime_liveness(browser, record.descriptor.namespace)
                if group is ProcessLiveness.LIVE:
                    _signal_group(browser, signal.SIGTERM)
                elif group is ProcessLiveness.UNKNOWN:
                    return False
        else:
            try:
                if process_identity(supervisor.pid) != supervisor:
                    return False
                os.kill(supervisor.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + min(float(timeout), 30.0)
        interval = 0.05
        while time.monotonic() < deadline:
            current = self.ledger.get(ref)
            if current is not None and current.status is RunStatus.CLOSED:
                self._forget_terminal_run(ref)
                return True
            liveness = self._probe_run(self.host_id, supervisor)
            if liveness is ProcessLiveness.DEAD:
                released = self.ledger.release_after_exit(ref, supervisor, self._probe_run)
                current = self.ledger.get(ref)
                stopped = released or (current is not None and current.status is RunStatus.CLOSED)
                if stopped:
                    self._forget_terminal_run(ref)
                return stopped
            if liveness is ProcessLiveness.UNKNOWN:
                return False
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 2, 0.8)
        current = self.ledger.get(ref)
        stopped = current is not None and current.status is RunStatus.CLOSED
        if stopped:
            self._forget_terminal_run(ref)
        return stopped

    def _forget_terminal_run(self, ref: BrowserRunRefDTO) -> None:
        run = self.ledger.get(ref)
        if run is None or run.status not in {RunStatus.CANCELLED, RunStatus.CLOSED}:
            return
        with self._lock:
            if self._owned_runs.get(ref.run_id) == ref:
                self._owned_runs.pop(ref.run_id, None)
            self._prepared_runs.discard(ref)
        root = self.data_dir / ".foxprofile-runtime"
        private_tmp = root / ref.run_id
        try:
            if (
                not root.is_symlink()
                and not private_tmp.is_symlink()
                and private_tmp.resolve().parent == root.resolve()
                and private_tmp.is_dir()
            ):
                shutil.rmtree(private_tmp)
        except OSError:
            pass

    def control(
        self,
        profile_id: str,
        run_id: str,
        action: str,
        params: dict[str, object] | None = None,
        timeout: float = 60,
    ) -> object:
        ref = BrowserRunRefDTO(profile_id, run_id)
        record = self.registry.get(ref)
        if record is None:
            raise RuntimeError("Browser is not running")
        descriptor = record.descriptor
        if self._probe_run(descriptor.host_id, descriptor.supervisor) is not ProcessLiveness.LIVE:
            raise RuntimeError("Browser runtime could not be verified")
        if descriptor.control_port is None or descriptor.control_token is None:
            raise RuntimeError("Browser is still starting")
        request = urllib.request.Request(
            f"http://127.0.0.1:{descriptor.control_port}/action",
            data=json.dumps({"action": action, "params": params or {}}).encode(),
            headers={"Content-Type": "application/json", TOKEN_HEADER: descriptor.control_token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read() or b"{}")
            except json.JSONDecodeError:
                body = {}
            raise RuntimeError(str(body.get("error", f"HTTP {error.code}"))) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError("Browser did not respond") from error
        if not isinstance(body, dict) or "result" not in body:
            raise RuntimeError("Browser returned an invalid response")
        return body["result"]

    def run_helper(
        self,
        action: str,
        profile_name: str,
        os_type: str,
        engine: str,
        stdin: str,
        timeout: float,
    ) -> object:
        """Authorize and admit a stopped-only helper through the same durable gate."""
        principal = current_principal()
        if (
            self.capacity is None
            or self.profile_by_name is None
            or self.authorize is None
            or self.lock_path is None
        ):
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        if (
            action not in {"export", "import"}
            or not isinstance(stdin, str)
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with catalog_lock(self.lock_path):
            profile = self.profile_by_name(profile_name)
            ref = ProfileRefDTO(profile.profile_id, profile.name)
            actor = self.authorize(principal, ref)
            if (
                profile.name != profile_name
                or profile.os_type != os_type
                or profile.engine != engine
            ):
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
            if any(run.ref.profile_id == ref.profile_id for run in self.ledger.active_runs()):
                raise DomainError(ErrorCode.PROFILE_BUSY)
            admission = self.capacity.reserve(actor, (ref,), self.host_id)
            run = admission.newly_reserved[0]
            try:
                process = self._spawn(
                    profile,
                    ref.profile_id,
                    run.run_id,
                    helper_command(action, profile_name, os_type, engine),
                    helper_stdin=stdin,
                )
            except Exception:
                self.ledger.cancel_unclaimed(run)
                raise
        # Cookie RESULT payloads are private: read directly, never through the
        # launcher's protocol/log monitor. Transport timeouts retain occupancy.
        try:
            output, _ = process.communicate(timeout=float(timeout))
        except subprocess.TimeoutExpired:
            self.request_stop(ref.profile_id, run.run_id, 5.0)
            raise RuntimeError("Protected cookie helper timed out") from None
        finally:
            if process.poll() is not None:
                self._watch(run, process)
        results = [
            line.removeprefix("RESULT:")
            for line in output.splitlines()
            if line.startswith("RESULT:")
        ]
        if not results:
            raise RuntimeError("Protected cookie helper failed")
        try:
            result = json.loads(results[-1])
        except (ValueError, TypeError):
            raise RuntimeError("Protected cookie helper returned an invalid result") from None
        if not isinstance(result, dict):
            raise RuntimeError("Protected cookie helper returned an invalid result")
        return result

    def shutdown(self, timeout: float = 30.0) -> None:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            return
        deadline = time.monotonic() + min(float(timeout), 30.0)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            owned = tuple(self._owned_runs.values())
        for ref in owned:
            run = self.ledger.get(ref)
            if run is None or run.host_id != self.host_id:
                continue
            if run.status is RunStatus.RESERVED and self.ledger.cancel_unclaimed(ref):
                self._forget_terminal_run(ref)
                continue
            remaining = max(0.0, deadline - time.monotonic())
            self.request_stop(ref.profile_id, ref.run_id, remaining)
        self._manager_file.close()

    def verified_descriptor(self, ref: BrowserRunRefDTO) -> RuntimeDescriptor | None:
        """Return only a current, locally verified confined run for manual use."""
        record = self.registry.get(ref)
        if record is None:
            return None
        descriptor = record.descriptor
        if (
            descriptor.host_id != self.host_id
            or descriptor.browser is None
            or descriptor.namespace is None
            or descriptor.tmp_path is None
            or descriptor.display is None
            or descriptor.xauthority_path is None
            or self._probe_run(descriptor.host_id, descriptor.supervisor)
            is not ProcessLiveness.LIVE
        ):
            return None
        if _runtime_liveness(descriptor.browser, descriptor.namespace) is not ProcessLiveness.LIVE:
            return None
        expected_tmp = self.data_dir / ".foxprofile-runtime" / ref.run_id
        tmp = Path(descriptor.tmp_path)
        authority = Path(descriptor.xauthority_path)
        try:
            if (
                tmp != expected_tmp
                or tmp.is_symlink()
                or not tmp.is_dir()
                or authority.is_symlink()
                or not authority.is_file()
                or tmp.resolve() not in authority.resolve().parents
                or authority.stat().st_mode & 0o077
            ):
                return None
        except OSError:
            return None
        return descriptor

    def recover(self):
        """Reconcile exact supervisor and browser process groups before admission."""
        report = self.ledger.recover(self._probe_run)
        with self._lock:
            for run in self.ledger.active_runs():
                if (
                    run.host_id == self.host_id
                    and run.process is not None
                    and self._probe_run(run.host_id, run.process) is ProcessLiveness.LIVE
                ):
                    self._owned_runs[run.ref.run_id] = run.ref
        return report

    def _probe_run(self, host_id: str, supervisor: ProcessIdentity) -> ProcessLiveness:
        supervisor_state = probe_process(host_id, supervisor)
        if supervisor_state is not ProcessLiveness.DEAD:
            return supervisor_state
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT run_id, profile_id FROM browser_runs "
                "WHERE host_id = ? AND pid = ? AND start_ticks = ? "
                "AND status IN ('starting', 'running', 'stopping')",
                (host_id, supervisor.pid, supervisor.start_ticks),
            ).fetchone()
        if row is None:
            return ProcessLiveness.DEAD
        record = self.registry.get(BrowserRunRefDTO(row["profile_id"], row["run_id"]))
        if record is None or record.descriptor.boundary is None:
            return ProcessLiveness.UNKNOWN
        return _runtime_liveness(record.descriptor.boundary, record.descriptor.namespace)

    def _watch(self, ref: BrowserRunRefDTO, process: subprocess.Popen[str]) -> None:
        process.wait()
        with self._lock:
            if self._handles.get(ref.run_id) is process:
                self._handles.pop(ref.run_id, None)
        run = self.ledger.get(ref)
        if run is None:
            return
        if run.status in {RunStatus.CLOSED, RunStatus.CANCELLED}:
            self._forget_terminal_run(ref)
            return
        if run.status is RunStatus.RESERVED:
            self.ledger.cancel_unclaimed(ref)
            self._forget_terminal_run(ref)
            return
        if (
            run.process is not None
            and self._probe_run(run.host_id, run.process) is ProcessLiveness.DEAD
        ):
            self.ledger.release_after_exit(ref, run.process, self._probe_run)
            self._forget_terminal_run(ref)


def _signal_group(identity: ProcessIdentity, signum: int) -> bool:
    if process_identity(identity.pid) != identity:
        return False
    try:
        if os.getpgid(identity.pid) != identity.pid:
            return False
        if process_identity(identity.pid) != identity:
            return False
        os.killpg(identity.pid, signum)
    except OSError:
        return False
    return True


def _process_group_liveness(identity: ProcessIdentity) -> ProcessLiveness:
    """Probe every host process in a launcher's isolated process group."""
    current = process_identity(identity.pid)
    if current is not None and current != identity:
        return ProcessLiveness.UNKNOWN
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return ProcessLiveness.UNKNOWN
    uncertain = False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            # Confined children retain the service host UID; prove foreign
            # ownership before skipping an otherwise unreadable process.
            if entry.stat().st_uid != os.getuid():
                continue
            stat = (entry / "stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(")") + 2 :].split()
            if int(fields[2]) == identity.pid and fields[0] not in {"Z", "X"}:
                return ProcessLiveness.LIVE
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            uncertain = True
    return ProcessLiveness.UNKNOWN if uncertain else ProcessLiveness.DEAD


def _runtime_liveness(identity: ProcessIdentity, namespace: str | None) -> ProcessLiveness:
    group = _process_group_liveness(identity)
    if group is not ProcessLiveness.DEAD:
        return group
    if namespace is None:
        return ProcessLiveness.UNKNOWN
    if namespace == os.readlink("/proc/self/ns/pid"):
        return ProcessLiveness.UNKNOWN
    uncertain = False
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return ProcessLiveness.UNKNOWN
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            # All processes inside our unprivileged user namespace map to the
            # service host UID. Unreadable foreign-UID namespaces cannot be ours.
            if entry.stat().st_uid != os.getuid():
                continue
            stat = (entry / "stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(")") + 2 :].split()
            if fields[0] in {"Z", "X"}:
                continue
            if os.readlink(entry / "ns/pid") == namespace:
                return ProcessLiveness.LIVE
        except FileNotFoundError:
            continue
        except OSError:
            uncertain = True
    return ProcessLiveness.UNKNOWN if uncertain else ProcessLiveness.DEAD
