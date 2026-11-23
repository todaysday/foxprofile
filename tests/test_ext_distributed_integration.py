"""Master orchestration with real SQLite/profile archives and a protocol worker.

No native browser is claimed: this fixture changes the worker's private browser
files and exercises the actual revision/identity/exclusivity boundaries.
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from foxprofile_ext.capacity import CapacityService
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind, ProfileRefDTO
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.runtime.coordinator import RuntimeCoordinator
from foxprofile_ext.storage import AssignmentDAO, CapacityLedger, ExtensionDatabase
from foxprofile_ext.storage.capacity import RunStatus
from foxprofile_ext.workers.coordinator import DistributedCoordinator, PlacementCapacity
from foxprofile_ext.workers.http import router
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.placement import PlacementScheduler
from foxprofile_ext.workers.profile_store import (
    ProfileStore,
    pack_browser_data,
    unpack_browser_data,
)
from foxprofile_ext.workers.registry import WorkerRegistry
from src.models.profile import Profile

ACTOR = PrincipalDTO("test-service", PrincipalKind.SERVICE)
GIB = 1024**3


class ProtocolWorker:
    def __init__(self, tmp_path):
        self.path = tmp_path / "worker-copy"
        self.calls = []
        self.fail_start = False
        self.fail_sync = False
        self.lose_stop_ack = False
        self.coordinator = None
        self.client = None
        self.last_snapshot = None
        self.last_upload = None
        self.last_stop_proof = None
        self.cleanup_calls = []
        self.last_start_profile = None

    def connection_for(self, run):
        return SimpleNamespace(
            worker_id=run.worker_id,
            boot_id=run.worker_boot_id,
            generation=run.connection_generation,
        )

    def command(self, run, operation, payload=None, *, timeout=30):
        self.calls.append(operation)
        if operation == "start":
            self.last_start_profile = payload["profile"]
            snapshot = self.coordinator.profiles.get_snapshot(run.ref.run_id)
            self.last_snapshot = payload["snapshot"]
            archive = snapshot.path
            if self.client:
                response = self.client.get(
                    payload["snapshot"]["url"],
                    headers=self.headers(run, payload["snapshot"]["token"]),
                )
                assert response.status_code == 200, response.text
                archive = self.path.parent / "download.zip"
                archive.write_bytes(response.content)
            unpack_browser_data(
                archive,
                self.path,
                snapshot.archive_digest,
                payload["profile"]["engine"],
                snapshot.identity_digest,
            )
            (self.path / "cookies.sqlite").write_bytes(b"new-cookie-session")
            (self.path / "storage.sqlite").write_bytes(b"persistent-local-storage")
            (self.path / "tabs.json").write_text('["https://example.test/account"]')
            if self.fail_start:
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
            return {"state": "running"}
        if operation == "control":
            return {"action": payload["action"], "png_base64": "eA=="}
        if operation == "stop":
            archive = self.path.parent / "return.zip"
            info = pack_browser_data(self.path, "camoufox", archive)
            self.last_stop_proof = {**stop_proof(run), "archive_digest": info.archive_digest}
            if self.lose_stop_ack:
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
            return {"stopped": True, "proof": self.last_stop_proof}
        if operation == "sync":
            self.last_upload = payload["upload"]
            if self.fail_sync:
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
            archive = self.path.parent / "return.zip"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if self.client:
                response = self.client.post(
                    payload["upload"]["url"],
                    headers={
                        **self.headers(run, payload["upload"]["token"]),
                        "X-Archive-Digest": digest,
                    },
                    content=archive.read_bytes(),
                )
                assert response.status_code == 200, response.text
                return {"commit_receipt": response.json()}
            # HTTPS delivery happens on another request thread while Stop waits.
            with ThreadPoolExecutor() as pool:
                receipt = pool.submit(self.coordinator.commit_upload, run, archive, digest).result(
                    5
                )
            return {"commit_receipt": receipt}
        raise AssertionError(operation)

    def shutdown(self):
        pass

    def cleanup_closed_run(self, run, connection, proof):
        assert self.coordinator.ledger.get(run.ref).status is RunStatus.CLOSED
        self.cleanup_calls.append((run.ref, connection.generation, proof))
        return {"cleaned": True}

    @staticmethod
    def headers(run, token):
        return {
            "Authorization": f"Bearer {token}",
            "X-Worker-ID": run.worker_id,
            "X-Worker-Boot-ID": run.worker_boot_id,
            "X-Connection-Generation": str(run.connection_generation),
        }


def stop_proof(run, *, reason="clean"):
    return {
        "profile_id": run.ref.profile_id,
        "run_id": run.ref.run_id,
        "worker_id": run.worker_id,
        "worker_boot_id": run.worker_boot_id,
        "connection_generation": run.connection_generation,
        "terminated": True,
        "reason": reason,
        "manual_helpers_stopped": True,
    }


@pytest.fixture
def cluster(tmp_path):
    database = ExtensionDatabase(tmp_path / "master.sqlite")
    ledger = CapacityLedger(database)
    registry = WorkerRegistry(database, "a" * 64)
    credential = registry.enroll("worker-a")
    worker = registry.register("worker-a", credential, "boot-a", "a" * 64, True)
    registry.heartbeat(
        "worker-a",
        "boot-a",
        worker.connection_generation,
        NodeMetrics(8 * GIB, None, 8 * GIB, ("camoufox",)),
    )
    profile = Profile("alpha", os_type="linux", proxy="http://user:pass@192.0.2.10:8080")
    data = tmp_path / "canonical"
    directory = data / profile.name
    directory.mkdir(parents=True)
    (directory / "fingerprint.json").write_text(
        json.dumps({"os": "linux", "fingerprint": {"navigator.userAgent": "fixture"}})
    )
    (directory / "cookies.sqlite").write_bytes(b"old-cookies")
    profiles = ProfileStore(database, data, tmp_path / "staging")
    assignments = AssignmentDAO(database)
    scheduler = PlacementScheduler(
        ledger,
        registry,
        local_metrics=lambda: NodeMetrics(GIB // 4, None, 8 * GIB, ("camoufox",)),
        browser_memory_bytes=GIB,
        system_reserve_bytes=GIB // 2,
        staging_reserve_bytes=GIB,
    )
    allowed = [True]

    def authorize(actor, ref):
        if not allowed[0]:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return actor

    local = RuntimeCoordinator(
        CapacityService(ledger, assignments),
        ledger,
        SimpleNamespace(host_id="master", shutdown=lambda *_: None),
        SimpleNamespace(wait_for_launch=lambda *_: (False, "failed")),
        profile_by_id=lambda _: profile,
        authorize=authorize,
        lock_path=str(tmp_path / "catalog.lock"),
    )
    hub = ProtocolWorker(tmp_path)
    coordinator = DistributedCoordinator(
        local,
        PlacementCapacity(ledger, assignments, scheduler, local.profile_by_id),
        registry,
        hub,
        profiles,
        "https://master.example.test",
    )
    hub.coordinator = coordinator
    yield SimpleNamespace(
        db=database,
        ledger=ledger,
        registry=registry,
        profile=profile,
        directory=directory,
        store=profiles,
        local=local,
        coordinator=coordinator,
        hub=hub,
        allowed=allowed,
        ref=ProfileRefDTO(profile.profile_id, profile.name),
    )
    coordinator.shutdown(timeout=0)


def launch(cluster):
    result = cluster.coordinator.reserve_and_start(ACTOR, (cluster.ref,), wait=True, timeout=5)[0]
    assert result.state == "running", result
    return cluster.ledger.active_runs()[0]


def test_remote_dispatch_captures_current_metadata_after_reservation(cluster, monkeypatch):
    from dataclasses import replace

    reservation = cluster.coordinator.capacity.reserve(ACTOR, (cluster.ref,), "master")
    run = cluster.ledger.get(reservation.newly_reserved[0])
    refreshed = replace(
        cluster.profile,
        proxy="http://user:pass@198.51.100.20:8080",
        timezone="Europe/Paris",
        locale="fr-FR",
    )
    monkeypatch.setattr(cluster.coordinator, "profile_by_id", lambda _: refreshed)
    captured = []
    capture = cluster.store.capture

    def record_capture(profile, assigned_run):
        captured.append(profile.to_dict())
        return capture(profile, assigned_run)

    monkeypatch.setattr(cluster.store, "capture", record_capture)
    # The background dispatch still holds the object from before the allowed edit.
    cluster.coordinator._start_remote(ACTOR, cluster.profile, run, cluster.profile.name)
    assert cluster.ledger.get(run.ref).status is RunStatus.RUNNING
    assert captured == [refreshed.to_dict()]
    assert cluster.hub.last_start_profile == refreshed.to_dict()


def test_remote_launch_control_stop_persists_complete_data_once(cluster):
    original_identity = (cluster.directory / "fingerprint.json").read_bytes()
    run = launch(cluster)
    assert run.worker_id == "worker-a" and run.base_revision == 1
    assert cluster.coordinator.status(ACTOR, cluster.ref).execution_location == "remote"
    assert cluster.coordinator.control(ACTOR, cluster.ref, "screenshot")["png_base64"] == "eA=="
    duplicate = cluster.coordinator.reserve_and_start(ACTOR, (cluster.ref,), wait=True)[0]
    assert duplicate.browser_run_id == run.ref.run_id and cluster.hub.calls.count("start") == 1
    cluster.profile.proxy = "http://user:pass@192.0.2.11:8080"
    assert cluster.coordinator.stop(ACTOR, cluster.ref)
    assert cluster.ledger.active_runs() == ()
    assert (cluster.directory / "cookies.sqlite").read_bytes() == b"new-cookie-session"
    assert (cluster.directory / "storage.sqlite").read_bytes() == b"persistent-local-storage"
    assert json.loads((cluster.directory / "tabs.json").read_text()) == [
        "https://example.test/account"
    ]
    assert (cluster.directory / "fingerprint.json").read_bytes() == original_identity
    assert cluster.profile.proxy.endswith("192.0.2.11:8080")
    assert cluster.store.revision(cluster.profile.profile_id)[0] == 2
    archive = cluster.hub.path.parent / "return.zip"
    receipt = cluster.coordinator.commit_upload(
        run, archive, hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    assert receipt["revision"] == 2 and cluster.store.revision(cluster.profile.profile_id)[0] == 2


def test_https_streams_are_scoped_and_exact_upload_retry_returns_same_receipt(cluster):
    app = FastAPI()
    app.state.container = SimpleNamespace(worker_coordinator=cluster.coordinator)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app, base_url="https://master.example.test") as client:
        cluster.hub.client = client
        run = launch(cluster)
        snapshot = cluster.hub.last_snapshot
        denied = client.get(snapshot["url"])
        assert denied.status_code == 401 and b"old-cookies" not in denied.content
        foreign = client.get(
            snapshot["url"],
            headers={
                **cluster.hub.headers(run, snapshot["token"]),
                "X-Worker-Boot-ID": "wrong-boot",
            },
        )
        assert foreign.status_code == 409
        assert cluster.coordinator.stop(ACTOR, cluster.ref)
        upload = cluster.hub.last_upload
        archive = (cluster.hub.path.parent / "return.zip").read_bytes()
        digest = hashlib.sha256(archive).hexdigest()
        headers = {**cluster.hub.headers(run, upload["token"]), "X-Archive-Digest": digest}
        first_retry = client.post(upload["url"], headers=headers, content=archive)
        second_retry = client.post(upload["url"], headers=headers, content=archive)
        assert first_retry.status_code == second_retry.status_code == 200
        assert first_retry.json() == second_retry.json()
        assert cluster.store.revision(cluster.profile.profile_id)[0] == 2
        altered = client.post(
            upload["url"], headers={**headers, "X-Archive-Digest": "0" * 64}, content=archive
        )
        assert altered.status_code == 409
        assert (cluster.directory / "cookies.sqlite").read_bytes() == b"new-cookie-session"
        assert not list(cluster.store.staging_dir.glob("upload-*"))
        cluster.hub.client = None


def test_interrupted_https_upload_preserves_canonical_and_occupied_slot(cluster):
    run = launch(cluster)
    cluster.hub.fail_sync = True
    with pytest.raises(DomainError) as failure:
        cluster.coordinator.stop(ACTOR, cluster.ref)
    assert failure.value.code is ErrorCode.SYNC_FAILED
    upload = cluster.hub.last_upload
    archive = (cluster.hub.path.parent / "return.zip").read_bytes()
    app = FastAPI()
    app.state.container = SimpleNamespace(worker_coordinator=cluster.coordinator)
    app.include_router(router, prefix="/api/v1")

    def interrupted():
        yield archive[:10]
        raise OSError("synthetic disconnected upload")

    with (
        TestClient(app, base_url="https://master.example.test") as client,
        pytest.raises(OSError, match="synthetic disconnected upload"),
    ):
        client.post(
            upload["url"],
            headers={
                **cluster.hub.headers(run, upload["token"]),
                "X-Archive-Digest": hashlib.sha256(archive).hexdigest(),
            },
            content=interrupted(),
        )
    assert cluster.ledger.get(run.ref).status is RunStatus.SYNCING
    assert (cluster.directory / "cookies.sqlite").read_bytes() == b"old-cookies"
    assert not list(cluster.store.staging_dir.glob("upload-*"))


def test_verified_host_fence_proves_old_manual_helpers_dead(cluster):
    run = launch(cluster)
    cluster.coordinator._disconnected(cluster.hub.connection_for(run))
    assert not cluster.coordinator.remote_helpers_stopped(run)
    assert cluster.coordinator.fence(run, "Operator verified the old VM was powered off")
    assert cluster.coordinator.remote_helpers_stopped(run)
    assert not cluster.ledger.active_runs()


def test_verified_fence_can_finish_cleanup_after_browser_already_closed(cluster):
    from foxprofile_ext.storage.manual import ManualLeaseDAO

    run = launch(cluster)
    leases = ManualLeaseDAO(cluster.db)
    now = time.time()
    leases.acquire(
        "manual-session",
        "owner",
        "personal-session",
        run.ref.profile_id,
        run.ref.run_id,
        run.host_id,
        now,
        now + 120,
        now + 60,
    )
    cluster.coordinator.accept_stop_proof(
        run,
        {
            **stop_proof(run, reason="loss"),
            "manual_helpers_stopped": False,
        },
    )
    assert cluster.ledger.get(run.ref).status is RunStatus.CLOSED
    assert leases.current() is not None
    assert not cluster.coordinator.remote_helpers_stopped(run)
    assert cluster.coordinator.fence(
        run, "Verified full-host shutdown after helper proof was unavailable"
    )
    assert cluster.coordinator.remote_helpers_stopped(run)
    assert cluster.coordinator.fence(run, "Repeated exact old-host proof")


def test_upload_retry_does_not_restart_or_release_unsaved_run(cluster):
    run = launch(cluster)
    cluster.hub.fail_sync = True
    with pytest.raises(DomainError) as error:
        cluster.coordinator.stop(ACTOR, cluster.ref)
    assert error.value.code is ErrorCode.SYNC_FAILED
    assert cluster.ledger.get(run.ref).status is RunStatus.SYNCING
    assert (cluster.directory / "cookies.sqlite").read_bytes() == b"old-cookies"
    assert cluster.coordinator.remote_helpers_stopped(run)
    assert (
        cluster.coordinator.reserve_and_start(ACTOR, (cluster.ref,))[0].browser_run_id
        == run.ref.run_id
    )
    cluster.hub.fail_sync = False
    assert cluster.coordinator.stop(ACTOR, cluster.ref)
    assert cluster.hub.calls.count("start") == 1
    assert cluster.hub.calls.count("stop") == 1


def test_loss_preserves_lock_until_exact_same_boot_proof(cluster):
    run = launch(cluster)
    connection = cluster.hub.connection_for(run)
    cluster.coordinator._disconnected(connection)
    assert cluster.ledger.get(run.ref).status is RunStatus.UNCERTAIN
    with pytest.raises(DomainError) as blocked:
        cluster.coordinator.reserve_and_start(ACTOR, (cluster.ref,))
    assert blocked.value.code is ErrorCode.RUN_UNCERTAIN
    with pytest.raises(DomainError):
        cluster.coordinator.control(ACTOR, cluster.ref, "click")
    connected = cluster.hub.connection_for

    def unavailable(_):
        raise DomainError(ErrorCode.WORKER_UNAVAILABLE)

    cluster.hub.connection_for = unavailable
    with pytest.raises(DomainError) as stopped:
        cluster.coordinator.stop(ACTOR, cluster.ref)
    assert stopped.value.code is ErrorCode.RUN_UNCERTAIN
    assert cluster.ledger.get(run.ref).status is RunStatus.UNCERTAIN
    cluster.hub.connection_for = connected
    wrong = {**stop_proof(run), "worker_boot_id": "different-boot"}
    with pytest.raises(DomainError):
        cluster.coordinator.accept_stop_proof(run, wrong, connection)
    assert cluster.ledger.active_runs()
    cluster.coordinator.accept_stop_proof(run, stop_proof(run, reason="loss"), connection)
    assert not cluster.ledger.active_runs()
    assert (cluster.directory / "cookies.sqlite").read_bytes() == b"old-cookies"


def test_authorization_revocation_precedes_any_remote_action(cluster):
    launch(cluster)
    before = len(cluster.hub.calls)
    cluster.allowed[0] = False
    with pytest.raises(DomainError) as error:
        cluster.coordinator.control(ACTOR, cluster.ref, "screenshot")
    assert error.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert len(cluster.hub.calls) == before


def test_start_timeout_keeps_uncertain_claim(cluster):
    cluster.hub.fail_start = True
    result = cluster.coordinator.reserve_and_start(ACTOR, (cluster.ref,), wait=True, timeout=5)[0]
    run = cluster.ledger.active_runs()[0]
    assert result.browser_run_id == run.ref.run_id
    assert result.state == "failed" and result.error_code == "RUN_UNCERTAIN"
    assert run.status is RunStatus.UNCERTAIN
    assert (cluster.directory / "cookies.sqlite").read_bytes() == b"old-cookies"


def test_lost_start_ack_can_be_explicitly_stopped_on_exact_live_connection(cluster):
    cluster.hub.fail_start = True
    result = cluster.coordinator.reserve_and_start(ACTOR, (cluster.ref,), wait=True, timeout=5)[0]
    run = cluster.ledger.active_runs()[0]
    assert result.error_code == "RUN_UNCERTAIN" and run.status is RunStatus.UNCERTAIN
    assert cluster.coordinator.stop(ACTOR, cluster.ref)
    assert not cluster.ledger.active_runs()
    assert cluster.hub.calls.count("start") == 1
    assert (cluster.directory / "cookies.sqlite").read_bytes() == b"new-cookie-session"


def test_late_clean_stop_proof_saves_after_lost_ack(cluster):
    run = launch(cluster)
    cluster.hub.lose_stop_ack = True
    with pytest.raises(DomainError):
        cluster.coordinator.stop(ACTOR, cluster.ref)
    assert cluster.ledger.get(run.ref).status is RunStatus.UNCERTAIN
    cluster.coordinator._event(
        cluster.hub.connection_for(run),
        {
            "type": "stopped",
            "proof": cluster.hub.last_stop_proof,
        },
    )
    deadline = time.monotonic() + 5
    while cluster.ledger.active_runs() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not cluster.ledger.active_runs()
    assert cluster.store.revision(cluster.profile.profile_id)[0] == 2
    assert (cluster.directory / "cookies.sqlite").read_bytes() == b"new-cookie-session"
    assert cluster.hub.calls.count("stop") == 1


def test_terminal_loss_proof_is_acknowledged_after_quarantine_on_new_connection(cluster):
    run = launch(cluster)
    cluster.coordinator._disconnected(cluster.hub.connection_for(run))
    connection = SimpleNamespace(
        worker_id=run.worker_id,
        boot_id=run.worker_boot_id,
        generation=run.connection_generation + 1,
    )
    proof = stop_proof(run, reason="loss")
    cluster.coordinator._event(connection, {"type": "heartbeat", "stopped_runs": [proof]})
    deadline = time.monotonic() + 5
    while not cluster.hub.cleanup_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cluster.hub.cleanup_calls == [(run.ref, connection.generation, proof)]
    assert cluster.ledger.get(run.ref).status is RunStatus.CLOSED
    assert cluster.ledger.lifecycle_snapshot()[run.ref.profile_id] == ("unsaved", "remote")
    assert (cluster.directory / "cookies.sqlite").read_bytes() == b"old-cookies"


def test_terminal_cleanup_is_not_sent_while_canonical_sync_is_pending(cluster):
    run = launch(cluster)
    cluster.hub.fail_sync = True
    with pytest.raises(DomainError):
        cluster.coordinator.stop(ACTOR, cluster.ref)
    connection = cluster.hub.connection_for(run)
    cluster.coordinator._event(
        connection, {"type": "heartbeat", "stopped_runs": [cluster.hub.last_stop_proof]}
    )
    assert cluster.ledger.get(run.ref).status is RunStatus.SYNCING
    assert not cluster.hub.cleanup_calls


def test_master_restart_reconciles_saved_receipt_before_releasing_run(cluster):
    run = launch(cluster)
    cluster.hub.fail_sync = True
    with pytest.raises(DomainError):
        cluster.coordinator.stop(ACTOR, cluster.ref)
    archive = cluster.hub.path.parent / "return.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = cluster.store.commit(cluster.profile, run, archive, run.base_revision, digest)
    assert receipt.revision == 2 and cluster.ledger.get(run.ref).status is RunStatus.SYNCING
    # Model a crash after the durable receipt but before central ledger release.
    cluster.ledger.recover(lambda *_: pytest.fail("A remote PID was probed on Master"))
    assert cluster.ledger.get(run.ref).status is RunStatus.UNCERTAIN
    restarted_hub = ProtocolWorker(archive.parent)
    restarted = DistributedCoordinator(
        cluster.local,
        cluster.coordinator.capacity,
        cluster.registry,
        restarted_hub,
        cluster.store,
        "https://master.example.test",
    )
    try:
        assert cluster.ledger.get(run.ref).status is RunStatus.CLOSED
        assert cluster.store.get_snapshot(run.ref.run_id) is None
        assert cluster.store.revision(cluster.profile.profile_id)[0] == 2
        assert (cluster.directory / "cookies.sqlite").read_bytes() == b"new-cookie-session"
        assert not restarted_hub.calls
    finally:
        restarted.shutdown(timeout=0)


def test_snapshot_failure_releases_only_never_dispatched_assignment(cluster, monkeypatch):
    def fail(*_):
        raise OSError("fixture disk full")

    monkeypatch.setattr(cluster.store, "capture", fail)
    cluster.coordinator.reserve_and_start(ACTOR, (cluster.ref,), wait=True, timeout=5)
    assert cluster.ledger.active_runs() == ()
    assert cluster.ledger.lifecycle_snapshot() == {}
    assert cluster.hub.calls == []
