"""Bounded lifecycle retention and concurrent distributed control regressions."""

import asyncio
import gc
import hashlib
import shutil
import sqlite3
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxprofile_ext.application import ProtectedApplication
from foxprofile_ext.dto import BrowserRunRefDTO, ProcessIdentity, ProfileRefDTO
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.runtime.processes import current_host_id
from foxprofile_ext.storage import CapacityLedger, ExtensionDatabase
from foxprofile_ext.storage.capacity import RunStatus
from foxprofile_ext.workers import profile_store
from foxprofile_ext.workers.channel import MasterWorkerHub, WorkerConnection
from foxprofile_ext.workers.coordinator import DistributedCoordinator
from foxprofile_ext.workers.manual import ManualRelay
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.registry import WorkerRegistry
from src.models.profile import Profile
from tests.test_ext_distributed_integration import ACTOR, GIB, ProtocolWorker, launch, stop_proof
from tests.test_ext_distributed_integration import cluster as cluster
from tests.test_ext_worker_agent import _agent
from tests.test_ext_worker_profile_store import _write_browser_state, _write_identity


def test_worker_releases_launch_profiles_and_idle_run_locks(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    agent.connection_generation = 5
    runtime = agent.runtime
    runtime.clean_exit = lambda *_: True
    ledger = runtime.ledger = CapacityLedger(agent.database)
    runtime.profile_directory = lambda run_id: agent.state_dir / "profiles" / run_id
    host_id = current_host_id()
    runtime.supervisor = SimpleNamespace(host_id=host_id)
    process = ProcessIdentity(123, 456)
    retained = {}
    runtime.descriptor = lambda ref: (
        SimpleNamespace(
            browser=process,
            display=":91",
            namespace="pid:[12]",
            tmp_path="/fixture",
            xauthority_path="/fixture/Xauthority",
        )
        if agent.database.run_status(ref) is RunStatus.RUNNING
        else None
    )
    runtime.tree_is_dead = lambda ref: (
        agent.database.run_status(ref) in {RunStatus.CLOSED, RunStatus.CANCELLED}
    )

    async def start(profile, ref, **_kwargs):
        retained[ref.run_id] = weakref.ref(profile)
        if ref.run_id == "fixture-run-33":
            raise RuntimeError("fixture spawn rejected before claiming")
        assert ledger.claim(ref, host_id, process)
        assert ledger.mark_running(ref)
        return "running"

    def stop(ref, _timeout):
        assert ledger.mark_stopping(ref)
        return ledger.release_after_exit(ref, process, lambda *_: "dead")

    runtime.start, runtime.stop = start, stop
    agent._manual_backend = object()
    monkeypatch.setattr(
        agent, "_metrics", lambda: NodeMetrics(32 * GIB, None, 32 * GIB, ("camoufox", "chrome"))
    )
    monkeypatch.setattr(agent, "manifest", SimpleNamespace(ready_engines=("camoufox", "chrome")))
    archives = {}
    for engine in ("camoufox", "chrome"):
        source = tmp_path / engine
        source.mkdir()
        _write_identity(source, engine)
        _write_browser_state(source)
        archive = tmp_path / (engine + ".zip")
        archives[engine] = (archive, profile_store.pack_browser_data(source, engine, archive))

    async def download(url, headers, destination, size, digest):
        archive = archives[Path(url).name][0]
        assert archive.stat().st_size == size
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
        shutil.copyfile(archive, destination)

    monkeypatch.setattr(agent, "_download_snapshot", download)
    proxy = "http://fixture:placeholder@192.0.2.10:8080"

    async def run_generations():
        for index in range(34):
            engine = "chrome" if index == 1 else "camoufox"
            ref = BrowserRunRefDTO("fixture-profile", f"fixture-run-{index:02d}")
            archive, info = archives[engine]
            payload = {
                "profile": Profile(
                    "fixture",
                    proxy=proxy,
                    os_type="linux",
                    engine=engine,
                    profile_id=ref.profile_id,
                ).to_dict(),
                "memory_budget_bytes": 1024,
                "snapshot": {
                    "url": "https://master.example.test/" + engine,
                    "token": "f" * 32,
                    "size_bytes": info.size_bytes,
                    "archive_digest": info.archive_digest,
                    "base_revision": 1,
                    "identity_digest": info.identity_digest,
                },
            }

            async def dispatch(operation, payload, ref=ref):
                return await agent._dispatch(
                    {**asdict(ref), "operation": operation, "payload": payload}, 5
                )

            if index == 33:
                with pytest.raises(RuntimeError, match="fixture spawn rejected"):
                    await dispatch("start", payload)
                proof = agent._proof_for_run(ref.run_id)
                assert proof["reason"] == "loss"
            else:
                assert await dispatch("start", payload) == {"state": "running"}
                assert agent.database.run_context(ref.run_id)["engine"] == engine
                if index < 2:
                    # Probe admission up to relay payload validation without opening a helper.
                    with pytest.raises(DomainError) as failure:
                        await dispatch("manual_open", {"session_id": "fixture-manual"})
                    expected = {
                        "camoufox": ErrorCode.VALIDATION_ERROR,
                        "chrome": ErrorCode.MANUAL_UNSUPPORTED_RUNTIME,
                    }
                    assert failure.value.code is expected[engine]
                proof = (await dispatch("stop", {}))["proof"]
                assert proof["reason"] == "clean"
            assert await dispatch("cleanup", {"proof": proof, "disposition": "closed"}) == {
                "cleaned": True,
                "disposition": "closed",
            }
            assert not runtime.profile_directory(ref.run_id).exists()
            assert not (agent.state_dir / "sync" / f"{ref.run_id}.zip").exists()
            if index == 0:
                with pytest.raises(DomainError) as failure:
                    await dispatch("manual_open", {"session_id": "fixture-manual"})
                assert failure.value.code is ErrorCode.MANUAL_UNSUPPORTED_RUNTIME

    asyncio.run(run_generations())
    gc.collect()
    assert ledger.active_runs() == ()
    assert agent.database.run_context("fixture-run-00") is None
    assert len(agent.database.stopped_proofs()) == 32
    assert not agent.database.stopped_proofs(include_acknowledged=False)
    assert all(reference() is None for reference in retained.values())
    assert not agent._run_locks
    assert not agent._finalize_locks


def _application(cluster, profiles):
    catalog = SimpleNamespace(
        lock_path=cluster.coordinator.lock_path,
        manager=SimpleNamespace(_data_path=lambda name: cluster.directory.parent / name),
        list_profiles=lambda: tuple(profiles),
    )
    return ProtectedApplication(
        SimpleNamespace(validate_principal=lambda principal: principal),
        catalog,
        cluster.coordinator.capacity.assignments,
        cluster.ledger,
        cluster.coordinator,
    )


def _upload_pending(cluster):
    run = launch(cluster)
    cluster.hub.fail_sync = True
    with pytest.raises(DomainError) as failure:
        cluster.coordinator.stop(ACTOR, cluster.ref)
    assert failure.value.code is ErrorCode.SYNC_FAILED
    archive = cluster.hub.path.parent / "return.zip"
    return run, archive, cluster.hub.last_upload


def _clear_worker_copy(hub):
    shutil.rmtree(hub.path)
    (hub.path.parent / "return.zip").unlink()


def test_master_releases_idle_run_locks_after_normal_stops(cluster):
    for _ in range(3):
        run = launch(cluster)
        assert cluster.coordinator.stop(ACTOR, cluster.ref)
        assert cluster.ledger.get(run.ref).status is RunStatus.CLOSED
        _clear_worker_copy(cluster.hub)
    gc.collect()
    assert not cluster.coordinator._locks


@pytest.mark.parametrize("repair_closed", [False, True])
def test_restart_only_releases_unfinished_saved_receipts(cluster, monkeypatch, repair_closed):
    historical = []
    for _ in range(3):
        historical.append(launch(cluster).ref.run_id)
        assert cluster.coordinator.stop(ACTOR, cluster.ref)
        _clear_worker_copy(cluster.hub)

    unsaved = launch(cluster)
    with monkeypatch.context() as patch:
        patch.setattr(cluster.store, "cleanup_run", lambda _: None)
        cluster.coordinator.accept_stop_proof(unsaved, stop_proof(unsaved, reason="loss"))
    assert cluster.store.get_snapshot(unsaved.ref.run_id) is not None
    shutil.rmtree(cluster.hub.path)

    pending, archive, upload = _upload_pending(cluster)
    receipt = cluster.store.commit(
        cluster.profile, pending, archive, pending.base_revision, upload["archive_digest"]
    )
    if repair_closed:
        assert cluster.ledger.complete_remote(
            *cluster.coordinator._owner(pending), process_stopped=True, quarantined=True
        )
        assert cluster.ledger.get(pending.ref).status is RunStatus.CLOSED
    else:
        cluster.ledger.recover(lambda *_: pytest.fail("remote process probe attempted"))
        assert cluster.ledger.get(pending.ref).status is RunStatus.UNCERTAIN

    actual_get, actual_complete = cluster.ledger.get, cluster.ledger.complete_remote
    reads, completed = [], []

    def get(ref):
        reads.append(ref.run_id)
        return actual_get(ref)

    def complete(ref, *owner, **flags):
        completed.append((ref.run_id, flags["commit_receipt_id"]))
        return actual_complete(ref, *owner, **flags)

    monkeypatch.setattr(cluster.ledger, "get", get)
    monkeypatch.setattr(cluster.ledger, "complete_remote", complete)
    restarted = DistributedCoordinator(
        cluster.local,
        cluster.coordinator.capacity,
        cluster.registry,
        ProtocolWorker(archive.parent),
        cluster.store,
        "https://master.example.test",
    )
    try:
        assert reads == [pending.ref.run_id]
        assert completed == [(pending.ref.run_id, receipt.receipt_id)]
        assert actual_get(pending.ref).status is RunStatus.CLOSED
        assert cluster.store.get_snapshot(pending.ref.run_id) is None
        assert cluster.store.get_snapshot(unsaved.ref.run_id) is None
        with cluster.db.read() as db:
            outcomes = dict(
                db.execute("SELECT run_id, storage_outcome FROM browser_run_placements")
            )
        assert outcomes[pending.ref.run_id] == "saved"
        assert outcomes[unsaved.ref.run_id] == "unsaved"
        assert all(outcomes[run_id] == "saved" for run_id in historical)
    finally:
        restarted.shutdown(timeout=0)


def test_worker_waiters_keep_one_operation_lock(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    ref = BrowserRunRefDTO("fixture-profile", "fixture-run")

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        active = 0
        calls = 0

        async def stop(_ref, _payload):
            nonlocal active, calls
            active += 1
            calls += 1
            assert active == 1
            if calls == 1:
                entered.set()
                await release.wait()
            await asyncio.sleep(0)
            active -= 1
            return {"stopped": True}

        monkeypatch.setattr(agent, "_stop_command_serialized", stop)
        first = asyncio.create_task(agent._stop_command(ref, {}))
        await entered.wait()
        second = asyncio.create_task(agent._stop_command(ref, {}))
        await asyncio.sleep(0)
        release.set()
        third = asyncio.create_task(agent._stop_command(ref, {}))
        assert await asyncio.gather(first, second, third) == [{"stopped": True}] * 3
        assert calls == 3

    asyncio.run(scenario())
    gc.collect()
    assert not agent._run_locks


def test_master_waiters_keep_one_operation_lock(cluster):
    coordinator = cluster.coordinator
    waiting, acquired = threading.Event(), threading.Event()
    selected = []
    first = coordinator._run_lock("fixture-run")

    def waiter():
        lock = coordinator._run_lock("fixture-run")
        selected.append(weakref.ref(lock))
        waiting.set()
        with lock:
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with first:
            future = pool.submit(waiter)
            assert waiting.wait(1)
            gc.collect()
            assert coordinator._run_lock("fixture-run") is first
            assert not acquired.is_set()
        future.result(timeout=2)
    del first
    gc.collect()
    assert acquired.is_set()
    assert selected[0]() is None
    assert not coordinator._locks


def test_relay_database_wait_keeps_event_loop_responsive(tmp_path, monkeypatch):
    database = ExtensionDatabase(tmp_path / "manual.sqlite")
    registry = WorkerRegistry(database, "a" * 64)
    token = registry.enroll("worker-a")
    worker = registry.register("worker-a", token, "boot-a", "a" * 64, True)
    registry.heartbeat(
        "worker-a",
        "boot-a",
        worker.connection_generation,
        NodeMetrics(8 * GIB, None, 8 * GIB, ("camoufox",)),
    )
    hub = MasterWorkerHub(registry)
    coordinator = SimpleNamespace(hub=hub, public_url="https://example.test")
    relay = ManualRelay(coordinator)
    run = SimpleNamespace(
        worker_id="worker-a",
        worker_boot_id="boot-a",
        connection_generation=worker.connection_generation,
    )
    reads = []
    watch = threading.Event()
    attempted = threading.Event()
    db_lock = threading.Event()
    callback = threading.Event()
    before_release = []
    actual_guard = registry.connection_guard

    @contextmanager
    def guard(*owner):
        reads.append(threading.get_ident())
        if watch.is_set():
            attempted.set()
        with actual_guard(*owner):
            yield

    monkeypatch.setattr(registry, "connection_guard", guard)

    class FakeWebSocket:
        def __init__(self):
            self.received = asyncio.Queue()
            self.sent = asyncio.Queue()
            self.accepted = asyncio.Event()

        async def accept(self):
            self.accepted.set()

        async def receive_bytes(self):
            return await self.received.get()

        async def send_bytes(self, data):
            await self.sent.put(data)

        async def close(self):
            pass

    def lock_database():
        with sqlite3.connect(database.path, isolation_level=None) as blocking:
            blocking.execute("BEGIN EXCLUSIVE")
            db_lock.set()
            assert attempted.wait(2)
            before_release.append(callback.wait(0.075))
            blocking.execute("COMMIT")

    async def exchange():
        loop = asyncio.get_running_loop()
        ws = FakeWebSocket()
        hub._connections["worker-a"] = WorkerConnection(
            "worker-a", "boot-a", worker.connection_generation, ws, loop
        )
        relay_id, channel, credentials = relay.create(run)
        task = asyncio.create_task(relay.serve(ws, relay_id, credentials["token"]))
        try:
            await asyncio.wait_for(ws.accepted.wait(), 2)
            channel.socket.setblocking(False)
            for _ in range(12):
                ws.received.put_nowait(b"from worker")
                assert (
                    await asyncio.wait_for(loop.sock_recv(channel.socket, 64), 2) == b"from worker"
                )
                await loop.sock_sendall(channel.socket, b"to worker")
                assert await asyncio.wait_for(ws.sent.get(), 2) == b"to worker"
            assert len(reads) == 25
            assert all(thread_id != threading.get_ident() for thread_id in reads)
            with ThreadPoolExecutor(max_workers=1) as pool:
                locker = pool.submit(lock_database)
                assert await asyncio.to_thread(db_lock.wait, 2)
                watch.set()
                loop.call_later(0.005, callback.set)
                ws.received.put_nowait(b"while busy")
                actual = await asyncio.wait_for(loop.sock_recv(channel.socket, 64), 2)
                assert actual == b"while busy"
                locker.result(timeout=2)
            assert before_release == [True]
            assert await asyncio.to_thread(callback.wait, 1)
            registry.revoke("worker-a")
            ws.received.put_nowait(b"after revoke")
            await asyncio.wait_for(task, 2)
            assert channel.revoked
            assert relay_id not in relay._relays
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            relay.revoke(relay_id)

    asyncio.run(exchange())


def test_bulk_stop_reaches_other_worker_while_first_sync_waits(cluster, monkeypatch, tmp_path):
    alpha = launch(cluster)
    beta = Profile("beta", os_type="linux")
    beta_dir = cluster.directory.parent / beta.name
    beta_dir.mkdir()
    _write_identity(beta_dir, "camoufox")
    _write_browser_state(beta_dir)
    beta_ref = ProfileRefDTO(beta.profile_id, beta.name)
    by_id = {item.profile_id: item for item in (cluster.profile, beta)}
    cluster.coordinator.profile_by_id = by_id.__getitem__
    cluster.coordinator.capacity.profile_by_id = by_id.__getitem__
    registry = cluster.registry
    credential = registry.enroll("worker-b")
    worker = registry.register("worker-b", credential, "boot-b", "a" * 64, True)
    registry.set_draining("worker-a", True)
    registry.heartbeat(
        "worker-b",
        "boot-b",
        worker.connection_generation,
        NodeMetrics(8 * GIB, None, 8 * GIB, ("camoufox",)),
    )
    second_root = tmp_path / "second"
    second_root.mkdir(mode=0o700)
    second_worker = ProtocolWorker(second_root)
    second_worker.coordinator = cluster.coordinator
    first_command = cluster.hub.command
    first_sync, release, second_stop = (threading.Event() for _ in range(3))
    commands = []

    def command(run, operation, payload=None, *, timeout=30):
        commands.append((run.worker_id, operation))
        if run.worker_id == "worker-a":
            if operation == "sync":
                assert timeout == 120
                first_sync.set()
                assert release.wait(3)
            return first_command(run, operation, payload, timeout=timeout)
        if operation == "stop":
            second_stop.set()
        return second_worker.command(run, operation, payload, timeout=timeout)

    monkeypatch.setattr(cluster.hub, "command", command)
    assert (
        cluster.coordinator.reserve_and_start(ACTOR, (beta_ref,), wait=True, timeout=5)[0].state
        == "running"
    )
    beta_run = next(
        result
        for result in cluster.ledger.active_runs()
        if result.ref.profile_id == beta.profile_id
    )
    assert (alpha.worker_id, beta_run.worker_id) == ("worker-a", "worker-b")
    app = _application(cluster, (cluster.profile, beta))
    commands.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(app.stop_runs, ACTOR, (cluster.profile.name, beta.name))
        try:
            assert first_sync.wait(2)
            assert cluster.ledger.get(alpha.ref).status is RunStatus.SYNCING
            assert second_stop.wait(1)
        finally:
            release.set()
        results = stopping.result(timeout=5)
    assert [result.profile_id for result in results] == [alpha.ref.profile_id, beta.profile_id]
    assert all(result.stopped for result in results)
    for worker in ("worker-a", "worker-b"):
        assert [operation for owner, operation in commands if owner == worker] == ["stop", "sync"]
