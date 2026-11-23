"""Worker lease, launch and return-data regression coverage with synthetic runtimes."""

import asyncio
import io
import json
import shutil
import sys
import threading
from types import SimpleNamespace

import pytest

from foxprofile_ext.dto import BrowserRunRefDTO, ProcessIdentity
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.runtime import cache
from foxprofile_ext.storage.capacity import CapacityLedger, ProcessLiveness, RunStatus
from foxprofile_ext.workers.agent import WorkerRuntime
from foxprofile_ext.workers.manifest import VerifiedManifest
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.profile_store import pack_browser_data
from src.models.profile import Profile
from src.services.browser.launcher import BrowserLauncher
from tests.test_ext_worker_agent import _agent, _FakeSocket, _Session, _worker_owner


def _welcome():
    class Acknowledged(_FakeSocket):
        acknowledged = False

        async def __anext__(self):
            if self.acknowledged:
                raise StopAsyncIteration
            self.acknowledged = True
            ack = {
                "type": "heartbeat_ack",
                "worker_id": "worker-a",
                "boot_id": self.sent[0]["boot_id"],
                "connection_generation": 2,
            }
            return SimpleNamespace(type=1, data=json.dumps(ack))

    return _Session(
        Acknowledged(
            {
                "type": "welcome",
                "worker_id": "worker-a",
                "connection_generation": 2,
                "heartbeat_interval": 10,
                "loss_timeout": 30,
            }
        )
    )


def _owned_run(agent):
    ref = BrowserRunRefDTO("profile", "run-owned")
    identity = ProcessIdentity(990001, 12)
    ledger = CapacityLedger(agent.database)
    context = _worker_owner(ref, worker_id=agent.worker_id, boot_id=agent.boot_id)
    assert agent.database.reserve_exact(ref, "fixture", context=context)
    assert ledger.claim(ref, "fixture", identity)
    assert ledger.mark_running(ref)
    return ref, identity, ledger


@pytest.mark.parametrize("can_retire", [True, False])
def test_reconnect_requires_retirement_of_old_browser(tmp_path, can_retire):
    agent = _agent(tmp_path, session=_welcome())
    ref, identity, ledger = _owned_run(agent)

    def stop(exact_ref, timeout):
        assert exact_ref == ref and not agent.watchdog.accepted
        if can_retire:
            assert ledger.release_after_exit(ref, identity, lambda *_: ProcessLiveness.DEAD)
        return can_retire

    agent.runtime = SimpleNamespace(
        ledger=ledger,
        stop=stop,
        tree_is_dead=lambda _: ledger.get(ref).status is RunStatus.CLOSED,
    )
    if can_retire:
        asyncio.run(agent._connect_once())
        assert ledger.get(ref).status is RunStatus.CLOSED
        (proof,) = agent.database.stopped_proofs()
        assert proof["reason"] == "loss" and proof["connection_generation"] == 1
        assert agent.watchdog.accepted == [(2, 30.0)] and agent.watchdog.renewed == [2]
    else:
        with pytest.raises(ConnectionError):
            asyncio.run(agent._connect_once())
        assert agent.watchdog.accepted == agent.watchdog.renewed == []
        assert agent.database.stopped_proofs() == ()


def _identity(directory):
    directory.mkdir(parents=True)
    (directory / "fingerprint.json").write_text('{"os":"linux","fingerprint":{"fixture":true}}')
    (directory / "tabs.json").write_text('["https://example.test/kept"]')


def _startup_snapshot(tmp_path, agent, ref):
    archive = tmp_path / "source.zip"
    source = tmp_path / "source"
    _identity(source)
    info = pack_browser_data(source, "camoufox", archive)

    async def download(_url, _headers, destination, _size, _digest):
        shutil.copyfile(archive, destination)

    agent._download_snapshot = download
    return {
        "profile": {"name": "fixture", "profile_id": ref.profile_id, "engine": "camoufox"},
        "memory_budget_bytes": 32 << 20,
        "snapshot": {
            "url": "https://master.example.test/snapshot",
            "token": "fixture-test-token",
            "size_bytes": info.size_bytes,
            "base_revision": 1,
            "archive_digest": info.archive_digest,
            "identity_digest": info.identity_digest,
        },
    }


