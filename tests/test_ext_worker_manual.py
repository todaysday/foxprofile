"""Remote manual attachment keeps the Master claim before worker activation."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from foxprofile_ext.dto import BrowserRunRefDTO, ProcessIdentity, RuntimeDescriptor
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.storage import CapacityLedger, ExtensionDatabase
from foxprofile_ext.storage.manual import HelperLiveness
from foxprofile_ext.workers.http import router
from foxprofile_ext.workers.manual import DistributedManualBackend, ManualRelay


@pytest.fixture
def remote_manual(tmp_path):
    calls = []
    ref = BrowserRunRefDTO("profile", "run")
    run = SimpleNamespace(
        ref=ref,
        worker_id="worker",
        worker_boot_id="boot",
        connection_generation=1,
        host_id="worker:worker:boot",
    )
    helper = ProcessIdentity(1234, 5678)

    def command(_run, operation, payload=None, **options):
        calls.append(operation)
        if operation == "manual_open":
            return {"helper": {"pid": helper.pid, "start_ticks": helper.start_ticks}}
        if operation == "manual_close":
            return {"stopped": True}
        if operation == "manual_probe":
            return {"liveness": "live"}
        return {"ok": True}

    database = ExtensionDatabase(tmp_path / "master.sqlite")
    ledger = CapacityLedger(database)
    coordinator = SimpleNamespace(
        ledger=SimpleNamespace(database=database, get=lambda _: run),
        public_url="https://master.example.test",
        hub=SimpleNamespace(command=command, connection_for=lambda _: object()),
        remote_helpers_stopped=lambda _: False,
    )
    coordinator.manual_relay = ManualRelay(coordinator)
    backend = DistributedManualBackend(
        coordinator, lambda: pytest.fail("Master X11 backend was used")
    )
    descriptor = RuntimeDescriptor(
        ref,
        run.host_id,
        ProcessIdentity(100, 10),
        helper,
        ":91",
        "pid:[9876]",
        "/private/run",
        "/private/run/Xauthority",
    )
    yield SimpleNamespace(
        coordinator=coordinator,
        backend=backend,
        run=run,
        calls=calls,
        descriptor=descriptor,
        helper=helper,
        ledger=ledger,
    )
    for relay_id in tuple(coordinator.manual_relay._relays):
        coordinator.manual_relay.revoke(relay_id)


def start(remote, claim):
    return remote.backend.start(
        "manual-session",
        remote.descriptor,
        claim,
        expires_at=9999999999,
        cleanup_deadline=9999999999,
        watchdog_deadline=9999999999,
    )


def test_worker_helper_waits_for_master_claim_and_back_keeps_browser(remote_manual):
    remote = remote_manual

    def claim(helper):
        assert helper == remote.helper
        assert remote.calls == ["manual_open"]
        remote.calls.append("master-claim")
        return True

    result = start(remote, claim)
    assert remote.calls == ["manual_open", "master-claim", "manual_activate"]
    assert remote.backend.connect("manual-session", remote.helper) is result.channel
    remote.backend.release_input("manual-session")
    remote.backend.stop("manual-session", remote.run.host_id, remote.helper, 9999999999)
    assert result.channel.revoked
    assert (
        remote.backend.probe("manual-session", remote.run.host_id, remote.helper)
        is HelperLiveness.DEAD
    )
    assert "stop" not in remote.calls and "start" not in remote.calls
    assert remote.ledger.active_runs() == ()  # Manual access creates no browser reservation.


def test_failed_master_claim_cancels_before_worker_x_attachment(remote_manual):
    remote = remote_manual
    with pytest.raises(DomainError):
        start(remote, lambda _: False)
    assert "manual_activate" not in remote.calls
    assert "manual_close" in remote.calls


def test_verified_run_cleanup_releases_helper_after_connection_loss(remote_manual):
    remote = remote_manual
    result = start(remote, lambda _: True)

    def unavailable(*args, **kwargs):
        raise DomainError(ErrorCode.WORKER_UNAVAILABLE)

    remote.coordinator.hub.command = unavailable
    assert (
        remote.backend.probe("manual-session", remote.run.host_id, remote.helper)
        is HelperLiveness.UNKNOWN
    )
    remote.coordinator.remote_helpers_stopped = lambda run: run is remote.run
    remote.backend.stop("manual-session", remote.run.host_id, remote.helper, 9999999999)
    assert result.channel.revoked
    assert (
        remote.backend.probe("manual-session", remote.run.host_id, remote.helper)
        is HelperLiveness.DEAD
    )
    assert (
        remote.backend.probe("manual-session", "different-host", remote.helper)
        is HelperLiveness.UNKNOWN
    )


def test_outbound_rfb_channel_is_one_use_and_bidirectional(remote_manual, monkeypatch):
    from threading import Event

    remote = remote_manual
    relay_id, channel, grant = remote.coordinator.manual_relay.create(remote.run)
    revoked = Event()
    revoke = remote.coordinator.manual_relay.revoke

    def record_revoke(identifier):
        revoke(identifier)
        if identifier == relay_id:
            revoked.set()

    monkeypatch.setattr(remote.coordinator.manual_relay, "revoke", record_revoke)
    app = FastAPI()
    app.state.container = SimpleNamespace(worker_coordinator=remote.coordinator)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        with client.websocket_connect(
            grant["url"], headers={"Authorization": f"Bearer {grant['token']}"}
        ) as socket:
            socket.send_bytes(b"RFB 003.008\n")
            channel.socket.settimeout(2)
            assert channel.socket.recv(1024) == b"RFB 003.008\n"
            channel.socket.sendall(b"keyboard-input")
            assert socket.receive_bytes() == b"keyboard-input"
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(
                    grant["url"], headers={"Authorization": f"Bearer {grant['token']}"}
                ),
            ):
                pass
        assert revoked.wait(2), "Server did not finish relay cleanup after client disconnect"
        assert channel.revoked
    assert relay_id not in remote.coordinator.manual_relay._relays


def test_foreign_relay_token_does_not_consume_owners_channel(remote_manual):
    remote = remote_manual
    relay_id, channel, grant = remote.coordinator.manual_relay.create(remote.run)
    app = FastAPI()
    app.state.container = SimpleNamespace(worker_coordinator=remote.coordinator)
    app.include_router(router, prefix="/api/v1")
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            grant["url"], headers={"Authorization": "Bearer wrong-token-not-owned-by-worker"}
        ),
    ):
        pass
    assert not channel.revoked
    assert not remote.coordinator.manual_relay._relays[relay_id].consumed
