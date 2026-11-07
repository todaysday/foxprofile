from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import tempfile
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from foxprofile_ext.auth import AuthService, hash_password
from foxprofile_ext.dto import (
    PrincipalDTO,
    PrincipalKind,
    Role,
)
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.run_contracts import RunLaunchResult
from foxprofile_ext.storage.dao import AssignmentDAO, SessionDAO, UserDAO
from foxprofile_ext.storage.database import ExtensionDatabase
from src.api import app as api_app
from src.api import auth as api_auth
from src.core.container import Container
from src.services.profile import manager as manager_module
from src.utils.proxy_parser import mask_proxy

SERVICE_TOKEN = "trusted-service-token-that-is-long-enough"
PASSWORD = "correct horse battery staple"
ORIGIN = "https://panel.example.test"


class FakeLauncher:
    def __init__(self):
        self.control_calls: list[tuple[str, str]] = []

    def is_running(self, name: str) -> bool:
        return name == "profile-a"

    def running_profile_names(self) -> list[str]:
        return ["profile-a", "profile-b"]

    def control(self, name: str, action: str, params: dict) -> dict:
        self.control_calls.append((name, action))
        return {"png_base64": "eA==", "action": action}


class FakeProtectedApplication:
    def __init__(self, auth_service: AuthService, assignments: AssignmentDAO):
        self.auth_service = auth_service
        self.assignments = assignments
        self.profiles = {
            "profile-a": SimpleNamespace(
                profile_id="id-a",
                name="profile-a",
                proxy="http://user:pass@192.0.2.10:8080",
                os_type="linux",
                timezone="UTC",
                locale="en-US",
                engine="camoufox",
            ),
            "profile-b": SimpleNamespace(
                profile_id="id-b",
                name="profile-b",
                proxy="http://foreign:secret@192.0.2.20:8080",
                os_type="linux",
                timezone="UTC",
                locale="en-US",
                engine="camoufox",
            ),
        }
        self.coordinator_calls = 0
        self.start_effects: list[str] = []

    def current_principal(self, principal: PrincipalDTO) -> PrincipalDTO:
        return self.auth_service.validate_principal(principal)

    def _resolve(self, principal: PrincipalDTO, identifier: str):
        principal = self.current_principal(principal)
        canonical = identifier.startswith("id:")
        profile_id = identifier[3:] if canonical else identifier
        profile = next(
            (
                item
                for item in self.profiles.values()
                if item.profile_id == profile_id or (not canonical and item.name == identifier)
            ),
            None,
        )
        if profile is None or (
            principal.kind is PrincipalKind.PERSONAL
            and principal.role is not Role.ADMIN
            and not self.assignments.is_assigned(principal.subject_id, profile.profile_id)
        ):
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return profile

    def list_profiles(self, principal: PrincipalDTO):
        from foxprofile_ext.application import VisibleProfileDTO

        principal = self.current_principal(principal)
        return tuple(
            VisibleProfileDTO(profile.profile_id, profile.name, proxy=mask_proxy(profile.proxy))
            for profile in self.profiles.values()
            if principal.kind is PrincipalKind.SERVICE
            or principal.role is Role.ADMIN
            or self.assignments.is_assigned(principal.subject_id, profile.profile_id)
        )

    def running_profiles(self, principal: PrincipalDTO):
        from foxprofile_ext.application import RunningProfileDTO

        return tuple(
            RunningProfileDTO(profile.profile_id, profile.name, "running")
            for profile in self.list_profiles(principal)
            if profile.name == "profile-a"
        )

    def perform_profile(self, principal: PrincipalDTO, identifier: str, effect):
        return effect(self._resolve(principal, identifier))

    def replace_assignments(self, principal, user_id, profile_ids):
        principal = self.current_principal(principal)
        for profile_id in profile_ids:
            if not any(item.profile_id == profile_id for item in self.profiles.values()):
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return self.auth_service.replace_assignments(principal, user_id, profile_ids)

    def start_profile(self, principal, name_or_id, *, wait=False, timeout=30):
        return self.start_profiles(principal, (name_or_id,), wait=wait, timeout=timeout)[0]

    def start_profiles(self, principal, names_or_ids, *, wait=False, timeout=30):
        # Resolve every target before recording a coordinator effect.
        profiles = []
        seen = set()
        for identifier in names_or_ids:
            profile = self._resolve(principal, identifier)
            if profile.profile_id not in seen:
                seen.add(profile.profile_id)
                profiles.append(profile)
        self.coordinator_calls += 1
        self.start_effects.extend(profile.profile_id for profile in profiles)
        return tuple(
            RunLaunchResult(
                profile.profile_id, profile.name, f"run-{profile.profile_id}", "starting"
            )
            for profile in profiles
        )

    def control_run(self, principal, identifier, action, params=None):
        profile = self._resolve(principal, identifier)
        return self.launcher.control(profile.name, action, params or {})

    def run_status(self, principal, identifier):
        profile = self._resolve(principal, identifier)
        if profile.name != "profile-a":
            return None
        from foxprofile_ext.run_contracts import RunStatusResult

        return RunStatusResult(
            profile.profile_id, profile.name, f"run-{profile.profile_id}", "running"
        )

    def stop_run(self, principal, identifier, *, timeout=30):
        self._resolve(principal, identifier)
        return True

    def create_profile(self, principal, **fields):
        principal = self.current_principal(principal)
        profile_id = "id-created"
        profile = SimpleNamespace(
            profile_id=profile_id,
            name=fields["name"],
            proxy=fields.get("proxy", ""),
            os_type=fields.get("os_type", "linux"),
            timezone=fields.get("timezone"),
            locale=fields.get("locale"),
            engine=fields.get("engine", "camoufox"),
        )
        self.profiles[profile.name] = profile
        if principal.kind is PrincipalKind.PERSONAL and principal.role is Role.USER:
            self.assignments.grant(principal.subject_id, profile_id)
        return profile


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.setattr(api_auth, "API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(api_app, "API_TOKEN", SERVICE_TOKEN)

    database = ExtensionDatabase(tmp_path / "extension.sqlite3")
    users = UserDAO(database)
    sessions = SessionDAO(database)
    assignments = AssignmentDAO(database)
    users.bootstrap_admin("admin-id", "admin", hash_password(PASSWORD))
    users.create("user-a", "alice", hash_password(PASSWORD), Role.USER)
    users.create("user-b", "bob", hash_password(PASSWORD), Role.USER)
    assignments.grant("user-a", "id-a")
    auth_service = AuthService(
        users,
        sessions,
        assignments,
        lambda profile_id: profile_id in {"id-a", "id-b"},
    )
    protected = FakeProtectedApplication(auth_service, assignments)
    launcher = FakeLauncher()
    protected.launcher = launcher
    container = SimpleNamespace(
        auth_service=auth_service,
        protected_application=protected,
        profile_manager=SimpleNamespace(),
        browser_launcher=launcher,
        proxy_service=SimpleNamespace(),
        event_bus=SimpleNamespace(emit=lambda: None),
        _get_profile_manager=lambda: SimpleNamespace(),
        _get_browser_launcher=lambda: launcher,
        _get_proxy_service=lambda: SimpleNamespace(),
        profile_manager_adapter=lambda: SimpleNamespace(),
        browser_launcher_adapter=lambda: launcher,
        proxy_service_adapter=lambda: SimpleNamespace(),
    )
    app = api_app.create_app(container, self_url="http://127.0.0.1:8000")
    return TestClient(app, base_url=ORIGIN), users, assignments, protected, launcher


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/ext/auth/login",
        json={"username": username, "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return response.json()


def _mutation_headers(csrf_token: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf_token}


def test_profile_lists_are_server_filtered_and_redacted(api):
    client, _, _, _, _ = api
    user = _login(client, "alice")

    ordinary = client.get("/api/v1/profiles")
    admin_client = TestClient(client.app, base_url=ORIGIN)
    admin = _login(admin_client, "admin")
    administrator = admin_client.get("/api/v1/profiles")
    service = client.get("/api/v1/profiles", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})

    assert [item["name"] for item in ordinary.json()["profiles"]] == ["profile-a"]
    assert [item["name"] for item in administrator.json()["profiles"]] == [
        "profile-a",
        "profile-b",
    ]
    assert [item["name"] for item in service.json()["profiles"]] == [
        "profile-a",
        "profile-b",
    ]
    assert ordinary.json()["profiles"][0]["proxy"] == "http://192.0.2.10:8080"
    assert "user:pass" not in ordinary.text
    assert "192.0.2.20" not in ordinary.text
    assert "data_dir" not in ordinary.text
    assert "foreign:secret" not in administrator.text
    assert user["user"]["role"] == "user"
    assert admin["user"]["role"] == "admin"


def test_profile_detail_resolves_alias_and_redacts_foreign_profile(api):
    client, _, _, _, _ = api
    _login(client, "alice")

    own = client.get("/api/v1/profiles/profile-a")
    foreign = client.get("/api/v1/profiles/id-b")

    assert own.status_code == 200
    assert own.json()["profile_id"] == "id-a"
    assert own.json()["proxy"] == "http://user:pass@192.0.2.10:8080"
    assert "data_dir" not in own.json()
    assert foreign.status_code == 404
    assert "foreign:secret" not in foreign.text


def test_personal_create_assigns_creator_and_uses_linux_default(api):
    client, _, assignments, _, _ = api
    auth = _login(client, "alice")

    response = client.post(
        "/api/v1/profiles",
        json={"name": "new-profile"},
        headers=_mutation_headers(auth["csrf_token"]),
    )

    assert response.status_code == 201
    assert response.json()["profile_id"] == "id-created"
    assert response.json()["os_type"] == "linux"
    assert assignments.is_assigned("user-a", "id-created") is True


def test_profile_role_or_owner_payload_is_rejected_before_create(api):
    client, _, assignments, protected, _ = api
    auth = _login(client, "alice")

    response = client.post(
        "/api/v1/profiles",
        json={"name": "forged", "owner_id": "user-b", "role": "admin"},
        headers=_mutation_headers(auth["csrf_token"]),
    )

    assert response.status_code == 422
    assert "forged" not in protected.profiles
    assert assignments.is_assigned("user-a", "id-created") is False


def test_mixed_forbidden_batch_has_no_coordinator_effect(api):
    client, _, _, protected, _ = api
    auth = _login(client, "alice")

    response = client.post(
        "/api/v1/ext/browser/start-batch",
        json={"profile_ids": ["id-a", "id-b"]},
        headers=_mutation_headers(auth["csrf_token"]),
    )

    assert response.status_code == 404
    assert protected.coordinator_calls == 0
    assert protected.start_effects == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": ORIGIN},
        {"Origin": "https://attacker.example.test", "X-CSRF-Token": "unused"},
    ],
)
def test_missing_csrf_or_wrong_origin_has_zero_batch_effects(api, headers):
    client, _, _, protected, _ = api
    auth = _login(client, "alice")
    headers = {**headers, "Cookie": f"foxprofile_session={client.cookies['foxprofile_session']}"}
    if headers.get("X-CSRF-Token") == "unused":
        headers["X-CSRF-Token"] = auth["csrf_token"]

    response = client.post(
        "/api/v1/ext/browser/start-batch",
        json={"profile_ids": ["id-a"]},
        headers=headers,
    )

    assert response.status_code == 403
    assert protected.coordinator_calls == 0


