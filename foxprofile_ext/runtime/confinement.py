"""Build fail-closed Linux process confinement with bubblewrap.

The caller resolves profile and run paths from trusted server state. This module
does not start an unconfined fallback and accepts no caller-supplied mount flags.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_CODE_DIRECTORIES = ("src", "chrome_engine", "foxprofile_ext")
_APP_ROOT = "/opt/foxprofile"
_PROFILE_ROOT = "/profile"
_TEMP_ROOT = "/tmp"
_SYSTEM_PATH = "/usr/bin:/bin"
_RESOLVER_FILES = frozenset(
    Path(value) for value in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf")
)
_PRIVATE_HOST_PATHS = (
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/run"),
    Path("/var/lib"),
    Path("/var/log"),
    Path("/etc/foxprofile"),
)
_ALLOWED_SETTINGS = frozenset(
    {
        "DISPLAY",
        "XAUTHORITY",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "FOXPROFILE_DATA_DIR",
        "FOXPROFILE_HEADLESS",
        "FOXPROFILE_LANG",
        "FOXPROFILE_LOG_LEVEL",
        "FOXPROFILE_LOG_DIR",
        "FOXPROFILE_PROXY_TIMEOUT",
        "FOXPROFILE_RESTORE_TABS",
    }
)


class ConfinementUnavailable(RuntimeError):
    """Required per-profile confinement cannot be established."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            "Per-profile browser confinement is unavailable: "
            f"{reason}. No unconfined fallback was started."
        )


class ConfinementPurpose(str, Enum):
    PROFILE_PROCESS = "profile-process"
    VNC_HELPER = "vnc-helper"


@dataclass(frozen=True, slots=True)
class ConfinementSpec:
    """Trusted host paths needed to construct one profile's sandbox."""

    profile_path: Path
    private_tmp_path: Path
    code_root: Path
    runtime_paths: tuple[Path, ...]
    purpose: ConfinementPurpose = ConfinementPurpose.PROFILE_PROCESS


def _directory(
    path: Path,
    label: str,
    *,
    private: bool = False,
    allow_root_owned: bool = False,
) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ConfinementUnavailable(f"{label} must be an absolute server-resolved path")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ConfinementUnavailable(f"{label} is unavailable") from error
    if not path.is_dir() or path.is_symlink() or not resolved.is_dir():
        raise ConfinementUnavailable(f"{label} must be a real directory")
    owned_by_service = info.st_uid == os.getuid()
    owned_by_root = allow_root_owned and info.st_uid == 0
    if not owned_by_service and not owned_by_root:
        raise ConfinementUnavailable(f"{label} must be owned by the FoxProfile service user")
    if private and info.st_mode & 0o077:
        raise ConfinementUnavailable(f"{label} must have owner-only permissions")
    return resolved


