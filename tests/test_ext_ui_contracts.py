"""UI contracts exercised through real personal sessions, catalog and SQLite."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from foxprofile_ext.auth import hash_password
from foxprofile_ext.dto import ProcessIdentity, Role
from foxprofile_ext.errors import DomainError, ErrorCode, ProfileCapacityExceeded
from foxprofile_ext.runtime.coordinator import RuntimeCoordinator
from foxprofile_ext.storage.capacity import CapacityLedger, ProcessLiveness
from foxprofile_ext.storage.dao import AssignmentDAO, UserDAO
from foxprofile_ext.storage.database import ExtensionDatabase
from src.api import app as api_app
from src.api import auth as api_auth
from src.core.container import Container
from src.services.profile import manager as manager_module

ORIGIN = "https://panel.example.test"
PASSWORD = "correct horse battery staple"
SERVICE_TOKEN = "trusted-service-token-that-is-long-enough"


class StopCoordinator:
    def __init__(self):
        self.calls = []
        self.results = {}

    def stop(self, actor, ref, *, timeout=30):
        self.calls.append(ref.profile_id)
        result = self.results.get(ref.profile_id, True)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def ui_api(monkeypatch, tmp_path):
    from foxprofile_ext import runtime_factory

    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.setenv("FOXPROFILE_EXT_DB", str(tmp_path / "extension.sqlite3"))
    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "browserdata")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(api_auth, "API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(api_app, "API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(runtime_factory, "_installed_executable", lambda _engine: None)
    database = ExtensionDatabase(tmp_path / "extension.sqlite3")
    users = UserDAO(database)
    users.bootstrap_admin("admin-id", "admin", hash_password(PASSWORD))
    users.create("alice-id", "alice", hash_password(PASSWORD))
    users.create("bob-id", "bob", hash_password(PASSWORD))
    container = Container()
    application = container.protected_application
    actors = {
        username: container.auth_service.login(username, PASSWORD).principal
        for username in ("admin", "alice", "bob")
    }
    own = application.create_profile(
        actors["alice"],
        name="Own",
        proxy="http://user:pass@192.0.2.10:8080",
        timezone="UTC",
        locale="en-US",
    )
    second = application.create_profile(actors["alice"], name="Second", engine="chrome")
    foreign = application.create_profile(
        actors["bob"], name="Foreign", proxy="http://foreign:secret@192.0.2.20:8080"
    )
    original_coordinator = application._run_coordinator
    coordinator = StopCoordinator()
    application._run_coordinator = coordinator
    client = TestClient(api_app.create_app(container), base_url=ORIGIN)
    yield SimpleNamespace(
        client=client,
        container=container,
        app=application,
        database=database,
        users=users,
        actors=actors,
        own=own,
        second=second,
        foreign=foreign,
        coordinator=coordinator,
        ledger=CapacityLedger(database),
        assignments=AssignmentDAO(database),
        data_dir=tmp_path / "browserdata",
    )
    application._run_coordinator = original_coordinator
    container.shutdown(timeout=0)


def login(api, username):
    response = api.client.post(
        "/api/v1/ext/auth/login",
        json={"username": username, "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


@pytest.mark.parametrize("origin", ["https://[", "https://]"])
def test_malformed_origin_login_mutation_is_forbidden_before_session_issue(monkeypatch, origin):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    container = SimpleNamespace(
        auth_service=SimpleNamespace(login=lambda *_: pytest.fail("session issue reached"))
    )
    client = TestClient(api_app.create_app(container), base_url=ORIGIN)
    response = client.post(
        "/api/v1/ext/auth/login",
        json={"username": "alice", "password": PASSWORD},
        headers={"Origin": origin},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Origin is not allowed"}
    assert "set-cookie" not in response.headers


def test_assignments_read_requires_current_personal_admin_and_existing_user(ui_api):
    api = ui_api
    for username in ("alice", "bob"):
        login(api, username)
        assert (
            api.client.get("/api/v1/ext/admin/users/alice-id/profile-assignments").status_code
            == 403
        )
    service = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    assert (
        api.client.get(
            "/api/v1/ext/admin/users/alice-id/profile-assignments", headers=service
        ).status_code
        == 403
    )
    headers = login(api, "admin")
    url = "/api/v1/ext/admin/users/alice-id/profile-assignments"
    assert api.client.get(url).json() == {
        "profile_ids": sorted([api.own.profile_id, api.second.profile_id])
    }
    assert api.client.get("/api/v1/ext/admin/users/missing/profile-assignments").status_code == 404
    # Read the current selection, then replace explicitly without erasing another grant.
    selected = api.client.get(url).json()["profile_ids"]
    assert api.client.put(url, json={"profile_ids": selected}, headers=headers).status_code == 200
    stale_admin = api.actors["admin"]
    api.container.auth_service.create_user(stale_admin, "other-admin", PASSWORD, Role.ADMIN)
    api.container.auth_service.update_user_state(stale_admin, "admin-id", role=Role.USER)
    with pytest.raises(DomainError) as denied:
        api.app.assignments_for_user(stale_admin, "alice-id")
    assert denied.value.code is ErrorCode.AUTH_REQUIRED


@pytest.mark.parametrize("operation", ["stop", "delete"])
def test_batch_denies_foreign_or_missing_target_before_any_effect(ui_api, operation, monkeypatch):
    api = ui_api
    headers = login(api, "alice")
    deleted = []
    monkeypatch.setattr(api.app._catalog, "delete", lambda *args: deleted.append(args))
    url = f"/api/v1/ext/{'browser/stop' if operation == 'stop' else 'profiles/delete'}-batch"
    for denied in (api.foreign.profile_id, "Foreign", "missing"):
        response = api.client.post(
            url, json={"profile_ids": [api.own.profile_id, denied]}, headers=headers
        )
        assert response.status_code == 404
    assert api.coordinator.calls == []
    assert deleted == []


@pytest.mark.parametrize("operation", ["stop", "delete"])
def test_batch_csrf_and_strict_identifiers_precede_effects(ui_api, operation):
    api = ui_api
    headers = login(api, "alice")
    url = f"/api/v1/ext/{'browser/stop' if operation == 'stop' else 'profiles/delete'}-batch"
    assert api.client.post(url, json={"profile_ids": [api.own.profile_id]}).status_code == 403
    for values in ([], [""], ["  "], [1], [True], [None], "Own"):
        assert (
            api.client.post(url, json={"profile_ids": values}, headers=headers).status_code == 422
        )
    assert api.coordinator.calls == []
    assert api.app.resolve_profile(api.actors["alice"], api.own.profile_id)


def test_stop_batch_deduplicates_and_reports_actual_per_target_results(ui_api):
    api = ui_api
    headers = login(api, "alice")
    api.coordinator.results[api.second.profile_id] = RuntimeError("private path /private/run/token")
    response = api.client.post(
        "/api/v1/ext/browser/stop-batch",
        json={"profile_ids": [api.own.profile_id, "Own", api.second.profile_id]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json() == {
        "profiles": [
            {"profile_id": api.own.profile_id, "name": "Own", "stopped": True, "error_code": None},
            {
                "profile_id": api.second.profile_id,
                "name": "Second",
                "stopped": False,
                "error_code": "RUN_STOP_FAILED",
            },
        ],
        "count": 2,
    }
    assert api.coordinator.calls == [api.own.profile_id, api.second.profile_id]
    assert "/private" not in response.text


def test_delete_batch_preflights_every_busy_target_then_deduplicates(ui_api):
    api = ui_api
    headers = login(api, "alice")
    run = api.ledger.reserve((api.second.profile_id,), "test-host").runs[0]
    body = {"profile_ids": [api.own.profile_id, "Own", api.second.profile_id]}
    denied = api.client.post("/api/v1/ext/profiles/delete-batch", json=body, headers=headers)
    assert denied.status_code == 409
    assert api.app.resolve_profile(api.actors["alice"], "Own")
    assert api.ledger.cancel_unclaimed(run.ref)
    response = api.client.post("/api/v1/ext/profiles/delete-batch", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "profiles": [
            {"profile_id": api.own.profile_id, "name": "Own", "deleted": True, "error_code": None},
            {
                "profile_id": api.second.profile_id,
                "name": "Second",
                "deleted": True,
                "error_code": None,
            },
        ],
        "count": 2,
    }
    assert not api.assignments.profiles_for_user("alice-id")
    assert api.app.resolve_profile(api.actors["bob"], "Foreign")


def test_deleted_batch_failure_does_not_claim_rollback_or_expose_private_error(ui_api, monkeypatch):
    api = ui_api
    headers = login(api, "alice")
    original = api.app._catalog.delete

    def fail_second(actor_id, profile_id):
        if profile_id == api.second.profile_id:
            raise OSError("private path /private/catalog")
        return original(actor_id, profile_id)

    monkeypatch.setattr(api.app._catalog, "delete", fail_second)
    response = api.client.post(
        "/api/v1/ext/profiles/delete-batch",
        json={"profile_ids": [api.own.profile_id, api.second.profile_id]},
        headers=headers,
    )
    assert response.status_code == 200
    results = response.json()["profiles"]
    assert results[0]["deleted"] is True
    assert results[1]["deleted"] is False
    assert results[1]["error_code"] == "PROFILE_DELETE_FAILED"
    assert "/private" not in response.text
    assert api.app.resolve_profile(api.actors["alice"], "Second")


def test_summary_masks_proxy_preserves_native_metadata_and_uses_one_snapshot(ui_api, monkeypatch):
    api = ui_api
    login(api, "alice")
    own_dir = api.data_dir / "Own"
    own_dir.mkdir(exist_ok=True)
    (own_dir / "fingerprint.json").write_text(
        '{"os":"linux","fingerprint":{"navigator":{"platform":"Linux x86_64",'
        '"hardwareConcurrency":4,"private":"secret"},"screen":{"width":1366,"height":768}}}'
    )
    api.ledger.reserve((api.foreign.profile_id,), "test-host")
    calls = []
    original_states = api.app._capacity.lifecycle_states

    def states():
        calls.append("states")
        return original_states()

    monkeypatch.setattr(api.app._capacity, "lifecycle_states", states)
    with monkeypatch.context() as listing:
        listing.setattr(api.app, "running_profiles", lambda _: pytest.fail("second roster read"))
        response = api.client.get("/api/v1/profiles")
    assert response.status_code == 200
    assert calls == ["states"]
    data = response.json()
    assert data["total"] == 2
    own = next(row for row in data["profiles"] if row["name"] == "Own")
    assert own == {
        "profile_id": api.own.profile_id,
        "name": "Own",
        "os_type": "linux",
        "engine": "camoufox",
        "proxy": "http://192.0.2.10:8080",
        "timezone": "UTC",
        "locale": "en-US",
        "state": "idle",
        "is_running": False,
        "fingerprint": {
            "exists": True,
            "os": "linux",
            "platform": "Linux x86_64",
            "screen": "1366x768",
            "hardware_concurrency": 4,
        },
    }
    chrome = next(row for row in data["profiles"] if row["name"] == "Second")
    assert chrome["os_type"] == "windows"
    assert chrome["engine"] == "chrome"
    for private in (
        "pass",
        "user:",
        "Foreign",
        "secret",
        "data_dir",
        "192.0.2.20",
        str(api.data_dir),
    ):
        assert private not in response.text
    detail = api.client.get(f"/api/v1/profiles/{api.own.profile_id}").json()
    assert detail["proxy"] == "http://user:pass@192.0.2.10:8080"


@pytest.mark.parametrize("state", ["reserved", "starting", "running", "stopping", "recovering"])
def test_summary_states_are_authoritative_durable_lifecycle(ui_api, state):
    api = ui_api
    login(api, "alice")
    run = api.ledger.reserve((api.own.profile_id,), "test-host").runs[0]
    if state != "reserved":
        assert api.ledger.claim(run.ref, "test-host", ProcessIdentity(2_000_000_000, 1))
    if state == "running":
        assert api.ledger.mark_running(run.ref)
    if state == "stopping":
        assert api.ledger.mark_stopping(run.ref)
    if state == "recovering":
        api.ledger.recover(lambda _host, _identity: ProcessLiveness.UNKNOWN)
    rows = api.client.get("/api/v1/profiles").json()["profiles"]
    own = next(row for row in rows if row["profile_id"] == api.own.profile_id)
    assert own["state"] == state
    assert own["is_running"] is True


def test_summary_reads_only_visible_fingerprints_and_filters_private_or_wrong_types(
    ui_api, monkeypatch
):
    api = ui_api
    login(api, "alice")
    reads = []

    def summary(profile_dir):
        reads.append(profile_dir)
        return {
            "os": ["private-os"],
            "platform": {"token": "private-token"},
            "screen": "1366x768",
            "hardware_concurrency": True,
            "data_dir": "private-path",
        }

    monkeypatch.setattr("foxprofile_ext.application.fingerprint.summary", summary)
    response = api.client.get("/api/v1/profiles")
    assert response.status_code == 200
    assert {str(path) for path in reads} == {
        str(api.data_dir / "Own"),
        str(api.data_dir / "Second"),
    }
    for row in response.json()["profiles"]:
        assert row["fingerprint"] == {
            "exists": True,
            "os": None,
            "platform": None,
            "screen": "1366x768",
            "hardware_concurrency": None,
        }
    assert "private-" not in response.text


def test_profile_creation_keeps_explicit_os_and_ordinary_has_no_occupancy_roster(ui_api):
    api = ui_api
    headers = login(api, "alice")
    created = api.client.post(
        "/api/v1/profiles", json={"name": "Explicit", "os_type": "macos"}, headers=headers
    )
    assert created.status_code == 201
    assert created.json()["os_type"] == "macos"
    api.ledger.reserve((api.foreign.profile_id,), "test-host")
    assert api.client.get("/api/v1/ext/admin/settings").status_code == 403
    running = api.client.get("/api/v1/browser")
    assert running.status_code == 200
    assert "Foreign" not in running.text
    assert api.foreign.profile_id not in running.text


def test_capacity_error_exposes_only_typed_public_integer(ui_api, monkeypatch):
    api = ui_api
    headers = login(api, "alice")

    def full(*args, **kwargs):
        raise ProfileCapacityExceeded(3)

    monkeypatch.setattr(api.app, "start_profiles", full)
    response = api.client.post(
        "/api/v1/ext/browser/start-batch",
        json={"profile_ids": [api.own.profile_id]},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "PROFILE_CAPACITY_EXCEEDED",
        "message": ProfileCapacityExceeded(3).message,
        "max_concurrent_profiles": 3,
    }
    assert "occupied" not in response.text
    assert "Foreign" not in response.text


def test_batch_live_session_and_grant_rechecks_before_effects(ui_api):
    api = ui_api
    api.assignments.revoke("alice-id", api.own.profile_id)
    for method in (api.app.stop_runs, api.app.delete_profiles):
        with pytest.raises(DomainError) as denied:
            method(api.actors["alice"], [api.second.profile_id, api.own.profile_id])
        assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    api.container.auth_service.logout(api.actors["alice"])
    for method in (api.app.stop_runs, api.app.delete_profiles):
        with pytest.raises(DomainError) as denied:
            method(api.actors["alice"], [api.second.profile_id])
        assert denied.value.code is ErrorCode.AUTH_REQUIRED
    assert api.coordinator.calls == []


def test_coordinator_stop_wait_does_not_hold_catalog_lock(ui_api):
    api = ui_api
    run = api.ledger.reserve((api.own.profile_id,), "test-host").runs[0]
    assert api.ledger.claim(run.ref, "test-host", ProcessIdentity(2_000_000_000, 1))
    entered, release = Event(), Event()

    def request_stop(profile_id, run_id, timeout):
        assert (profile_id, run_id) == (api.own.profile_id, run.ref.run_id)
        entered.set()
        assert release.wait(3)
        return True

    coordinator = RuntimeCoordinator(
        None,
        api.ledger,
        SimpleNamespace(host_id="test-host", request_stop=request_stop),
        None,
        profile_by_id=api.app._catalog.get_profile,
        authorize=lambda actor, ref: api.app.current_principal(actor),
        lock_path=str(api.app._catalog.lock_path),
    )
    api.app._run_coordinator = coordinator
    with ThreadPoolExecutor(max_workers=2) as pool:
        stopped = pool.submit(api.app.stop_runs, api.actors["alice"], [api.own.profile_id])
        try:
            assert entered.wait(3)
            listed = pool.submit(api.app.list_profiles, api.actors["bob"])
            assert [row.name for row in listed.result(timeout=1)] == ["Foreign"]
        finally:
            release.set()
        assert stopped.result(timeout=3)[0].stopped is True


def test_coordinator_stop_handles_claim_winning_reserved_cancellation(ui_api, monkeypatch):
    api = ui_api
    run = api.ledger.reserve((api.own.profile_id,), "test-host").runs[0]
    cancel = api.ledger.cancel_unclaimed
    stopped = []

    def claim_before_cancel(ref):
        assert api.ledger.claim(ref, "test-host", ProcessIdentity(2_000_000_000, 1))
        return cancel(ref)

    def request_stop(profile_id, run_id, timeout):
        stopped.append((profile_id, run_id, timeout))
        return True

    monkeypatch.setattr(api.ledger, "cancel_unclaimed", claim_before_cancel)
    api.app._run_coordinator = RuntimeCoordinator(
        None,
        api.ledger,
        SimpleNamespace(host_id="test-host", request_stop=request_stop),
        None,
        profile_by_id=api.app._catalog.get_profile,
        authorize=lambda actor, ref: api.app.current_principal(actor),
        lock_path=str(api.app._catalog.lock_path),
    )
    assert api.app.stop_runs(api.actors["alice"], [api.own.profile_id], timeout=2)[0].stopped
    assert stopped == [(api.own.profile_id, run.ref.run_id, 2)]
