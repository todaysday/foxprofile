"""Worker sampling failures, authenticated leases, and per-host placement budgets."""

import asyncio
import json
import mmap
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage.capacity import CapacityLedger
from foxprofile_ext.storage.database import ExtensionDatabase
from foxprofile_ext.workers.agent import WorkerAgent
from foxprofile_ext.workers.metrics import NodeMetrics, read_local_metrics
from foxprofile_ext.workers.placement import PlacementScheduler
from foxprofile_ext.workers.registry import WorkerRegistry
from src.models.profile import Profile
from tests.test_ext_worker_agent import _agent, _FakeSocket, _Session
from tests.test_ext_worker_channel import DIGEST, connect, ready

pytest_plugins = ["tests.test_ext_worker_channel"]

GIB = 1 << 30


def scheduler_for(registry, *, local_metrics=None):
    return PlacementScheduler(
        CapacityLedger(registry.database),
        registry,
        local_metrics=local_metrics or (lambda: NodeMetrics(0, None, 0, ())),
        browser_memory_bytes=GIB,
        system_reserve_bytes=GIB,
        staging_reserve_bytes=0,
        clock=registry.clock,
    )


@pytest.mark.parametrize("failure", ["resources", "inventory", "stopped_proofs", "corrupt_proofs"])
def test_heartbeat_survives_sample_failure_and_recovers(tmp_path, monkeypatch, failure):
    agent = _agent(tmp_path)
    agent.connection_generation = 1
    agent.heartbeat_interval = 0.05
    sent = []
    lease_acks = asyncio.Queue()
    row = {"profile_id": "profile-a", "run_id": "run-a", "status": "running"}

    def resources():
        if failure == "resources" and len(sent) == 1:
            raise OSError("private resource path")
        return NodeMetrics(3 * GIB, None, GIB, ("camoufox",))

    def inventory():
        if failure == "inventory" and len(sent) == 1:
            raise sqlite3.OperationalError("private inventory")
        return (row,)

    def proofs(**_kwargs):
        if failure == "stopped_proofs" and len(sent) == 1:
            raise sqlite3.OperationalError("private proof")
        if failure == "corrupt_proofs" and len(sent) == 1:
            raise RuntimeError("Worker execution state is corrupt")
        return ({"profile_id": "closed-profile", "run_id": "closed-run"},)

    class Socket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            message = await lease_acks.get()
            if message is None:
                raise StopAsyncIteration
            return SimpleNamespace(type=1, data=json.dumps(message))

        async def send_str(self, raw):
            message = json.loads(raw)
            assert "private" not in raw
            sent.append(message)
            ack = {k: message[k] for k in ("worker_id", "boot_id", "connection_generation")}
            await lease_acks.put({**ack, "type": "heartbeat_ack"})
            if len(sent) == 3:
                await lease_acks.put(None)

    monkeypatch.setattr(agent, "_metrics", resources)
    monkeypatch.setattr(agent.database, "active_runs", inventory)
    monkeypatch.setattr(agent.database, "stopped_proofs", proofs)

    async def scenario():
        await asyncio.wait_for(agent._serve_connection(Socket(), 1), 3)

    asyncio.run(scenario())
    good, missing, recovered = sent
    assert missing["metrics"] is None
    assert missing["memory_accounted_runs"] == []
    assert missing["active_runs"] == (good["active_runs"] if failure == "resources" else None)
    assert missing["stopped_runs"] == (
        [] if failure in {"stopped_proofs", "corrupt_proofs"} else good["stopped_runs"]
    )
    assert recovered["stopped_runs"] == [{"profile_id": "closed-profile", "run_id": "closed-run"}]
    for sample in (good, recovered):
        assert sample["metrics"]["mem_available_bytes"] == 3 * GIB
        assert (
            sample["active_runs"]
            == sample["memory_accounted_runs"]
            == [{"profile_id": "profile-a", "run_id": "run-a"}]
        )
    assert agent.watchdog.renewed == [1, 1, 1]


