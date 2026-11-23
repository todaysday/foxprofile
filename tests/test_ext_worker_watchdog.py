"""The independent Worker lease watchdog proves full process-tree cleanup."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from foxprofile_ext.dto import BrowserRunRefDTO
from foxprofile_ext.runtime.processes import RuntimeSupervisor
from foxprofile_ext.runtime.registry import process_identity
from foxprofile_ext.storage.capacity import RunStatus
from foxprofile_ext.workers.execution import WorkerExecutionDatabase
from foxprofile_ext.workers.watchdog import WorkerWatchdog
from tests.test_ext_worker_agent import _worker_owner


def test_watchdog_budgets_cannot_extend_loss_or_cleanup_contract(tmp_path):
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    for values in (
        {"loss_timeout": 30.1},
        {"term_grace": 5.1},
        {"kill_grace": 5.1},
        {"loss_timeout": 0},
    ):
        with pytest.raises(ValueError):
            WorkerWatchdog(database, **values)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux Worker process supervision")
def test_watchdog_kills_exact_owned_groups_without_event_loop_progress(tmp_path):
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    code_root = Path(__file__).resolve().parents[1]
    runtime = RuntimeSupervisor(
        database,
        data_dir=tmp_path / "profiles",
        code_root=code_root,
        runtime_paths=("/usr",),
        worker_execution=True,
    )
    ref = BrowserRunRefDTO("profile-watchdog", "master-run-watchdog")
    context = _worker_owner(ref, worker_id="worker-watchdog", boot_id="boot-watchdog", generation=7)
    assert database.reserve_exact(ref, runtime.host_id, context=context)
    watchdog = WorkerWatchdog(
        database,
        loss_timeout=0.8,
        term_grace=0.2,
        kill_grace=0.2,
    )
    children = []
    try:
        watchdog.start()
        watchdog.accept_generation(7)
        # A stale generation does not renew the independent lease.
        watchdog.renew(6)
        for _ in range(2):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        supervisor, boundary = (process_identity(child.pid) for child in children)
        assert supervisor is not None and boundary is not None
        assert runtime.registry.register_supervisor(ref, runtime.host_id, supervisor)
        assert runtime.registry.publish_boundary(ref, supervisor, boundary)
        assert runtime.registry.publish_runtime_details(
            ref,
            supervisor,
            display=None,
            namespace="pid:[999999999]",
            tmp_path=str(tmp_path / "runtime"),
            xauthority_path=None,
        )
        # Deliberately block this process; only the separate watchdog can react.
        time.sleep(1.1)
        for child in children:
            child.wait(timeout=3)
        # Process death precedes the watchdog's SQLite proof publication.
        proof = None
        deadline = time.monotonic() + 2
        while proof is None and time.monotonic() < deadline:
            proof = next(
                (value for value in database.stopped_proofs() if value.get("run_id") == ref.run_id),
                None,
            )
            if proof is None:
                time.sleep(0.01)
        assert proof == {
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "worker_id": "worker-watchdog",
            "worker_boot_id": "boot-watchdog",
            "connection_generation": 7,
            "terminated": True,
            "process_tree_dead": True,
            "manual_helpers_stopped": True,
            "reason": "loss",
            "shutdown_kind": "watchdog",
        }
        assert runtime.ledger.get(ref).status is RunStatus.CLOSED
    finally:
        watchdog.close()
        for child in children:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
        runtime.shutdown(0)
