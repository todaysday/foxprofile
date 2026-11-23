"""Atomic local-first placement over shared SQLite capacity and worker memory."""

from multiprocessing import get_context

import pytest

from foxprofile_ext.dto import ProcessIdentity
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage.capacity import CapacityLedger, RunStatus
from foxprofile_ext.storage.database import ExtensionDatabase
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.placement import PlacementScheduler
from foxprofile_ext.workers.registry import WorkerRegistry
from src.models.profile import Profile

MANIFEST = "a" * 64
MEMORY = 100


def _metrics(memory, *, cgroup=None, staging=1000, engines=("camoufox", "chrome")):
    return NodeMetrics(memory, cgroup, staging, engines)


def _add_worker(registry, worker_id, memory, *, boot_id="boot-a", engines=("camoufox", "chrome")):
    secret = registry.enroll(worker_id)
    connection = registry.register(worker_id, secret, boot_id, MANIFEST, True)
    registry.heartbeat(
        worker_id,
        boot_id,
        connection.connection_generation,
        _metrics(memory, engines=engines),
    )


def _empty_local():
    return NodeMetrics(0, 0, 0, ())


def test_existing_only_retry_preserves_runs_after_capacity_is_lowered(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    scheduler = PlacementScheduler(
        ledger,
        WorkerRegistry(database, MANIFEST),
        local_metrics=lambda: _metrics(1000),
        browser_memory_bytes=100,
        system_reserve_bytes=20,
        staging_reserve_bytes=0,
    )
    first, second, new = Profile("First"), Profile("Second"), Profile("New")
    admitted = scheduler.reserve((first, second), "master")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE settings SET setting_value = '1' WHERE setting_key = 'max_concurrent_profiles'"
        )
    retry = scheduler.reserve((first,), "master")
    assert retry.newly_reserved == ()
    assert retry.runs[0].ref == admitted.runs[0].ref
    with pytest.raises(DomainError) as error:
        scheduler.reserve((first, new), "master")
    assert error.value.code is ErrorCode.PROFILE_CAPACITY_EXCEEDED
    assert set(ledger.active_runs()) == set(admitted.runs)


def test_starting_inventory_does_not_release_ram_until_a_running_memory_sample(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, MANIFEST)
    _add_worker(registry, "worker-a", 130)
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=_empty_local,
        browser_memory_bytes=100,
        system_reserve_bytes=20,
        staging_reserve_bytes=0,
    )
    (first,) = scheduler.reserve((Profile("First"),), "master").runs
    owner = (first.ref, first.worker_id, first.worker_boot_id, first.connection_generation)
    assert ledger.transition_remote(*owner, RunStatus.RESERVED, RunStatus.TRANSFERRING)
    assert ledger.transition_remote(*owner, RunStatus.TRANSFERRING, RunStatus.STARTING)
    registry.heartbeat(
        first.worker_id,
        first.worker_boot_id,
        first.connection_generation,
        _metrics(130),
        active_run_ids=(first.ref.run_id,),
    )
    assert ledger.transition_remote(*owner, RunStatus.STARTING, RunStatus.RUNNING)
    second = Profile("Second")
    with pytest.raises(DomainError) as overbooked:
        scheduler.reserve((second,), "master")
    assert overbooked.value.code is ErrorCode.NO_WORKER_MEMORY
    with pytest.raises(ValueError, match="same sample"):
        registry.heartbeat(
            first.worker_id,
            first.worker_boot_id,
            first.connection_generation,
            _metrics(130),
            active_run_ids=(first.ref.run_id,),
            memory_accounted_run_ids=("foreign-run",),
        )
    registry.heartbeat(
        first.worker_id,
        first.worker_boot_id,
        first.connection_generation,
        _metrics(130),
        active_run_ids=(first.ref.run_id,),
        memory_accounted_run_ids=(first.ref.run_id,),
    )
    (admitted,) = scheduler.reserve((second,), "master").runs
    assert admitted.worker_id == first.worker_id


