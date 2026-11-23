"""Durable distributed run phases and remote ownership recovery safety."""

import sqlite3

import pytest

from foxprofile_ext.dto import BrowserRunRefDTO, ProcessIdentity
from foxprofile_ext.errors import DomainError, ErrorCode, ProfileCapacityExceeded
from foxprofile_ext.storage.capacity import CapacityLedger, ProcessLiveness, RunStatus
from foxprofile_ext.storage.dao import SettingsDAO
from foxprofile_ext.storage.database import ExtensionDatabase


def _remote_run(
    database,
    *,
    run_id="run-a",
    profile_id="profile-a",
    status="reserved",
    pid=None,
    start_ticks=None,
):
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO browser_runs (run_id, profile_id, host_id, status, pid, start_ticks, created_at) "
            "VALUES (?, ?, 'worker:worker-a:boot-a', ?, ?, ?, 1)",
            (run_id, profile_id, status, pid, start_ticks),
        )
        connection.execute(
            "INSERT INTO browser_run_placements "
            "(run_id, worker_id, worker_boot_id, connection_generation, reserved_memory_bytes, base_revision) "
            "VALUES (?, 'worker-a', 'boot-a', 4, 512, 8)",
            (run_id,),
        )
    return BrowserRunRefDTO(profile_id, run_id)


def test_all_distributed_phases_are_active_for_capacity_and_profile_uniqueness(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    phases = (
        "reserved",
        "transferring",
        "starting",
        "running",
        "stopping",
        "syncing",
        "uncertain",
    )
    with database.transaction() as connection:
        for index, status in enumerate(phases):
            connection.execute(
                "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
                "VALUES (?, ?, 'local-host', ?, ?)",
                (f"run-{index}", f"profile-{index}", status, index),
            )
    assert {run.status.value for run in ledger.active_runs()} == set(phases)

    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
            "VALUES ('unique-transfer', 'same-profile', 'worker:one:boot', 'transferring', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
                "VALUES ('unique-uncertain', 'same-profile', 'worker:two:boot', 'uncertain', 2)"
            )
        connection.execute(
            "UPDATE browser_runs SET status = 'syncing' WHERE run_id = 'unique-transfer'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO browser_runs (run_id, profile_id, host_id, status, created_at) "
                "VALUES ('unique-second', 'same-profile', 'worker:two:boot', 'reserved', 3)"
            )

    SettingsDAO(database).set_capacity(len(phases) + 2)
    with pytest.raises(ProfileCapacityExceeded):
        ledger.reserve(("new-a", "new-b"), "local-host")
    assert len(ledger.active_runs()) == len(phases) + 1


