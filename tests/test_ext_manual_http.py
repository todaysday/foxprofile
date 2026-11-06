"""Manual gateway: real identity/ACL/lease storage, private sockets, native RFB."""

from __future__ import annotations

import hashlib
import os
import select
import socket
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from foxprofile_ext.application import ProtectedApplication
from foxprofile_ext.auth import AuthService, hash_password
from foxprofile_ext.catalog import ProfileCatalog
from foxprofile_ext.dto import ProcessIdentity, RuntimeDescriptor
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.manual import BackendSession
from foxprofile_ext.manual.backend import VNCBackendUnavailable
from foxprofile_ext.manual.factory import ManualGateway, build_manual_gateway
from foxprofile_ext.manual.private_channel import PrivateChannel
from foxprofile_ext.runtime.confinement import ConfinementPurpose, build_confined_command
from foxprofile_ext.storage import (
    AssignmentDAO,
    CapacityLedger,
    CatalogOperationDAO,
    ExtensionDatabase,
    ManualLeaseDAO,
    RunStatus,
    SessionDAO,
    UserDAO,
)
from foxprofile_ext.storage.manual import HelperLiveness
from foxprofile_ext.transport import websocket_transport_options
from src.api import app as api_app
from src.api import auth as api_auth
from src.api import server as api_server
from src.core.container import Container
from src.services.profile import manager as manager_module
from src.services.profile.manager import ProfileManager
from tests.test_ext_vnc_backend import native as native  # Reuse isolated Xvfb fixture; no browser.

ORIGIN = "https://panel.example.test"
PASSWORD = "canary fixture password"
SERVICE_TOKEN = "canary-service-key-never-personal-123456"


class SocketBackend:
    """Actual connected binary socket; only process ownership is simulated."""

    def __init__(self, **configuration):
        self.configuration = configuration
        self.entries = {}
        self.events = []

    def start(self, session_id, descriptor, claim, **bounds):
        gateway, peer = socket.socketpair()
        peer.settimeout(3)
        helper = ProcessIdentity(700, 800)
        channel = PrivateChannel(gateway)
        self.entries[session_id] = SimpleNamespace(
            peer=peer,
            channel=channel,
            helper=helper,
            live=True,
            bounds=bounds,
        )
        assert claim(helper)
        peer.sendall(b"RFB 003.008\n")
        self.events.append("start")
        return BackendSession(helper, channel)

    def connect(self, session_id, helper):
        entry = self.entries[session_id]
        assert helper == entry.helper
        self.events.append("connect")
        return entry.channel

    def heartbeat(self, session_id, deadline):
        assert self.entries[session_id].live

    def revoke(self, session_id):
        if session_id in self.entries:
            self.entries[session_id].channel.revoke()

    def release_input(self, session_id):
        self.events.append("release")

    def stop(self, session_id, host_id, helper, cleanup_deadline):
        self.events.append("stop")
        if session_id in self.entries:
            entry = self.entries[session_id]
            entry.channel.revoke()
            entry.peer.close()
            entry.live = False

    def probe(self, session_id, host_id, helper):
        entry = self.entries.get(session_id)
        return HelperLiveness.LIVE if entry and entry.live else HelperLiveness.DEAD


