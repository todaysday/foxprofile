"""Cross-process capacity admission and run lifecycle over real SQLite."""

from multiprocessing import get_context

import pytest

from foxprofile_ext.capacity import CapacityService
from foxprofile_ext.dto import (
    PrincipalDTO,
    PrincipalKind,
    ProcessIdentity,
    ProfileRefDTO,
    Role,
)
from foxprofile_ext.errors import DomainError, ErrorCode, ProfileCapacityExceeded
from foxprofile_ext.storage import (
    AssignmentDAO,
    CapacityLedger,
    ExtensionDatabase,
    ProcessLiveness,
    RunStatus,
    SettingsDAO,
    UserDAO,
)

ADMIN = PrincipalDTO("admin-a", PrincipalKind.PERSONAL, Role.ADMIN, "session-a")
USER = PrincipalDTO("user-a", PrincipalKind.PERSONAL, Role.USER, "session-b")
SERVICE = PrincipalDTO("automation", PrincipalKind.SERVICE)
PROFILE_A = ProfileRefDTO("profile-a", "A")
PROFILE_B = ProfileRefDTO("profile-b", "B")


def _setup(path, limit=10):
    database = ExtensionDatabase(path)
    users = UserDAO(database)
    users.bootstrap_admin("admin-a", "admin-a", "hash")
    users.create("user-a", "user-a", "hash")
    assignments = AssignmentDAO(database)
    assignments.replace_for_user("user-a", ["profile-a", "profile-b"])
    SettingsDAO(database).set_capacity(limit)
    ledger = CapacityLedger(database)
    return database, ledger, CapacityService(ledger, assignments)


def _race_reserve(path, profile_id, barrier, results):
    database = ExtensionDatabase(path)
    service = CapacityService(CapacityLedger(database), AssignmentDAO(database))
    profile = ProfileRefDTO(profile_id, profile_id)
    barrier.wait(10)
    try:
        admission = service.reserve(USER, [profile], "host-a")
        results.put(("reserved", admission.runs[0].ref.run_id, len(admission.newly_reserved)))
    except ProfileCapacityExceeded:
        results.put(("full", None, 0))


def test_two_independent_processes_compete_atomically_for_last_place(tmp_path):
    path = str(tmp_path / "capacity.sqlite3")
    _setup(path, limit=1)
    context = get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_race_reserve, args=(path, profile_id, barrier, results))
        for profile_id in ("profile-a", "profile-b")
    ]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert sorted((item[0], item[2]) for item in outcomes) == [
        ("full", 0),
        ("reserved", 1),
    ]
    assert len(CapacityLedger(ExtensionDatabase(path)).active_runs()) == 1


def test_batch_authorizes_all_targets_and_reserves_none_when_forbidden_or_too_large(tmp_path):
    database, ledger, service = _setup(tmp_path / "capacity.sqlite3", limit=1)
    first = service.reserve(USER, [PROFILE_A, PROFILE_A], "host-a")
    assert len(first.runs) == 1
    assert first.newly_reserved == (first.runs[0].ref,)
    with pytest.raises(DomainError) as denied:
        service.reserve(USER, [PROFILE_B, ProfileRefDTO("foreign", "Hidden")], "host-a")
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert len(ledger.active_runs()) == 1

    with pytest.raises(ProfileCapacityExceeded):
        service.reserve(USER, [PROFILE_B], "host-a")
    assert [run.ref.profile_id for run in ledger.active_runs()] == ["profile-a"]


@pytest.mark.parametrize("principal", [ADMIN, USER, SERVICE])
def test_privileged_or_service_identity_does_not_bypass_capacity(tmp_path, principal):
    _, ledger, service = _setup(tmp_path / f"{principal.subject_id}.sqlite3", limit=1)
    service.reserve(principal, [PROFILE_A], "host-a")
    with pytest.raises(ProfileCapacityExceeded):
        service.reserve(principal, [PROFILE_B], "host-a")
    assert len(ledger.active_runs()) == 1


def test_existing_run_is_reused_and_unclaimed_cancel_rejects_late_child_claim(tmp_path):
    _, ledger, service = _setup(tmp_path / "capacity.sqlite3", limit=1)
    first_admission = service.reserve(USER, [PROFILE_A], "host-a")
    first = first_admission.runs[0]
    reused_admission = service.reserve(USER, [PROFILE_A], "host-a")
    reused = reused_admission.runs[0]
    assert first == reused
    assert first_admission.newly_reserved == (first.ref,)
    assert reused_admission.newly_reserved == ()
    assert len(ledger.active_runs()) == 1

    assert ledger.cancel_unclaimed(first.ref)
    assert not ledger.claim(first.ref, "host-a", ProcessIdentity(101, 2000))
    assert ledger.get(first.ref).status is RunStatus.CANCELLED


def test_claimed_stopping_run_requires_matching_exit_identity_to_release(tmp_path):
    _, ledger, _ = _setup(tmp_path / "capacity.sqlite3", limit=1)
    run = ledger.reserve(["profile-a"], "host-a").runs[0]
    process = ProcessIdentity(101, 2000)
    assert ledger.claim(run.ref, "host-a", process)
    assert ledger.mark_running(run.ref)
    assert ledger.mark_stopping(run.ref)
    assert not ledger.cancel_unclaimed(run.ref)

    def dead(host, process):
        return ProcessLiveness.DEAD

    assert not ledger.release_after_exit(run.ref, ProcessIdentity(101, 2001), dead)
    assert len(ledger.active_runs()) == 1
    assert not ledger.release_after_exit(
        run.ref, process, lambda host, identity: ProcessLiveness.UNKNOWN
    )
    assert ledger.release_after_exit(run.ref, process, dead)
    assert not ledger.release_after_exit(run.ref, process, dead)
    replacement = ledger.reserve(["profile-a"], "host-a").runs[0]
    assert replacement.ref != run.ref
    assert not ledger.release_after_exit(run.ref, process, dead)


def test_restart_recovery_cancels_unclaimed_and_releases_only_proven_dead(tmp_path):
    database, ledger, _ = _setup(tmp_path / "capacity.sqlite3", limit=2)
    pending = ledger.reserve(["profile-a", "profile-b"], "host-a").runs
    process = ProcessIdentity(102, 3000)
    assert ledger.claim(pending[1].ref, "host-a", process)

    report = CapacityLedger(ExtensionDatabase(database.path)).recover(
        lambda host, identity: ProcessLiveness.DEAD
    )
    assert (report.cancelled_unclaimed, report.released_dead, report.uncertain) == (1, 1, 0)
    assert not CapacityLedger(ExtensionDatabase(database.path)).active_runs()
    assert not ledger.claim(pending[0].ref, "host-a", ProcessIdentity(103, 4000))


def test_unknown_process_liveness_blocks_new_admission_until_reconciled(tmp_path):
    database, ledger, service = _setup(tmp_path / "capacity.sqlite3", limit=2)
    run = ledger.reserve(["profile-a"], "host-a").runs[0]
    identity = ProcessIdentity(104, 5000)
    assert ledger.claim(run.ref, "host-a", identity)
    unknown = ledger.recover(lambda host, process: ProcessLiveness.UNKNOWN)
    assert unknown.uncertain == 1
    with pytest.raises(DomainError) as blocked:
        service.reserve(USER, [PROFILE_B], "host-a")
    assert blocked.value.code is ErrorCode.RUNTIME_RECOVERING

    resolved = ledger.recover(lambda host, process: ProcessLiveness.LIVE)
    assert resolved.confirmed_live == 1
    assert service.reserve(USER, [PROFILE_B], "host-a")