def test_batch_start_deduplicates_ids_after_whole_request_authorization(api):
    client, _, _, protected, _ = api
    auth = _login(client, "alice")

    response = client.post(
        "/api/v1/ext/browser/start-batch",
        json={"profile_ids": ["id-a", "id-a"]},
        headers=_mutation_headers(auth["csrf_token"]),
    )

    assert response.status_code == 202
    assert protected.coordinator_calls == 1
    assert protected.start_effects == ["id-a"]
    assert response.json()["count"] == 1


def test_batch_start_rejects_name_alias_before_any_effect(api):
    client, _, _, protected, _ = api
    auth = _login(client, "alice")
    response = client.post(
        "/api/v1/ext/browser/start-batch",
        json={"profile_ids": ["id-a", "profile-a"]},
        headers=_mutation_headers(auth["csrf_token"]),
    )
    assert response.status_code == 404
    assert protected.coordinator_calls == 0
    assert protected.start_effects == []


def test_screenshot_and_control_recheck_revoked_profile_assignment(api):
    client, _, assignments, _, launcher = api
    auth = _login(client, "alice")

    screenshot = client.get("/api/v1/browser/profile-a/page/screenshot?format=json")
    assert screenshot.status_code == 200
    assert len(launcher.control_calls) == 1

    assignments.revoke("user-a", "id-a")
    screenshot_after_revoke = client.get("/api/v1/browser/profile-a/page/screenshot?format=json")
    control_after_revoke = client.post(
        "/api/v1/browser/profile-a/page/navigate",
        json={"url": "https://example.test"},
        headers=_mutation_headers(auth["csrf_token"]),
    )

    assert screenshot_after_revoke.status_code == 404
    assert control_after_revoke.status_code == 404
    assert len(launcher.control_calls) == 1