@pytest.fixture
def gateway_api(tmp_path, monkeypatch, request):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.setattr(api_auth, "API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(api_app, "API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "profiles")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", tmp_path / "profiles.json")
    manager = ProfileManager()
    database = ExtensionDatabase(tmp_path / "extension.sqlite3")
    users, sessions, assignments = UserDAO(database), SessionDAO(database), AssignmentDAO(database)
    password_hash = hash_password(PASSWORD)
    users.bootstrap_admin("admin", "admin", password_hash)
    users.create("alice", "alice", password_hash)
    users.create("bob", "bob", password_hash)
    catalog = ProfileCatalog(
        manager, CatalogOperationDAO(database), assignments, tmp_path / "catalog.lock"
    )
    profile_a = catalog.create("alice", name="Canary A")
    profile_b = catalog.create("bob", name="Canary B")
    auth = AuthService(
        users,
        sessions,
        assignments,
        lambda profile_id: (
            profile_id
            in {
                profile_a.profile_id,
                profile_b.profile_id,
            }
        ),
    )
    ledger = CapacityLedger(database)
    runs = ledger.reserve([profile_a.profile_id, profile_b.profile_id], "canary-host").runs
    status = getattr(request, "param", RunStatus.RUNNING)
    for run in runs:
        if status is not RunStatus.RESERVED:
            assert ledger.claim(run.ref, "canary-host", ProcessIdentity(101, 201))
        if status in {RunStatus.RUNNING, RunStatus.STOPPING}:
            assert ledger.mark_running(run.ref)
        if status is RunStatus.STOPPING:
            assert ledger.mark_stopping(run.ref)
    descriptors = {
        run.ref.profile_id: RuntimeDescriptor(
            run.ref,
            "canary-host",
            ProcessIdentity(101, 201),
            ProcessIdentity(102, 202),
            ":101",
            "pid:[456]",
            str(tmp_path / run.ref.run_id),
            str(tmp_path / run.ref.run_id / "Xauthority"),
        )
        for run in runs
    }
    lookups = []

    def verified_descriptor(ref):
        lookups.append(ref)
        descriptor = descriptors.get(ref.profile_id)
        return descriptor if descriptor and descriptor.run == ref else None

    runtime = SimpleNamespace(verified_descriptor=verified_descriptor)
    coordinator = SimpleNamespace(runtime=runtime)
    protected = ProtectedApplication(auth, catalog, assignments, ledger, coordinator)
    executable = tmp_path / "canary-native-binary"
    executable.write_bytes(b"canary executable; fake backend never executes this")
    executable.chmod(0o700)
    monkeypatch.setenv("FOXPROFILE_X11VNC_EXECUTABLE", str(executable))
    monkeypatch.setenv(
        "FOXPROFILE_X11VNC_SHA256", hashlib.sha256(executable.read_bytes()).hexdigest()
    )
    backends = []

    def backend_factory(**configuration):
        backend = SocketBackend(**configuration)
        backends.append(backend)
        return backend

    gateway = build_manual_gateway(
        database, catalog, auth, protected, coordinator, backend_factory=backend_factory
    )
    backend = backends[0]
    container = SimpleNamespace(
        auth_service=auth, protected_application=protected, manual_gateway=gateway
    )
    app = api_app.create_app(container)
    clients, issued = {}, {}
    for name in ("alice", "bob", "admin"):
        issued[name] = auth.login(name, PASSWORD)
        client = TestClient(app, base_url=ORIGIN)
        client.cookies.set(
            "foxprofile_session", issued[name].session_token, domain="panel.example.test"
        )
        clients[name] = client
    env = SimpleNamespace(**locals())
    try:
        yield env
    finally:
        gateway.shutdown()
        for client in clients.values():
            client.close()


def create(env, user="alice", profile=None, **options):
    return env.clients[user].post(
        f"/api/v1/ext/profiles/{profile or env.profile_a.profile_id}/manual-sessions",
        json=options.pop("json", {}),
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": env.issued[user].csrf_token,
            **options.pop("headers", {}),
        },
        **options,
    )


def ws_url(session):
    return "wss://panel.example.test" + session["websocket_path"]


