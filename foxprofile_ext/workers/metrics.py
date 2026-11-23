"""Validated worker resource metrics and Linux host measurements."""

import mmap
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
    memory_reserve_bytes: int = 0

    def __post_init__(self) -> None:
        _resource_bytes(self.mem_available_bytes, "mem_available_bytes")
        if self.cgroup_headroom_bytes is not None:
            _resource_bytes(self.cgroup_headroom_bytes, "cgroup_headroom_bytes")
        _resource_bytes(self.staging_free_bytes, "staging_free_bytes")
        _resource_bytes(self.memory_reserve_bytes, "memory_reserve_bytes")
        if not isinstance(self.ready_engines, tuple):
            raise ValueError("ready_engines must be a tuple")
        if any(
            not isinstance(engine, str) or engine not in _ENGINES for engine in self.ready_engines
        ):
            raise ValueError("ready_engines contains an unsupported engine")
        if len(set(self.ready_engines)) != len(self.ready_engines):
            raise ValueError("ready_engines cannot contain duplicates")

    @classmethod
    def from_report(cls, *, ready_engines, memory_reserve_bytes, **measurements) -> "NodeMetrics":
        if not isinstance(ready_engines, list):
            raise ValueError("reported ready_engines must be a list")
        return cls(
            **measurements,
            ready_engines=tuple(ready_engines),
            memory_reserve_bytes=memory_reserve_bytes,
        )

    @property
    def usable_memory_bytes(self) -> int:
        available = self.mem_available_bytes
        if self.cgroup_headroom_bytes is not None:
            available = min(available, self.cgroup_headroom_bytes)
        return max(0, available - self.memory_reserve_bytes)


def read_local_metrics(
    staging_path: str | Path,
    ready_engines: tuple[str, ...] = (),
    *,
    proc_root: str | Path = "/proc",
    memory_reserve_bytes: int = 0,
) -> NodeMetrics:
    """Read MemAvailable, effective cgroup headroom, and staging free space."""
    proc_path = Path(proc_root)
    mem_available = _read_mem_available(proc_path / "meminfo")
    cgroup_headroom = _read_cgroup_headroom(proc_path)
    staging_free = shutil.disk_usage(staging_path).free
    return NodeMetrics(
        mem_available, cgroup_headroom, staging_free, ready_engines, memory_reserve_bytes
    )


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
    except OSError as error:
        raise ValueError("cannot read cgroup metadata") from error
    if not memberships or not mounts:
        raise ValueError("cgroup metadata is missing")
    hierarchies = _cgroup_mounts(mounts)
    candidates: list[int | None] = []
    for line in memberships:
        fields = line.split(":", 2)
        if len(fields) != 3 or not fields[0].isdecimal():
            raise ValueError("invalid cgroup membership")
        hierarchy, controllers, member = fields
        if (
            (hierarchy == "0") != (controllers == "")
            or (controllers and not re.fullmatch(r"[\w.=-]+(?:,[\w.=-]+)*", controllers))
            or not PurePosixPath(member).is_absolute()
            or ".." in PurePosixPath(member).parts
        ):
            raise ValueError("invalid cgroup membership")
        if controllers == "":
            candidate = _headroom_for_mount(
                hierarchies, "cgroup2", member, "memory.max", "memory.current"
            )
        elif "memory" in controllers.split(","):
            candidate = _headroom_for_mount(
                hierarchies,
                "cgroup",
                member,
                "memory.limit_in_bytes",
                "memory.usage_in_bytes",
                "memory",
            )
        else:
            continue
        candidates.append(candidate)
    if not candidates and any(
        kind == "cgroup2" or "memory" in controllers for kind, controllers, _, _ in hierarchies
    ):
        raise ValueError("cgroup memory membership is unavailable")
    return min((value for value in candidates if value is not None), default=None)


def _cgroup_mounts(lines: list[str]) -> list[tuple[str, set[str], PurePosixPath, Path]]:
    mounts = []
    for line in lines:
        before, separator, after = line.partition(" - ")
        fields, mount = before.split(), after.split()
        if (
            not separator
            or len(fields) < 6
            or len(mount) != 3
            or not fields[0].isdecimal()
            or not fields[1].isdecimal()
            or not re.fullmatch(r"\d+:\d+", fields[2])
        ):
            raise ValueError("invalid cgroup mount table")
        if mount[0] in {"cgroup", "cgroup2"}:
            root = PurePosixPath(_unescape_mount(fields[3]))
            mountpoint = Path(_unescape_mount(fields[4]))
            if not root.is_absolute() or not mountpoint.is_absolute():
                raise ValueError("invalid cgroup mount path")
            mounts.append((mount[0], set(mount[2].split(",")), root, mountpoint))
    return mounts


def _headroom_for_mount(
    mounts: list[tuple[str, set[str], PurePosixPath, Path]],
    filesystem: str,
    membership: str,
    limit_file: str,
    usage_file: str,
    controller: str | None = None,
) -> int | None:
    values: list[int] = []
    matched = False
    member_path = PurePosixPath(membership)
    for kind, controllers, mount_root, mountpoint in mounts:
        if kind != filesystem or (controller and controller not in controllers):
            continue
        # A subtree-only mount hides ancestors that can impose a tighter limit.
        if mount_root != PurePosixPath("/"):
            continue
        # A cgroup namespace can also present its own non-root group as "/".
        if kind == "cgroup2" and (mountpoint / "cgroup.type").is_file():
            continue
        matched = True
        current = mountpoint / member_path.relative_to(mount_root)
        while True:
            headroom = _read_limit_usage(
                current / limit_file,
                current / usage_file,
                unified_root=filesystem == "cgroup2" and current == mountpoint,
            )
            if headroom is not None:
                values.append(headroom)
            if current == mountpoint:
                break
            current = current.parent
    if not matched:
        raise ValueError("cgroup memory hierarchy is not mounted")
    return min(values) if values else None


def _read_limit_usage(limit_path: Path, usage_path: Path, *, unified_root: bool) -> int | None:
    try:
        limit_text = limit_path.read_text(encoding="ascii").strip()
    except FileNotFoundError as error:
        if limit_path.name == "memory.max":
            try:
                controllers = limit_path.with_name("cgroup.controllers").read_text(encoding="ascii")
            except OSError as exc:
                raise ValueError("cannot determine cgroup memory controller availability") from exc
            # v2 exposes memory.max only on non-root, memory-enabled groups.
            if unified_root or "memory" not in controllers.split():
                return None
        raise ValueError("missing cgroup memory limit") from error
    except OSError as error:
        raise ValueError("cannot read cgroup memory limit") from error
    if limit_text == "max" and limit_path.name == "memory.max":
        return None
    if not limit_text.isdecimal():
        raise ValueError("invalid cgroup memory limit")
    limit = int(limit_text)
    if limit_path.name == "memory.limit_in_bytes" and limit == (1 << 63) - mmap.PAGESIZE:
        return None
    _resource_bytes(limit, "cgroup memory limit")
    try:
        usage = int(usage_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as error:
        # A known finite limit with unknown usage is not unlimited memory.
        raise ValueError("cannot measure constrained cgroup memory") from error
    if usage < 0 or usage > _MAX_PLAUSIBLE_BYTES:
        raise ValueError("invalid cgroup memory accounting")
    return max(0, limit - usage)


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)
