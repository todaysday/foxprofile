"""Exercise the outbound Worker agent over a real, locally trusted WSS socket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI

from foxprofile_ext.capacity import CapacityService
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind, ProfileRefDTO
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.runtime.coordinator import RuntimeCoordinator
from foxprofile_ext.storage import AssignmentDAO, CapacityLedger
from foxprofile_ext.storage.capacity import RunStatus
from foxprofile_ext.storage.database import ExtensionDatabase
from foxprofile_ext.workers.agent import WorkerAgent
from foxprofile_ext.workers.channel import MasterWorkerHub
from foxprofile_ext.workers.coordinator import DistributedCoordinator, PlacementCapacity
from foxprofile_ext.workers.execution import WorkerExecutionDatabase
from foxprofile_ext.workers.http import router
from foxprofile_ext.workers.manifest import VerifiedManifest
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.placement import PlacementScheduler
from foxprofile_ext.workers.profile_store import ProfileStore
from foxprofile_ext.workers.registry import WorkerRegistry
from src.models.profile import Profile

_DIGEST = "a" * 64
_WORKER_ID = "worker-roundtrip"
_BOOT_ID = "boot-roundtrip"
_ACTOR = PrincipalDTO("test-service", PrincipalKind.SERVICE)
_GIB = 1024**3


class _TestWatchdog:
    def __init__(self):
        self.generations = []
        self.renewed = threading.Event()
        self.closed = threading.Event()

    def start(self):
        pass

    def accept_generation(self, generation, loss_timeout=None):
        self.generations.append(generation)

    def renew(self, generation):
        self.renewed.set()

    def close(self, timeout=6):
        self.closed.set()


class _TestRuntime:
    def __init__(self):
        self.entered_hold = threading.Event()
        self.release_hold = threading.Event()
        self.shutdown_called = threading.Event()
        self.ledger = SimpleNamespace(get=lambda _ref: SimpleNamespace(status=RunStatus.RUNNING))

    def control(self, _ref, action, params, timeout):
        if action == "hold":
            self.entered_hold.set()
            self.release_hold.wait(timeout)
        return {"action": action, "params": params}

    def descriptor(self, _ref):
        return None

    def shutdown(self, _timeout=5):
        self.shutdown_called.set()


class _BrowserFixtureRuntime:
    """Fake only native process control; transfers and browser data stay real."""

    def __init__(self, database, state_dir):
        self.database = database
        self.state_dir = state_dir
        self.data_dir = state_dir / "profiles"
        self.ledger = CapacityLedger(database)
        self.supervisor = SimpleNamespace(host_id="worker:fixture")
        self.shutdown_called = threading.Event()
        self.dead_runs = set()
        self.started_path = None
        self.initial_identity = None
        self.initial_cookies = None

    def profile_directory(self, run_id):
        return self.data_dir / run_id

    async def start(self, _profile, ref, *, on_ready, on_stop, timeout):
        path = self.profile_directory(ref.run_id)
        self.started_path = path
        self.initial_identity = (path / "fingerprint.json").read_bytes()
        self.initial_cookies = (path / "cookies.sqlite").read_bytes()
        (path / "cookies.sqlite").write_bytes(b"worker-cookie-session")
        (path / "storage.sqlite").write_bytes(b"worker-local-storage")
        (path / "tabs.json").write_text('["https://example.test/roundtrip"]', encoding="utf-8")
        with self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE browser_runs SET status = 'running' "
                "WHERE profile_id = ? AND run_id = ? AND status = 'reserved'",
                (ref.profile_id, ref.run_id),
            )
            assert updated.rowcount == 1
        on_ready()
        return "running"

    def control(self, ref, action, params, _timeout):
        path = self.profile_directory(ref.run_id)
        return {
            "action": action,
            "params": params,
            "cookies": (path / "cookies.sqlite").read_bytes().decode("utf-8"),
        }

    def stop(self, ref, _timeout):
        self.dead_runs.add(ref.run_id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE browser_runs SET status = 'closed' "
                "WHERE profile_id = ? AND run_id = ? AND status = 'running'",
                (ref.profile_id, ref.run_id),
            )
        return True

    def tree_is_dead(self, ref):
        return ref.run_id in self.dead_runs

    def descriptor(self, _ref):
        return None

    def shutdown(self, _timeout=5):
        self.shutdown_called.set()


def _write_self_signed_certificate(directory: Path) -> tuple[Path, Path]:
    x509 = pytest.importorskip("cryptography.x509")
    hashes = pytest.importorskip("cryptography.hazmat.primitives.hashes")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before((now - timedelta(minutes=1)).replace(tzinfo=None))
        .not_valid_after((now + timedelta(days=1)).replace(tzinfo=None))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "worker-test-cert.pem"
    key_path = directory / "worker-test-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def _start_tls_server(app, cert_path: Path, key_path: Path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="critical",
            access_log=False,
        )
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise AssertionError("local TLS Master did not start")
    return f"wss://127.0.0.1:{port}", server, thread, listener


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_worker_agent_roundtrip_over_trusted_tls(tmp_path, monkeypatch):
    import foxprofile_ext.workers.channel as channel_module

    monkeypatch.setattr(channel_module, "HEARTBEAT_INTERVAL_SECONDS", 1)
    certificate_path, key_path = _write_self_signed_certificate(tmp_path)
    master_database = ExtensionDatabase(tmp_path / "master.sqlite")
    registry = WorkerRegistry(master_database, _DIGEST)
    credential = registry.enroll(_WORKER_ID)
    hub = MasterWorkerHub(registry)
    app = FastAPI()
    app.state.container = SimpleNamespace(
        worker_coordinator=SimpleNamespace(
            workers=registry,
            hub=hub,
            ledger=SimpleNamespace(active_runs=lambda: ()),
        )
    )
    app.include_router(router, prefix="/api/v1")
    master_url, server, server_thread, listener = _start_tls_server(app, certificate_path, key_path)

    runtime = _TestRuntime()
    watchdog = _TestWatchdog()
    worker_database = WorkerExecutionDatabase(tmp_path / "worker-state" / "execution.sqlite")
    agent = WorkerAgent(
        worker_id=_WORKER_ID,
        credential=credential,
        master_url=master_url,
        state_dir=tmp_path / "worker-state",
        database=worker_database,
        manifest=VerifiedManifest(_DIGEST, ("camoufox",), {}),
        runtime=runtime,
        watchdog=watchdog,
        boot_id=_BOOT_ID,
    )
    agent._metrics = lambda: NodeMetrics(8 * 1024**3, None, 8 * 1024**3, ("camoufox",))
    agent.start_watchdog()
    agent_thread_errors = []

    def run_agent():
        async def serve_agent():
            import aiohttp

            trust = ssl.create_default_context(cafile=str(certificate_path))
            connector = aiohttp.TCPConnector(ssl=trust)
            async with aiohttp.ClientSession(connector=connector) as session:
                agent.session = session
                await agent.run()

        try:
            asyncio.run(serve_agent())
        except BaseException as error:
            agent_thread_errors.append(error)

    thread = Thread(target=run_agent, daemon=True)
    thread.start()
    command_pool = None
    run = None
    try:
        _wait_for(lambda: watchdog.generations and registry.get(_WORKER_ID).metrics)
        worker = registry.get(_WORKER_ID)
        run = SimpleNamespace(
            ref=SimpleNamespace(profile_id="profile-roundtrip", run_id="run-roundtrip"),
            worker_id=_WORKER_ID,
            worker_boot_id=_BOOT_ID,
            connection_generation=worker.connection_generation,
        )
        assert hub.command(
            run,
            "control",
            {"action": "fixture", "params": {"marker": "roundtrip"}, "timeout": 3},
            timeout=3,
        ) == {"action": "fixture", "params": {"marker": "roundtrip"}}
        assert watchdog.renewed.wait(2)

        from concurrent.futures import ThreadPoolExecutor

        command_pool = ThreadPoolExecutor(max_workers=1)
        pending = command_pool.submit(
            hub.command,
            run,
            "control",
            {"action": "hold", "params": {}, "timeout": 4},
            timeout=4,
        )
        assert runtime.entered_hold.wait(3), "Worker did not dispatch the scoped command"
        asyncio.run_coroutine_threadsafe(agent.stop(), agent._loop).result(2)
        hub.disconnect(_WORKER_ID)
        with pytest.raises(DomainError) as failure:
            pending.result(3)
        assert failure.value.code is ErrorCode.WORKER_UNAVAILABLE
        _wait_for(lambda: registry.get(_WORKER_ID).state == "offline")
        assert registry.get(_WORKER_ID).boot_id == _BOOT_ID
        _wait_for(lambda: agent.connection_generation is None)
    finally:
        runtime.release_hold.set()
        if agent._loop is not None and not agent._loop.is_closed():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(agent.stop(), agent._loop).result(2)
        if run is not None:
            hub.disconnect(_WORKER_ID)
        thread.join(timeout=6)
        server.should_exit = True
        server_thread.join(timeout=6)
        if server_thread.is_alive():
            server.force_exit = True
            server_thread.join(timeout=2)
        listener.close()
        if command_pool is not None:
            command_pool.shutdown(wait=True, cancel_futures=True)

    assert not thread.is_alive(), "Worker agent thread was not shut down"
    assert not server_thread.is_alive(), "local TLS Master thread was not shut down"
    assert not agent_thread_errors
    assert runtime.shutdown_called.is_set()
    assert watchdog.closed.is_set()


def test_worker_agent_distributed_start_sync_and_close_over_tls(tmp_path, monkeypatch):
    import foxprofile_ext.workers.channel as channel_module

    monkeypatch.setattr(channel_module, "HEARTBEAT_INTERVAL_SECONDS", 1)
    certificate_path, key_path = _write_self_signed_certificate(tmp_path)
    master_database = ExtensionDatabase(tmp_path / "master.sqlite")
    ledger = CapacityLedger(master_database)
    registry = WorkerRegistry(master_database, _DIGEST)
    credential = registry.enroll(_WORKER_ID)
    hub = MasterWorkerHub(registry)
    app = FastAPI()
    app.state.container = SimpleNamespace(worker_coordinator=None)
    app.include_router(router, prefix="/api/v1")
    websocket_url, server, server_thread, listener = _start_tls_server(
        app, certificate_path, key_path
    )
    public_url = websocket_url.replace("wss://", "https://", 1)

    profile = Profile("alpha", os_type="linux", proxy="http://user:pass@192.0.2.10:8080")
    data_dir = tmp_path / "canonical"
    profile_dir = data_dir / profile.name
    profile_dir.mkdir(parents=True)
    (profile_dir / "fingerprint.json").write_text(
        json.dumps({"os": "linux", "fingerprint": {"navigator.userAgent": "fixture"}}),
        encoding="utf-8",
    )
    original_identity = (profile_dir / "fingerprint.json").read_bytes()
    (profile_dir / "cookies.sqlite").write_bytes(b"master-cookie-session")
    (profile_dir / "storage.sqlite").write_bytes(b"master-local-storage")
    store = ProfileStore(master_database, data_dir, tmp_path / "staging")
    assignments = AssignmentDAO(master_database)
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=lambda: NodeMetrics(_GIB // 4, None, 8 * _GIB, ("camoufox",)),
        browser_memory_bytes=_GIB,
        system_reserve_bytes=_GIB // 2,
        staging_reserve_bytes=_GIB,
    )

    def authorize(actor, _ref):
        return actor

    local = RuntimeCoordinator(
        CapacityService(ledger, assignments),
        ledger,
        SimpleNamespace(host_id="master", shutdown=lambda *_: None),
        SimpleNamespace(wait_for_launch=lambda *_: (False, "failed")),
        profile_by_id=lambda _profile_id: profile,
        authorize=authorize,
        lock_path=str(tmp_path / "catalog.lock"),
    )
    coordinator = DistributedCoordinator(
        local,
        PlacementCapacity(ledger, assignments, scheduler, local.profile_by_id),
        registry,
        hub,
        store,
        public_url,
    )
    app.state.container.worker_coordinator = coordinator

    worker_state = tmp_path / "worker-state"
    worker_database = WorkerExecutionDatabase(worker_state / "execution.sqlite")
    runtime = _BrowserFixtureRuntime(worker_database, worker_state)
    watchdog = _TestWatchdog()
    agent = WorkerAgent(
        worker_id=_WORKER_ID,
        credential=credential,
        master_url=websocket_url,
        state_dir=worker_state,
        database=worker_database,
        manifest=VerifiedManifest(_DIGEST, ("camoufox",), {}),
        runtime=runtime,
        watchdog=watchdog,
        boot_id=_BOOT_ID,
    )
    agent._metrics = lambda: NodeMetrics(8 * _GIB, None, 8 * _GIB, ("camoufox",))
    agent.start_watchdog()
    agent_thread_errors = []

    def run_agent():
        async def serve_agent():
            import aiohttp

            trust = ssl.create_default_context(cafile=str(certificate_path))
            connector = aiohttp.TCPConnector(ssl=trust)
            async with aiohttp.ClientSession(connector=connector) as session:
                agent.session = session
                await agent.run()

        try:
            asyncio.run(serve_agent())
        except BaseException as error:
            agent_thread_errors.append(error)

    thread = Thread(target=run_agent, daemon=True)
    thread.start()
    try:
        _wait_for(lambda: watchdog.generations and registry.get(_WORKER_ID).metrics)
        result = coordinator.reserve_and_start(
            _ACTOR,
            (ProfileRefDTO(profile.profile_id, profile.name),),
            wait=True,
            timeout=10,
        )[0]
        assert result.state == "running", result
        run = ledger.active_runs()[0]
        assert run.worker_id == _WORKER_ID and run.base_revision == 1
        assert runtime.initial_identity == original_identity
        assert runtime.initial_cookies == b"master-cookie-session"
        assert (runtime.started_path / "storage.sqlite").read_bytes() == b"worker-local-storage"
        assert json.loads((runtime.started_path / "tabs.json").read_text(encoding="utf-8")) == [
            "https://example.test/roundtrip"
        ]
        assert coordinator.control(
            _ACTOR,
            ProfileRefDTO(profile.profile_id, profile.name),
            "screenshot",
            {"marker": "real-worker"},
        ) == {
            "action": "screenshot",
            "params": {"marker": "real-worker"},
            "cookies": "worker-cookie-session",
        }

        assert coordinator.stop(_ACTOR, ProfileRefDTO(profile.profile_id, profile.name), timeout=5)
        assert ledger.get(run.ref).status is RunStatus.CLOSED
        assert ledger.active_runs() == ()
        assert (profile_dir / "cookies.sqlite").read_bytes() == b"worker-cookie-session"
        assert (profile_dir / "storage.sqlite").read_bytes() == b"worker-local-storage"
        assert json.loads((profile_dir / "tabs.json").read_text(encoding="utf-8")) == [
            "https://example.test/roundtrip"
        ]
        assert (profile_dir / "fingerprint.json").read_bytes() == original_identity
        assert store.revision(profile.profile_id)[0] == 2
        receipt = worker_database.run_context(run.ref.run_id)["sync_receipt"]
        assert receipt["profile_id"] == profile.profile_id
        assert receipt["run_id"] == run.ref.run_id
        assert receipt["revision"] == 2
        assert receipt["archive_digest"] == store.revision(profile.profile_id)[1]
        assert not runtime.started_path.exists()
        assert not list(store.staging_dir.glob("upload-*"))
        _wait_for(lambda: worker_database.run_context(run.ref.run_id).get("terminal_acknowledged"))
        assert any(proof["run_id"] == run.ref.run_id for proof in worker_database.stopped_proofs())
    finally:
        if agent._loop is not None and not agent._loop.is_closed():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(agent.stop(), agent._loop).result(2)
        hub.disconnect(_WORKER_ID)
        thread.join(timeout=6)
        coordinator.shutdown(timeout=0)
        server.should_exit = True
        server_thread.join(timeout=6)
        if server_thread.is_alive():
            server.force_exit = True
            server_thread.join(timeout=2)
        listener.close()

    assert not thread.is_alive(), "Worker agent thread was not shut down"
    assert not server_thread.is_alive(), "local TLS Master thread was not shut down"
    assert not agent_thread_errors
    assert runtime.shutdown_called.is_set()
    assert watchdog.closed.is_set()