def test_unavailable_sample_keeps_control_and_ownership_but_blocks_new_placement(channel_api):
    api = channel_api
    scheduler = scheduler_for(api.registry)
    with connect(api) as socket:
        owner = ready(socket)
        (running,) = scheduler.reserve([Profile("Existing")], "master").runs
        envelope = {
            "type": "heartbeat",
            "worker_id": owner.worker_id,
            "boot_id": owner.worker_boot_id,
            "connection_generation": owner.connection_generation,
            "memory_accounted_runs": [],
        }
        good = {
            **envelope,
            "metrics": asdict(NodeMetrics(8 * GIB, None, GIB, ("camoufox",))),
            "active_runs": [asdict(running.ref)],
        }
        socket.send_json(good)
        assert socket.receive_json()["type"] == "heartbeat_ack"
        socket.send_json({**envelope, "metrics": None, "active_runs": None})
        assert socket.receive_json()["type"] == "heartbeat_ack"
        assert api.registry.get(owner.worker_id).metrics is None
        with api.registry.database.read() as db:
            previous = db.execute("SELECT active_run_ids_json FROM workers").fetchone()[0]
        assert json.loads(previous) == [running.ref.run_id]
        with pytest.raises(DomainError) as denied:
            scheduler.reserve([Profile("During outage")], "master")
        assert denied.value.code is ErrorCode.WORKER_UNAVAILABLE
        assert scheduler.ledger.active_runs() == (running,)
        with ThreadPoolExecutor() as pool:
            result = pool.submit(api.hub.command, running, "describe", timeout=2)
            command = socket.receive_json()
            socket.send_json({**command, "type": "result", "ok": True, "result": "live"})
            assert result.result(3) == "live"
        socket.send_json(good)
        assert socket.receive_json()["type"] == "heartbeat_ack"
        (new,) = scheduler.reserve([Profile("After recovery")], "master").runs
        assert new.worker_id == owner.worker_id


@pytest.fixture
def cgroup_proc(tmp_path):
    proc = tmp_path / "proc"
    mount = tmp_path / "cgroupfs"
    (proc / "self").mkdir(parents=True)
    (mount / "job").mkdir(parents=True)
    (proc / "meminfo").write_text("MemAvailable: 8388608 kB\n")
    (proc / "self/cgroup").write_text("0::/job\n")
    (proc / "self/mountinfo").write_text(f"42 1 0:1 / {mount} rw - cgroup2 cgroup rw\n")
    for group in (mount, mount / "job"):
        (group / "memory.max").write_text("max\n")
    return proc


