"""Worker admission crash and exact no-spawn proof regressions."""

import asyncio
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxprofile_ext.dto import BrowserRunRefDTO
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.runtime.processes import RuntimeSupervisor
from foxprofile_ext.storage.capacity import CapacityLedger, RunStatus
from foxprofile_ext.workers import watchdog
from foxprofile_ext.workers.agent import WorkerRuntime
from foxprofile_ext.workers.coordinator import DistributedCoordinator
from foxprofile_ext.workers.execution import WorkerExecutionDatabase
from foxprofile_ext.workers.metrics import NodeMetrics
from tests.test_ext_worker_agent import _agent, _worker_owner
from tests.test_ext_worker_agent_review import _startup_snapshot
from tests.test_ext_worker_profile_store import _remote_fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Worker process supervision")


class _Interrupted(BaseException):
    pass


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    agent = _agent(tmp_path)
    supervisor = RuntimeSupervisor(
        agent.database,
        data_dir=agent.state_dir / "profiles",
        code_root=Path.cwd(),
        runtime_paths=(),
        worker_execution=True,
    )
    agent.runtime = WorkerRuntime(
        agent.database, agent.state_dir, agent.manifest, supervisor=supervisor
    )
    yield agent
    agent.runtime.shutdown(0)


@pytest.mark.parametrize("busy", [False, True])
def test_owner_write_failure_rolls_back_exact_worker_row(worker, monkeypatch, busy):
    ref = BrowserRunRefDTO("profile", "failed-owner-write")
    host_id = worker.runtime.supervisor.host_id
    if busy:
        old_ref = BrowserRunRefDTO(ref.profile_id, "busy-profile")
        assert worker.database.reserve_exact(old_ref, host_id, context=_worker_owner(old_ref))
    connect = worker.database._connect
    denied = []

    def authorize(operation, table, column, *_args):
        if operation == sqlite3.SQLITE_UPDATE and (table, column) == (
            "capacity_recovery",
            "run_context_json",
        ):
            denied.append(ref)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def denied_connect():
        connection = connect()
        connection.set_authorizer(authorize)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(worker.database, "_connect", denied_connect)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            asyncio.run(worker._start_command(ref, {}, 7))

    reopened = WorkerExecutionDatabase(worker.database.path)
    assert denied == [ref]
    assert reopened.run_status(ref) is None
    assert reopened.run_context(ref.run_id) is None
    assert reopened.stopped_proofs() == ()

    with pytest.raises(DomainError) as retried:
        asyncio.run(worker._start_command(ref, {}, 7))
    expected = ErrorCode.PROFILE_BUSY if busy else ErrorCode.VALIDATION_ERROR
    assert retried.value.code is expected
    assert reopened.run_status(ref) is RunStatus.CANCELLED
    (proof,) = reopened.stopped_proofs()
    assert proof["shutdown_kind"] == "pre-spawn"
    assert proof["connection_generation"] == 7
    if busy:
        assert reopened.run_status(old_ref) is RunStatus.RESERVED


@pytest.mark.parametrize("recover_with", ["agent", "watchdog"])
@pytest.mark.parametrize("interruption", ["reservation", "busy-tombstone", "cancellation"])
def test_recovery_reconstructs_exact_pre_spawn_proof(
    worker, monkeypatch, recover_with, interruption
):
    ref = BrowserRunRefDTO("profile", "interrupted-admission")
    host_id = worker.runtime.supervisor.host_id
    boot_id = worker.boot_id
    if interruption == "busy-tombstone":
        old_ref = BrowserRunRefDTO(ref.profile_id, "busy-profile")
        assert worker.database.reserve_exact(old_ref, host_id, context=_worker_owner(old_ref))

    with monkeypatch.context() as patch:
        if interruption == "cancellation":

            def before_proof(*_args, **_options):
                assert worker.database.run_status(ref) is RunStatus.CANCELLED
                raise _Interrupted

            patch.setattr(worker.database, "record_stop_proof", before_proof)
        else:
            method = "reserve_exact" if interruption == "reservation" else "record_cancelled_exact"
            commit = getattr(worker.database, method)

            def after_commit(*args, **options):
                assert commit(*args, **options)
                raise _Interrupted

            patch.setattr(worker.database, method, after_commit)
        with pytest.raises(_Interrupted):
            asyncio.run(worker._start_command(ref, {}, 7))

    worker.database = WorkerExecutionDatabase(worker.database.path)
    assert worker.database.stopped_proofs() == ()
    if recover_with == "agent":
        CapacityLedger(worker.database).recover(lambda *_: pytest.fail("no PID to probe"))
        worker.boot_id = "replacement-worker-boot"
        worker.connection_generation = 8
        asyncio.run(worker._retire_previous_runs())
    else:
        watchdog._run_cleanup(worker.database, host_id, term_grace=0, kill_grace=0, reason="loss")

    assert worker.database.run_status(ref) is RunStatus.CANCELLED
    assert worker.runtime.ledger.get(ref).process is None
    proofs = tuple(
        proof for proof in worker.database.stopped_proofs() if proof["run_id"] == ref.run_id
    )
    assert proofs == (
        {
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "worker_id": worker.worker_id,
            "worker_boot_id": boot_id,
            "connection_generation": 7,
            "terminated": True,
            "process_tree_dead": True,
            "manual_helpers_stopped": True,
            "reason": "loss",
            "shutdown_kind": "pre-spawn",
        },
    )