def close(env, session, user="alice", **body):
    return env.clients[user].request(
        "DELETE",
        f"/api/v1/ext/manual-sessions/{session['session_id']}",
        headers={"Origin": ORIGIN, "X-CSRF-Token": env.issued[user].csrf_token},
        json={"browser_run_id": session["browser_run_id"], **body},
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_lifecycle_owner_session_acl_redaction_and_capacity(gateway_api):
    env = gateway_api
    before = env.ledger.active_runs()
    response = create(env)
    assert response.status_code == 200
    session = response.json()
    assert set(session) == {
        "session_id",
        "profile_id",
        "browser_run_id",
        "state",
        "websocket_path",
        "expires_at",
    }
    assert create(env).json() == session
    assert len(env.backend.entries) == 1
    for name in ("bob", "admin"):
        path = f"/api/v1/ext/manual-sessions/{session['session_id']}"
        assert env.clients[name].get(path).status_code == 404
        assert close(env, session, name).status_code == 404
    busy = create(env, "bob", env.profile_b.profile_id)
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "MANUAL_SESSION_BUSY"
    assert "Canary A" not in busy.text and "alice" not in busy.text
    assert close(env, session, browser_run_id="stale").status_code == 409
    assert close(env, session).json()["state"] == "closed"
    assert close(env, session).json()["state"] == "closed"
    assert env.ledger.active_runs() == before
    assert env.backend.events[-2:] == ["release", "stop"]
    assert env.lookups


@pytest.mark.parametrize(
    "gateway_api", [RunStatus.RESERVED, RunStatus.STARTING, RunStatus.STOPPING], indirect=True
)
@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_factory_rejects_nonrunning_run_before_backend_start(gateway_api):
    env = gateway_api
    before = env.ledger.active_runs()
    response = create(env)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_BROWSER_RUN"
    assert not env.backend.events
    assert not env.backend.entries
    assert not env.lookups
    assert ManualLeaseDAO(env.database).current() is None
    assert env.ledger.active_runs() == before


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_factory_opens_only_exact_running_ref(gateway_api):
    env = gateway_api
    descriptor = env.descriptors[env.profile_a.profile_id]
    before = env.ledger.active_runs()
    assert env.ledger.get(descriptor.run).status is RunStatus.RUNNING
    response = create(env)
    assert response.status_code == 200
    assert response.json()["browser_run_id"] == descriptor.run.run_id
    assert env.backend.events == ["start"]
    assert env.lookups and all(ref == descriptor.run for ref in env.lookups)
    assert env.ledger.active_runs() == before


@pytest.mark.parametrize(
    "field", ["display", "port", "host", "pid", "xauthority_path", "target", "key", "password"]
)
@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_target_fields_forbidden(gateway_api, field):
    assert create(gateway_api, json={field: "canary"}).status_code == 422
    assert not gateway_api.backend.entries


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_http_auth_csrf_service_and_foreign_profile(gateway_api):
    env = gateway_api
    path = f"/api/v1/ext/profiles/{env.profile_a.profile_id}/manual-sessions"
    anonymous = TestClient(env.app, base_url=ORIGIN)
    assert anonymous.post(path, json={}).status_code == 401
    assert env.clients["alice"].post(path, json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert create(env, headers={"Origin": "https://attacker.example.test"}).status_code == 403
    assert create(env, "bob").status_code == 404
    assert create(env, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}).status_code == 404
    assert create(env, headers={"X-API-Key": SERVICE_TOKEN}).status_code == 404
    assert create(env, profile="Canary A").status_code == 404
    assert not env.backend.entries


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_delete_requires_csrf_and_rejects_target_fields(gateway_api):
    env = gateway_api
    session = create(env).json()
    path = f"/api/v1/ext/manual-sessions/{session['session_id']}"
    for headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://attacker.example.test"}):
        response = env.clients["alice"].request(
            "DELETE", path, headers=headers, json={"browser_run_id": session["browser_run_id"]}
        )
        assert response.status_code == 403
    assert close(env, session, host="192.0.2.1").status_code == 422
    assert close(env, session, detection_budget_seconds=0).status_code == 422
    assert env.backend.entries[session["session_id"]].live
    assert close(env, session).status_code == 200


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_websocket_missing_cookie_cannot_use_locator(gateway_api):
    env = gateway_api
    session = create(env).json()
    with (
        TestClient(env.app, base_url=ORIGIN) as anonymous,
        pytest.raises(WebSocketDenialResponse) as denied,
        anonymous.websocket_connect(ws_url(session), headers={"Origin": ORIGIN}),
    ):
        pass
    assert denied.value.status_code == 401
    assert env.backend.entries[session["session_id"]].live
    assert "connect" not in env.backend.events


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_remote_manual_http_and_websocket_require_encrypted_transport(gateway_api):
    env = gateway_api
    origin = "http://panel.example.test"
    with TestClient(env.app, base_url=origin) as plain:
        plain.cookies.set(
            "foxprofile_session", env.issued["alice"].session_token, domain="panel.example.test"
        )
        response = plain.post(
            f"/api/v1/ext/profiles/{env.profile_a.profile_id}/manual-sessions",
            headers={"Origin": origin, "X-CSRF-Token": env.issued["alice"].csrf_token},
            json={},
        )
        assert response.status_code == 403
        assert not env.backend.entries
        session = create(env).json()
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            plain.websocket_connect(
                ws_url(session).replace("wss:", "ws:"),
                headers={
                    "Origin": origin,
                },
            ),
        ):
            pass
        assert denied.value.status_code == 403
        assert env.backend.entries[session["session_id"]].live
        assert "connect" not in env.backend.events