@pytest.mark.parametrize(
    ("filename", "invalid"),
    [
        ("cgroup", None),
        ("mountinfo", None),
        ("cgroup", "garbled"),
        ("cgroup", "0::relative"),
        ("cgroup", "2:cpu:/job"),
        ("mountinfo", "garbled"),
        ("mountinfo", "42 1 0:1 / /proc rw - proc proc rw"),
    ],
)
def test_unknown_cgroup_information_denies_master_and_worker(
    tmp_path, cgroup_proc, monkeypatch, filename, invalid
):
    agent = _agent(tmp_path)
    path = cgroup_proc / "self" / filename
    if invalid is None:
        original = Path.read_text

        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError("denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    else:
        path.write_text(invalid)

    def sample():
        return read_local_metrics(tmp_path, ("camoufox",), proc_root=cgroup_proc)

    with pytest.raises(ValueError):
        sample()
    monkeypatch.setattr(agent, "_metrics", sample)
    with pytest.raises(DomainError) as refusal:
        agent._check_local_capacity("camoufox", GIB)
    assert refusal.value.code is ErrorCode.NO_WORKER_MEMORY
    registry = WorkerRegistry(ExtensionDatabase(tmp_path / "master.sqlite"), DIGEST)
    scheduler = scheduler_for(registry, local_metrics=sample)
    with pytest.raises(DomainError):
        scheduler.reserve([Profile("Private")], "master", allow_remote=False)
    assert scheduler.ledger.active_runs() == ()


def test_known_unlimited_and_unknown_controller_files(tmp_path, cgroup_proc):
    def sample():
        return read_local_metrics(tmp_path, proc_root=cgroup_proc)

    assert sample().cgroup_headroom_bytes is None
    job = tmp_path / "cgroupfs/job"
    (job / "memory.max").unlink()
    with pytest.raises(ValueError):
        sample()
    (job / "cgroup.controllers").write_text("cpu io\n")
    assert sample().cgroup_headroom_bytes is None
    (job / "cgroup.controllers").write_text("cpu memory\n")
    with pytest.raises(ValueError):
        sample()


@pytest.mark.parametrize("deadline", [5, 10])
def test_worker_rejects_loss_cap_at_or_before_first_ack(tmp_path, deadline):
    template = _agent(tmp_path)
    with pytest.raises(ValueError, match="heartbeat"):
        WorkerAgent(
            worker_id=template.worker_id,
            boot_id=template.boot_id,
            credential=template.credential,
            master_url=template.master_url,
            state_dir=template.state_dir,
            database=template.database,
            manifest=template.manifest,
            runtime=template.runtime,
            watchdog=template.watchdog,
            loss_timeout=deadline,
        )
    assert template.watchdog.accepted == []


def test_incompatible_welcome_never_arms_generation(tmp_path):
    socket = _FakeSocket(
        {
            "type": "welcome",
            "worker_id": "worker-a",
            "connection_generation": 1,
            "heartbeat_interval": 10,
            "loss_timeout": 10,
        }
    )
    agent = _agent(tmp_path, session=_Session(socket))
    with pytest.raises(ConnectionError):
        asyncio.run(agent._connect_once())
    assert agent.watchdog.accepted == [] and agent.connection_generation is None


def test_master_ranks_by_reported_worker_reserve_and_keeps_inflight_charge(tmp_path, monkeypatch):
    registry = WorkerRegistry(ExtensionDatabase(tmp_path / "master.sqlite"), DIGEST)
    scheduler = scheduler_for(registry)
    for worker_id, available, reserve in (("a", 8 * GIB, 15 * GIB // 2), ("b", 2 * GIB, GIB)):
        state = tmp_path / worker_id
        state.mkdir()
        agent = _agent(state)
        agent.memory_reserve_bytes = reserve
        # Read real local memory sampler and resource policy with controlled host headroom.
        monkeypatch.setattr(
            "foxprofile_ext.workers.metrics._read_mem_available", lambda _, memory=available: memory
        )
        monkeypatch.setattr("foxprofile_ext.workers.metrics._read_cgroup_headroom", lambda _: None)
        secret = registry.enroll(worker_id)
        owner = registry.register(worker_id, secret, agent.boot_id, DIGEST, True)
        registry.heartbeat(worker_id, agent.boot_id, owner.connection_generation, agent._metrics())
        if worker_id == "a":
            with pytest.raises(DomainError) as refused:
                agent._check_local_capacity("camoufox", GIB)
            assert refused.value.code is ErrorCode.NO_WORKER_MEMORY
        else:
            agent._check_local_capacity("camoufox", GIB)
    (first,) = scheduler.reserve([Profile("First")], "master").runs
    assert first.worker_id == "b"
    with pytest.raises(DomainError) as projected:
        scheduler.reserve([Profile("During reservation")], "master")
    assert projected.value.code is ErrorCode.NO_WORKER_MEMORY


def test_slow_sample_preserves_heartbeat_cadence_and_discards_late_result(tmp_path, monkeypatch):
    from threading import Event

    agent = _agent(tmp_path)
    agent.connection_generation = 1
    agent.heartbeat_interval = 0.05
    release = Event()
    samples = []
    heartbeats = []

    def resources():
        samples.append(len(samples))
        if len(samples) == 1:
            assert release.wait(5)
        return NodeMetrics(len(samples) * GIB, None, GIB, ("camoufox",))

    async def record(_, heartbeat):
        heartbeats.append(heartbeat["metrics"])
        if len(heartbeats) == 2:
            assert len(samples) == 1
            release.set()
        if heartbeat["metrics"] is not None:
            agent.connection_generation = None

    monkeypatch.setattr(agent, "_metrics", resources)
    monkeypatch.setattr(agent, "_send_json", record)

    async def scenario():
        try:
            await asyncio.wait_for(agent._heartbeat_loop(None, 1), 3)
        finally:
            release.set()

    asyncio.run(scenario())
    assert heartbeats[:2] == [None, None]
    assert heartbeats[-1]["mem_available_bytes"] == 2 * GIB


@pytest.mark.parametrize("invalid_reserve", ["missing", -1, True, 1 << 61])
def test_absent_or_invalid_worker_reserve_fails_closed(channel_api, invalid_reserve):
    from starlette.websockets import WebSocketDisconnect

    api = channel_api
    with connect(api) as socket:
        owner = ready(socket)
        metrics = asdict(NodeMetrics(8 * GIB, None, GIB, ("camoufox",)))
        if invalid_reserve == "missing":
            metrics.pop("memory_reserve_bytes")
        else:
            metrics["memory_reserve_bytes"] = invalid_reserve
        socket.send_json(
            {
                "type": "heartbeat",
                "worker_id": owner.worker_id,
                "boot_id": owner.worker_boot_id,
                "connection_generation": owner.connection_generation,
                "metrics": metrics,
                "active_runs": [],
            }
        )
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert api.registry.get(owner.worker_id).state == "offline"


def test_v1_memory_limit_and_unlimited_sentinel(tmp_path, cgroup_proc):
    mount = tmp_path / "cgroupfs"
    (cgroup_proc / "self/cgroup").write_text("3:memory:/job\n")
    (cgroup_proc / "self/mountinfo").write_text(
        f"42 1 0:1 / {mount} rw - cgroup cgroup rw,memory\n"
    )
    sentinel = str((1 << 63) - mmap.PAGESIZE)
    for directory in (mount, mount / "job"):
        (directory / "memory.limit_in_bytes").write_text(sentinel)
    assert read_local_metrics(tmp_path, proc_root=cgroup_proc).cgroup_headroom_bytes is None
    (mount / "memory.limit_in_bytes").write_text("100")
    (mount / "memory.usage_in_bytes").write_text("70")
    assert read_local_metrics(tmp_path, proc_root=cgroup_proc).cgroup_headroom_bytes == 30
    (mount / "job/memory.limit_in_bytes").unlink()
    with pytest.raises(ValueError, match="missing cgroup memory limit"):
        read_local_metrics(tmp_path, proc_root=cgroup_proc)


def test_hidden_cgroup_ancestors_are_unknown(tmp_path, cgroup_proc):
    mount = tmp_path / "cgroupfs"
    # cgroup.type is present only on a non-root group, including namespace roots.
    (mount / "cgroup.type").write_text("domain\n")
    with pytest.raises(ValueError):
        read_local_metrics(tmp_path, proc_root=cgroup_proc)


def test_invalid_service_timeout_stops_before_watchdog_or_systemd_ready(tmp_path, monkeypatch):
    from foxprofile_ext.workers import agent as module

    template = _agent(tmp_path)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("FOXPROFILE_WORKER_STATE_DIR", str(template.state_dir))
    monkeypatch.setenv("FOXPROFILE_WORKER_MANIFEST", "test-manifest")
    monkeypatch.setenv("FOXPROFILE_WORKER_NATIVE_EVIDENCE", "test-evidence")
    monkeypatch.setenv("FOXPROFILE_WORKER_LOSS_TIMEOUT", "5")
    monkeypatch.setattr(module, "read_worker_credential", lambda _path: template.credential)
    monkeypatch.setattr(module, "load_manifest", lambda *_: template.manifest)
    monkeypatch.setattr(module.WorkerWatchdog, "start", lambda _: pytest.fail("watchdog started"))
    monkeypatch.setattr(module, "_systemd_notify", lambda _: pytest.fail("announced ready"))
    with pytest.raises(ValueError, match="heartbeat"):
        module._load_manifest_and_agent()