def test_default_launcher_early_exit_callback_reports_not_started(worker, tmp_path, monkeypatch):
    cluster = _remote_fixture(tmp_path)
    run = cluster.run
    worker.boot_id = run.worker_boot_id
    worker.connection_generation = run.connection_generation
    assert cluster.ledger.transition_remote(
        run.ref,
        run.worker_id,
        run.worker_boot_id,
        run.connection_generation,
        RunStatus.TRANSFERRING,
        RunStatus.STARTING,
    )
    local = SimpleNamespace(
        ledger=cluster.ledger,
        runtime=SimpleNamespace(host_id="master-local"),
        launcher=SimpleNamespace(),
        profile_by_id=lambda _: cluster.profile,
        authorize=lambda principal, _: principal,
        lock_path=str(tmp_path / "catalog.lock"),
    )
    master = DistributedCoordinator(
        local,
        None,
        cluster.workers,
        SimpleNamespace(),
        cluster.store,
        "https://master.example.test",
    )
    worker._metrics = lambda: NodeMetrics(2 << 30, None, 2 << 30, ("camoufox",))
    payload = _startup_snapshot(tmp_path, worker, run.ref)
    callback_published = threading.Event()
    wait_for_launch = worker.runtime.launcher.wait_for_launch

    def fail_before_spawn(ref):
        assert ref == run.ref
        assert worker.runtime.profile_directory(ref.run_id).is_dir()
        raise OSError("injected pre-spawn failure")

    def await_callback(*args):
        assert callback_published.wait(3), "launcher callback did not publish proof"
        return wait_for_launch(*args)

    def emitted(message):
        if message["type"] == "stopped":
            callback_published.set()

    monkeypatch.setattr(worker.runtime.supervisor, "_require_open_and_reserved", fail_before_spawn)
    monkeypatch.setattr(worker.runtime.launcher, "wait_for_launch", await_callback)
    monkeypatch.setattr(worker, "_emit_event", emitted)

    async def start():
        worker._loop = asyncio.get_running_loop()
        with pytest.raises(DomainError) as failure:
            await worker._start_command(run.ref, payload, run.connection_generation)
        assert failure.value.code is ErrorCode.WORKER_UNAVAILABLE

    asyncio.run(start())
    assert worker.database.run_status(run.ref) is RunStatus.CANCELLED
    assert worker.runtime.ledger.get(run.ref).process is None
    (proof,) = worker.database.stopped_proofs()
    assert proof["shutdown_kind"] == "pre-spawn"
    assert proof["manual_helpers_stopped"] is True
    assert not worker.runtime.profile_directory(run.ref.run_id).exists()
    master.accept_stop_proof(run, proof)
    assert master.ledger.get(run.ref).status is RunStatus.CLOSED
    with master.ledger.database.read() as connection:
        outcome = connection.execute(
            "SELECT storage_outcome FROM browser_run_placements WHERE run_id = ?", (run.ref.run_id,)
        ).fetchone()[0]
    assert outcome == "not_started"


def test_default_launcher_refusal_discards_prepared_generation(worker, tmp_path, monkeypatch):
    ref = BrowserRunRefDTO("profile", "refused-launch")
    worker._metrics = lambda: NodeMetrics(2 << 30, None, 2 << 30, ("camoufox",))
    payload = _startup_snapshot(tmp_path, worker, ref)
    unrelated = object()
    monkeypatch.setitem(worker.runtime.launcher._active_sessions, ref.run_id, unrelated)

    with pytest.raises(DomainError) as refused:
        asyncio.run(worker._start_command(ref, payload, 7))

    assert refused.value.code is ErrorCode.PROFILE_BUSY
    assert worker.database.run_status(ref) is RunStatus.CANCELLED
    assert ref not in worker.runtime.supervisor._prepared_runs
    assert worker.runtime.launcher._active_sessions[ref.run_id] is unrelated
    (proof,) = worker.database.stopped_proofs()
    assert proof["shutdown_kind"] == "pre-spawn"


@pytest.mark.parametrize("recover_with", ["agent", "watchdog"])
def test_pre_spawn_proof_waits_for_verified_helper_cleanup(worker, recover_with):
    ref = BrowserRunRefDTO("profile", "unverified-helper")
    host_id = worker.runtime.supervisor.host_id
    assert worker.database.reserve_exact(
        ref,
        host_id,
        context={
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "worker_id": worker.worker_id,
            "worker_boot_id": worker.boot_id,
            "connection_generation": 7,
            "manual_sessions": {"unverified": {"pid": 900001, "start_ticks": 123}},
        },
    )
    assert worker.runtime.ledger.cancel_unclaimed(ref)
    if recover_with == "agent":
        asyncio.run(worker._finish_failed_start(ref))
    else:
        watchdog._run_cleanup(worker.database, host_id, term_grace=0, kill_grace=0, reason="loss")

    assert worker.database.stopped_proofs() == ()


@pytest.mark.parametrize(
    "invalid_owner",
    [
        {"profile_id": "other-profile"},
        {"run_id": "other-run"},
        {"worker_id": " "},
        {"worker_boot_id": None},
        {"connection_generation": False},
        {"connection_generation": 0},
    ],
)
def test_exact_worker_row_rejects_invalid_owner_before_writing(worker, monkeypatch, invalid_owner):
    ref = BrowserRunRefDTO("profile", "invalid-owner")
    host_id = worker.runtime.supervisor.host_id
    context = _worker_owner(ref) | invalid_owner
    with monkeypatch.context() as patch:
        patch.setattr(worker.database, "transaction", lambda: pytest.fail("unexpected DB write"))
        for insert in (worker.database.reserve_exact, worker.database.record_cancelled_exact):
            with pytest.raises(ValueError, match="invalid exact worker run owner"):
                insert(ref, host_id, context=context)

    assert worker.database.run_status(ref) is None
    assert worker.database.run_context(ref.run_id) is None