def _path_overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _runtime_paths(spec: ConfinementSpec, code_root: Path) -> tuple[Path, ...]:
    if not spec.runtime_paths:
        raise ConfinementUnavailable("at least one read-only runtime path is required")

    runtime_paths: list[Path] = []
    seen: set[Path] = set()
    home = Path.home().resolve()
    protected_roots = (home, code_root, Path.cwd().resolve())
    blocked_roots = {
        Path("/"),
        Path("/home"),
        Path("/root"),
        Path("/tmp"),
        Path("/etc"),
        Path("/var"),
    }
    for candidate in spec.runtime_paths:
        source = Path(candidate)
        if not source.is_absolute():
            raise ConfinementUnavailable("runtime paths must be absolute")
        mount_path = Path(os.path.abspath(source))
        try:
            resolved = source.resolve(strict=True)
        except OSError as error:
            raise ConfinementUnavailable("a read-only runtime path is unavailable") from error
        if not resolved.is_dir() and not resolved.is_file():
            raise ConfinementUnavailable("runtime paths must be files or directories")
        resolver_file = mount_path in _RESOLVER_FILES
        if resolver_file:
            info = resolved.stat()
            if (
                not resolved.is_file()
                or info.st_uid not in {0, os.getuid()}
                or info.st_mode & 0o022
            ):
                raise ConfinementUnavailable(
                    "resolver configuration must be a trusted read-only file"
                )
        if resolved in blocked_roots or resolved == home:
            raise ConfinementUnavailable("a broad or private path cannot be a runtime mount")
        if not resolver_file and any(
            _path_overlaps(resolved, private) for private in _PRIVATE_HOST_PATHS
        ):
            raise ConfinementUnavailable(
                "a private namespace or service path cannot be a runtime mount"
            )
        if any(resolved == item or resolved in item.parents for item in protected_roots):
            raise ConfinementUnavailable(
                "a private or application parent cannot be a runtime mount"
            )
        if Path("/tmp") in mount_path.parents and code_root not in mount_path.parents:
            raise ConfinementUnavailable("a host temporary path cannot be a runtime mount")
        if code_root in resolved.parents:
            relative = resolved.relative_to(code_root)
            if relative.parts[0] not in {".venv", "venv"}:
                raise ConfinementUnavailable(
                    "only a Python virtual environment may be mounted from the application root"
                )
        if any(part in {".env", "profiles.json", "extension.sqlite3"} for part in resolved.parts):
            raise ConfinementUnavailable(
                "a private configuration or catalog path cannot be mounted"
            )
        if any(
            _path_overlaps(resolved, private)
            for private in (spec.profile_path, spec.private_tmp_path)
        ):
            raise ConfinementUnavailable("runtime mounts cannot overlap profile or run data")
        if mount_path not in seen:
            seen.add(mount_path)
            # Preserve aliases such as /lib and /lib64 as mount destinations;
            # the dynamic loader still opens those absolute paths in the sandbox.
            runtime_paths.append(mount_path)
    return tuple(sorted(runtime_paths, key=lambda item: (len(item.parts), str(item))))


def _validate_spec(
    spec: ConfinementSpec,
) -> tuple[Path, Path, Path, tuple[Path, ...], ConfinementPurpose]:
    if not sys.platform.startswith("linux"):
        raise ConfinementUnavailable("bubblewrap namespaces are supported only on Linux")
    profile = _directory(spec.profile_path, "profile directory")
    private_tmp = _directory(spec.private_tmp_path, "per-run temporary directory", private=True)
    code_root = _directory(spec.code_root, "application code root", allow_root_owned=True)
    if (
        _path_overlaps(profile, private_tmp)
        or profile == code_root
        or profile in code_root.parents
        or private_tmp == code_root
        or private_tmp in code_root.parents
    ):
        raise ConfinementUnavailable(
            "profile and temporary paths must not contain the code root or overlap each other"
        )
    try:
        purpose = ConfinementPurpose(spec.purpose)
    except (TypeError, ValueError):
        raise ConfinementUnavailable("unknown sandbox purpose") from None
    for name in _CODE_DIRECTORIES:
        code_dir = code_root / name
        if not code_dir.is_dir() or code_dir.is_symlink():
            raise ConfinementUnavailable(f"allowlisted code directory '{name}' is unavailable")
        if _path_overlaps(profile, code_dir) or _path_overlaps(private_tmp, code_dir):
            raise ConfinementUnavailable("profile and run paths cannot overlap allowlisted code")

    return profile, private_tmp, code_root, _runtime_paths(spec, code_root), purpose


def _bubblewrap() -> str:
    binary = shutil.which("bwrap")
    if binary is None:
        raise ConfinementUnavailable("the bubblewrap executable is missing")
    return str(Path(binary).resolve())


