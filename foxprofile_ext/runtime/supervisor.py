"""Child-side launch gate and stdout proxy for one admitted browser run."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from ..dto import BrowserRunRefDTO, ProcessIdentity
from ..storage.capacity import CapacityLedger, ProcessLiveness
from ..storage.database import ExtensionDatabase
from .confinement import ConfinementSpec, build_confined_command
from .processes import _process_group_liveness, _runtime_liveness, _signal_group
from .registry import RuntimeRegistry, current_host_id, process_identity

_DISPLAY = re.compile(r"^:[0-9]{1,5}(?:\.[0-9]{1,2})?$")
_NAMESPACE = re.compile(r"^pid:\[[0-9]+\]$")
_MAX_CONFIG_BYTES = 1_000_000


def _read_config(stream: TextIO) -> dict[str, object]:
    raw = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("supervisor configuration is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid supervisor configuration")
    return value


def _runtime_details(
    value: str,
    expected_tmp: Path,
) -> tuple[str | None, str | None, str, str | None]:
    details = json.loads(value)
    if not isinstance(details, dict):
        raise ValueError("invalid runtime details")
    display = details.get("display")
    tmp_path = details.get("tmp_path")
    xauthority_path = details.get("xauthority_path")
    namespace = details.get("namespace")
    if display is not None and (not isinstance(display, str) or not _DISPLAY.fullmatch(display)):
        raise ValueError("invalid runtime display")
    # The confined runner sees the private host directory at the fixed mount
    # point /tmp. Persist only the trusted host mapping held by its supervisor.
    if tmp_path != "/tmp":
        raise ValueError("runtime temporary path does not match this run")
    if xauthority_path is not None:
        if not isinstance(xauthority_path, str):
            raise ValueError("invalid X authority path")
        candidate = Path(xauthority_path)
        if (
            not candidate.is_absolute()
            or Path("/tmp") not in candidate.parents
            or ".." in candidate.parts
        ):
            raise ValueError("X authority path is outside this run")
        host_authority = expected_tmp / candidate.relative_to("/tmp")
        if (
            host_authority.is_symlink()
            or not host_authority.is_file()
            or expected_tmp.resolve() not in host_authority.resolve().parents
            or host_authority.stat().st_mode & 0o077
            or host_authority.stat().st_uid != os.getuid()
        ):
            raise ValueError("X authority file is not private to this run")
        xauthority_path = str(host_authority)
    if display is not None and xauthority_path is None:
        raise ValueError("runtime display has no private X authority")
    if namespace is not None and (
        not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace)
    ):
        raise ValueError("invalid runtime namespace")
    return display, namespace, str(expected_tmp), xauthority_path


def supervise(config_stream: TextIO, output: TextIO) -> int:
    """Claim run before side effects, then run confined browser and proxy protocol."""
    child: subprocess.Popen[str] | None = None
    boundary: ProcessIdentity | None = None
    namespace: str | None = None
    ledger = None
    ref = identity = None
    claimed = False
    runtime_published = False
    browser_effects_allowed = False
    try:
        config = _read_config(config_stream)
        ref = BrowserRunRefDTO(str(config["profile_id"]), str(config["run_id"]))
        host_id = str(config["host_id"])
        if host_id != current_host_id():
            return 2
        identity = process_identity(os.getpid())
        if identity is None:
            return 2
        database = ExtensionDatabase(str(config["database_path"]))
        ledger = CapacityLedger(database)
        registry = RuntimeRegistry(database, ledger)
        if not registry.register_supervisor(ref, host_id, identity):
            return 3

        claimed = True
        code_root = Path(str(config["code_root"]))
        profile_path = Path(str(config["profile_path"]))
        tmp_path = Path(str(config["private_tmp_path"]))
        runtime_paths = tuple(Path(str(path)) for path in config["runtime_paths"])
        settings = config["settings"]
        command = config["command"]
        if not isinstance(settings, dict) or not isinstance(command, list):
            raise ValueError("invalid supervisor launch settings")
        settings = {str(key): str(value) for key, value in settings.items()}
        command = _confined_arguments(command, code_root)
        helper_stdin = config.get("helper_stdin")
        if helper_stdin is not None and not isinstance(helper_stdin, str):
            raise ValueError("invalid helper input")
        settings["FOXPROFILE_DATA_DIR"] = "/profile"
        settings["FOXPROFILE_EXT_ENABLED"] = "true"
        spec = ConfinementSpec(profile_path, tmp_path, code_root, runtime_paths)
        gated_command = [command[0], "-m", "foxprofile_ext.runtime.child_gate", "--", *command[1:]]
        confined_command, environment = build_confined_command(spec, gated_command, settings)
        environment["FOXPROFILE_EXT_ENABLED"] = "true"
        profile_name = profile_path.name
        if not profile_name or profile_name in {".", ".."} or "/" in profile_name:
            raise ValueError("invalid selected profile directory")
        mount = confined_command.index("--bind")
        if confined_command[mount + 1 : mount + 3] != [str(profile_path.resolve()), "/profile"]:
            raise ValueError("selected profile mount could not be verified")
        confined_command[mount + 2] = f"/profile/{profile_name}"
        confined_command[mount:mount] = ["--dir", "/profile"]
        child = subprocess.Popen(
            confined_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            cwd=code_root,
            start_new_session=True,
        )
        boundary = process_identity(child.pid)
        if boundary is None or not registry.publish_boundary(ref, identity, boundary):
            child.terminate()
            child.wait(timeout=5)
            return 4

        def terminate_child(_signum: int, _frame: object) -> None:
            if child is None or boundary is None:
                return
            try:
                _signal_group(boundary, signal.SIGTERM)
                child.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                _signal_group(boundary, signal.SIGKILL)
                child.wait(timeout=2)

        signal.signal(signal.SIGTERM, terminate_child)
        signal.signal(signal.SIGINT, terminate_child)
        output_open = True
        assert child.stdout is not None
        for raw_line in child.stdout:
            line = raw_line.rstrip("\r\n")
            if line.startswith("BOUNDARY:"):
                candidate = line.removeprefix("BOUNDARY:")
                if (
                    browser_effects_allowed
                    or not _NAMESPACE.fullmatch(candidate)
                    or not _verified_namespace(boundary, candidate)
                ):
                    terminate_child(signal.SIGTERM, None)
                    break
                namespace = candidate
                if not registry.publish_runtime_details(
                    ref,
                    identity,
                    display=None,
                    namespace=namespace,
                    tmp_path=str(tmp_path),
                    xauthority_path=None,
                ):
                    terminate_child(signal.SIGTERM, None)
                    break
                # Durable namespace ownership is established before the child
                # can execute the runner and create Xvfb or a browser.
                browser_effects_allowed = True
                assert child.stdin is not None
                child.stdin.write("GO\n")
                if helper_stdin is not None:
                    child.stdin.write(helper_stdin)
                child.stdin.close()
                continue
            if line.startswith("RUNTIME:"):
                try:
                    raw_details = line.removeprefix("RUNTIME:")
                    display, reported_namespace, runtime_tmp, xauthority = _runtime_details(
                        raw_details, tmp_path
                    )
                    if not browser_effects_allowed or reported_namespace != namespace:
                        raise ValueError("runtime namespace does not belong to this run")
                    if not registry.publish_runtime_details(
                        ref,
                        identity,
                        display=display,
                        namespace=namespace,
                        tmp_path=runtime_tmp,
                        xauthority_path=xauthority,
                    ):
                        raise ValueError("runtime generation is no longer active")
                    process = json.loads(raw_details).get("browser_process")
                    if process is not None:
                        engine = str(config.get("engine", "camoufox"))
                        executable = settings.get(f"FOXPROFILE_{engine.upper()}_EXECUTABLE")
                        actual_browser = _map_browser_process(
                            boundary, namespace, process, executable
                        )
                        if actual_browser is None or not registry.publish_browser(
                            ref,
                            identity,
                            actual_browser,
                            display=display,
                            namespace=namespace,
                            tmp_path=runtime_tmp,
                            xauthority_path=xauthority,
                        ):
                            raise ValueError("browser process does not belong to this run")
                    runtime_published = True
                except (TypeError, ValueError, json.JSONDecodeError):
                    if output_open:
                        try:
                            output.write("LAUNCH_FAILED: runtime metadata could not be verified\n")
                            output.flush()
                        except (BrokenPipeError, OSError):
                            output_open = False
                    terminate_child(signal.SIGTERM, None)
                    break
                continue
            if line == "BROWSER_STARTED":
                if not runtime_published or registry.get(ref).descriptor.control_port is None:
                    terminate_child(signal.SIGTERM, None)
                    break
                ledger.mark_running(ref)
            if line.startswith("CONTROL:"):
                try:
                    if not runtime_published:
                        raise ValueError("control readiness precedes runtime registration")
                    _, raw_port, token = line.split(":", 2)
                    port = int(raw_port)
                    if not registry.publish_control(ref, identity, port, token):
                        raise ValueError("control endpoint belongs to a stale run")
                except (ValueError, TypeError):
                    terminate_child(signal.SIGTERM, None)
                    break
            if output_open:
                try:
                    output.write(line + "\n")
                    output.flush()
                except (BrokenPipeError, OSError):
                    # Parent manager may restart. Keep draining and supervising;
                    # this run remains adoptable through the durable registry.
                    output_open = False
        return child.wait()
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return 5
    finally:
        clean = child is None
        if child is not None and boundary is not None:
            try:
                if child.poll() is None:
                    _signal_group(boundary, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        _signal_group(boundary, signal.SIGKILL)
                        child.wait(timeout=2)
                # A normal child exit can still leave descendants in its group.
                if _process_group_liveness(boundary) is ProcessLiveness.LIVE:
                    _signal_group(boundary, signal.SIGTERM)
                state = (
                    _runtime_liveness(boundary, namespace)
                    if namespace is not None
                    else (
                        _process_group_liveness(boundary)
                        if not browser_effects_allowed
                        else ProcessLiveness.UNKNOWN
                    )
                )
                clean = child.poll() is not None and state is ProcessLiveness.DEAD
            except (OSError, subprocess.SubprocessError):
                clean = False
        if clean and claimed and ledger is not None:
            # The trusted supervisor has completed its final cleanup, will
            # create no further children, and verified the browser boundary.
            ledger.release_after_exit(ref, identity, lambda _host, _pid: ProcessLiveness.DEAD)
            # Remove only this trusted generation's private directory, after
            # proving its browser boundary empty. Never glob namespace PIDs.
            if (
                "tmp_path" in locals()
                and tmp_path.name == ref.run_id
                and tmp_path.parent.name == ".foxprofile-runtime"
                and not tmp_path.is_symlink()
                and tmp_path.is_dir()
                and tmp_path.stat().st_uid == os.getuid()
            ):
                shutil.rmtree(tmp_path)


def _confined_path(value: str, code_root: Path) -> str:
    path = Path(value)
    if path.is_absolute() and code_root in path.parents:
        relative = path.relative_to(code_root)
        if relative.parts[0] not in {"src", "chrome_engine", "foxprofile_ext", ".venv", "venv"}:
            raise ValueError("launch path is outside allowlisted application code")
        return str(Path("/opt/foxprofile") / relative)
    return value


def _confined_arguments(command: list[str], code_root: Path) -> list[str]:
    # Only the executable and fixed script argument are paths. Profile names,
    # proxy credentials and other user data must remain literal arguments.
    result = list(command)
    if len(result) > 1 and result[1].endswith(".py"):
        result[1] = _confined_path(result[1], code_root)
    return result


def _verified_namespace(browser: ProcessIdentity, namespace: str) -> bool:
    if process_identity(browser.pid) != browser:
        return False
    if namespace == os.readlink("/proc/self/ns/pid"):
        return False
    descendants = {browser.pid}
    try:
        entries = tuple(Path("/proc").iterdir())
        for _ in range(8):
            previous = len(descendants)
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text(encoding="ascii")
                    fields = stat[stat.rfind(")") + 2 :].split()
                    if int(fields[1]) in descendants:
                        descendants.add(int(entry.name))
                except (OSError, ValueError, IndexError):
                    continue
            if len(descendants) == previous:
                break
        return any(_namespace_of(pid) == namespace for pid in descendants)
    except OSError:
        return False


def _namespace_of(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None


def _map_browser_process(
    boundary: ProcessIdentity,
    namespace: str,
    value: object,
    executable: str | None,
) -> ProcessIdentity | None:
    """Map a namespace PID to its exact host engine process, never to bwrap."""
    if (
        not isinstance(value, dict)
        or type(value.get("pid")) is not int
        or type(value.get("start_ticks")) is not int
        or executable is None
        or not Path(executable).is_absolute()
        or value["pid"] < 1
        or value["start_ticks"] < 0
        or process_identity(boundary.pid) != boundary
    ):
        return None
    expected = Path(executable).resolve()
    matches = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if os.readlink(entry / "ns/pid") != namespace:
                    continue
                status = (entry / "status").read_text(encoding="ascii")
                namespace_pids = next(
                    line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")
                )
                if int(namespace_pids[-1]) != value["pid"]:
                    continue
                identity = process_identity(int(entry.name))
                if (
                    identity is None
                    or identity.start_ticks != value["start_ticks"]
                    or Path(os.readlink(entry / "exe")).resolve() != expected
                    or not _is_descendant(identity.pid, boundary)
                ):
                    continue
                matches.append(identity)
            except (OSError, ValueError, StopIteration):
                continue
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def _is_descendant(pid: int, boundary: ProcessIdentity) -> bool:
    seen = set()
    try:
        while pid > 1 and pid not in seen:
            seen.add(pid)
            if pid == boundary.pid:
                return process_identity(pid) == boundary
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(")") + 2 :].split()
            pid = int(fields[1])
    except (OSError, ValueError, IndexError):
        return False
    return False


def main() -> int:
    return supervise(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
