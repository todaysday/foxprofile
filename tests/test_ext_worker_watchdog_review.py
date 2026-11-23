"""Deterministic watchdog regressions using owned pipes and synthetic identities."""

import json
import os
import signal
import sys

import pytest

from foxprofile_ext.dto import BrowserRunRefDTO, ProcessIdentity
from foxprofile_ext.runtime.registry import RuntimeRegistry
from foxprofile_ext.storage.capacity import CapacityLedger, ProcessLiveness, RunStatus
from foxprofile_ext.workers import watchdog
from foxprofile_ext.workers.execution import WorkerExecutionDatabase
from tests.test_ext_worker_agent import _worker_owner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Worker watchdog")


@pytest.fixture(autouse=True)
def fixture_boot(monkeypatch):
    monkeypatch.setattr(watchdog, "current_host_id", lambda: "fixture-host")


@pytest.mark.parametrize(
    "mismatch", ["prior-boot", "descriptor-host", "supervisor", "unknown-boot"]
)
def test_watchdog_preserves_unverifiable_boot_and_descriptor_ownership(
    tmp_path, monkeypatch, mismatch
):
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    ledger = CapacityLedger(database)
    ref = BrowserRunRefDTO("fixture-profile", "fixture-run")
    host = "prior-host" if mismatch == "prior-boot" else "fixture-host"
    if mismatch == "unknown-boot":
        host = "fixture:unknown-boot"
        monkeypatch.setattr(watchdog, "current_host_id", lambda: host)
    identity = ProcessIdentity(900006, 128)
    context = _worker_owner(ref, worker_id="fixture-worker", boot_id="old-boot")
    assert database.reserve_exact(ref, host, context=context)
    assert RuntimeRegistry(database, ledger).register_supervisor(ref, host, identity)
    with database.transaction() as db:
        if mismatch == "descriptor-host":
            db.execute("UPDATE runtime_descriptors SET host_id = 'other-host'")
        elif mismatch == "supervisor":
            db.execute("UPDATE runtime_descriptors SET supervisor_start_ticks = 129")
    signals = []
    monkeypatch.setattr(watchdog, "_signal_exact", lambda *args: signals.append(args))
    monkeypatch.setattr(watchdog, "_signal_group", lambda *args: signals.append(args))
    monkeypatch.setattr(watchdog, "_tree_liveness", lambda *_: ProcessLiveness.DEAD)
    watchdog._run_cleanup(
        database, watchdog.current_host_id(), term_grace=0.01, kill_grace=0.01, reason="loss"
    )
    assert signals == []
    assert ledger.get(ref).status is RunStatus.STARTING
    assert database.stopped_proofs() == ()


def test_watchdog_rechecks_claim_after_unclaimed_cancellation_loses(tmp_path, monkeypatch):
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    ledger = CapacityLedger(database)
    registry = RuntimeRegistry(database, ledger)
    ref = BrowserRunRefDTO("fixture-profile", "fixture-run")
    identity = ProcessIdentity(900001, 123)
    assert database.reserve_exact(ref, "fixture-host", context=_worker_owner(ref))
    cancel = CapacityLedger.cancel_unclaimed

    def claim_before_cancel(self, run_ref):
        assert registry.register_supervisor(ref, "fixture-host", identity)
        return cancel(self, run_ref)

    signals = []
    monkeypatch.setattr(CapacityLedger, "cancel_unclaimed", claim_before_cancel)
    monkeypatch.setattr(watchdog, "_signal_exact", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(watchdog, "_tree_liveness", lambda *_: ProcessLiveness.DEAD)
    watchdog._run_cleanup(database, "fixture-host", term_grace=0.1, kill_grace=0.1, reason="loss")
    assert signals == [(identity, signal.SIGTERM)]
    assert ledger.get(ref).status is RunStatus.CLOSED
    assert database.stopped_proofs()[0]["run_id"] == ref.run_id


def test_watchdog_consumes_coalesced_control_messages_before_waiting(tmp_path, monkeypatch):
    identity = ProcessIdentity(900002, 124)
    parent_signals = []
    monkeypatch.setattr(watchdog, "process_identity", lambda _: identity)
    monkeypatch.setattr(watchdog.os, "kill", lambda *args: parent_signals.append(args))
    monkeypatch.setattr(watchdog.signal, "signal", lambda *_: None)
    monkeypatch.setattr(watchdog, "_notify_systemd", lambda *_: None)
    config = {
        "op": "configure",
        "database_path": str(tmp_path / "worker.sqlite"),
        "host_id": "fixture-host",
        "loss_timeout": 0.2,
        "term_grace": 0.05,
        "kill_grace": 0.05,
        "parent_pid": identity.pid,
    }
    messages = [
        config,
        {"op": "welcome", "generation": 1},
        {"op": "renew", "generation": 1},
        {"op": "stop"},
    ]
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, encoding="utf-8") as reader,
        os.fdopen(write_fd, "wb", buffering=0) as writer,
    ):
        writer.write("".join(json.dumps(message) + "\n" for message in messages).encode())
        assert watchdog._watchdog_loop(reader) == 0
    assert parent_signals == []


@pytest.mark.parametrize("same_identity", [True, False])
def test_watchdog_persists_proof_when_exact_supervisor_closed_the_run_first(
    tmp_path, monkeypatch, same_identity
):
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    ledger = CapacityLedger(database)
    registry = RuntimeRegistry(database, ledger)
    ref = BrowserRunRefDTO("fixture-profile", "fixture-run")
    identity = ProcessIdentity(900005, 127)
    assert database.reserve_exact(ref, "fixture-host", context=_worker_owner(ref, generation=7))
    assert registry.register_supervisor(ref, "fixture-host", identity)

    def finish_supervisor(exact, signum):
        assert exact == identity and signum == signal.SIGTERM
        assert ledger.release_after_exit(ref, identity, lambda *_: ProcessLiveness.DEAD)
        if not same_identity:
            with database.transaction() as db:
                db.execute(
                    "UPDATE browser_runs SET start_ticks = start_ticks + 1 WHERE run_id = ?",
                    (ref.run_id,),
                )

    monkeypatch.setattr(watchdog, "_signal_exact", finish_supervisor)
    monkeypatch.setattr(watchdog, "_tree_liveness", lambda *_: ProcessLiveness.DEAD)
    watchdog._run_cleanup(database, "fixture-host", term_grace=0.1, kill_grace=0.1, reason="loss")
    reopened = WorkerExecutionDatabase(database.path)
    if same_identity:
        (proof,) = reopened.stopped_proofs()
        assert proof["run_id"] == ref.run_id and proof["connection_generation"] == 7
        assert proof["process_tree_dead"] is True
    else:
        assert reopened.stopped_proofs() == ()
    assert ledger.get(ref).status is RunStatus.CLOSED
