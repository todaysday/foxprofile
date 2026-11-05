"""SQLite lease invariants, without browser or VNC processes."""

from multiprocessing import get_context

import pytest

from foxprofile_ext.dto import ManualSessionState, ProcessIdentity
from foxprofile_ext.storage import (
    ExtensionDatabase,
    HelperLiveness,
    ManualLeaseBusy,
    ManualLeaseDAO,
    SessionDAO,
    UserDAO,
)


def _acquire(dao, *, owner="user-a", personal="personal-a", profile="profile-a", run="run-a"):
    return dao.acquire(
        f"manual-{owner}",
        owner,
        personal,
        profile,
        run,
        "host-a",
        100,
        200,
        220,
    )


def _race_lease(path, owner, barrier, results):
    dao = ManualLeaseDAO(ExtensionDatabase(path))
    barrier.wait(10)
    try:
        result = _acquire(dao, owner=owner, profile=f"profile-{owner}", run=f"run-{owner}")
        results.put(("created" if result.created else "reused", result.record.session_id))
    except ManualLeaseBusy as error:
        results.put(("busy", str(error)))


def test_two_processes_can_claim_only_one_installation_lease(tmp_path):
    path = str(tmp_path / "manual.sqlite3")
    ExtensionDatabase(path)
    context = get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_race_lease, args=(path, owner, barrier, results))
        for owner in ("user-a", "user-b")
    ]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(15)
        assert process.exitcode == 0

    assert sorted(outcome[0] for outcome in outcomes) == ["busy", "created"]
    busy = next(outcome for outcome in outcomes if outcome[0] == "busy")
    assert "user-" not in busy[1] and "profile-" not in busy[1]
    assert ManualLeaseDAO(ExtensionDatabase(path)).current() is not None


def test_same_owner_session_and_run_idempotently_reuses_lease(tmp_path):
    dao = ManualLeaseDAO(ExtensionDatabase(tmp_path / "manual.sqlite3"))
    first = _acquire(dao)
    retry = dao.acquire(
        "new-request-id",
        "user-a",
        "personal-a",
        "profile-a",
        "run-a",
        "host-a",
        110,
        210,
        230,
    )
    assert first.created
    assert not retry.created
    assert retry.record == first.record
    with pytest.raises(ManualLeaseBusy):
        _acquire(dao, owner="user-b", personal="personal-b", profile="foreign", run="foreign-run")


def test_lease_remains_occupied_through_closing_and_recovery_until_exit_proof(tmp_path):
    database = ExtensionDatabase(tmp_path / "manual.sqlite3")
    dao = ManualLeaseDAO(database)
    lease = _acquire(dao).record
    assert dao.transition(
        lease.session_id,
        "user-a",
        "personal-a",
        "profile-a",
        "run-a",
        "host-a",
        ManualSessionState.PROVISIONING,
        ManualSessionState.ACTIVE,
        101,
    )
    process = ProcessIdentity(101, 9000)
    assert dao.register_helper(
        lease.session_id, "user-a", "personal-a", "profile-a", "run-a", "host-a", process, 102
    )
    assert dao.transition(
        lease.session_id,
        "user-a",
        "personal-a",
        "profile-a",
        "run-a",
        "host-a",
        ManualSessionState.ACTIVE,
        ManualSessionState.CLOSING,
        103,
    )
    assert dao.transition(
        lease.session_id,
        "user-a",
        "personal-a",
        "profile-a",
        "run-a",
        "host-a",
        ManualSessionState.CLOSING,
        ManualSessionState.RECOVERING,
        104,
    )
    with pytest.raises(ManualLeaseBusy):
        _acquire(dao, owner="user-b", personal="personal-b", profile="profile-b", run="run-b")

    exact = dict(
        session_id=lease.session_id,
        owner_user_id="user-a",
        personal_session_id="personal-a",
        profile_id="profile-a",
        browser_run_id="run-a",
        host_id="host-a",
        updated_at=105,
    )
    assert not dao.close_after_exit(**exact, probe=lambda host, identity: HelperLiveness.LIVE)
    assert dao.current().state is ManualSessionState.RECOVERING
    assert dao.close_after_exit(**exact, probe=lambda host, identity: HelperLiveness.DEAD)
    closed = dao.current()
    assert closed is None
    with database.read() as connection:
        row = connection.execute(
            "SELECT state FROM manual_leases WHERE session_id = ?", (lease.session_id,)
        ).fetchone()
    assert row[0] == ManualSessionState.CLOSED.value
    assert dao.get(lease.session_id).state is ManualSessionState.CLOSED
    assert not dao.close_after_exit(**exact, probe=lambda host, identity: HelperLiveness.DEAD)

    replacement = _acquire(
        dao, owner="user-b", personal="personal-b", profile="profile-b", run="run-b"
    )
    assert replacement.created
    assert dao.current().session_id == replacement.record.session_id


def test_stale_transition_or_close_cannot_change_replacement_lease(tmp_path):
    dao = ManualLeaseDAO(ExtensionDatabase(tmp_path / "manual.sqlite3"))
    old = _acquire(dao).record
    assert dao.close_after_exit(
        old.session_id,
        "user-a",
        "personal-a",
        "profile-a",
        "run-a",
        "host-a",
        101,
        lambda host, identity: HelperLiveness.DEAD,
    )
    new = _acquire(dao, owner="user-b", personal="personal-b", profile="profile-b", run="run-b")
    assert not dao.transition(
        old.session_id,
        "user-a",
        "personal-a",
        "profile-a",
        "run-a",
        "host-a",
        ManualSessionState.PROVISIONING,
        ManualSessionState.ACTIVE,
        102,
    )
    assert dao.current() == new.record


def test_logout_does_not_delete_live_lease_and_private_helper_is_not_repr(tmp_path):
    database = ExtensionDatabase(tmp_path / "manual.sqlite3")
    users = UserDAO(database)
    users.bootstrap_admin("admin-a", "admin-a", "hash")
    users.create("user-a", "user-a", "hash")
    sessions = SessionDAO(database)
    sessions.create("personal-a", "user-a", "token-digest", 1, 20)

    dao = ManualLeaseDAO(database)
    lease = _acquire(dao).record
    helper = ProcessIdentity(120, 4500)
    assert dao.register_helper(
        lease.session_id, "user-a", "personal-a", "profile-a", "run-a", "host-a", helper, 101
    )
    sessions.revoke("personal-a")
    assert dao.current().helper == helper
    assert "4500" not in repr(dao.current())


@pytest.mark.parametrize(
    "times", [(100, float("inf"), 220), (100, 200, float("nan")), (True, 200, 220)]
)
def test_invalid_lease_timestamps_are_rejected(tmp_path, times):
    dao = ManualLeaseDAO(ExtensionDatabase(tmp_path / "manual.sqlite3"))
    with pytest.raises(ValueError):
        dao.acquire("m", "u", "s", "p", "r", "h", *times)
