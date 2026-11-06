"""Protected profile facade checks over isolated extension/catalog fixtures."""

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

from foxprofile_ext.application import ProtectedApplication
from foxprofile_ext.auth import AuthService, hash_password
from foxprofile_ext.catalog import CatalogOperationError, ProfileCatalog
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.request_context import current_principal as current_request_principal
from foxprofile_ext.run_contracts import RunLaunchResult, RunStatusResult
from foxprofile_ext.storage import (
    AssignmentDAO,
    CapacityLedger,
    CatalogOperationDAO,
    ExtensionDatabase,
    SessionDAO,
    UserDAO,
)
from src.core.container import Container
from src.services.profile import manager as manager_module
from src.services.profile.manager import ProfileManager


class _Coordinator:
    def __init__(self):
        self.calls = []
        self.shutdown_timeouts = []
        self.effect_calls = []

    def reserve_and_start(self, principal, profiles, *, wait=False, timeout=30):
        self.calls.append((principal, tuple(profiles), wait, timeout))
        return tuple(
            RunLaunchResult(
                profile.profile_id,
                profile.name,
                f"run-{profile.profile_id}",
                "starting",
            )
            for profile in profiles
        )

    def shutdown(self, *, timeout=30.0):
        self.shutdown_timeouts.append(timeout)

    def status(self, principal, profile):
        self.effect_calls.append(("status", principal, profile))
        return RunStatusResult(
            profile.profile_id, profile.name, f"run-{profile.profile_id}", "running"
        )

    def stop(self, principal, profile, *, timeout=30.0):
        self.effect_calls.append(("stop", principal, profile, timeout))
        return True

    def control(self, principal, profile, action, params=None):
        self.effect_calls.append(("control", principal, profile, action, params))
        return {"ok": True}