@pytest.mark.parametrize(
    "origin",
    [None, "null", "https://attacker.example.test", ORIGIN + "/", ORIGIN + ":443", "https://["],
)
@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_websocket_exact_origin_rejection_keeps_owner_lease(gateway_api, origin):
    env = gateway_api
    session = create(env).json()
    headers = {"Origin": origin} if origin is not None else {}
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        env.clients["alice"].websocket_connect(ws_url(session), headers=headers),
    ):
        pass
    assert denied.value.status_code == 403
    assert (
        env.gateway.service.status(env.issued["alice"].principal, session["session_id"]).state.value
        == "active"
    )
    assert "connect" not in env.backend.events


@pytest.mark.parametrize(
    "user,headers,query,status",
    [
        ("bob", {}, "", 404),
        ("alice", {"Authorization": f"Bearer {SERVICE_TOKEN}"}, "", 404),
        ("alice", {"X-API-Key": SERVICE_TOKEN}, "", 404),
        ("alice", {}, "?token=canary", 422),
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_websocket_foreign_service_query_denials_do_not_close(
    gateway_api, user, headers, query, status
):
    env = gateway_api
    session = create(env).json()
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        env.clients[user].websocket_connect(
            ws_url(session) + query, headers={"Origin": ORIGIN, **headers}
        ),
    ):
        pass
    assert denied.value.status_code == status
    assert env.backend.entries[session["session_id"]].live


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_binary_socket_bridge_duplicate_stream_and_disconnect(gateway_api):
    env = gateway_api
    session = create(env).json()
    entry = env.backend.entries[session["session_id"]]
    before = env.ledger.active_runs()
    with env.clients["alice"].websocket_connect(
        ws_url(session), headers={"Origin": ORIGIN}
    ) as websocket:
        assert websocket.receive_bytes() == b"RFB 003.008\n"
        websocket.send_bytes(b"\x00\x01\xffcanary-input")
        assert entry.peer.recv(100) == b"\x00\x01\xffcanary-input"
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            env.clients["alice"].websocket_connect(ws_url(session), headers={"Origin": ORIGIN}),
        ):
            pass
        assert denied.value.status_code == 409
        entry.peer.sendall(b"\x00\xffcanary-pixels")
        assert websocket.receive_bytes() == b"\x00\xffcanary-pixels"
        assert env.backend.events.count("connect") == 1
    assert (
        env.gateway.service.status(env.issued["alice"].principal, session["session_id"]).state.value
        == "closed"
    )
    with (
        pytest.raises(WebSocketDenialResponse) as gone,
        env.clients["alice"].websocket_connect(ws_url(session), headers={"Origin": ORIGIN}),
    ):
        pass
    assert gone.value.status_code == 410
    assert env.ledger.active_runs() == before


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_websocket_cleanup_reserves_transport_detection_budget(gateway_api):
    env = gateway_api
    now = [100.0]
    env.gateway.service._clock = lambda: now[0]
    session = create(env).json()
    with env.clients["alice"].websocket_connect(
        ws_url(session), headers={"Origin": ORIGIN}
    ) as websocket:
        assert websocket.receive_bytes() == b"RFB 003.008\n"
        now[0] = 120  # Worst-case detection after loss at 100.
    lease = ManualLeaseDAO(env.database).get(session["session_id"])
    assert lease.state.value == "closed"
    assert lease.cleanup_deadline == 160  # 20 detection + 40 cleanup, never 20 + 60.


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_real_transport_detects_unresponsive_peer_without_idle_cutoff(gateway_api, monkeypatch):
    env = gateway_api
    session = create(env).json()
    entry = env.backend.entries[session["session_id"]]
    monkeypatch.setattr(api_server, "API_HOST", "127.0.0.1")
    monkeypatch.setattr(api_server, "API_PORT", 0)
    server = api_server.APIServer(env.app)
    server.start()
    try:
        startup = time.monotonic() + 5
        while not server._server.started and time.monotonic() < startup:
            select.select([], [], [], 0.01)
        assert server._server.started
        port = server._server.servers[0].sockets[0].getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=3) as peer:
            peer.settimeout(24)
            started = time.monotonic()
            peer.sendall(
                (
                    f"GET {session['websocket_path']} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"Origin: http://127.0.0.1:{port}\r\n"
                    f"Cookie: foxprofile_session={env.issued['alice'].session_token}\r\n"
                    "Connection: Upgrade\r\nUpgrade: websocket\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            received = bytearray()
            # Raw peer receives Ping but deliberately sends no Pong or Close reply.
            while data := peer.recv(4096):
                received.extend(data)
            assert received.startswith(b"HTTP/1.1 101 ")
            assert b"RFB 003.008\n" in received
            finished = time.monotonic() + 2
            while entry.live and time.monotonic() < finished:
                select.select([], [], [], 0.01)
            assert not entry.live
            assert time.monotonic() - started < 23
            assert env.backend.events[-2:] == ["release", "stop"]
    finally:
        server.stop()
        server._thread.join(timeout=5)
        assert not server.is_running


def test_legacy_transport_keeps_default_protocol(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "false")
    assert websocket_transport_options() == {}


def test_legacy_api_fresh_import_does_not_require_argon2():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
from types import SimpleNamespace

class MissingArgon2(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "argon2" or fullname.startswith("argon2."):
            raise ModuleNotFoundError("optional argon2 deliberately unavailable")

sys.meta_path.insert(0, MissingArgon2())
import src.api.app
import src.api.server
import src.server
src.api.app.create_app(SimpleNamespace())
assert "argon2" not in sys.modules
assert "foxprofile_ext.manual" not in sys.modules
assert src.api.server.websocket_transport_options() == {}
""",
        ],
        env={**os.environ, "FOXPROFILE_EXT_ENABLED": "false"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_protected_transport_requires_keepalive_capable_dependency(monkeypatch):
    from uvicorn.protocols.websockets import websockets_sansio_impl

    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.delattr(websockets_sansio_impl.WebSocketsSansIOProtocol, "keepalive_timeout")
    with pytest.raises(RuntimeError, match=r"uvicorn\[standard\]>=0.54.0"):
        websocket_transport_options()


@pytest.mark.parametrize(
    "trigger", ["logout", "disabled", "grant", "stale", "cookie-expired", "revoke"]
)
@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_passive_stream_stops_for_current_authority_and_run(gateway_api, trigger):
    env = gateway_api
    session = create(env).json()
    entry = env.backend.entries[session["session_id"]]
    before = env.ledger.active_runs()
    started = time.monotonic()
    with env.clients["alice"].websocket_connect(
        ws_url(session), headers={"Origin": ORIGIN}
    ) as websocket:
        assert websocket.receive_bytes() == b"RFB 003.008\n"
        if trigger == "logout":
            response = env.clients["alice"].post(
                "/api/v1/ext/auth/logout",
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": env.issued["alice"].csrf_token,
                },
            )
            assert response.status_code == 200
        elif trigger == "disabled":
            env.users.set_active("alice", False)
        elif trigger == "grant":
            env.assignments.replace_for_user("alice", ())
        elif trigger == "stale":
            old = env.descriptors[env.profile_a.profile_id]
            env.descriptors[env.profile_a.profile_id] = replace(
                old, run=replace(old.run, run_id="replacement-run")
            )
        elif trigger == "cookie-expired":
            record = env.sessions.get_valid_by_id(
                env.issued["alice"].principal.session_id, time.time()
            )
            env.auth._clock = lambda: record.expires_at
        else:
            entry.channel.revoke()
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_bytes()
    assert time.monotonic() - started < 5
    assert not entry.live
    assert env.ledger.active_runs() == before
    assert env.backend.events[-2:] == ["release", "stop"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_guessed_and_other_personal_cookie_do_not_cleanup(gateway_api):
    env = gateway_api
    session = create(env).json()
    second = env.auth.login("alice", PASSWORD)
    other = TestClient(env.app, base_url=ORIGIN)
    other.cookies.set("foxprofile_session", second.session_token, domain="panel.example.test")
    for client, path in (
        (other, ws_url(session)),
        (env.clients["alice"], ws_url(session).replace(session["session_id"], "guessed")),
    ):
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            client.websocket_connect(path, headers={"Origin": ORIGIN}),
        ):
            pass
        assert denied.value.status_code == 404
    assert env.backend.entries[session["session_id"]].live


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_gateway_reconcile_and_process_stream_lock(gateway_api):
    env = gateway_api
    session = create(env).json()
    with env.gateway.claim_stream(session["session_id"]):
        # Separate object and separately opened FD must still conflict.
        other = object.__new__(ManualGateway)
        other.state_root, other._stopped = env.gateway.state_root, env.gateway._stopped
        with pytest.raises(DomainError) as denied, other.claim_stream(session["session_id"]):
            pass
        assert denied.value.code is ErrorCode.MANUAL_SESSION_BUSY
    assert env.gateway.service.reconcile()
    assert (
        env.gateway.service.status(env.issued["alice"].principal, session["session_id"]).state.value
        == "closed"
    )
    assert not ManualLeaseDAO(env.database).current()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_new_gateway_preserves_valid_existing_owner_stream(gateway_api):
    env = gateway_api
    session = create(env).json()
    entry = env.backend.entries[session["session_id"]]
    other = None
    try:
        with env.clients["alice"].websocket_connect(
            ws_url(session), headers={"Origin": ORIGIN}
        ) as websocket:
            assert websocket.receive_bytes() == b"RFB 003.008\n"
            other = ManualGateway(env.gateway.service, env.gateway.state_root)
            assert entry.live
            assert not any(event in env.backend.events for event in ("release", "stop"))
            entry.peer.sendall(b"still-owned-pixels")
            assert websocket.receive_bytes() == b"still-owned-pixels"
    finally:
        if other is not None:
            other.shutdown()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_factory_configuration_and_confinement_evidence(gateway_api, monkeypatch):
    env = gateway_api
    descriptor = env.descriptors[env.profile_a.profile_id]
    spec = env.backend.configuration["confinement_for"](descriptor)
    assert spec.purpose is ConfinementPurpose.VNC_HELPER
    assert str(spec.private_tmp_path) == descriptor.tmp_path
    assert spec.profile_path == env.manager.data_dir / env.profile_a.name
    assert spec.private_tmp_path not in spec.runtime_paths
    assert env.gateway.state_root.parent == env.database.path.parent
    assert env.gateway.state_root.stat().st_mode & 0o777 == 0o700
    monkeypatch.delenv("FOXPROFILE_X11VNC_SHA256")
    with pytest.raises(VNCBackendUnavailable, match="FOXPROFILE_X11VNC_SHA256"):
        build_manual_gateway(env.database, env.catalog, env.auth, env.protected, env.coordinator)
    monkeypatch.setenv("FOXPROFILE_X11VNC_SHA256", "0" * 64)
    with pytest.raises(VNCBackendUnavailable, match="verify"):
        build_manual_gateway(env.database, env.catalog, env.auth, env.protected, env.coordinator)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_missing_native_configuration_is_actionable_and_does_not_break_auth(gateway_api):
    env = gateway_api

    class Unconfigured:
        auth_service = env.auth
        protected_application = env.protected

        @property
        def manual_gateway(self):
            raise VNCBackendUnavailable("Configure FOXPROFILE_X11VNC_SHA256 for pinned x11vnc.")

    env.app.state.container = Unconfigured()
    assert env.clients["alice"].get("/api/v1/ext/auth/me").status_code == 200
    assert create(env).status_code == 503
    assert "FOXPROFILE_X11VNC_SHA256" in create(env).text
    assert create(env, "bob").status_code == 404


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_container_lazily_composes_one_gateway_and_shuts_it_before_runtime(
    gateway_api, monkeypatch
):
    from foxprofile_ext.manual import factory

    env = gateway_api
    monkeypatch.setenv("FOXPROFILE_EXT_DB", str(env.database.path))
    built, stopped = [], []

    def build(*args):
        gateway = build_manual_gateway(*args, backend_factory=env.backend_factory)
        shutdown = gateway.shutdown

        def stop_manual():
            stopped.append("manual")
            shutdown()

        gateway.shutdown = stop_manual
        built.append(gateway)
        return gateway

    monkeypatch.setattr(factory, "build_manual_gateway", build)
    env.coordinator.shutdown = lambda **kwargs: stopped.append(("runtime", kwargs))
    container = Container(run_coordinator=env.coordinator)
    env.app.state.container = container
    try:
        assert env.clients["alice"].get("/api/v1/ext/auth/me").status_code == 200
        assert not built
        with ThreadPoolExecutor(max_workers=4) as pool:
            gateways = list(pool.map(lambda _: container.manual_gateway, range(8)))
        assert len(built) == 1
        assert all(gateway is built[0] for gateway in gateways)
        response = create(env)
        assert response.status_code == 200, response.text
        entry = built[0].service._backend.entries[response.json()["session_id"]]
        assert entry.live
    finally:
        container.shutdown(timeout=4)
    assert not entry.live
    assert stopped == ["manual", ("runtime", {"timeout": 4})]


def test_legacy_app_does_not_mount_manual_routes(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "false")
    monkeypatch.setattr(api_auth, "API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(api_app, "API_TOKEN", SERVICE_TOKEN)
    app = api_app.create_app(SimpleNamespace())
    assert not any("manual-sessions" in getattr(route, "path", "") for route in app.routes)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/ext/manual-sessions/guessed").status_code == 404


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bubblewrap/fcntl gateway")
def test_native_rfb_handshake_through_authenticated_websocket(gateway_api, native, monkeypatch):
    env = gateway_api
    env.gateway.shutdown()
    descriptor = replace(native.runtime, run=env.descriptors[env.profile_a.profile_id].run)
    env.descriptors[env.profile_a.profile_id] = descriptor
    monkeypatch.setenv("FOXPROFILE_X11VNC_EXECUTABLE", native.binaries["x11vnc"])
    # Explicit Debian amd64 0.9.17-1 developer binary, never an inferred production pin.
    monkeypatch.setenv(
        "FOXPROFILE_X11VNC_SHA256",
        "249bdc17e09fd96ea73ca7fbec2d8f3cd9ff411d0bbfb634d99431548fb77132",
    )
    libraries = tuple(
        Path(value) for value in os.getenv("FOXPROFILE_NATIVE_LIBRARY_PATH", "").split(":") if value
    )
    gateway = build_manual_gateway(
        env.database, env.catalog, env.auth, env.protected, env.coordinator, library_paths=libraries
    )
    env.container.manual_gateway = gateway
    before = env.ledger.active_runs()
    try:
        spec = gateway.service._backend._confinement_for(descriptor)
        command, _ = build_confined_command(spec, [native.binaries["x11vnc"]])
        assert str(spec.profile_path) not in command
        response = create(env)
        assert response.status_code == 200, response.text
        session = response.json()
        with env.clients["alice"].websocket_connect(
            ws_url(session), headers={"Origin": ORIGIN}, subprotocols=["binary"]
        ) as websocket:
            buffered = bytearray()

            def read_exact(count):
                while len(buffered) < count:
                    buffered.extend(websocket.receive_bytes())
                result = bytes(buffered[:count])
                del buffered[:count]
                return result

            assert websocket.accepted_subprotocol == "binary"
            assert read_exact(12) == b"RFB 003.008\n"
            websocket.send_bytes(b"RFB 003.008\n")
            assert read_exact(read_exact(1)[0]) == b"\x01"
            websocket.send_bytes(b"\x01")
            assert read_exact(4) == b"\x00" * 4
            websocket.send_bytes(b"\x01")
            header = read_exact(24)
            assert struct.unpack("!HH", header[:4]) == (640, 480)
            assert read_exact(struct.unpack("!I", header[20:])[0]) == b"FoxProfile"
        assert (
            gateway.service.status(env.issued["alice"].principal, session["session_id"]).state.value
            == "closed"
        )
        assert env.ledger.active_runs() == before
        assert native.process.poll() is None
    finally:
        gateway.shutdown()