def test_ordinary_user_cannot_replace_assignments(api):
    client, _, assignments, _, _ = api
    user = _login(client, "alice")

    response = client.put(
        "/api/v1/ext/admin/users/user-b/profile-assignments",
        json={"profile_ids": ["id-b"]},
        headers=_mutation_headers(user["csrf_token"]),
    )

    assert response.status_code == 403
    assert assignments.is_assigned("user-b", "id-b") is False


def test_admin_can_replace_only_the_selected_users_assignments(api):
    client, _, assignments, _, _ = api
    admin = _login(client, "admin")

    response = client.put(
        "/api/v1/ext/admin/users/user-b/profile-assignments",
        json={"profile_ids": ["id-b"]},
        headers=_mutation_headers(admin["csrf_token"]),
    )

    assert response.status_code == 200
    assert assignments.is_assigned("user-b", "id-b") is True
    assert assignments.is_assigned("user-a", "id-a") is True


def test_service_credential_is_not_a_personal_cookie_or_admin(api):
    client, _, _, _, _ = api

    response = client.get(
        "/api/v1/ext/auth/me", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
    )
    admin_users = client.get(
        "/api/v1/ext/admin/users", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
    )

    assert response.status_code == 404
    assert admin_users.status_code == 403


def test_mcp_setup_is_personal_admin_only(api):
    client, _, _, _, _ = api
    _login(client, "alice")
    ordinary = client.get("/api/v1/mcp/setup?base_url=https%3A%2F%2Fpanel.example.test")
    service = client.get(
        "/api/v1/mcp/setup?base_url=https%3A%2F%2Fpanel.example.test",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert ordinary.status_code == 403
    assert service.status_code == 404


@pytest.fixture
def real_api(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    database_path = tmp_path / "extension.sqlite3"
    monkeypatch.setenv("FOXPROFILE_EXT_DB", str(database_path))
    monkeypatch.setattr(manager_module, "DATA_DIR", tmp_path / "browserdata")
    monkeypatch.setattr(manager_module, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(api_auth, "API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(api_app, "API_TOKEN", SERVICE_TOKEN)
    database = ExtensionDatabase(database_path)
    users = UserDAO(database)
    users.bootstrap_admin("admin-id", "admin", hash_password(PASSWORD))
    users.create("alice-id", "alice", hash_password(PASSWORD))
    users.create("bob-id", "bob", hash_password(PASSWORD))
    container = Container(
        run_coordinator=SimpleNamespace(status=lambda *_: None, shutdown=lambda **_: None)
    )
    alice = container.auth_service.login("alice", PASSWORD).principal
    bob = container.auth_service.login("bob", PASSWORD).principal
    own = container.protected_application.create_profile(alice, name="Own")
    foreign = container.protected_application.create_profile(bob, name="Foreign")
    app = api_app.create_app(container)
    client = TestClient(app, base_url=ORIGIN)
    yield client, container, database, own, foreign
    container.shutdown(timeout=0)


def _profile_zip(name, extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("profile.json", json.dumps({"name": name, "profile_id": "forged-id"}))
        for path, value in (extra or {}).items():
            archive.writestr(path, value)
    return output.getvalue()


def test_invalid_proxy_archive_cannot_register_or_start(real_api):
    client, container, _, _, _ = real_api
    auth = _login(client, "alice")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(
            "profile.json", json.dumps({"name": "Invalid proxy", "proxy": "not-a-proxy"})
        )
        zipped.writestr("data/Cookies", "must not extract")

    response = client.post(
        "/api/v1/ext/profiles/import",
        content=archive.getvalue(),
        headers={**_mutation_headers(auth["csrf_token"]), "Content-Type": "application/zip"},
    )

    assert response.status_code == 422
    actor = container.auth_service.login("alice", PASSWORD).principal
    assert "Invalid proxy" not in {
        item.name for item in container.protected_application.list_profiles(actor)
    }
    with pytest.raises(DomainError):
        container.protected_application.start_profile(actor, "Invalid proxy")


def test_real_managed_import_export_download_is_scoped_and_one_use(real_api):
    client, container, database, _, foreign = real_api
    auth = _login(client, "alice")
    headers = {**_mutation_headers(auth["csrf_token"]), "Content-Type": "application/zip"}
    imported = client.post(
        "/api/v1/ext/profiles/import",
        content=_profile_zip("Imported", {"data/Cookies": "own-cookie-data"}),
        headers=headers,
    )
    assert imported.status_code == 201
    profile_id = imported.json()["profile_id"]
    assert profile_id != "forged-id"
    assert AssignmentDAO(database).is_assigned("alice-id", profile_id)
    exported = client.post(
        f"/api/v1/ext/profiles/{profile_id}/export",
        json={"include_data": True},
        headers=_mutation_headers(auth["csrf_token"]),
    )
    assert exported.status_code == 200
    url = exported.json()["download_url"]
    bob = TestClient(client.app, base_url=ORIGIN)
    _login(bob, "bob")
    assert bob.get(url).status_code == 404
    assert client.get(url.replace(profile_id, foreign.profile_id)).status_code == 404
    download = client.get(url)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.read("data/Cookies") == b"own-cookie-data"
        assert "profile_id" not in json.loads(archive.read("profile.json"))
        assert all("sqlite" not in name and ".env" not in name for name in archive.namelist())
    assert client.get(url).status_code == 404
    assert not list((database.path.parent / ".foxprofile-transfers").glob("*.zip"))
    # The injected idle runtime keeps catalog/transfer coverage independent of
    # installed browsers and native process namespaces.
    actor = container.auth_service.login("alice", PASSWORD).principal
    assert container.protected_application.run_status(actor, profile_id) is None


def test_real_managed_overwrite_preserves_identity_and_denies_foreign_before_effect(real_api):
    client, _, database, own, foreign = real_api
    auth = _login(client, "alice")
    headers = {**_mutation_headers(auth["csrf_token"]), "Content-Type": "application/zip"}
    denied = client.post(
        f"/api/v1/ext/profiles/import?target_profile_id={foreign.profile_id}",
        content=_profile_zip("Hijacked"),
        headers=headers,
    )
    assert denied.status_code == 404
    assert not list((database.path.parent / ".foxprofile-transfers").glob("*.zip"))
    replaced = client.post(
        f"/api/v1/ext/profiles/import?target_profile_id={own.profile_id}",
        content=_profile_zip("Different", {"data/Cookies": "replacement"}),
        headers=headers,
    )
    assert replaced.status_code == 201
    assert replaced.json() == {"profile_id": own.profile_id, "name": "Own"}
    assignments = AssignmentDAO(database)
    assert assignments.is_assigned("alice-id", own.profile_id)
    assert not assignments.is_assigned("bob-id", own.profile_id)
    forbidden = client.post(
        f"/api/v1/ext/profiles/{foreign.profile_id}/export",
        json={},
        headers=_mutation_headers(auth["csrf_token"]),
    )
    assert forbidden.status_code == 404


def test_real_download_rechecks_revocation_and_session_liveness(real_api):
    client, _, database, own, _ = real_api
    auth = _login(client, "alice")
    exported = client.post(
        f"/api/v1/ext/profiles/{own.profile_id}/export",
        json={},
        headers=_mutation_headers(auth["csrf_token"]),
    )
    url = exported.json()["download_url"]
    AssignmentDAO(database).revoke("alice-id", own.profile_id)
    assert client.get(url).status_code == 404
    SessionDAO(database).revoke_user("alice-id")
    assert client.get(url).status_code == 401


@pytest.mark.parametrize("revocation", ["grant", "session"])
def test_archive_stream_rechecks_revocation_without_holding_catalog_lock(real_api, revocation):
    from src.api.routes.ext_transfer import _ArchiveResponse

    _, container, database, own, _ = real_api
    actor = container.auth_service.login("alice", PASSWORD).principal
    profile_data = container.protected_application._catalog.manager.data_dir / own.name
    profile_data.mkdir(parents=True, exist_ok=True)
    (profile_data / "stream-data.bin").write_bytes(os.urandom(192 * 1024))
    download_id = container.protected_application.export_profile_archive(actor, own.profile_id)
    staged = database.path.parent / ".foxprofile-transfers" / f"{download_id}.zip"
    response = _ArchiveResponse(container.protected_application, actor, download_id, own.profile_id)
    body_blocked = threading.Event()
    release_body = threading.Event()
    messages = []

    async def send(message):
        messages.append(message)
        if (
            message["type"] == "http.response.body"
            and message["body"]
            and not body_blocked.is_set()
        ):
            body_blocked.set()
            assert await asyncio.to_thread(release_body.wait, 5)

    async def receive():
        return {"type": "http.disconnect"}

    async def stream_and_revoke():
        task = asyncio.create_task(
            response(
                {"type": "http", "method": "GET", "path": "/downloads"},
                receive,
                send,
            )
        )
        assert await asyncio.to_thread(body_blocked.wait, 5)
        created = await asyncio.wait_for(
            asyncio.to_thread(
                container.protected_application.create_profile,
                actor,
                name="Catalog remains available",
            ),
            5,
        )
        assert created.name == "Catalog remains available"
        if revocation == "grant":
            AssignmentDAO(database).revoke("alice-id", own.profile_id)
        else:
            SessionDAO(database).revoke_user("alice-id")
        release_body.set()
        await task

    asyncio.run(stream_and_revoke())

    body_chunks = [
        item["body"] for item in messages if item["type"] == "http.response.body" and item["body"]
    ]
    assert len(body_chunks) == 1
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert not staged.exists()


def test_archive_stream_disconnect_cleans_one_use_staging_file(real_api):
    from src.api.routes.ext_transfer import _ArchiveResponse

    _, container, database, own, _ = real_api
    actor = container.auth_service.login("alice", PASSWORD).principal
    profile_data = container.protected_application._catalog.manager.data_dir / own.name
    profile_data.mkdir(parents=True, exist_ok=True)
    (profile_data / "stream-data.bin").write_bytes(os.urandom(192 * 1024))
    download_id = container.protected_application.export_profile_archive(actor, own.profile_id)
    staged = database.path.parent / ".foxprofile-transfers" / f"{download_id}.zip"
    response = _ArchiveResponse(container.protected_application, actor, download_id, own.profile_id)

    async def send(message):
        if message["type"] == "http.response.body" and message["body"]:
            raise OSError("client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(OSError, match="client disconnected"):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/downloads"},
                receive,
                send,
            )
        )

    assert not staged.exists()
    with (
        pytest.raises(DomainError),
        container.protected_application.download_profile_archive(
            actor, download_id, own.profile_id
        ),
    ):
        pass


def test_real_managed_archive_and_legacy_path_inputs_fail_before_effects(real_api):
    client, _, database, _, _ = real_api
    auth = _login(client, "alice")
    headers = {**_mutation_headers(auth["csrf_token"]), "Content-Type": "application/zip"}
    traversal = client.post(
        "/api/v1/ext/profiles/import",
        content=_profile_zip("Escape", {"data/../../escaped": "private"}),
        headers=headers,
    )
    assert traversal.status_code == 422
    assert not (database.path.parent / "escaped").exists()
    injected = client.post(
        "/api/v1/ext/profiles/import?zip_path=/etc/passwd",
        content=_profile_zip("Injected"),
        headers=headers,
    )
    assert injected.status_code == 422
    legacy = client.post(
        "/api/v1/profiles/import",
        json={"zip_path": "/etc/passwd"},
        headers=_mutation_headers(auth["csrf_token"]),
    )
    assert legacy.status_code == 404
    no_csrf = client.post(
        "/api/v1/ext/profiles/import",
        content=_profile_zip("No CSRF"),
        headers={"Content-Type": "application/zip"},
    )
    assert no_csrf.status_code == 403
    assert {item["name"] for item in client.get("/api/v1/profiles").json()["profiles"]} == {"Own"}


def test_real_admin_settings_are_human_admin_only_and_count_all_occupied_states(real_api):
    from foxprofile_ext.dto import ProcessIdentity
    from foxprofile_ext.storage import CapacityLedger

    client, _, database, _, _ = real_api
    ordinary = _login(client, "alice")
    assert client.get("/api/v1/ext/admin/settings").status_code == 403
    assert (
        client.patch(
            "/api/v1/ext/admin/settings",
            json={"max_concurrent_profiles": 3},
            headers=_mutation_headers(ordinary["csrf_token"]),
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/ext/admin/settings", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
        ).status_code
        == 403
    )
    admin = _login(client, "admin")
    ledger = CapacityLedger(database)
    runs = ledger.reserve(("reserved", "starting", "running", "stopping"), "test-host").runs
    for index, run in enumerate(runs[1:]):
        assert ledger.claim(run.ref, "test-host", ProcessIdentity(2_000_000_000 + index, 1))
    assert ledger.mark_running(runs[2].ref)
    assert ledger.mark_stopping(runs[3].ref)
    assert client.get("/api/v1/ext/admin/settings").json() == {
        "max_concurrent_profiles": 10,
        "occupied": 4,
    }
    for invalid in (True, 1.5, "2", None, 0, -1):
        response = client.patch(
            "/api/v1/ext/admin/settings",
            json={"max_concurrent_profiles": invalid},
            headers=_mutation_headers(admin["csrf_token"]),
        )
        assert response.status_code == 422
    lowered = client.patch(
        "/api/v1/ext/admin/settings",
        json={"max_concurrent_profiles": 2},
        headers=_mutation_headers(admin["csrf_token"]),
    )
    assert lowered.status_code == 200
    assert lowered.json() == {"max_concurrent_profiles": 2, "occupied": 4}
    assert len(ledger.active_runs()) == 4


def test_real_stopped_only_profile_effect_uses_durable_occupancy(real_api, monkeypatch):
    from foxprofile_ext.storage import CapacityLedger
    from src.api.routes import profiles as profile_routes

    client, _, database, own, _ = real_api
    auth = _login(client, "alice")
    CapacityLedger(database).reserve((own.profile_id,), "test-host")
    reset_calls = []
    monkeypatch.setattr(profile_routes.fingerprint, "reset", lambda path: reset_calls.append(path))

    response = client.delete(
        f"/api/v1/profiles/{own.profile_id}/fingerprint",
        headers=_mutation_headers(auth["csrf_token"]),
    )

    assert response.status_code == 409
    assert reset_calls == []


def test_real_large_archive_streams_incrementally_without_an_added_size_limit(real_api):
    from src.api.routes.ext_transfer import import_profile

    client, container, database, _, _ = real_api
    actor = container.auth_service.login("alice", PASSWORD).principal
    block = b"x" * (64 * 1024)
    reads = []
    with tempfile.TemporaryFile(mode="w+b") as archive_file:
        with zipfile.ZipFile(archive_file, "w") as archive:
            archive.writestr("profile.json", json.dumps({"name": "Large import"}))
            with archive.open("data/Large", "w") as data:
                for _ in range(513):
                    data.write(block)
        archive_file.seek(0)

        async def receive():
            chunk = archive_file.read(len(block))
            reads.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

        request = Request(
            {
                "type": "http",
                "app": client.app,
                "method": "POST",
                "path": "/api/v1/ext/profiles/import",
                "query_string": b"",
                "headers": [(b"content-type", b"application/zip")],
            },
            receive=receive,
        )
        result = asyncio.run(import_profile(request, target_profile_id=None, principal=actor))
    assert result["name"] == "Large import"
    assert len(reads) > 512
    assert max(reads) == len(block)
    assert (
        database.path.parent / "browserdata" / "Large import" / "Large"
    ).stat().st_size > 32 * 1024 * 1024


def test_service_archives_use_configured_roots_and_preserve_catalog_identity(
    real_api, monkeypatch, tmp_path
):
    client, container, database, own, _ = real_api
    root = tmp_path / "transfers"
    root.mkdir()
    second_root = tmp_path / "second-transfers"
    second_root.mkdir()
    monkeypatch.setenv("FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS", f"{root}{os.pathsep}{second_root}")
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}

    exported = client.post(f"/api/v1/profiles/{own.profile_id}/export", json={}, headers=headers)
    assert exported.status_code == 200
    archive_path = Path(exported.json()["zip_path"])
    assert archive_path.parent == root
    with zipfile.ZipFile(archive_path) as archive:
        assert "profile_id" not in json.loads(archive.read("profile.json"))

    source = second_root / "new.zip"
    source.write_bytes(_profile_zip("Service imported", {"data/Cookies": "service-data"}))
    imported = client.post(
        "/api/v1/profiles/import", json={"zip_path": str(source)}, headers=headers
    )
    assert imported.status_code == 200
    actor = PrincipalDTO("trusted-service", PrincipalKind.SERVICE)
    created = container.protected_application.resolve_profile(actor, "Service imported")
    assert created.profile_id != "forged-id"
    assignments = AssignmentDAO(database)
    assert not assignments.is_assigned("alice-id", created.profile_id)
    assert not assignments.is_assigned("bob-id", created.profile_id)
    assert not assignments.is_assigned("trusted-service", created.profile_id)

    archive_path.write_bytes(_profile_zip("Own", {"data/Cookies": "replacement"}))
    overwritten = client.post(
        "/api/v1/profiles/import",
        json={"zip_path": str(archive_path), "overwrite": True},
        headers=headers,
    )
    assert overwritten.status_code == 200
    assert (
        container.protected_application.resolve_profile(actor, "Own").profile_id == own.profile_id
    )
    assert assignments.is_assigned("alice-id", own.profile_id)
    assert not assignments.is_assigned("trusted-service", own.profile_id)
    assert (tmp_path / "browserdata" / "Own" / "Cookies").read_text() == "replacement"


def test_service_archive_paths_fail_closed_before_file_or_catalog_effects(
    real_api, monkeypatch, tmp_path
):
    client, container, _, own, _ = real_api
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    monkeypatch.delenv("FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS", raising=False)
    for url, body in (
        (f"/api/v1/profiles/{own.profile_id}/export", {}),
        ("/api/v1/profiles/import", {"zip_path": str(tmp_path / "missing.zip")}),
    ):
        denied = client.post(url, json=body, headers=headers)
        assert denied.status_code == 403
        assert "FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS" in denied.text

    root = tmp_path / "transfers"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "source.zip"
    source.write_bytes(_profile_zip("Must not import"))
    (root / "link.zip").symlink_to(source)
    (root / "directory-link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS", str(root))
    for candidate in (source, root / ".." / "outside" / "source.zip", root / "link.zip"):
        denied = client.post(
            "/api/v1/profiles/import", json={"zip_path": str(candidate)}, headers=headers
        )
        assert denied.status_code == 403
    for destination in (outside, root / "directory-link"):
        denied = client.post(
            f"/api/v1/profiles/{own.profile_id}/export",
            json={"export_dir": str(destination)},
            headers=headers,
        )
        assert denied.status_code == 403
    actor = PrincipalDTO("trusted-service", PrincipalKind.SERVICE)
    assert {item.name for item in container.protected_application.list_profiles(actor)} == {
        "Own",
        "Foreign",
    }
    assert list(root.glob("*.zip")) == [root / "link.zip"]
    assert list(outside.iterdir()) == [source]


def test_service_export_storage_error_is_not_mapped_to_forbidden(real_api, monkeypatch, tmp_path):
    from foxprofile_ext import application as application_module

    client, _, _, own, _ = real_api
    root = tmp_path / "transfers"
    root.mkdir()
    monkeypatch.setenv("FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS", str(root))

    def export_without_space(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(application_module, "export_to_zip", export_without_space)
    client_without_server_exceptions = TestClient(
        client.app, base_url=ORIGIN, raise_server_exceptions=False
    )
    response = client_without_server_exceptions.post(
        f"/api/v1/profiles/{own.profile_id}/export",
        json={},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == 500


def test_personal_admin_and_user_cannot_use_service_archive_paths(real_api, monkeypatch, tmp_path):
    client, _, _, own, _ = real_api
    monkeypatch.setenv("FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS", str(tmp_path))
    for username in ("alice", "admin"):
        auth = _login(client, username)
        headers = _mutation_headers(auth["csrf_token"])
        for url, body in (
            (f"/api/v1/profiles/{own.profile_id}/export", {"export_dir": str(tmp_path)}),
            ("/api/v1/profiles/import", {"zip_path": str(tmp_path / "source.zip")}),
        ):
            assert client.post(url, json=body, headers=headers).status_code == 404


def test_service_overwrite_requires_stopped_and_exports_preserve_native_running_behavior(
    real_api, monkeypatch, tmp_path
):
    from foxprofile_ext.storage import CapacityLedger

    client, _, database, own, _ = real_api
    root = tmp_path / "transfers"
    root.mkdir()
    source = root / "source.zip"
    source.write_bytes(_profile_zip("Own", {"data/Cookies": "must-not-write"}))
    monkeypatch.setenv("FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS", str(root))
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    CapacityLedger(database).reserve((own.profile_id,), "test-host")
    assert (
        client.post(
            "/api/v1/profiles/import",
            json={"zip_path": str(source), "overwrite": True},
            headers=headers,
        ).status_code
        == 409
    )
    assert not (tmp_path / "browserdata" / "Own" / "Cookies").exists()
    exported = client.post(f"/api/v1/profiles/{own.profile_id}/export", json={}, headers=headers)
    assert exported.status_code == 200
    assert Path(exported.json()["zip_path"]).is_file()
    auth = _login(client, "alice")
    personal = client.post(
        f"/api/v1/ext/profiles/{own.profile_id}/export",
        json={},
        headers=_mutation_headers(auth["csrf_token"]),
    )
    assert personal.status_code == 200
    assert client.get(personal.json()["download_url"]).status_code == 200


def test_service_archive_paths_remain_pinned_across_replacement_races(
    real_api, monkeypatch, tmp_path
):
    from foxprofile_ext import application

    client, container, _, own, _ = real_api
    root = tmp_path / "transfers"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    safe_source = root / "source.zip"
    safe_source.write_bytes(_profile_zip("Pinned source"))
    foreign_source = outside / "source.zip"
    foreign_source.write_bytes(_profile_zip("Must not import"))
    monkeypatch.setenv("FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS", str(root))
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    original_copy = application.shutil.copyfileobj

    def replace_input_after_open(source, destination):
        safe_source.unlink()
        safe_source.symlink_to(foreign_source)
        return original_copy(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(application.shutil, "copyfileobj", replace_input_after_open)
        imported = client.post(
            "/api/v1/profiles/import", json={"zip_path": str(safe_source)}, headers=headers
        )
    assert imported.status_code == 200
    actor = PrincipalDTO("trusted-service", PrincipalKind.SERVICE)
    assert {item.name for item in container.protected_application.list_profiles(actor)} == {
        "Own",
        "Foreign",
        "Pinned source",
    }

    destination = root / "destination"
    destination.mkdir()
    original_export = application.export_to_zip

    def replace_output_directory(*args):
        result = original_export(*args)
        destination.rename(root / "original-destination")
        destination.symlink_to(outside, target_is_directory=True)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(application, "export_to_zip", replace_output_directory)
        denied = client.post(
            f"/api/v1/profiles/{own.profile_id}/export",
            json={"export_dir": str(destination)},
            headers=headers,
        )
    assert denied.status_code == 403
    assert list((root / "original-destination").iterdir()) == []
    assert list(outside.iterdir()) == [foreign_source]

    protected_file = outside / "protected"
    protected_file.write_text("unchanged")

    def plant_output_symlink(*args):
        succeeded, path = original_export(*args)
        prepared = Path(path).with_name("prepared.zip")
        Path(path).rename(prepared)
        (root / "prepared_fixed.zip").symlink_to(protected_file)
        return succeeded, str(prepared)

    with monkeypatch.context() as patch:
        patch.setattr(application, "export_to_zip", plant_output_symlink)
        patch.setattr(
            application, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="fixed"))
        )
        denied = client.post(f"/api/v1/profiles/{own.profile_id}/export", json={}, headers=headers)
    assert denied.status_code == 403
    assert protected_file.read_text() == "unchanged"
