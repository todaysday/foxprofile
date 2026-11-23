"""Worker outbound protocol, durable execution state, and clean stop replay."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import anyio
import pytest

from foxprofile_ext.dto import BrowserRunRefDTO
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.runtime.processes import RuntimeSupervisor
from foxprofile_ext.runtime.registry import current_host_id, process_identity
from foxprofile_ext.storage.capacity import ProcessLiveness, RunStatus
from foxprofile_ext.workers.agent import WorkerAgent, _profile_from_payload
from foxprofile_ext.workers.execution import WorkerExecutionDatabase, assert_worker_only_database
from foxprofile_ext.workers.manifest import VerifiedManifest

_DIGEST = "a" * 64


class _Watchdog:
    def __init__(self):
        self.accepted = []
        self.renewed = []

    def accept_generation(self, generation, loss_timeout=None):
        self.accepted.append((generation, loss_timeout))

    def renew(self, generation):
        self.renewed.append(generation)

    def close(self, *_args):
        pass


class _FakeSocket:
    def __init__(self, welcome):
        self.welcome = welcome
        self.sent = []

    async def send_json(self, value):
        self.sent.append(value)

    async def receive(self, timeout=None):
        del timeout
        return SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps(self.welcome),
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _SocketContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, socket):
        self.socket = socket
        self.connect_args = None

    def ws_connect(self, *args, **kwargs):
        self.connect_args = (args, kwargs)
        return _SocketContext(self.socket)


def _agent(tmp_path, *, runtime=None, session=None, watchdog=None):
    state_dir = tmp_path / "worker"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    database = WorkerExecutionDatabase(state_dir / "runtime.sqlite3")
    manifest = VerifiedManifest(_DIGEST, ("camoufox",), {"binaries": {}, "native": {}})
    agent = WorkerAgent(
        worker_id="worker-a",
        credential="a-private-worker-credential",
        master_url="wss://master.example.test",
        state_dir=state_dir,
        database=database,
        manifest=manifest,
        runtime=runtime or SimpleNamespace(),
        watchdog=watchdog or _Watchdog(),
        boot_id="11111111-1111-4111-8111-111111111111",
        session=session,
    )
    return agent


def test_worker_execution_database_has_only_exact_run_process_tables(tmp_path):
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    ref = BrowserRunRefDTO("profile-a", "master-run-7")
    assert database.reserve_exact(ref, current_host_id()) is True
    assert database.reserve_exact(ref, current_host_id()) is False
    assert database.run_status(ref) is RunStatus.RESERVED
    with pytest.raises(ValueError):
        database.reserve_exact(BrowserRunRefDTO("profile-a", "../../run"), current_host_id())
    assert_worker_only_database(database.path)
    with database.read() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert names == {"browser_runs", "capacity_recovery", "runtime_descriptors"}
        assert tuple(
            connection.execute(
                "SELECT worker_id, connection_generation FROM browser_run_placements"
            ).fetchone()
        ) == (None, None)


def test_runtime_supervisor_and_capacity_ledger_recover_worker_execution_db(tmp_path):
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    code_root = Path(__file__).resolve().parents[1]
    runtime = RuntimeSupervisor(
        database,
        data_dir=tmp_path / "profiles",
        code_root=code_root,
        runtime_paths=("/usr",),
        worker_execution=True,
    )
    ref = BrowserRunRefDTO("profile-a", "master-run-8")
    assert database.reserve_exact(ref, runtime.host_id)
    processes = []
    try:
        for _ in range(2):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        supervisor, boundary = (process_identity(process.pid) for process in processes)
        assert supervisor is not None and boundary is not None
        assert runtime.registry.register_supervisor(ref, runtime.host_id, supervisor)
        assert runtime.registry.publish_boundary(ref, supervisor, boundary)
        assert runtime.registry.publish_runtime_details(
            ref,
            supervisor,
            display=None,
            namespace="pid:[999999999]",
            tmp_path=str(tmp_path / "private-tmp"),
            xauthority_path=None,
        )
        run = runtime.ledger.get(ref)
        assert run is not None and run.process == supervisor and run.status is RunStatus.STARTING
        report = runtime.recover()
        assert report.confirmed_live == 1

        for process in processes:
            os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            process.wait(timeout=3)
        report = runtime.recover()
        assert report.released_dead == 1
        assert runtime.ledger.get(ref).status is RunStatus.CLOSED
        assert runtime._probe_run(runtime.host_id, supervisor) is ProcessLiveness.DEAD
    finally:
        for process in processes:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        runtime.shutdown(0)


def test_connection_sends_hello_before_accepting_welcome(tmp_path):
    watchdog = _Watchdog()
    socket = _FakeSocket(
        {
            "type": "welcome",
            "worker_id": "worker-a",
            "connection_generation": 4,
            "heartbeat_interval": 10,
            "loss_timeout": 30,
        }
    )
    session = _Session(socket)
    agent = _agent(tmp_path, session=session, watchdog=watchdog)
    anyio.run(agent._connect_once)
    assert socket.sent[0] == {
        "type": "hello",
        "worker_id": "worker-a",
        "boot_id": "11111111-1111-4111-8111-111111111111",
        "manifest_digest": _DIGEST,
        "preflight_ok": True,
    }
    assert session.connect_args[0][0] == "wss://master.example.test/api/v1/workers/connect"
    assert watchdog.accepted == [(4, 30.0)]


def test_worker_rejects_commands_for_other_generation_and_keeps_profile_identity(tmp_path):
    agent = _agent(tmp_path)
    command = {
        "worker_id": "worker-a",
        "boot_id": agent.boot_id,
        "connection_generation": 8,
        "command_id": "command-8",
        "profile_id": "canonical-profile-id",
        "run_id": "master-run-8",
        "operation": "start",
        "payload": {},
    }
    with pytest.raises(ConnectionError):
        agent._validate_command(command, generation=7)
    valid = {**command, "connection_generation": 7}
    agent._validate_command(valid, generation=7)
    profile = _profile_from_payload(
        {
            "profile_id": "canonical-profile-id",
            "name": "renamable catalog label",
            "proxy": None,
            "os_type": "windows",
            "timezone": "UTC",
            "locale": "en-US",
            "engine": "camoufox",
        },
        "canonical-profile-id",
        "master-run-8",
    )
    assert profile.name == "master-run-8"
    assert profile.profile_id == "canonical-profile-id"


def test_pre_spawn_rejection_returns_exact_no_process_proof_and_frees_worker_slot(tmp_path):
    state_dir = tmp_path / "worker"
    state_dir.mkdir(mode=0o700)
    database = WorkerExecutionDatabase(state_dir / "runtime.sqlite3")
    from foxprofile_ext.storage.capacity import CapacityLedger

    ledger = CapacityLedger(database)
    runtime = SimpleNamespace(
        supervisor=SimpleNamespace(host_id=current_host_id()),
        ledger=ledger,
        profile_directory=lambda run_id: state_dir / "profiles" / run_id,
        tree_is_dead=lambda _ref: True,
    )
    agent = _agent(tmp_path, runtime=runtime)

    def reject_capacity(*_args):
        raise DomainError(ErrorCode.NO_WORKER_MEMORY)

    agent._check_local_capacity = reject_capacity
    ref = BrowserRunRefDTO("profile-rejected", "master-run-rejected")
    payload = {
        "profile": {
            "profile_id": ref.profile_id,
            "name": "display-name",
            "engine": "camoufox",
            "os_type": "windows",
        },
        "memory_budget_bytes": 64 * 1024**2,
        "snapshot": {},
    }
    with pytest.raises(DomainError) as failure:
        anyio.run(agent._start_command, ref, payload, 11)
    assert failure.value.code is ErrorCode.NO_WORKER_MEMORY
    assert ledger.get(ref).status is RunStatus.CANCELLED
    proof = agent.database.stopped_proofs()[0]
    assert proof["run_id"] == ref.run_id
    assert proof["profile_id"] == ref.profile_id
    assert proof["connection_generation"] == 11
    assert proof["reason"] == "loss"
    assert proof["terminated"] is True and proof["process_tree_dead"] is True
    assert proof["manual_helpers_stopped"] is True
    assert not runtime.profile_directory(ref.run_id).exists()
    next_ref = BrowserRunRefDTO(ref.profile_id, "master-run-retry")
    assert database.reserve_exact(next_ref, current_host_id()) is True


def test_profile_busy_pre_spawn_proof_has_terminal_tombstone_and_preserves_old_run(tmp_path):
    state_dir = tmp_path / "worker"
    state_dir.mkdir(mode=0o700)
    database = WorkerExecutionDatabase(state_dir / "runtime.sqlite3")
    from foxprofile_ext.storage.capacity import CapacityLedger

    ledger = CapacityLedger(database)
    old_ref = BrowserRunRefDTO("profile-busy", "master-run-existing")
    assert database.reserve_exact(old_ref, current_host_id())
    old_data = state_dir / "profiles" / old_ref.run_id
    old_data.mkdir(parents=True)
    (old_data / "keep.txt").write_text("existing generation", encoding="utf-8")
    runtime = SimpleNamespace(
        supervisor=SimpleNamespace(host_id=current_host_id()),
        ledger=ledger,
        profile_directory=lambda run_id: state_dir / "profiles" / run_id,
        tree_is_dead=lambda _ref: True,
    )
    agent = _agent(tmp_path, runtime=runtime)
    new_ref = BrowserRunRefDTO(old_ref.profile_id, "master-run-rejected-busy")
    payload = {
        "profile": {
            "profile_id": new_ref.profile_id,
            "name": "profile-busy",
            "engine": "camoufox",
            "os_type": "windows",
        },
        "memory_budget_bytes": 64 * 1024**2,
        "snapshot": {},
    }
    with pytest.raises(DomainError) as failure:
        anyio.run(agent._start_command, new_ref, payload, 12)
    assert failure.value.code is ErrorCode.PROFILE_BUSY
    assert database.run_status(old_ref) is RunStatus.RESERVED
    assert database.run_status(new_ref) is RunStatus.CANCELLED
    proof = next(item for item in database.stopped_proofs() if item["run_id"] == new_ref.run_id)
    assert proof["shutdown_kind"] == "pre-spawn"
    assert proof["manual_helpers_stopped"] is True
    assert (
        agent._cleanup_command(new_ref, {"disposition": "closed", "proof": proof})["cleaned"]
        is True
    )
    assert database.run_status(new_ref) is RunStatus.CANCELLED
    assert old_data.is_dir()
    assert (old_data / "keep.txt").read_text(encoding="utf-8") == "existing generation"


def test_terminal_cleanup_removes_only_exact_run_and_replays_proof_idempotently(tmp_path):
    state_dir = tmp_path / "worker"
    state_dir.mkdir(mode=0o700)
    database = WorkerExecutionDatabase(state_dir / "runtime.sqlite3")
    from foxprofile_ext.storage.capacity import CapacityLedger

    ledger = CapacityLedger(database)
    data_dir = state_dir / "profiles" / "master-run-cleanup"
    data_dir.mkdir(parents=True)
    transfer = state_dir / "transfers" / "master-run-cleanup.zip"
    archive = state_dir / "sync" / "master-run-cleanup.zip"
    transfer.parent.mkdir()
    archive.parent.mkdir()
    transfer.write_bytes(b"partial")
    archive.write_bytes(b"staged")
    runtime = SimpleNamespace(
        ledger=ledger,
        profile_directory=lambda run_id: state_dir / "profiles" / run_id,
        tree_is_dead=lambda _ref: True,
    )
    agent = _agent(tmp_path, runtime=runtime)
    ref = BrowserRunRefDTO("profile-cleanup", "master-run-cleanup")
    assert database.reserve_exact(ref, current_host_id())
    assert ledger.cancel_unclaimed(ref)
    context = {
        "profile_id": ref.profile_id,
        "run_id": ref.run_id,
        "worker_id": agent.worker_id,
        "worker_boot_id": agent.boot_id,
        "connection_generation": 5,
        "data_dir": str(data_dir),
        "manual_sessions": {},
    }
    database.put_run_context(ref.run_id, context)
    proof = {
        "profile_id": ref.profile_id,
        "run_id": ref.run_id,
        "worker_id": agent.worker_id,
        "worker_boot_id": agent.boot_id,
        "connection_generation": 5,
        "terminated": True,
        "process_tree_dead": True,
        "manual_helpers_stopped": True,
        "reason": "loss",
    }
    database.record_stop_proof(ref.run_id, proof)
    other = state_dir / "profiles" / "another-run"
    other.mkdir()
    for _ in range(2):
        assert agent._cleanup_command(ref, {"disposition": "closed", "proof": proof}) == {
            "cleaned": True,
            "disposition": "closed",
        }
    assert not data_dir.exists()
    assert not transfer.exists() and not archive.exists()
    assert other.is_dir()
    assert database.stopped_proofs() == (proof,)
    assert database.stopped_proofs(include_acknowledged=False) == ()
    assert database.run_context(ref.run_id)["terminal_acknowledged"] is True


def test_acknowledged_run_retention_is_bounded_without_pruning_live_or_unacked_state(
    tmp_path, monkeypatch
):
    from foxprofile_ext.workers import execution as execution_module

    monkeypatch.setattr(execution_module, "_MAX_STATE_JSON_BYTES", 8192)
    monkeypatch.setattr(execution_module, "_MAX_RETAINED_ACKNOWLEDGED", 3)
    monkeypatch.setattr(execution_module, "_MAX_RETAINED_PROOFS", 32)
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    from foxprofile_ext.storage.capacity import CapacityLedger

    ledger = CapacityLedger(database)
    host_id = current_host_id()
    for index in range(40):
        ref = BrowserRunRefDTO(f"profile-{index}", f"master-run-{index}")
        assert database.reserve_exact(ref, host_id)
        assert database.run_status(ref) is RunStatus.RESERVED
        context = {
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "worker_id": "worker-a",
            "worker_boot_id": "boot-a",
            "connection_generation": 3,
            "manual_sessions": {},
            "private_launch_settings": "x" * 3000,
        }
        database.put_run_context(ref.run_id, context)
        assert ledger.cancel_unclaimed(ref)
        proof = {
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "worker_id": "worker-a",
            "worker_boot_id": "boot-a",
            "connection_generation": 3,
            "terminated": True,
            "process_tree_dead": True,
            "manual_helpers_stopped": True,
            "reason": "loss",
        }
        database.record_stop_proof(ref.run_id, proof)
        assert database.acknowledge_terminal_run(ref.run_id)

    contexts = database._read_json_map("run_context_json")
    proofs = database._read_json_map("stopped_proofs_json")
    assert len(contexts) == len(proofs) == 3
    assert not any("private_launch_settings" in value for value in contexts.values())
    assert database.reserve_exact(BrowserRunRefDTO("profile-next", "master-run-next"), host_id)

    active_ref = BrowserRunRefDTO("profile-live", "master-run-live")
    assert database.reserve_exact(active_ref, host_id)
    database.put_run_context(
        active_ref.run_id,
        {"profile_id": active_ref.profile_id, "run_id": active_ref.run_id},
    )
    unacked_ref = BrowserRunRefDTO("profile-unacked", "master-run-unacked")
    assert database.reserve_exact(unacked_ref, host_id)
    assert ledger.cancel_unclaimed(unacked_ref)
    unacked_proof = {
        "profile_id": unacked_ref.profile_id,
        "run_id": unacked_ref.run_id,
        "worker_id": "worker-a",
        "worker_boot_id": "boot-a",
        "connection_generation": 3,
        "terminated": True,
        "process_tree_dead": True,
        "manual_helpers_stopped": True,
        "reason": "loss",
    }
    database.put_run_context(
        unacked_ref.run_id,
        {"profile_id": unacked_ref.profile_id, "run_id": unacked_ref.run_id},
    )
    database.record_stop_proof(unacked_ref.run_id, unacked_proof)
    assert database.run_context(active_ref.run_id) is not None
    assert database.run_context(unacked_ref.run_id) is not None
    assert unacked_proof in database.stopped_proofs(include_acknowledged=False)


def test_parallel_exit_callback_and_stop_create_one_immutable_clean_archive(tmp_path, monkeypatch):
    from foxprofile_ext.workers import profile_store

    entered_pack = threading.Event()
    allow_pack = threading.Event()
    calls = []

    def pack(_profile_path, _engine, target_path):
        calls.append(target_path)
        entered_pack.set()
        assert allow_pack.wait(3)
        Path(target_path).write_bytes(b"immutable profile snapshot")
        return SimpleNamespace(
            archive_digest=hashlib.sha256(b"immutable profile snapshot").hexdigest(),
            size_bytes=len(b"immutable profile snapshot"),
            identity_digest="b" * 64,
        )

    monkeypatch.setattr(profile_store, "pack_browser_data", pack)
    runtime = SimpleNamespace(
        tree_is_dead=lambda _ref: True,
        profile_directory=lambda run_id: tmp_path / "worker" / "profiles" / run_id,
    )
    agent = _agent(tmp_path, runtime=runtime)
    ref = BrowserRunRefDTO("profile-a", "master-run-9")
    data_dir = agent.runtime.profile_directory(ref.run_id)
    data_dir.mkdir(parents=True)
    agent.database.put_run_context(
        ref.run_id,
        {
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "worker_id": agent.worker_id,
            "worker_boot_id": agent.boot_id,
            "connection_generation": 3,
            "engine": "camoufox",
            "identity_digest": "b" * 64,
            "base_revision": 2,
            "data_dir": str(data_dir),
            "manual_sessions": {},
        },
    )

    async def overlap():
        callback = asyncio.create_task(agent._finalize_stopped(ref, reason="clean", event=True))
        assert await asyncio.to_thread(entered_pack.wait, 2)
        stop_command = asyncio.create_task(
            agent._finalize_stopped(ref, reason="clean", event=False)
        )
        await asyncio.sleep(0)
        allow_pack.set()
        return await asyncio.gather(callback, stop_command)

    first, second = anyio.run(overlap)
    assert len(calls) == 1
    assert first == second
    assert first["reason"] == "clean"
    assert first["archive_digest"] == hashlib.sha256(b"immutable profile snapshot").hexdigest()
    stored = agent.database.stopped_proofs()
    assert stored == (first,)
    replacement = {**first, "reason": "loss", "archive_digest": None}
    agent.database.record_stop_proof(ref.run_id, replacement)
    assert agent.database.stopped_proofs() == (first,)