def test_scheduler_prefers_local_then_selects_greatest_remaining_worker(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, MANIFEST)
    _add_worker(registry, "worker-z", 230)
    _add_worker(registry, "worker-a", 250)
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=lambda: NodeMetrics(150, None, 1000, ("camoufox",)),
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=50,
        staging_reserve_bytes=100,
    )
    profiles = [Profile(f"Profile {index}", profile_id=f"profile-{index}") for index in range(3)]
    profiles[0].base_revision = 7

    result = scheduler.reserve(profiles, "master-boot")

    assert len(result.newly_reserved) == 3
    local, first_remote, second_remote = result.runs
    assert local.host_id == "master-boot"
    assert local.worker_id is None
    assert local.reserved_memory_bytes == MEMORY
    assert local.base_revision == 7
    assert first_remote.worker_id == "worker-a"
    assert first_remote.host_id == f"worker:worker-a:{first_remote.worker_boot_id}"
    assert first_remote.connection_generation == 1
    assert second_remote.worker_id == "worker-z"


def test_batch_rolls_back_when_last_worker_ram_slot_cannot_fit_all_profiles(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, MANIFEST)
    _add_worker(registry, "worker-a", 130)
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=_empty_local,
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=20,
        staging_reserve_bytes=0,
    )

    with pytest.raises(DomainError) as full:
        scheduler.reserve(
            [Profile("A", profile_id="profile-a"), Profile("B", profile_id="profile-b")],
            "master-boot",
        )
    assert full.value.code is ErrorCode.NO_WORKER_MEMORY
    assert ledger.active_runs() == ()
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM browser_run_placements").fetchone()[0] == 0


def test_forced_local_reservation_does_not_fall_back_to_remote(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, MANIFEST)
    _add_worker(registry, "worker-a", 1000)
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=_empty_local,
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=20,
        staging_reserve_bytes=0,
    )

    with pytest.raises(DomainError) as no_local_memory:
        scheduler.reserve(
            [Profile("Helper", profile_id="helper-profile")], "master-boot", allow_remote=False
        )
    assert no_local_memory.value.code is ErrorCode.NO_WORKER_MEMORY
    assert ledger.active_runs() == ()


def test_zero_free_staging_is_valid_telemetry_but_cannot_place(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, MANIFEST)
    secret = registry.enroll("worker-no-disk")
    connected = registry.register("worker-no-disk", secret, "boot-a", MANIFEST, True)
    registry.heartbeat(
        "worker-no-disk",
        "boot-a",
        connected.connection_generation,
        NodeMetrics(1000, None, 0, ("camoufox",)),
    )
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=lambda: NodeMetrics(1000, None, 0, ("camoufox",)),
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=20,
        staging_reserve_bytes=0,
    )

    with pytest.raises(DomainError) as no_staging:
        scheduler.reserve([Profile("A", profile_id="profile-a")], "master-boot")
    assert no_staging.value.code is ErrorCode.NO_WORKER_MEMORY
    assert ledger.active_runs() == ()


def test_draining_stale_and_incompatible_workers_are_excluded(tmp_path):
    now = [100.0]
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, MANIFEST, clock=lambda: now[0])
    _add_worker(registry, "worker-stale", 1000)
    registry.set_draining("worker-stale", True)
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=_empty_local,
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=20,
        staging_reserve_bytes=0,
        clock=lambda: now[0],
    )
    profile = [Profile("A", profile_id="profile-a")]
    with pytest.raises(DomainError) as draining:
        scheduler.reserve(profile, "master-boot")
    assert draining.value.code is ErrorCode.WORKER_UNAVAILABLE

    registry.set_draining("worker-stale", False)
    now[0] += 30
    with pytest.raises(DomainError) as stale:
        scheduler.reserve(profile, "master-boot")
    assert stale.value.code is ErrorCode.WORKER_UNAVAILABLE

    secret = registry.enroll("worker-incompatible")
    registry.register("worker-incompatible", secret, "boot-a", "b" * 64, True)
    with pytest.raises(DomainError) as incompatible:
        scheduler.reserve(profile, "master-boot")
    assert incompatible.value.code is ErrorCode.RUNTIME_INCOMPATIBLE


