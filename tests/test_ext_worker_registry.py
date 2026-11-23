"""Worker enrollment, connection fencing, and validated health telemetry."""

import pytest

from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage.database import ExtensionDatabase
from foxprofile_ext.workers.metrics import NodeMetrics, read_local_metrics
from foxprofile_ext.workers.registry import WorkerRegistry

MANIFEST = "a" * 64


def _metrics(*, memory=4096, cgroup=3072, staging=8192, engines=("camoufox", "chrome")):
    return NodeMetrics(memory, cgroup, staging, engines)


def test_enrollment_persists_hash_only_and_heartbeat_fences_prior_generation(tmp_path):
    now = [100.0]
    database = ExtensionDatabase(tmp_path / "workers.sqlite3")
    registry = WorkerRegistry(database, MANIFEST, clock=lambda: now[0])
    secret = registry.enroll("worker-1")

    with database.read() as connection:
        stored_hash = connection.execute(
            "SELECT credential_hash FROM workers WHERE worker_id = 'worker-1'"
        ).fetchone()[0]
    assert stored_hash != secret
    assert len(stored_hash) == 64
    assert not hasattr(registry.list()[0], "credential")

    with pytest.raises(DomainError) as bad_secret:
        registry.register("worker-1", "wrong-secret", "boot-a", MANIFEST, True)
    assert bad_secret.value.code is ErrorCode.WORKER_UNAVAILABLE

    registered = registry.register("worker-1", secret, "boot-a", MANIFEST, True)
    assert registered.state == "offline"  # no valid telemetry has arrived yet
    first_generation = registered.connection_generation
    heartbeat = registry.heartbeat(
        "worker-1",
        "boot-a",
        first_generation,
        _metrics(),
        active_run_ids=("run-a",),
    )
    assert heartbeat.state == "ready"
    assert heartbeat.metrics == _metrics()

    second = registry.register("worker-1", secret, "boot-b", MANIFEST, True)
    assert second.connection_generation == first_generation + 1
    assert second.boot_id == "boot-b"
    with pytest.raises(DomainError) as stale:
        registry.heartbeat("worker-1", "boot-a", first_generation, _metrics())
    assert stale.value.code is ErrorCode.WORKER_UNAVAILABLE


def test_rotation_revocation_staleness_and_drain_are_fail_closed(tmp_path):
    now = [200.0]
    registry = WorkerRegistry(
        ExtensionDatabase(tmp_path / "workers.sqlite3"), MANIFEST, clock=lambda: now[0]
    )
    old_secret = registry.enroll("worker-2")
    connected = registry.register("worker-2", old_secret, "boot-a", MANIFEST, True)
    registry.heartbeat("worker-2", "boot-a", connected.connection_generation, _metrics())

    registry.set_draining("worker-2", True)
    assert registry.get("worker-2").state == "draining"
    registry.set_draining("worker-2", False)
    assert registry.get("worker-2").state == "ready"

    now[0] += 29.999
    assert registry.get("worker-2").state == "ready"
    now[0] += 0.001
    assert registry.get("worker-2").state == "offline"
    registry.set_draining("worker-2", True)
    assert registry.get("worker-2").state == "offline"
    assert registry.get("worker-2").draining is True
    registry.set_draining("worker-2", False)
    assert registry.get("worker-2").draining is False

    new_secret = registry.rotate("worker-2")
    with pytest.raises(DomainError):
        registry.register("worker-2", old_secret, "boot-a", MANIFEST, True)
    new_connection = registry.register("worker-2", new_secret, "boot-b", MANIFEST, True)
    registry.revoke("worker-2")
    assert registry.get("worker-2").state == "revoked"
    with pytest.raises(DomainError) as revoked:
        registry.heartbeat("worker-2", "boot-b", new_connection.connection_generation, _metrics())
    assert revoked.value.code is ErrorCode.WORKER_UNAVAILABLE


def test_incompatible_preflight_and_invalid_metrics_are_not_selectable(tmp_path):
    registry = WorkerRegistry(ExtensionDatabase(tmp_path / "workers.sqlite3"), MANIFEST)
    secret = registry.enroll("worker-3")
    incompatible = registry.register("worker-3", secret, "boot-a", "b" * 64, True)
    assert incompatible.state == "incompatible"

    with pytest.raises(ValueError):
        NodeMetrics(True, 1, 1, ("camoufox",))
    with pytest.raises(ValueError):
        NodeMetrics(-1, None, 1, ("camoufox",))
    with pytest.raises(ValueError):
        NodeMetrics(1, 0, 1, ("unknown-engine",))
    zero_metrics = NodeMetrics(0, 0, 0, ())
    assert (zero_metrics.mem_available_bytes, zero_metrics.cgroup_headroom_bytes) == (0, 0)


def test_local_metrics_reads_memavailable_and_nested_cgroup_headroom(tmp_path):
    proc = tmp_path / "proc"
    self_dir = proc / "self"
    cgroup_root = tmp_path / "sys/fs/cgroup"
    job = cgroup_root / "tenant/job"
    tenant = cgroup_root / "tenant"
    for path in (self_dir, job, tenant, cgroup_root):
        path.mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text("MemTotal: 4096 kB\nMemAvailable: 2048 kB\n")
    (self_dir / "cgroup").write_text("0::/tenant/job\n")
    (self_dir / "mountinfo").write_text(f"1 0 0:1 / {cgroup_root} rw - cgroup2 cgroup rw\n")
    (cgroup_root / "memory.max").write_text("8192\n")
    (cgroup_root / "memory.current").write_text("1024\n")
    (tenant / "memory.max").write_text("2048\n")
    (tenant / "memory.current").write_text("1024\n")
    (job / "memory.max").write_text("max\n")
    (job / "memory.current").write_text("512\n")

    metrics = read_local_metrics(tmp_path, ("camoufox",), proc_root=proc)
    assert metrics.mem_available_bytes == 2048 * 1024
    assert metrics.cgroup_headroom_bytes == 1024
    assert metrics.staging_free_bytes > 0
    assert metrics.ready_engines == ("camoufox",)

    (tenant / "memory.max").write_text("0\n")
    assert read_local_metrics(tmp_path, proc_root=proc).cgroup_headroom_bytes == 0
    (tenant / "memory.max").write_text("2048\n")
    (tenant / "memory.current").unlink()
    with pytest.raises(ValueError, match="constrained cgroup memory"):
        read_local_metrics(tmp_path, proc_root=proc)
