"""Validated worker resource metrics and Linux host measurements."""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_MAX_PLAUSIBLE_BYTES = 1 << 60
_ENGINES = frozenset({"camoufox", "chrome"})


def _resource_bytes(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_PLAUSIBLE_BYTES:
        raise ValueError(f"{label} must be a non-negative plausible byte count")
    return value


@dataclass(frozen=True, slots=True)
class NodeMetrics:
    mem_available_bytes: int
    cgroup_headroom_bytes: int | None
    staging_free_bytes: int
    ready_engines: tuple[str, ...]

    def __post_init__(self) -> None:
        _resource_bytes(self.mem_available_bytes, "mem_available_bytes")
        if self.cgroup_headroom_bytes is not None:
            _resource_bytes(self.cgroup_headroom_bytes, "cgroup_headroom_bytes")
        _resource_bytes(self.staging_free_bytes, "staging_free_bytes")
        if not isinstance(self.ready_engines, tuple):
            raise ValueError("ready_engines must be a tuple")
        if any(
            not isinstance(engine, str) or engine not in _ENGINES for engine in self.ready_engines
        ):
            raise ValueError("ready_engines contains an unsupported engine")
        if len(set(self.ready_engines)) != len(self.ready_engines):
            raise ValueError("ready_engines cannot contain duplicates")


def read_local_metrics(
    staging_path: str | Path,
    ready_engines: tuple[str, ...] = (),
    *,
    proc_root: str | Path = "/proc",
) -> NodeMetrics:
    """Read MemAvailable, effective cgroup headroom, and staging free space."""
    proc_path = Path(proc_root)
    mem_available = _read_mem_available(proc_path / "meminfo")
    cgroup_headroom = _read_cgroup_headroom(proc_path)
    staging_free = shutil.disk_usage(staging_path).free
    return NodeMetrics(mem_available, cgroup_headroom, staging_free, ready_engines)


def _read_mem_available(path: Path) -> int:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ValueError("cannot read Linux MemAvailable") from exc
    for line in lines:
        match = re.fullmatch(r"MemAvailable:\s+(\d+)\s+kB", line)
        if match:
            return _resource_bytes(int(match.group(1)) * 1024, "MemAvailable")
    raise ValueError("Linux MemAvailable is unavailable")


def _read_cgroup_headroom(proc_root: Path) -> int | None:
    try:
        memberships = (proc_root / "self/cgroup").read_text(encoding="ascii").splitlines()
        mounts = (proc_root / "self/mountinfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    candidates: list[int] = []
    for line in memberships:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        controllers, member = fields[1], fields[2]
        if controllers == "":
            candidate = _headroom_for_mount(
                mounts, "cgroup2", member, "memory.max", "memory.current"
            )
        elif "memory" in controllers.split(","):
            candidate = _headroom_for_mount(
                mounts, "cgroup", member, "memory.limit_in_bytes", "memory.usage_in_bytes", "memory"
            )
        else:
            continue
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    return _resource_bytes(min(candidates), "cgroup headroom")


def _headroom_for_mount(
    mount_lines: list[str],
    filesystem: str,
    membership: str,
    limit_file: str,
    usage_file: str,
    controller: str | None = None,
) -> int | None:
    values: list[int] = []
    for line in mount_lines:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        before_fields = before.split()
        after_fields = after.split()
        if len(before_fields) < 5 or len(after_fields) < 3 or after_fields[0] != filesystem:
            continue
        if controller is not None and controller not in after_fields[2].split(","):
            continue
        mount_root = _unescape_mount(before_fields[3])
        mountpoint = Path(_unescape_mount(before_fields[4]))
        member_path = Path(membership)
        try:
            relative = member_path.relative_to(mount_root)
        except ValueError:
            relative = Path(str(member_path).lstrip("/"))
        cgroup_path = mountpoint / relative
        current = cgroup_path
        while True:
            headroom = _read_limit_usage(current / limit_file, current / usage_file)
            if headroom is not None:
                values.append(headroom)
            if current == mountpoint or current.parent == current:
                break
            current = current.parent
    return min(values) if values else None


def _read_limit_usage(limit_path: Path, usage_path: Path) -> int | None:
    try:
        limit_text = limit_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        # A controller may impose no limit at this ancestor.
        return None
    except OSError as error:
        raise ValueError("cannot read cgroup memory limit") from error
    if limit_text == "max":
        return None
    try:
        limit = int(limit_text)
    except ValueError as error:
        raise ValueError("invalid cgroup memory limit") from error
    if limit >= _MAX_PLAUSIBLE_BYTES:
        return None
    try:
        usage = int(usage_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as error:
        # A known finite limit with unknown usage is not unlimited memory.
        raise ValueError("cannot measure constrained cgroup memory") from error
    if limit < 0 or usage < 0 or usage > _MAX_PLAUSIBLE_BYTES:
        raise ValueError("invalid cgroup memory accounting")
    return max(0, limit - usage)


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)
