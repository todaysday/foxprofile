"""Explicit opt-in and capacity budgets for a single distributed Master."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DistributedSettings:
    public_url: str
    manifest_digest: str
    browser_memory_bytes: int
    system_reserve_bytes: int
    staging_reserve_bytes: int
    manifest_path: Path
    native_evidence_path: Path

    @classmethod
    def from_environment(cls):
        if os.getenv("FOXPROFILE_WORKERS_ENABLED", "false").lower() not in {"true", "1", "yes"}:
            return None
        url = os.getenv("FOXPROFILE_MASTER_URL", "").rstrip("/")
        try:
            parsed = urlsplit(url)
            _ = parsed.port  # Validate malformed and out-of-range explicit ports.
        except ValueError:
            raise ValueError("FOXPROFILE_MASTER_URL must be an HTTPS origin") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("FOXPROFILE_MASTER_URL must be an HTTPS origin")
        digest = os.getenv("FOXPROFILE_RUNTIME_MANIFEST_SHA256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Configure a verified FOXPROFILE_RUNTIME_MANIFEST_SHA256")
        paths = []
        for name in ("FOXPROFILE_RUNTIME_MANIFEST", "FOXPROFILE_NATIVE_EVIDENCE"):
            path = Path(os.getenv(name, ""))
            if not path.is_absolute():
                raise ValueError(f"{name} must be an absolute preflight file path")
            paths.append(path)
        values = []
        for name, default in (
            ("FOXPROFILE_BROWSER_MEMORY_BYTES", 1024**3),
            ("FOXPROFILE_SYSTEM_RESERVE_BYTES", 512 * 1024**2),
            ("FOXPROFILE_STAGING_RESERVE_BYTES", 2 * 1024**3),
        ):
            value = int(os.getenv(name, str(default)))
            if value < 0 or (not value and "BROWSER" in name):
                raise ValueError(f"{name} must be a positive byte budget")
            values.append(value)
        return cls(url, digest, *values, *paths)


def build_distributed_coordinator(local, database, catalog, assignments, settings):
    from .channel import MasterWorkerHub
    from .coordinator import DistributedCoordinator, PlacementCapacity
    from .manifest import load_manifest
    from .metrics import read_local_metrics
    from .placement import PlacementScheduler
    from .profile_store import ProfileStore
    from .registry import WorkerRegistry

    manifest = load_manifest(
        settings.manifest_path, settings.native_evidence_path, settings.manifest_digest
    )
    for engine in manifest.ready_engines:
        configured = local.runtime.settings.get(f"FOXPROFILE_{engine.upper()}_EXECUTABLE")
        pinned = Path(manifest.runtime["binaries"][engine]["path"]).resolve()
        if configured is None or Path(configured).resolve() != pinned:
            raise ValueError(f"Master {engine} executable must match its verified runtime manifest")
    registry = WorkerRegistry(database, settings.manifest_digest)
    for worker in registry.list():
        if worker.boot_id and worker.connection_generation:
            registry.disconnect(worker.worker_id, worker.boot_id, worker.connection_generation)
    staging = database.path.with_name(".foxprofile-worker-transfers")
    staging.mkdir(parents=True, mode=0o700, exist_ok=True)
    if staging.is_symlink() or staging.stat().st_mode & 0o077:
        raise ValueError("Worker transfer staging must be a private directory")
    scheduler = PlacementScheduler(
        local.ledger,
        registry,
        local_metrics=lambda: read_local_metrics(staging, manifest.ready_engines),
        browser_memory_bytes=settings.browser_memory_bytes,
        system_reserve_bytes=settings.system_reserve_bytes,
        staging_reserve_bytes=settings.staging_reserve_bytes,
    )
    capacity = PlacementCapacity(local.ledger, assignments, scheduler, local.profile_by_id)
    # Helpers use the same global RAM/profile ledger, but operate on Master's
    # stopped canonical directory and therefore must execute locally.
    local.runtime.capacity = PlacementCapacity(
        local.ledger,
        assignments,
        scheduler,
        local.profile_by_id,
        allow_remote=False,
    )
    return DistributedCoordinator(
        local,
        capacity,
        registry,
        MasterWorkerHub(registry),
        ProfileStore(
            database,
            Path(catalog.manager.data_dir),
            staging,
            profile_by_id=local.profile_by_id,
            staging_reserve_bytes=settings.staging_reserve_bytes,
        ),
        settings.public_url,
    )