def test_exact_running_heartbeat_is_not_double_counted_and_old_uncertainty_is(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, MANIFEST)
    secret = registry.enroll("worker-a")
    registered = registry.register("worker-a", secret, "boot-new", MANIFEST, True)
    registry.heartbeat(
        "worker-a",
        "boot-new",
        registered.connection_generation,
        _metrics(130),
        active_run_ids=("current-run",),
        memory_accounted_run_ids=("current-run",),
    )
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
            "VALUES ('current-run', 'running-profile', 'worker:worker-a:boot-new', 'running', 1)"
        )
        connection.execute(
            "INSERT INTO browser_run_placements "
            "(run_id, worker_id, worker_boot_id, connection_generation, reserved_memory_bytes) "
            "VALUES ('current-run', 'worker-a', 'boot-new', ?, 100)",
            (registered.connection_generation,),
        )
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=_empty_local,
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=20,
        staging_reserve_bytes=0,
    )
    first = scheduler.reserve([Profile("First", profile_id="first-profile")], "master-boot").runs[0]
    assert first.worker_id == "worker-a"

    registry.heartbeat(
        "worker-a",
        "boot-new",
        registered.connection_generation,
        _metrics(300),
        active_run_ids=("current-run",),
        memory_accounted_run_ids=("current-run",),
    )
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
            "VALUES ('old-uncertain', 'old-profile', 'worker:worker-a:boot-old', 'uncertain', 2)"
        )
        connection.execute(
            "INSERT INTO browser_run_placements "
            "(run_id, worker_id, worker_boot_id, connection_generation, reserved_memory_bytes) "
            "VALUES ('old-uncertain', 'worker-a', 'boot-old', 4, 200)"
        )
    with pytest.raises(DomainError) as old_memory:
        scheduler.reserve([Profile("Second", profile_id="second-profile")], "master-boot")
    assert old_memory.value.code is ErrorCode.NO_WORKER_MEMORY


def test_local_running_memory_is_reflected_but_post_sample_claim_stays_charged(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, MANIFEST)

    def local_metrics():
        return NodeMetrics(150, None, 1000, ("camoufox",))

    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=local_metrics,
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=50,
        staging_reserve_bytes=0,
    )

    running = []
    for index in range(2):
        run = scheduler.reserve(
            [Profile(f"Local {index}", profile_id=f"local-{index}")], "master-boot"
        ).runs[0]
        assert ledger.claim(run.ref, "master-boot", ProcessIdentity(700 + index, 100 + index))
        assert ledger.mark_running(run.ref)
        running.append(run)
    assert all(run.host_id == "master-boot" for run in running)

    late = scheduler.reserve([Profile("Late", profile_id="local-late")], "master-boot").runs[0]
    assert ledger.claim(late.ref, "master-boot", ProcessIdentity(710, 110))

    def advance_while_sampling():
        assert ledger.mark_running(late.ref)
        return NodeMetrics(150, None, 1000, ("camoufox",))

    racing = PlacementScheduler(
        ledger,
        registry,
        local_metrics=advance_while_sampling,
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=50,
        staging_reserve_bytes=0,
    )
    with pytest.raises(DomainError) as projected:
        racing.reserve(
            [Profile("Too soon", profile_id="local-too-soon")],
            "master-boot",
            allow_remote=False,
        )
    assert projected.value.code is ErrorCode.NO_WORKER_MEMORY


def _race_worker_slot(path, profile_id, barrier, results):
    database = ExtensionDatabase(path)
    registry = WorkerRegistry(database, MANIFEST)
    scheduler = PlacementScheduler(
        CapacityLedger(database),
        registry,
        local_metrics=_empty_local,
        browser_memory_bytes=MEMORY,
        system_reserve_bytes=20,
        staging_reserve_bytes=0,
    )
    barrier.wait(10)
    try:
        result = scheduler.reserve([Profile(profile_id, profile_id=profile_id)], "master-boot")
        results.put(("reserved", result.runs[0].worker_id))
    except DomainError as error:
        results.put((error.code.value, None))


def test_processes_race_atomically_for_one_worker_ram_slot(tmp_path):
    path = str(tmp_path / "capacity.sqlite3")
    database = ExtensionDatabase(path)
    _add_worker(WorkerRegistry(database, MANIFEST), "worker-a", 130)

    context = get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_worker_slot,
            args=(path, f"profile-{index}", barrier, results),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert sorted(outcome[0] for outcome in outcomes) == [
        ErrorCode.NO_WORKER_MEMORY.value,
        "reserved",
    ]
    assert len(CapacityLedger(ExtensionDatabase(path)).active_runs()) == 1