def test_lifecycle_snapshot_keeps_state_and_execution_location_together(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    local = ledger.reserve(("local-profile",), "master-host").runs[0]
    remote = _remote_run(database, status="syncing")

    snapshot = ledger.lifecycle_snapshot()

    assert snapshot == {
        local.ref.profile_id: ("reserved", "local"),
        remote.profile_id: ("syncing", "remote"),
    }
    assert ledger.lifecycle_states() == {
        profile_id: state for profile_id, (state, _location) in snapshot.items()
    }

    with database.transaction() as connection:
        connection.execute("UPDATE capacity_recovery SET blocked = 1 WHERE singleton = 1")
    assert ledger.lifecycle_snapshot()[remote.profile_id] == ("recovering", "remote")


def test_remote_pid_and_no_pid_recovery_never_release_or_globally_block(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    ref = _remote_run(database, status="running", pid=4200, start_ticks=99)
    probed = []

    assert not ledger.release_after_exit(
        ref,
        ProcessIdentity(4200, 99),
        lambda host, process: probed.append((host, process)) or ProcessLiveness.DEAD,
    )
    report = ledger.recover(
        lambda host, process: probed.append((host, process)) or ProcessLiveness.DEAD
    )
    assert report.uncertain == 1
    assert probed == []
    assert ledger.get(ref).status is RunStatus.UNCERTAIN
    assert ledger.get(ref).reserved_memory_bytes == 512

    local = ledger.reserve(("profile-b",), "master-boot").runs[0]
    assert local.status is RunStatus.RESERVED
    assert not ledger.cancel_unclaimed(ref)

    second = _remote_run(database, run_id="run-b", profile_id="profile-c", status="reserved")
    report = ledger.recover(lambda *_: pytest.fail("remote no-PID runs must not be probed"))
    assert report.uncertain == 2
    assert ledger.get(second).status is RunStatus.UNCERTAIN
    assert not ledger.cancel_unclaimed(second)


@pytest.mark.parametrize("next_location", ["local", "remote"])
def test_unsaved_outcome_survives_restart_and_pre_spawn_failure_until_success(
    tmp_path, next_location
):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    lost = _remote_run(database, status="uncertain")
    assert ledger.complete_remote(
        lost, "worker-a", "boot-a", 4, process_stopped=True, quarantined=True
    )
    assert ledger.active_runs() == ()
    ledger = CapacityLedger(ExtensionDatabase(database.path))
    assert ledger.lifecycle_snapshot() == {lost.profile_id: ("unsaved", "remote")}

    cancelled = ledger.reserve((lost.profile_id,), "master").newly_reserved[0]
    assert ledger.cancel_unclaimed(cancelled)
    never_started = _remote_run(database, run_id="no-child", status="uncertain")
    assert ledger.complete_remote(
        never_started,
        "worker-a",
        "boot-a",
        4,
        process_stopped=True,
        quarantined=True,
        no_process_created=True,
    )
    assert ledger.lifecycle_snapshot() == {lost.profile_id: ("unsaved", "remote")}

    if next_location == "remote":
        replacement = _remote_run(database, run_id="saved", status="syncing")
        assert ledger.lifecycle_snapshot()[lost.profile_id] == ("syncing", "remote")
        assert ledger.complete_remote(
            replacement, "worker-a", "boot-a", 4, process_stopped=True, sync_verified=True
        )
    else:
        replacement = ledger.reserve((lost.profile_id,), "master").newly_reserved[0]
        process = ProcessIdentity(123, 456)
        assert ledger.claim(replacement, "master", process)
        assert ledger.lifecycle_snapshot()[lost.profile_id] == ("starting", "local")
        from foxprofile_ext.workers.profile_store import ProfileStore
        from src.models.profile import Profile

        profile = Profile("saved", profile_id=lost.profile_id)
        store = ProfileStore(database, tmp_path / "data", tmp_path / "stage")
        directory = store.data_dir / profile.name
        directory.mkdir()
        (directory / "fingerprint.json").write_text('{"os":"linux","fingerprint":{"fixture":1}}')
        assert ledger.mark_running(replacement, track_local_data=True)
        assert ledger.release_after_exit(replacement, process, lambda *_: ProcessLiveness.DEAD)
        store.record_local_stop(profile, ledger.get(replacement))
    assert ledger.active_runs() == ()
    assert ledger.lifecycle_snapshot() == {}


def test_remote_transitions_and_completion_are_exact_generation_cas(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    ref = _remote_run(database)
    record = ledger.get(ref)
    assert (record.worker_id, record.worker_boot_id, record.connection_generation) == (
        "worker-a",
        "boot-a",
        4,
    )
    assert record.base_revision == 8

    assert not ledger.transition_remote(
        ref, "worker-a", "boot-a", 3, RunStatus.RESERVED, RunStatus.TRANSFERRING
    )
    assert not ledger.transition_remote(
        ref, "worker-a", "other-boot", 4, RunStatus.RESERVED, RunStatus.TRANSFERRING
    )
    assert ledger.transition_remote(
        ref, "worker-a", "boot-a", 4, RunStatus.RESERVED, RunStatus.TRANSFERRING
    )
    assert ledger.transition_remote(
        ref, "worker-a", "boot-a", 4, RunStatus.TRANSFERRING, RunStatus.STARTING
    )
    assert ledger.mark_remote_uncertain(
        ref, "worker-a", "boot-a", 4, expected_status=RunStatus.STARTING
    )
    assert not ledger.complete_remote(
        ref, "worker-a", "boot-a", 4, process_stopped=False, sync_verified=True
    )
    assert not ledger.complete_remote(
        ref, "worker-a", "boot-a", 3, process_stopped=True, sync_verified=True
    )
    assert ledger.complete_remote(
        ref, "worker-a", "boot-a", 4, process_stopped=True, quarantined=True
    )
    assert ledger.get(ref).status is RunStatus.CLOSED
    assert not ledger.active_runs()


def test_worker_uncertainty_is_per_profile_not_global_capacity_recovery(tmp_path):
    database = ExtensionDatabase(tmp_path / "capacity.sqlite3")
    ledger = CapacityLedger(database)
    ref = _remote_run(database, status="syncing")

    assert ledger.mark_remote_uncertain(ref, "worker-a", "boot-a", 4)
    with pytest.raises(DomainError) as locked:
        ledger.reserve(("profile-a",), "master-boot")
    assert locked.value.code is ErrorCode.PROFILE_BUSY
    assert ledger.reserve(("profile-b",), "master-boot").runs[0].ref.profile_id == "profile-b"