def build_confined_environment(settings: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a small child environment with service credentials omitted."""
    environment = {
        "HOME": _TEMP_ROOT,
        "TMPDIR": _TEMP_ROOT,
        "PATH": _SYSTEM_PATH,
        "PYTHONPATH": _APP_ROOT,
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    for name, value in (settings or {}).items():
        if name in _ALLOWED_SETTINGS and isinstance(value, str) and "\x00" not in value:
            environment[name] = value
    return environment


def _runtime_target(path: Path, code_root: Path) -> Path:
    if code_root in path.parents:
        return Path(_APP_ROOT) / path.relative_to(code_root)
    return path


def _validate_command(
    command: Sequence[str], runtime_paths: tuple[Path, ...], code_root: Path
) -> Path:
    executable = Path(command[0])
    if not executable.is_absolute():
        raise ValueError("command executable must be an absolute path")
    try:
        resolved = executable.resolve(strict=True)
    except OSError as error:
        raise ConfinementUnavailable("command executable is unavailable") from error
    # A venv Python is often a symlink to the system interpreter. Preserve
    # its lexical path so Python still discovers the mounted pyvenv.cfg and
    # site-packages, after verifying both alias and real target are approved.
    target_approved = any(
        resolved == path.resolve() or (path.is_dir() and path.resolve() in resolved.parents)
        for path in runtime_paths
    )
    if target_approved:
        for path in runtime_paths:
            if executable == path or (path.is_dir() and path in executable.parents):
                return _runtime_target(executable, code_root)
    for path in runtime_paths:
        runtime_root = path.resolve()
        if resolved == runtime_root:
            return _runtime_target(path, code_root)
        if path.is_dir() and runtime_root in resolved.parents:
            relative = resolved.relative_to(runtime_root)
            return _runtime_target(path, code_root) / relative
    raise ConfinementUnavailable("command executable is outside read-only runtime paths")


def build_confined_command(
    spec: ConfinementSpec,
    command: Sequence[str],
    settings: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build a structured bwrap command and allowlisted environment.

    Only the selected profile and its private run directory are writable bind
    mounts. Code and runtime paths are mounted read-only at fixed destinations.
    """
    if not command or any(not isinstance(value, str) or "\x00" in value for value in command):
        raise ValueError("command must be a non-empty sequence of strings without NUL bytes")

    profile, private_tmp, code_root, runtime_paths, purpose = _validate_spec(spec)
    executable = _validate_command(command, runtime_paths, code_root)
    argv = [
        _bubblewrap(),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--dir",
        "/opt",
        "--dir",
        _APP_ROOT,
        "--ro-bind",
        str(code_root / "src"),
        f"{_APP_ROOT}/src",
        "--ro-bind",
        str(code_root / "chrome_engine"),
        f"{_APP_ROOT}/chrome_engine",
        "--ro-bind",
        str(code_root / "foxprofile_ext"),
        f"{_APP_ROOT}/foxprofile_ext",
    ]
    if purpose is ConfinementPurpose.PROFILE_PROCESS:
        argv.extend(("--bind", str(profile), _PROFILE_ROOT))
    argv.extend(("--bind", str(private_tmp), _TEMP_ROOT))
    for path in runtime_paths:
        argv.extend(("--ro-bind", str(path), str(_runtime_target(path, code_root))))
    argv.extend(
        (
            "--chdir",
            _APP_ROOT,
            "--remount-ro",
            "/",
            "--",
            str(executable),
            *command[1:],
        )
    )
    environment = build_confined_environment(settings)
    for name in ("FOXPROFILE_CAMOUFOX_EXECUTABLE", "FOXPROFILE_CHROME_EXECUTABLE"):
        value = (settings or {}).get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ConfinementUnavailable("browser executable must be an absolute runtime path")
        if not Path(value).is_file():
            raise ConfinementUnavailable("browser executable is unavailable")
        environment[name] = str(_validate_command((value,), runtime_paths, code_root))
    cache_home = (settings or {}).get("XDG_CACHE_HOME")
    if cache_home is not None:
        if (
            not isinstance(cache_home, str)
            or "\x00" in cache_home
            or not Path(cache_home).is_absolute()
        ):
            raise ConfinementUnavailable("GeoIP cache home must be an absolute runtime path")
        cache = Path(cache_home) / "camoufox" / "geoip"
        if cache not in runtime_paths or not cache.is_dir() or cache != cache.resolve():
            raise ConfinementUnavailable("GeoIP cache requires its exact read-only runtime mount")
        environment["XDG_CACHE_HOME"] = str(_runtime_target(cache, code_root).parents[1])
    return argv, environment


def probe_confinement(
    spec: ConfinementSpec,
    command: Sequence[str],
    settings: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> None:
    """Run a harmless caller-supplied stand-in to verify namespaces before use."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    argv, environment = build_confined_command(spec, command, settings)
    try:
        result = subprocess.run(
            argv,
            env=environment,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConfinementUnavailable(
            "bubblewrap could not start its required namespaces"
        ) from error
    if result.returncode != 0:
        raise ConfinementUnavailable("bubblewrap could not establish its required namespaces")