@pytest.fixture
def access(tmp_path, monkeypatch):
    data_dir = tmp_path / "browserdata"
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(manager_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(manager_module, "PROFILES_FILE", profiles_file)
    manager = ProfileManager()
    database = ExtensionDatabase(tmp_path / "extension.sqlite3")
    users = UserDAO(database)
    users.bootstrap_admin("admin-0", "admin", hash_password("admin password"))
    users.create("user-a", "alice", hash_password("alice password"))
    users.create("user-b", "bob", hash_password("bob password"))
    assignments = AssignmentDAO(database)
    catalog = ProfileCatalog(
        manager,
        CatalogOperationDAO(database),
        assignments,
        tmp_path / "catalog.lock",
    )
    catalog.create("user-a", name="Profile A", proxy="http://a:secret@192.0.2.1:8080")
    catalog.create("user-b", name="Profile B", proxy="http://b:secret@192.0.2.2:8080")
    catalog.create("admin-0", name="Unassigned", proxy="http://admin:secret@192.0.2.3:8080")

    def profile_exists(profile_id):
        try:
            catalog.get_profile(profile_id)
        except CatalogOperationError:
            return False
        return True

    auth = AuthService(UserDAO(database), SessionDAO(database), assignments, profile_exists)
    admin = auth.login("admin", "admin password").principal
    user_a = auth.login("alice", "alice password").principal
    user_b = auth.login("bob", "bob password").principal
    coordinator = _Coordinator()
    app = ProtectedApplication(
        auth,
        catalog,
        assignments,
        CapacityLedger(database),
        coordinator,
    )
    return {
        "app": app,
        "auth": auth,
        "catalog": catalog,
        "assignments": assignments,
        "ledger": CapacityLedger(database),
        "admin": admin,
        "user_a": user_a,
        "user_b": user_b,
        "service": PrincipalDTO("trusted-service", PrincipalKind.SERVICE),
        "coordinator": coordinator,
    }


def _profiles(access):
    catalog = access["catalog"]
    return {profile.name: profile for profile in catalog.list_profiles()}


def test_missing_actor_denied_at_direct_facade_boundary(access):
    app = access["app"]
    reached = []

    calls = (
        lambda: app.current_principal(None),
        lambda: app.list_profiles(None),
        lambda: app.resolve_profile(None, "Profile A"),
        lambda: app.perform_profile(None, "Profile A", lambda profile: reached.append(profile)),
        lambda: app.start_profile(None, "Profile A"),
        lambda: app.run_status(None, "Profile A"),
        lambda: app.stop_run(None, "Profile A"),
        lambda: app.control_run(None, "Profile A", "click"),
        lambda: app.create_profile(None, name="No actor"),
    )
    for call in calls:
        with pytest.raises(DomainError) as denied:
            call()
        assert denied.value.code is ErrorCode.AUTH_REQUIRED
    assert reached == []
    assert access["coordinator"].calls == []
    assert access["coordinator"].effect_calls == []
    assert "No actor" not in {profile.name for profile in app.list_profiles(access["admin"])}


def test_perform_profile_scopes_and_resets_principal_after_success_or_error(access):
    app = access["app"]
    with pytest.raises(DomainError) as absent:
        current_request_principal()
    assert absent.value.code is ErrorCode.AUTH_REQUIRED

    actor = app.perform_profile(
        access["user_a"], "Profile A", lambda _profile: current_request_principal()
    )
    assert actor == access["user_a"]
    with pytest.raises(DomainError) as after_success:
        current_request_principal()
    assert after_success.value.code is ErrorCode.AUTH_REQUIRED

    def fail(_profile):
        raise RuntimeError("adapter failure")

    with pytest.raises(RuntimeError, match="adapter failure"):
        app.perform_profile(access["user_a"], "Profile A", fail)
    with pytest.raises(DomainError) as after_error:
        current_request_principal()
    assert after_error.value.code is ErrorCode.AUTH_REQUIRED


def test_personal_admin_service_profile_and_running_views_are_filtered(access):
    app = access["app"]
    profiles = _profiles(access)
    access["ledger"].reserve(
        (profiles["Profile A"].profile_id, profiles["Profile B"].profile_id),
        "test-host",
    )

    assert [row.name for row in app.list_profiles(access["user_a"])] == ["Profile A"]
    assert [row.name for row in app.list_profiles(access["user_b"])] == ["Profile B"]
    assert {row.name for row in app.list_profiles(access["admin"])} == {
        "Profile A",
        "Profile B",
        "Unassigned",
    }
    assert {row.name for row in app.list_profiles(access["service"])} == {
        "Profile A",
        "Profile B",
        "Unassigned",
    }
    assert [row.name for row in app.running_profiles(access["user_a"])] == ["Profile A"]
    assert [row.name for row in app.running_profiles(access["user_b"])] == ["Profile B"]
    assert {row.name for row in app.running_profiles(access["admin"])} == {
        "Profile A",
        "Profile B",
    }
    assert {row.name for row in app.running_profiles(access["service"])} == {
        "Profile A",
        "Profile B",
    }
    assert app.profile_count(access["user_a"]) == 1
    assert app.running_count(access["user_b"]) == 1


def test_visible_profile_serialization_has_only_redacted_fields(access):
    visible = access["app"].list_profiles(access["user_a"])
    serialized = [asdict(profile) for profile in visible]
    content = json.dumps(serialized)

    assert serialized == [{"profile_id": visible[0].profile_id, "name": "Profile A"}]
    assert set(serialized[0]) == {"profile_id", "name"}
    assert "secret" not in content
    assert "192.0.2." not in content
    assert "data_dir" not in content
    assert "Profile B" not in content


def test_current_profile_resolution_hides_foreign_and_stale_resources(access):
    app = access["app"]
    profiles = _profiles(access)
    authorized = app.resolve_profile(access["user_a"], "Profile A")
    assert authorized.profile_id == profiles["Profile A"].profile_id
    assert app.resolve_profile(access["user_a"], authorized.profile_id) == authorized

    errors = []
    for identifier in ("Profile B", profiles["Profile B"].profile_id, "missing"):
        with pytest.raises(DomainError) as denied:
            app.resolve_profile(access["user_a"], identifier)
        errors.append(denied.value)
    assert {error.code for error in errors} == {ErrorCode.FORBIDDEN_OR_NOT_FOUND}
    assert len({str(error) for error in errors}) == 1

    access["catalog"].rename("user-a", authorized.profile_id, "Renamed A")
    assert app.resolve_profile(access["user_a"], authorized.profile_id).name == "Renamed A"
    with pytest.raises(DomainError) as stale_name:
        app.resolve_profile(access["user_a"], "Profile A")
    assert stale_name.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND


def test_revoked_or_disabled_personal_context_fails_on_next_interaction(access):
    app = access["app"]
    admin = access["admin"]
    stale_a = access["user_a"]
    access["auth"].reset_password(admin, "user-a", "new alice password")
    with pytest.raises(DomainError) as reset:
        app.list_profiles(stale_a)
    assert reset.value.code is ErrorCode.AUTH_REQUIRED

    current_admin = access["auth"].login("admin", "admin password").principal
    stale_b = access["user_b"]
    access["auth"].update_user_state(current_admin, "user-b", active=False)
    with pytest.raises(DomainError) as disabled:
        app.resolve_profile(stale_b, "Profile B")
    assert disabled.value.code is ErrorCode.AUTH_REQUIRED


def test_mixed_batch_is_denied_before_coordinator_and_success_deduplicates(access):
    app = access["app"]
    coordinator = access["coordinator"]
    with pytest.raises(DomainError) as denied:
        app.start_profiles(access["user_a"], ("Profile A", "Profile B"))
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert coordinator.calls == []

    results = app.start_profiles(
        access["user_a"], ("Profile A", _profiles(access)["Profile A"].profile_id)
    )
    assert len(results) == 1
    assert results[0].profile_name == "Profile A"
    assert len(coordinator.calls) == 1
    assert len(coordinator.calls[0][1]) == 1


def test_readiness_wait_does_not_block_unrelated_catalog_operations(access):
    waiting = threading.Event()
    release = threading.Event()
    coordinator = access["coordinator"]
    original_start = coordinator.reserve_and_start

    def blocking_start(principal, profiles, *, wait=False, timeout=30):
        results = original_start(principal, profiles, wait=wait, timeout=timeout)
        waiting.set()
        assert release.wait(5), "test did not release readiness wait"
        return results

    coordinator.reserve_and_start = blocking_start
    with ThreadPoolExecutor(max_workers=2) as executor:
        launch = executor.submit(
            access["app"].start_profile, access["user_a"], "Profile A", wait=True
        )
        try:
            assert waiting.wait(5), "launch did not reach readiness wait"
            unrelated = executor.submit(
                access["app"].create_profile, access["user_b"], name="Concurrent B"
            )
            assert unrelated.result(timeout=2).name == "Concurrent B"
            assert not launch.done()
        finally:
            release.set()
        assert launch.result(timeout=2).profile_name == "Profile A"


def test_pending_profile_is_hidden_from_every_facade_path_without_effects(access):
    app = access["app"]
    profile = _profiles(access)["Profile A"]
    access["catalog"].operations.begin("pending-a", "rename", profile.profile_id, 1)
    reached = []

    assert app.list_profiles(access["user_a"]) == ()
    access["ledger"].reserve((profile.profile_id,), "test-host")
    assert app.running_profiles(access["user_a"]) == ()
    for identifier in (profile.profile_id, profile.name):
        calls = (
            lambda identifier=identifier: app.resolve_profile(access["user_a"], identifier),
            lambda identifier=identifier: app.perform_profile(
                access["user_a"], identifier, lambda item: reached.append(item)
            ),
            lambda identifier=identifier: app.update_profile(
                access["user_a"], identifier, {"proxy": ""}
            ),
            lambda identifier=identifier: app.delete_profile(access["user_a"], identifier),
            lambda identifier=identifier: app.start_profiles(
                access["admin"], ("Profile B", identifier)
            ),
            lambda identifier=identifier: app.run_status(access["user_a"], identifier),
            lambda identifier=identifier: app.stop_run(access["user_a"], identifier),
            lambda identifier=identifier: app.control_run(access["user_a"], identifier, "click"),
        )
        for call in calls:
            with pytest.raises(DomainError) as denied:
                call()
            assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert reached == []
    assert access["coordinator"].calls == []
    assert access["coordinator"].effect_calls == []


def test_start_rejects_runtime_result_bound_to_another_profile_name(access):
    profile = _profiles(access)["Profile A"]
    access["coordinator"].reserve_and_start = lambda *args, **kwargs: (
        RunLaunchResult(profile.profile_id, "Foreign profile", "run-a", "starting"),
    )

    with pytest.raises(DomainError) as invalid_runtime:
        access["app"].start_profile(access["user_a"], profile.profile_id)

    assert invalid_runtime.value.code is ErrorCode.RUNTIME_RECOVERING


def test_run_effects_refresh_current_profile_and_deny_foreign_before_coordinator(access):
    app = access["app"]
    coordinator = access["coordinator"]

    with pytest.raises(DomainError) as denied:
        app.run_status(access["user_a"], "Profile B")
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    with pytest.raises(DomainError) as denied:
        app.stop_run(access["user_a"], "Profile B")
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    with pytest.raises(DomainError) as denied:
        app.control_run(access["user_a"], "Profile B", "navigate", {"url": "https://example.test"})
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert coordinator.effect_calls == []

    status = app.run_status(access["user_a"], "Profile A")
    assert status is not None and status.state == "running"
    assert app.stop_run(access["admin"], "Profile A", timeout=3) is True
    assert app.control_run(access["service"], "Profile A", "click", {"x": 1}) == {"ok": True}
    assert [call[0] for call in coordinator.effect_calls] == ["status", "stop", "control"]

    profile_a = _profiles(access)["Profile A"]
    access["assignments"].revoke("user-a", profile_a.profile_id)
    with pytest.raises(DomainError) as revoked:
        app.control_run(access["user_a"], profile_a.profile_id, "click")
    assert revoked.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert [call[0] for call in coordinator.effect_calls] == ["status", "stop", "control"]


def test_profile_creation_defaults_linux_and_assigns_only_ordinary_creator(access):
    app = access["app"]

    created = app.create_profile(access["user_a"], name="New Linux")
    assert created.os_type == "linux"
    assert access["assignments"].is_assigned("user-a", created.profile_id)
    assert not access["assignments"].is_assigned("user-b", created.profile_id)

    created_admin = app.create_profile(access["admin"], name="Admin Windows", os_type="windows")
    created_service = app.create_profile(access["service"], name="Service Chrome", engine="chrome")
    assert created_admin.os_type == "windows"
    assert created_service.os_type == "windows"
    assert created_service.engine == "chrome"
    assert access["assignments"].is_assigned("admin-0", created_admin.profile_id)
    assert not access["assignments"].is_assigned("trusted-service", created_service.profile_id)
    with pytest.raises(DomainError) as invalid:
        app.create_profile(access["user_a"], name="Invalid", os_type="weird")
    assert invalid.value.code is ErrorCode.VALIDATION_ERROR
    assert "Invalid" not in {profile.name for profile in app.list_profiles(access["admin"])}


def test_catalog_profile_edits_keep_identity_grants_and_denials_effect_free(access):
    app = access["app"]
    profile_a = _profiles(access)["Profile A"]
    original_id = profile_a.profile_id

    with pytest.raises(DomainError) as denied:
        app.update_profile(
            access["user_a"],
            "Profile B",
            {"name": "Hijacked"},
        )
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    with pytest.raises(DomainError) as denied:
        app.delete_profile(access["user_a"], "Profile B")
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert "Hijacked" not in {profile.name for profile in app.list_profiles(access["admin"])}

    updated = app.update_profile(access["user_a"], original_id, {"name": "Renamed A"})
    assert updated.profile_id == original_id
    assert updated.proxy == profile_a.proxy
    assert access["assignments"].is_assigned("user-a", original_id)
    assert app.resolve_profile(access["user_a"], original_id).name == "Renamed A"
    with pytest.raises(DomainError) as invalid:
        app.update_profile(access["user_a"], original_id, {"engine": "chrome"})
    assert invalid.value.code is ErrorCode.VALIDATION_ERROR

    access["ledger"].reserve((original_id,), "test-host")
    with pytest.raises(DomainError) as busy:
        app.delete_profile(access["user_a"], original_id)
    assert busy.value.code is ErrorCode.PROFILE_BUSY
    assert app.resolve_profile(access["user_a"], original_id).name == "Renamed A"


def test_start_fails_closed_without_coordinator_after_authorization(access):
    profiles = _profiles(access)
    app = ProtectedApplication(
        access["auth"],
        access["catalog"],
        access["assignments"],
        access["ledger"],
    )
    with pytest.raises(DomainError) as recovering:
        app.start_profile(access["user_a"], "Profile A")
    assert recovering.value.code is ErrorCode.RUNTIME_RECOVERING
    with pytest.raises(DomainError) as denied:
        app.start_profile(access["user_a"], profiles["Profile B"].profile_id)
    assert denied.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
    assert not hasattr(app, "profile_manager")
    assert not hasattr(app, "browser_launcher")

    incompatible = ProtectedApplication(
        access["auth"],
        access["catalog"],
        access["assignments"],
        access["ledger"],
        object(),
    )
    with pytest.raises(DomainError) as invalid_runtime:
        incompatible.start_profile(access["user_a"], "Profile A")
    assert invalid_runtime.value.code is ErrorCode.RUNTIME_RECOVERING
    for call in (
        lambda: incompatible.run_status(access["user_a"], "Profile A"),
        lambda: incompatible.stop_run(access["user_a"], "Profile A"),
        lambda: incompatible.control_run(access["user_a"], "Profile A", "click"),
    ):
        with pytest.raises(DomainError) as recovering:
            call()
        assert recovering.value.code is ErrorCode.RUNTIME_RECOVERING


def test_assignment_replacement_is_admin_only_and_current_profile_scoped(access):
    profile_a = _profiles(access)["Profile A"]
    assert access["app"].replace_assignments(
        access["admin"], "user-b", (profile_a.profile_id,)
    ) == (profile_a.profile_id,)
    assert access["assignments"].is_assigned("user-b", profile_a.profile_id)
    with pytest.raises(DomainError) as forbidden:
        access["app"].replace_assignments(access["user_a"], "user-b", ("stale-id",))
    assert forbidden.value.code is ErrorCode.ADMIN_REQUIRED
    with pytest.raises(DomainError) as unavailable:
        access["app"].replace_assignments(access["admin"], "user-b", ("stale-id",))
    assert unavailable.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND


def test_run_launch_result_has_a_small_explicit_contract():
    result = RunLaunchResult("profile-a", "Profile A", "run-a", "failed", "LAUNCH_FAILED")
    assert asdict(result) == {
        "profile_id": "profile-a",
        "profile_name": "Profile A",
        "browser_run_id": "run-a",
        "state": "failed",
        "error_code": "LAUNCH_FAILED",
    }
    with pytest.raises(DomainError):
        RunLaunchResult("profile-a", "Profile A", "run-a", "failed", "raw launch error")


@pytest.mark.parametrize(
    "state", ["reserved", "starting", "running", "stopping", "cancelled", "closed"]
)
def test_run_status_contract_accepts_every_ledger_state(state):
    assert RunStatusResult("profile-a", "Profile A", "run-a", state).state == state


def test_container_composes_one_auth_service_and_protected_application(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    coordinator = _Coordinator()
    container = Container(run_coordinator=coordinator)

    assert container.auth_service is container.auth_service
    assert container.protected_application is container.protected_application
    with pytest.raises(DomainError) as denied:
        container.protected_application.list_profiles(None)
    assert denied.value.code is ErrorCode.AUTH_REQUIRED
    container.shutdown(timeout=4)
    assert coordinator.shutdown_timeouts == [4]


def test_container_disabled_mode_keeps_upstream_and_skips_extension_state(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", profiles_file)
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "false")
    container = Container()

    assert container.profile_manager is not None
    assert not (tmp_path / "foxprofile_ext.sqlite3").exists()
    with pytest.raises(DomainError) as denied:
        _ = container.protected_application
    assert denied.value.code is ErrorCode.AUTH_REQUIRED


def test_enabled_container_recovers_before_serving_and_blocks_raw_services(tmp_path, monkeypatch):
    from foxprofile_ext.catalog import ProfileCatalog

    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    recovered = []
    monkeypatch.setattr(ProfileCatalog, "recover", lambda self: recovered.append(self))

    container = Container()

    assert len(recovered) == 1
    assert (tmp_path / "foxprofile_ext.sqlite3").exists()
    for service in ("profile_manager", "browser_launcher", "proxy_service"):
        with pytest.raises(DomainError) as denied:
            getattr(container, service)
        assert denied.value.code is ErrorCode.AUTH_REQUIRED
    for getter in ("_get_profile_manager", "_get_browser_launcher", "_get_proxy_service"):
        with pytest.raises(DomainError) as denied:
            getattr(container, getter)()
        assert denied.value.code is ErrorCode.AUTH_REQUIRED
    with pytest.raises(DomainError) as raw_getter:
        container._get("pm", object)
    assert raw_getter.value.code is ErrorCode.AUTH_REQUIRED
    with pytest.raises(DomainError) as unscoped_adapter:
        container.profile_manager_adapter().list_profiles()
    assert unscoped_adapter.value.code is ErrorCode.AUTH_REQUIRED
    container.shutdown()
    assert not hasattr(container, "_instances")


def test_enabled_container_fails_startup_when_catalog_recovery_fails(tmp_path, monkeypatch):
    from foxprofile_ext.catalog import ProfileCatalog

    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")

    def fail_recovery(_catalog):
        raise RuntimeError("catalog recovery failed")

    monkeypatch.setattr(ProfileCatalog, "recover", fail_recovery)
    with pytest.raises(RuntimeError, match="catalog recovery failed"):
        Container()


def test_enabled_container_fails_closed_on_invalid_catalog_migration(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text("{ malformed", encoding="utf-8")
    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", profiles_file)
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")

    with pytest.raises(json.JSONDecodeError):
        Container()


def test_default_container_runs_confined_stub_and_reports_status_without_browser(
    tmp_path, monkeypatch
):
    from foxprofile_ext import runtime_factory
    from foxprofile_ext.runtime import processes
    from src.services.browser.launcher import BrowserLauncher

    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.setenv("FOXPROFILE_EXT_DB", str(tmp_path / "extension.sqlite3"))
    monkeypatch.setattr(runtime_factory, "_installed_executable", lambda _engine: None)
    script = (
        "import time, json, os; import src.models.profile; import foxprofile_ext.request_context; "
        "print('RUNTIME:' + json.dumps({'display': None, 'xauthority_path': None, "
        "'tmp_path': '/tmp', 'namespace': os.readlink('/proc/self/ns/pid')}), flush=True); "
        "print('CONTROL:9001:stub-control-token', flush=True); "
        "print('BROWSER_STARTED', flush=True); time.sleep(5)"
    )
    monkeypatch.setattr(
        processes, "runner_command", lambda _profile: [sys.executable, "-c", script]
    )
    launch_logs = []
    monitor = BrowserLauncher._monitor_process

    def capture_monitor(self, process, name, log, *args, **kwargs):
        return monitor(self, process, name, launch_logs.append, *args, **kwargs)

    monkeypatch.setattr(BrowserLauncher, "_monitor_process", capture_monitor)
    database = ExtensionDatabase(tmp_path / "extension.sqlite3")
    UserDAO(database).bootstrap_admin("admin-id", "admin", hash_password("admin password"))
    container = Container()
    actor = container.auth_service.login("admin", "admin password").principal
    app = container.protected_application
    created = app.create_profile(actor, name="Stub")
    try:
        assert app.run_status(actor, created.profile_id) is None
        launched = app.start_profile(actor, created.profile_id, wait=True, timeout=5)
        assert launched.state == "running", "\n".join(launch_logs)
        status = app.run_status(actor, created.profile_id)
        assert status is not None
        assert status.browser_run_id == launched.browser_run_id
        assert status.state == "running"
    finally:
        container.shutdown(timeout=3)
    deadline = time.monotonic() + 3
    ledger = CapacityLedger(database)
    while ledger.active_runs() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ledger.active_runs() == ()