@pytest.mark.skipif(sys.platform != "linux", reason="Protected supervisor requires Linux")
def test_worker_launch_uses_verified_binaries_and_prepares_camoufox_only(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    cache_root = tmp_path / "cache" / "camoufox" / "geoip"
    monkeypatch.setattr(cache, "geoip_cache_directory", lambda: cache_root)
    prepares = []
    monkeypatch.setattr(cache, "prepare_geoip_cache", lambda: prepares.append(True))
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    engines = ("camoufox", "chrome")
    pins = {}
    for engine in engines:
        pin = tmp_path / "approved" / engine
        pin.parent.mkdir(exist_ok=True)
        pin.touch(mode=0o700)
        alternative = tmp_path / "alternative" / engine
        alternative.parent.mkdir(exist_ok=True)
        alternative.touch(mode=0o700)
        monkeypatch.setenv(f"FOXPROFILE_{engine.upper()}_EXECUTABLE", str(alternative))
        pins[engine] = {"path": str(pin)}
    manifest = VerifiedManifest("a" * 64, engines, {"binaries": pins})
    runtime = WorkerRuntime(agent.database, agent.state_dir, manifest)
    try:
        for engine in engines:
            name = f"FOXPROFILE_{engine.upper()}_EXECUTABLE"
            assert runtime.supervisor.settings[name] == pins[engine]["path"]
            runtime.supervisor.prepare_launch(Profile(name="fixture", engine=engine))
        assert prepares == [True]
        assert str(cache_root) in runtime.supervisor.runtime_paths
    finally:
        runtime.shutdown(0)


def test_capacity_drop_during_preparation_prevents_launch(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    ref = BrowserRunRefDTO("profile", "run-budget")
    available = 128 << 20
    samples = []
    ledger = CapacityLedger(agent.database)

    def metrics():
        samples.append(available)
        return NodeMetrics(128 << 20, available, 128 << 20, ("camoufox",))

    def prepare(*_args):
        nonlocal available
        assert threading.current_thread() is not threading.main_thread()
        available = 0

    supervisor = SimpleNamespace(
        ledger=ledger, host_id="fixture", prepare_browser=prepare, discard_prepared=lambda _: None
    )
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    agent.runtime = WorkerRuntime(
        agent.database,
        agent.state_dir,
        agent.manifest,
        supervisor=supervisor,
        launcher=SimpleNamespace(
            start_thread=lambda *a, **kw: pytest.fail("launch at zero headroom")
        ),
    )
    agent._metrics = metrics
    payload = _startup_snapshot(tmp_path, agent, ref)
    with pytest.raises(DomainError) as failed:
        asyncio.run(agent._start_command(ref, payload, 1))
    assert failed.value.code is ErrorCode.NO_WORKER_MEMORY
    assert samples == [128 << 20, 0]
    assert agent.database.run_status(ref) is RunStatus.CANCELLED
    (proof,) = agent.database.stopped_proofs()
    assert proof["shutdown_kind"] == "pre-spawn" and proof["run_id"] == ref.run_id
    assert not agent.runtime.profile_directory(ref.run_id).exists()


@pytest.mark.parametrize(
    ("exit_code", "completed"),
    [(0, True), (0, False), (17, False), (-9, False), (None, False), (17, True)],
)
def test_supervisor_exit_controls_worker_stop_proof(tmp_path, monkeypatch, exit_code, completed):
    agent = _agent(tmp_path)
    ref, identity, ledger = _owned_run(agent)
    data = agent.state_dir / "profiles" / ref.run_id
    _identity(data)
    info = pack_browser_data(data, "camoufox", tmp_path / "source.zip")
    agent.database.update_run_context(
        ref.run_id, data_dir=str(data), engine="camoufox", identity_digest=info.identity_digest
    )
    proceed = threading.Event()

    def wait():
        assert proceed.wait(2)
        assert ledger.release_after_exit(ref, identity, lambda *_: ProcessLiveness.DEAD)
        if exit_code is None:
            raise OSError("fixture wait failed")
        return exit_code

    output = "BROWSER_STARTED\n" + ("RUNTIME:CLEAN_EXIT\n" if completed else "")
    adapter = SimpleNamespace(
        spawn_browser=lambda *_: SimpleNamespace(stdout=io.StringIO(output), wait=wait),
        shutdown=lambda *_: None,
    )
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    launcher = BrowserLauncher(adapter)
    agent.runtime = SimpleNamespace(
        tree_is_dead=lambda _: ledger.get(ref).status is RunStatus.CLOSED
    )

    async def scenario():
        agent.connection_generation = 1
        agent._loop = asyncio.get_running_loop()
        agent._events = asyncio.Queue()
        assert launcher.start_thread(
            Profile(name=ref.run_id, profile_id=ref.profile_id),
            lambda _: None,
            on_exit=lambda clean: agent._browser_exited(ref, 1, clean),
            profile_id=ref.profile_id,
            run_id=ref.run_id,
        )
        assert launcher.wait_for_launch(ref.run_id, 1, ref.run_id) == (True, None)
        proceed.set()
        return (await asyncio.wait_for(agent._events.get(), 3))["proof"]

    try:
        proof = asyncio.run(scenario())
    finally:
        proceed.set()
    expected_clean = exit_code == 0 and completed
    assert proof["reason"] == ("clean" if expected_clean else "loss")
    assert ("archive_digest" in proof) == expected_clean


def test_restarted_worker_retains_stopped_copy_after_staging_exhaustion(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    agent.connection_generation = 1
    ref, identity, ledger = _owned_run(agent)
    data = agent.state_dir / "profiles" / ref.run_id
    _identity(data)
    info = pack_browser_data(data, "camoufox", tmp_path / "source.zip")
    agent.database.update_run_context(
        ref.run_id, data_dir=str(data), engine="camoufox", identity_digest=info.identity_digest
    )
    stop_calls = []

    def stop(exact, timeout):
        stop_calls.append(exact)
        assert ledger.release_after_exit(exact, identity, lambda *_: ProcessLiveness.DEAD)
        return True

    agent.runtime = SimpleNamespace(
        ledger=ledger,
        stop=stop,
        clean_exit=lambda *_: True,
        tree_is_dead=lambda _: ledger.get(ref).status is RunStatus.CLOSED,
    )
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        with pytest.raises(DomainError) as failed:
            asyncio.run(agent._stop_command(ref, {}))
        assert failed.value.code is ErrorCode.SYNC_FAILED
    assert not agent.database.stopped_proofs()
    assert ledger.get(ref).status is RunStatus.CLOSED and data.is_dir()
    # A restarted Worker on another transport retains the stopped copy and old owner.
    restarted = _agent(tmp_path, runtime=agent.runtime, session=_welcome())
    asyncio.run(restarted._connect_once())
    assert restarted.database.stopped_proofs() == () and data.is_dir()
    with pytest.raises(DomainError) as stale:
        asyncio.run(
            restarted._dispatch(
                {"profile_id": ref.profile_id, "run_id": ref.run_id, "operation": "stop"},
                generation=2,
            )
        )
    assert stale.value.code is ErrorCode.STALE_RUN
    assert stop_calls == [ref]


@pytest.mark.parametrize("scheme", ["https", "wss"])
def test_transfer_and_relay_endpoints_are_bound_to_master_origin(tmp_path, scheme):
    agent = _agent(tmp_path)
    for host in ("MASTER.example.test", "master.example.test:443"):
        assert agent._master_endpoint(f"{scheme}://{host}/path", web_socket=scheme == "wss")
    for host in ("other.example.test", "master.example.test:8443"):
        with pytest.raises(ValueError):
            agent._master_endpoint(f"{scheme}://{host}/path", web_socket=scheme == "wss")


@pytest.mark.parametrize("operation", ["snapshot", "upload", "relay"])
def test_origin_checked_at_each_worker_command_boundary(tmp_path, operation):
    agent = _agent(tmp_path)
    ref = BrowserRunRefDTO("profile", "fixture-run")
    ledger = CapacityLedger(agent.database)
    descriptor = SimpleNamespace(
        browser="fixture",
        display=":1",
        namespace="fixture",
        tmp_path="/tmp",
        xauthority_path="fixture",
    )
    agent.runtime = SimpleNamespace(
        ledger=ledger,
        supervisor=SimpleNamespace(host_id="fixture"),
        profile_directory=lambda run: agent.state_dir / "profiles" / run,
        descriptor=lambda _: descriptor,
    )
    agent._metrics = lambda: NodeMetrics(128 << 20, None, 128 << 20, ("camoufox",))
    foreign_url = "https://other.example.test/path"
    if operation == "snapshot":
        expected = agent._start_command(
            ref,
            {
                "profile": {"profile_id": ref.profile_id, "name": "fixture"},
                "memory_budget_bytes": 1,
                "snapshot": {"url": foreign_url},
            },
            1,
        )
    else:
        agent.database.put_run_context(ref.run_id, {"profile_id": ref.profile_id})
        if operation == "upload":
            agent.database.record_stop_proof(
                ref.run_id, {"run_id": ref.run_id, "reason": "clean", "archive_digest": "a" * 64}
            )
            expected = agent._sync_command(ref, {"upload": {"url": foreign_url}})
        else:
            agent.database.update_run_context(ref.run_id, engine="camoufox")
            agent._manual_backend = object()
            expected = agent._manual_open_command(
                ref,
                {
                    "session_id": "fixture-session",
                    "relay": {"url": "wss://other.example.test/relay"},
                },
            )
    with pytest.raises(ValueError, match="configured Master origin"):
        asyncio.run(expected)


def test_cancelled_start_waits_for_extractor_before_proof_and_cleanup(tmp_path, monkeypatch):
    from foxprofile_ext.workers import profile_store

    agent = _agent(tmp_path)
    ref = BrowserRunRefDTO("profile", "run-extract")
    ledger = CapacityLedger(agent.database)
    agent.runtime = SimpleNamespace(
        supervisor=SimpleNamespace(host_id="fixture"),
        ledger=ledger,
        tree_is_dead=lambda _: (
            ledger.get(ref).process is None and ledger.get(ref).status is RunStatus.CANCELLED
        ),
        profile_directory=lambda run: agent.state_dir / "profiles" / run,
    )
    agent._metrics = lambda: NodeMetrics(128 << 20, None, 128 << 20, ("camoufox",))

    started, release = threading.Event(), threading.Event()
    disk_usage = shutil.disk_usage

    def block_after_zip_validation(path):
        if path == agent.runtime.profile_directory(ref.run_id).parent:
            started.set()
            assert release.wait(4)
        return disk_usage(path)

    monkeypatch.setattr(profile_store.shutil, "disk_usage", block_after_zip_validation)
    payload = _startup_snapshot(tmp_path, agent, ref)

    async def scenario():
        task = asyncio.create_task(agent._start_command(ref, payload, 1))
        assert await asyncio.to_thread(started.wait, 2)
        try:
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done() and not agent.database.stopped_proofs()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        (proof,) = agent.database.stopped_proofs()
        assert proof["shutdown_kind"] == "pre-spawn"
        assert agent._cleanup_command(ref, {"disposition": "closed", "proof": proof})["cleaned"]
        assert not agent.runtime.profile_directory(ref.run_id).exists()

    asyncio.run(scenario())


def test_slow_download_flush_and_sync_digest_keep_control_loop_responsive(tmp_path, monkeypatch):
    import hashlib
    import os

    from foxprofile_ext.workers import agent as agent_module

    data = b"synthetic archive"
    digest = hashlib.sha256(data).hexdigest()
    ref = BrowserRunRefDTO("profile", "sync-run")
    agent = _agent(tmp_path)

    class Response:
        headers = {}
        status = 200
        content = None

        async def __aenter__(self):
            self.content = self
            return self

        async def __aexit__(self, *_):
            pass

        async def iter_chunked(self, _size):
            yield data

        async def read(self, _size):
            return b'{"revision": 2}'

    agent.session = SimpleNamespace(get=lambda *a, **k: Response(), post=lambda *a, **k: Response())
    expected = agent.state_dir / "sync" / f"{ref.run_id}.zip"
    agent.database.put_run_context(
        ref.run_id,
        {
            "profile_id": ref.profile_id,
            "connection_generation": 1,
            "archive_path": str(expected),
            "base_revision": 1,
        },
    )
    agent.database.record_stop_proof(
        ref.run_id,
        {
            "profile_id": ref.profile_id,
            "run_id": ref.run_id,
            "reason": "clean",
            "archive_digest": digest,
            "archive_size_bytes": len(data),
        },
    )
    agent.runtime = SimpleNamespace(
        profile_directory=lambda run: agent.state_dir / "profiles" / run
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        observations = []

        def observe(function):
            def checked(*args):
                ack = threading.Event()
                loop.call_soon_threadsafe(ack.set)
                observations.append(ack.wait(0.15))
                return function(*args)

            return checked

        monkeypatch.setattr(agent_module.os, "fsync", observe(os.fsync))
        monkeypatch.setattr(agent_module, "_file_digest", observe(agent_module._file_digest))
        await agent._download_snapshot(
            "https://master.example.test/data", {}, expected, len(data), digest
        )
        result = await agent._sync_command(
            ref,
            {
                "upload": {
                    "url": "https://master.example.test/upload",
                    "token": "fixture-token-long",
                    "base_revision": 1,
                    "archive_digest": digest,
                }
            },
        )
        assert result["receipt"]["revision"] == 2
        assert observations == [True, True]

    asyncio.run(scenario())


def test_heartbeat_counts_only_runs_already_running_during_memory_sample(tmp_path):
    agent = _agent(tmp_path)
    running, _, ledger = _owned_run(agent)
    starting = BrowserRunRefDTO("profile-new", "run-new")
    context = _worker_owner(starting, worker_id=agent.worker_id, boot_id=agent.boot_id)
    assert agent.database.reserve_exact(starting, "fixture", context=context)
    assert ledger.claim(starting, "fixture", ProcessIdentity(990002, 14))

    def metrics():
        assert ledger.mark_running(starting)
        return NodeMetrics(128 << 20, None, 128 << 20, ("camoufox",))

    agent._metrics = metrics
    agent.connection_generation = 1
    agent.heartbeat_interval = 0.1
    heartbeat = {}

    async def send(_, payload):
        heartbeat.update(payload)
        agent.connection_generation = None

    agent._send_json = send
    asyncio.run(agent._heartbeat_loop(None, 1))
    assert {r["run_id"] for r in heartbeat["active_runs"]} == {running.run_id, starting.run_id}
    assert heartbeat["memory_accounted_runs"] == [
        {"profile_id": running.profile_id, "run_id": running.run_id}
    ]
