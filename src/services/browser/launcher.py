from __future__ import annotations

import atexit
import contextlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from ...core.logging import get_logger
from ...core.strings import get_string
from ...models.profile import Profile
from .control import TOKEN_HEADER
from .process import spawn_browser, terminate, wait_for_exit

logger = get_logger("browser.launcher")

if TYPE_CHECKING:
    from foxprofile_ext.runtime.contracts import BrowserRuntimeAdapter

_NOISY_PREFIXES = (
    "- [pid=",
    "console.error:",
    "Crash Annotation",
    "JavaScript error:",
    "WARNING: At least one completion condition",
)


class _LaunchResult:
    """Outcome of one launch, settled once the browser is ready or has failed."""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id
        self.settled = threading.Event()
        self.ok = False
        self.error: str | None = None
        self._lock = threading.Lock()

    def settle(self, ok: bool, error: str | None = None) -> None:
        with self._lock:
            if self.settled.is_set():
                return
            self.ok = ok
            self.error = error
            self.settled.set()


class ProfileBusyError(Exception):
    """The profile's browser is running or another operation holds its data dir."""


class BrowserControlError(Exception):
    """A page action failed or the profile has no controllable browser."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class BrowserLauncher:
    def __init__(self, runtime_adapter: BrowserRuntimeAdapter | None = None) -> None:
        self._runtime_adapter = runtime_adapter
        self._lock = threading.Lock()
        self._active_sessions: dict[str, subprocess.Popen] = {}
        self._run_bindings: dict[str, tuple[str, str]] = {}
        self._stop_notifiers: dict[str, threading.Event] = {}
        self._launch_results: dict[str, _LaunchResult] = {}
        self._busy: set[str] = set()
        self._controls: dict[str, tuple[int, str]] = {}
        atexit.register(self.shutdown_all)

    @contextlib.contextmanager
    def exclusive(self, profile_name: str) -> Iterator[None]:
        """Hold a stopped profile's data dir; launches are refused meanwhile."""
        protected = self._protected_mode()
        if protected and self._runtime_adapter is None:
            raise RuntimeError("Protected profile operations require the runtime adapter")
        with self._lock:
            if profile_name in self._busy:
                raise ProfileBusyError(profile_name)
            proc = self._active_sessions.get(profile_name)
            if proc is not None and proc.poll() is None:
                raise ProfileBusyError(profile_name)
            self._busy.add(profile_name)
        try:
            if self._runtime_adapter is None:
                yield
            else:
                # Imported lazily to keep cookie conversion independent of the
                # launcher in legacy mode and avoid a module import cycle.
                from .cookies import runtime_tool_context

                with runtime_tool_context(self._runtime_adapter, profile_name):
                    yield
        finally:
            with self._lock:
                self._busy.discard(profile_name)

    def wait_for_launch(
        self, profile_name: str, timeout: float, run_id: str | None = None
    ) -> tuple[bool, str | None]:
        """Block until the latest launch of a profile is ready or has failed.

        Returns (ok, error). A launch still pending after `timeout` reports
        (False, "timeout") while the browser keeps starting in the background.
        """
        with self._lock:
            result = self._launch_results.get(profile_name)
        if result is None:
            return False, "No launch in progress"
        if run_id is not None and result.run_id != run_id:
            return False, "launch superseded"
        if not result.settled.wait(timeout):
            return False, "timeout"
        return result.ok, result.error

    def shutdown_all(self) -> None:
        if self._protected_mode():
            if self._runtime_adapter is None:
                raise RuntimeError("Protected browser shutdown requires the runtime adapter")
            self._runtime_adapter.shutdown(30.0)
            return
        with self._lock:
            for name, proc in list(self._active_sessions.items()):
                notifier = self._stop_notifiers.pop(name, None)
                if notifier:
                    notifier.set()
                terminate(proc, name)
            self._active_sessions.clear()
        logger.info("All browser sessions terminated")

    def start_thread(
        self,
        profile: Profile,
        log_callback: Callable[[str], None],
        on_start: Callable[[], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        *,
        run_id: str | None = None,
        profile_id: str | None = None,
    ) -> bool:
        """Launch a profile's browser in the background.

        Returns False, without calling any callback, when the profile already
        has a browser. Otherwise returns True and later calls on_ready or
        on_stop, also when spawning fails. Raises ProfileBusyError while
        another launch or a stopped-profile operation holds the profile.
        """
        with self._lock:
            protected = self._protected_mode()
            if protected and (self._runtime_adapter is None or not run_id or not profile_id):
                raise RuntimeError("Protected browser launch requires an admitted run")
            if (run_id is None) != (profile_id is None):
                raise ValueError("run_id and profile_id must be supplied together")
            if profile.name in self._active_sessions:
                return False
            if profile.name in self._busy:
                raise ProfileBusyError(profile.name)
            # Reserve the profile until the process is registered, so two
            # concurrent launches cannot both spawn a browser.
            self._busy.add(profile.name)

        stop_event = threading.Event()
        notify_lock = threading.Lock()
        result = _LaunchResult(run_id)

        def notify_stopped() -> None:
            with notify_lock:
                if stop_event.is_set():
                    return
                stop_event.set()
                result.settle(False, "Browser exited before it was ready")
                with self._lock:
                    # A newer launch may already own this name; leave it alone.
                    if self._stop_notifiers.get(profile.name) is stop_event:
                        self._controls.pop(profile.name, None)
                        self._active_sessions.pop(profile.name, None)
                        self._run_bindings.pop(profile.name, None)
                        self._stop_notifiers.pop(profile.name, None)
                log_callback(get_string("session_ended", name=profile.name))
                logger.info(f"Session ended for profile: {profile.name}")
                if on_stop:
                    on_stop()

        registered = False
        try:
            with self._lock:
                self._launch_results[profile.name] = result
            log_callback(get_string("starting_profile", name=profile.name, os=profile.os_type))
            logger.info(f"Starting browser for profile: {profile.name}")
            if on_start:
                on_start()

            try:
                if protected:
                    assert self._runtime_adapter is not None
                    assert run_id is not None and profile_id is not None
                    proc = self._runtime_adapter.spawn_browser(profile, profile_id, run_id)
                else:
                    proc = spawn_browser(profile)
                with self._lock:
                    self._active_sessions[profile.name] = proc
                    if run_id is not None and profile_id is not None:
                        self._run_bindings[profile.name] = (profile_id, run_id)
                    self._stop_notifiers[profile.name] = stop_event
                    self._busy.discard(profile.name)
                    registered = True

                threading.Thread(
                    target=self._monitor_process,
                    args=(
                        proc,
                        profile.name,
                        log_callback,
                        on_ready,
                        notify_stopped,
                        result,
                        (profile_id, run_id)
                        if profile_id is not None and run_id is not None
                        else None,
                    ),
                    daemon=True,
                ).start()
                threading.Thread(
                    target=wait_for_exit,
                    args=(proc, profile.name, notify_stopped),
                    kwargs={"cleanup_runner": False} if protected else {},
                    daemon=True,
                ).start()
            except Exception as e:
                if protected:
                    logger.error("Error starting protected browser for %s", profile.name)
                    error = "Browser launch failed"
                else:
                    logger.exception("Error starting browser for %s: %s", profile.name, e)
                    error = str(e)
                result.settle(False, error)
                stopped = False
                if protected:
                    try:
                        assert self._runtime_adapter is not None
                        assert profile_id is not None and run_id is not None
                        stopped = self._runtime_adapter.request_stop(profile_id, run_id, 1.0)
                    except Exception:
                        stopped = False
                if stopped:
                    notify_stopped()
                else:
                    with self._lock:
                        self._busy.discard(profile.name)
                    log_callback(get_string("error_starting", error=error))
                    if on_stop:
                        on_stop()
        finally:
            # Never keep the reservation when a callback raised before the
            # process was registered; the profile would stay busy for good.
            if not registered:
                with self._lock:
                    self._busy.discard(profile.name)
        return True

    def stop_profile(self, profile_name: str, timeout: int = 2) -> bool:
        with self._lock:
            if profile_name not in self._active_sessions:
                return False
            binding = self._run_bindings.get(profile_name)
            if self._protected_mode():
                if self._runtime_adapter is None or binding is None:
                    return False
                profile_id, run_id = binding
                proc = self._active_sessions[profile_name]
                notifier = self._stop_notifiers.get(profile_name)
            else:
                profile_id = run_id = ""
                proc = self._active_sessions.pop(profile_name)
                notifier = self._stop_notifiers.pop(profile_name, None)
                self._controls.pop(profile_name, None)
        if self._protected_mode():
            assert self._runtime_adapter is not None
            stopped = self._runtime_adapter.request_stop(profile_id, run_id, timeout)
            if not stopped:
                return False
            if notifier:
                notifier.set()
            with self._lock:
                if self._run_bindings.get(profile_name) == (profile_id, run_id):
                    self._run_bindings.pop(profile_name, None)
                    self._active_sessions.pop(profile_name, None)
                    self._stop_notifiers.pop(profile_name, None)
                    self._controls.pop(profile_name, None)
        else:
            if notifier:
                notifier.set()
            terminate(proc, profile_name, timeout)
        logger.info("Stopped browser for profile: %s", profile_name)
        return True

    def control(
        self,
        profile_name: str,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> Any:
        """Run a page action in a running profile's browser and return its result."""
        with self._lock:
            binding = self._run_bindings.get(profile_name)
            endpoint = self._controls.get(profile_name)
        if not self.is_running(profile_name):
            raise BrowserControlError("Browser is not running", status=409)
        if self._protected_mode():
            if self._runtime_adapter is None or binding is None:
                raise BrowserControlError("Browser run is unavailable", status=409)
            return self._runtime_adapter.control(
                binding[0], binding[1], action, params or {}, timeout
            )
        if endpoint is None:
            # Process is up but has not announced its control channel yet.
            raise BrowserControlError("Browser is still starting", status=503)
        port, token = endpoint
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/action",
            data=json.dumps({"action": action, "params": params or {}}).encode(),
            headers={"Content-Type": "application/json", TOKEN_HEADER: token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            raise BrowserControlError(body.get("error", f"HTTP {e.code}"), status=e.code) from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise BrowserControlError(f"Browser did not respond: {e}", status=504) from e
        return body["result"]

    def running_profile_names(self) -> set[str]:
        with self._lock:
            stale = [n for n, p in self._active_sessions.items() if p.poll() is not None]
            for n in stale:
                self._active_sessions.pop(n, None)
                self._stop_notifiers.pop(n, None)
                self._controls.pop(n, None)
                self._run_bindings.pop(n, None)
            return set(self._active_sessions.keys())

    def running_count(self) -> int:
        return len(self.running_profile_names())

    def is_running(self, profile_name: str) -> bool:
        with self._lock:
            if profile_name not in self._active_sessions:
                return False
            if self._active_sessions[profile_name].poll() is None:
                return True
            del self._active_sessions[profile_name]
            self._stop_notifiers.pop(profile_name, None)
            self._controls.pop(profile_name, None)
            self._run_bindings.pop(profile_name, None)
            return False

    @staticmethod
    def _protected_mode() -> bool:
        from foxprofile_ext.config import extension_enabled

        return extension_enabled()

    def _monitor_process(
        self,
        proc: subprocess.Popen,
        name: str,
        log_callback: Callable[[str], None],
        on_ready: Callable[[], None] | None,
        notify_stopped: Callable[[], None],
        result: _LaunchResult,
        run_binding: tuple[str, str] | None = None,
    ) -> None:
        ready_notified = False
        try:
            if proc.stdout is None:
                return
            for line in iter(proc.stdout.readline, ""):
                msg = line.strip()
                if not msg:
                    continue
                if msg.startswith("CONTROL:"):
                    # Holds the control token: never log this line.
                    if not self._protected_mode():
                        _, port, token = msg.split(":", 2)
                        with self._lock:
                            self._controls[name] = (int(port), token)
                    continue
                if msg.startswith("RUNTIME:"):
                    # Private supervisor metadata: never surface in logs or UI.
                    continue
                if msg == "BROWSER_STARTED":
                    result.settle(True)
                    if not ready_notified:
                        ready_notified = True
                        if on_ready:
                            on_ready()
                    log_callback(get_string("browser_started"))
                    logger.info("Browser started for profile: %s", name)
                    continue
                if msg == "BROWSER_CLOSED":
                    logger.info("Browser close detected for profile: %s", name)
                    if self._protected_mode():
                        if self._stop_exact_run(run_binding):
                            notify_stopped()
                    else:
                        notify_stopped()
                        terminate(proc, name, timeout=1)
                    continue
                if msg.startswith("LAUNCH_FAILED:") or msg == "LAUNCH_CANCELLED":
                    if self._protected_mode():
                        failure = "Browser launch failed"
                        safe_msg = "LAUNCH_FAILED: Browser launch failed"
                    else:
                        failure = msg.removeprefix("LAUNCH_FAILED:").strip()
                        safe_msg = msg
                    result.settle(False, failure)
                    log_callback(f"[{name}] {safe_msg}")
                    logger.warning("Launch failed for profile %s: %s", name, safe_msg)
                    if self._protected_mode():
                        if self._stop_exact_run(run_binding):
                            notify_stopped()
                    else:
                        notify_stopped()
                        terminate(proc, name, timeout=1)
                    break
                if msg.startswith(_NOISY_PREFIXES):
                    logger.debug("[%s] %s", name, msg)
                    continue
                if len(msg) > 400:
                    msg = msg[:400] + "..."
                log_callback(f"[{name}] {msg}")
                logger.debug("[%s] %s", name, msg)
        except Exception as e:
            if self._protected_mode():
                logger.warning("Monitor error for protected profile %s", name)
                log_callback(f"[{name}] Browser monitor failed")
            else:
                logger.exception("Monitor error for profile %s: %s", name, e)
                log_callback(f"[{name}] Monitor error: {e}")
        finally:
            if proc.stdout is not None:
                with contextlib.suppress(Exception):
                    proc.stdout.close()

    def _stop_exact_run(self, binding: tuple[str, str] | None) -> bool:
        """Delegate protocol-driven protected exits to their exact supervisor run."""
        if self._runtime_adapter is None or binding is None:
            return False
        try:
            return self._runtime_adapter.request_stop(binding[0], binding[1], 1.0)
        except Exception:
            # The supervisor still owns process monitoring and recovery. A
            # source callback must not guess a host PID or clear capacity.
            logger.warning("Could not request stop for protected browser run")
            return False
