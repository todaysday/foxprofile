"""Exercise real Master WebSocket frames, correlation and admin boundaries."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from foxprofile_ext.application import ProtectedApplication
from foxprofile_ext.dto import BrowserRunRefDTO, PrincipalDTO, PrincipalKind, Role
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage.database import ExtensionDatabase
from foxprofile_ext.workers.channel import MasterWorkerHub
from foxprofile_ext.workers.http import router
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.registry import WorkerRegistry
from src.api.dependencies import get_request_principal

DIGEST = "a" * 64
ORIGIN = "https://master.example.test"


@pytest.fixture
def channel_api(tmp_path):
    database = ExtensionDatabase(tmp_path / "master.sqlite")
    registry = WorkerRegistry(database, DIGEST)
    token = registry.enroll("worker-a")
    dropped = []
    hub = MasterWorkerHub(
        registry, on_disconnect=lambda connection: dropped.append(connection.generation)
    )
    app = FastAPI()
    coordinator = SimpleNamespace(
        workers=registry, hub=hub, ledger=SimpleNamespace(active_runs=lambda: ())
    )
    application = ProtectedApplication(None, None, None, None, coordinator)
    application.current_principal = lambda principal: principal
    app.state.container = SimpleNamespace(
        worker_coordinator=coordinator,
        protected_application=application,
    )
    app.include_router(router, prefix="/api/v1")
    actor = PrincipalDTO("admin", PrincipalKind.PERSONAL, Role.ADMIN, "session")
    app.dependency_overrides[get_request_principal] = lambda: actor
    with TestClient(app, base_url=ORIGIN) as client:
        yield SimpleNamespace(
            client=client,
            registry=registry,
            token=token,
            hub=hub,
            app=app,
            coordinator=coordinator,
            dropped=dropped,
        )


def connect(api, *, token=None):
    return api.client.websocket_connect(
        "wss://master.example.test/api/v1/workers/connect",
        headers={"Authorization": f"Bearer {token or api.token}"},
    )


def ready(socket):
    socket.send_json(
        {
            "type": "hello",
            "worker_id": "worker-a",
            "boot_id": "boot-a",
            "manifest_digest": DIGEST,
            "preflight_ok": True,
        }
    )
    welcome = socket.receive_json()
    envelope = {
        "worker_id": "worker-a",
        "boot_id": "boot-a",
        "connection_generation": welcome["connection_generation"],
    }
    socket.send_json(
        {
            "type": "heartbeat",
            **envelope,
            "metrics": asdict(NodeMetrics(8 * 1024**3, None, 8 * 1024**3, ("camoufox",))),
            "active_runs": [],
            "stopped_runs": [],
        }
    )
    assert socket.receive_json()["type"] == "heartbeat_ack"
    return SimpleNamespace(
        ref=BrowserRunRefDTO("profile-a", "run-a"),
        worker_id="worker-a",
        worker_boot_id="boot-a",
        connection_generation=welcome["connection_generation"],
    )


def test_scoped_commands_ignore_cross_run_results(channel_api):
    api = channel_api
    with connect(api) as socket:
        run = ready(socket)
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(
                api.hub.command, run, "control", {"action": "screenshot"}, timeout=2
            )
            command = socket.receive_json()
            assert command["profile_id"] == "profile-a"
            socket.send_json(
                {**command, "type": "result", "ok": True, "run_id": "foreign", "result": "wrong"}
            )
            socket.send_json(
                {**command, "type": "result", "ok": True, "result": {"png_base64": "eA=="}}
            )
            assert pending.result(3) == {"png_base64": "eA=="}
    assert api.dropped == [run.connection_generation]


def test_timeout_discards_pending_without_freeing_run(channel_api):
    api = channel_api
    with connect(api) as socket:
        run = ready(socket)
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(api.hub.command, run, "start", {}, timeout=0.02)
            socket.receive_json()
            with pytest.raises(DomainError) as failure:
                pending.result(3)
            assert failure.value.code is ErrorCode.WORKER_UNAVAILABLE
        assert api.hub.connection_for(run).pending == {}


def test_terminal_cleanup_uses_current_channel_but_preserves_original_owner(channel_api):
    api = channel_api
    with connect(api) as socket:
        old_run = ready(socket)
    with connect(api) as socket:
        current_run = ready(socket)
        connection = api.hub.connection_for(current_run)
        assert current_run.connection_generation > old_run.connection_generation
        with pytest.raises(DomainError):
            api.hub.command(old_run, "control", {})
        proof = {
            "profile_id": old_run.ref.profile_id,
            "run_id": old_run.ref.run_id,
            "worker_id": old_run.worker_id,
            "worker_boot_id": old_run.worker_boot_id,
            "connection_generation": old_run.connection_generation,
            "terminated": True,
            "manual_helpers_stopped": True,
            "reason": "loss",
        }
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(api.hub.cleanup_closed_run, old_run, connection, proof)
            command = socket.receive_json()
            assert command["connection_generation"] == current_run.connection_generation
            assert command["operation"] == "cleanup"
            assert command["payload"] == {"disposition": "closed", "proof": proof}
            socket.send_json({**command, "type": "result", "ok": True, "result": {"cleaned": True}})
            assert pending.result(3) == {"cleaned": True}
        foreign = SimpleNamespace(
            worker_id=old_run.worker_id, boot_id="different-boot", generation=connection.generation
        )
        with pytest.raises(DomainError) as error:
            api.hub.cleanup_closed_run(old_run, foreign, proof)
        assert error.value.code is ErrorCode.STALE_RUN


def test_nonheartbeat_traffic_cannot_keep_worker_ready(channel_api, monkeypatch):
    import foxprofile_ext.workers.channel as module

    monkeypatch.setattr(module, "LOSS_TIMEOUT_SECONDS", 0.1)
    api = channel_api
    with connect(api) as socket:
        run = ready(socket)
        for _ in range(6):
            socket.send_json(
                {
                    "type": "result",
                    "command_id": "unissued",
                    "worker_id": run.worker_id,
                    "boot_id": run.worker_boot_id,
                    "connection_generation": run.connection_generation,
                }
            )
            time.sleep(0.02)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert api.dropped == [run.connection_generation]


def test_oversized_command_is_rejected_before_transport(channel_api, monkeypatch):
    import foxprofile_ext.workers.channel as module

    monkeypatch.setattr(module, "MAX_MESSAGE_BYTES", 1024)
    api = channel_api
    with connect(api) as socket:
        run = ready(socket)
        with pytest.raises(DomainError) as failure:
            api.hub.command(run, "control", {"text": "x" * 1024})
        assert failure.value.code is ErrorCode.VALIDATION_ERROR
        assert api.hub.connection_for(run).pending == {}


def test_missing_active_run_inventory_invalidates_worker_readiness(channel_api):
    api = channel_api
    with connect(api) as socket:
        run = ready(socket)
        socket.send_json(
            {
                "type": "heartbeat",
                "worker_id": run.worker_id,
                "boot_id": run.worker_boot_id,
                "connection_generation": run.connection_generation,
                "metrics": asdict(NodeMetrics(8 * 1024**3, None, 8 * 1024**3, ("camoufox",))),
            }
        )
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert api.registry.get(run.worker_id).state == "offline"


def test_revoked_identity_cannot_route_commands(channel_api):
    api = channel_api
    with connect(api) as socket:
        run = ready(socket)
        api.registry.revoke("worker-a")
        with pytest.raises(DomainError):
            api.hub.command(run, "control", {})
    with connect(api) as socket:
        socket.send_json(
            {
                "type": "hello",
                "worker_id": "worker-a",
                "boot_id": "boot-a",
                "manifest_digest": DIGEST,
                "preflight_ok": True,
            }
        )
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


@pytest.mark.parametrize("invalidation", ["revoke", "rotate", "register", "disconnect", "expire"])
def test_pending_result_requires_current_durable_owner(channel_api, invalidation):
    api = channel_api
    with connect(api) as socket:
        run = ready(socket)
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(api.hub.command, run, "control", {}, timeout=2)
            command = socket.receive_json()
            if invalidation == "register":
                api.registry.register("worker-a", api.token, "boot-a", DIGEST, True)
            elif invalidation == "disconnect":
                api.registry.disconnect("worker-a", "boot-a", run.connection_generation)
            elif invalidation == "expire":
                expired_at = time.time() + 31
                api.registry.clock = lambda: expired_at
            else:
                getattr(api.registry, invalidation)("worker-a")
            # Deliberately leave the old in-memory socket installed: the durable
            # change must fence a result before the asynchronous drop callback.
            socket.send_json({**command, "type": "result", "ok": True, "result": "stale-image"})
            with pytest.raises(DomainError) as failure:
                pending.result(3)
            assert failure.value.code in {ErrorCode.STALE_RUN, ErrorCode.WORKER_UNAVAILABLE}


def test_pending_result_from_current_draining_owner_remains_valid(channel_api):
    api = channel_api
    with connect(api) as socket:
        run = ready(socket)
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(api.hub.command, run, "control", {}, timeout=2)
            command = socket.receive_json()
            api.registry.set_draining("worker-a", True)
            socket.send_json({**command, "type": "result", "ok": True, "result": "current-image"})
            assert pending.result(3) == "current-image"


@pytest.mark.parametrize("outcome", ["current", "timed-out", "disconnected"])
def test_result_waiting_for_database_writer_keeps_event_loop_available(
    channel_api, monkeypatch, outcome
):
    api = channel_api
    checking, loop_progress = Event(), Event()
    original_guard = api.registry.connection_guard

    def observed_guard(*args):
        checking.set()
        return original_guard(*args)

    with connect(api) as socket, ThreadPoolExecutor() as pool:
        run = ready(socket)
        connection = api.hub.connection_for(run)
        timeout = 0.5 if outcome == "timed-out" else 5
        pending = pool.submit(api.hub.command, run, "control", {}, timeout=timeout)
        command = socket.receive_json()
        monkeypatch.setattr(api.registry, "connection_guard", observed_guard)
        with api.registry.database.transaction():
            reply = pool.submit(
                socket.send_json, {**command, "type": "result", "ok": True, "result": "first"}
            )
            assert checking.wait(2)
            connection.loop.call_soon_threadsafe(loop_progress.set)
            assert loop_progress.wait(1), "Result ownership check blocked the ASGI event loop"
            if outcome == "disconnected":
                api.hub.disconnect(run.worker_id)
            if outcome != "current":
                with pytest.raises(DomainError) as failure:
                    pending.result(2)
                assert failure.value.code is ErrorCode.WORKER_UNAVAILABLE
        reply.result(2)
        if outcome == "current":
            assert pending.result(2) == "first"
        if outcome == "disconnected":
            return
        # A timeout while the result is being checked must leave the socket usable.
        following = pool.submit(api.hub.command, run, "control", {}, timeout=3)
        command = socket.receive_json()
        socket.send_json({**command, "type": "result", "ok": True, "result": "next"})
        assert following.result(3) == "next"


def test_forged_worker_and_stale_reply_are_rejected(channel_api):
    api = channel_api
    with connect(api, token="forged-token-that-is-not-authorized") as socket:
        socket.send_json(
            {
                "type": "hello",
                "worker_id": "worker-a",
                "boot_id": "boot-a",
                "manifest_digest": DIGEST,
                "preflight_ok": True,
            }
        )
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert api.registry.get("worker-a").boot_id is None


def test_worker_inventory_is_personal_admin_only(channel_api):
    api = channel_api
    for actor in (
        PrincipalDTO("alice", PrincipalKind.PERSONAL, Role.USER, "session"),
        PrincipalDTO("service", PrincipalKind.SERVICE),
    ):
        api.app.dependency_overrides[get_request_principal] = lambda actor=actor: actor
        response = api.client.get("/api/v1/workers")
        assert response.status_code == 403
        assert "worker-a" not in response.text
    api.app.dependency_overrides[get_request_principal] = lambda: PrincipalDTO(
        "admin", PrincipalKind.PERSONAL, Role.ADMIN, "session"
    )
    response = api.client.get("/api/v1/workers")
    assert response.status_code == 200
    assert response.json()["workers"][0]["worker_id"] == "worker-a"
    assert "credential" not in response.text and api.token not in response.text
