from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from foxprofile_ext.auth import AuthService, hash_password
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind, Role
from foxprofile_ext.storage.dao import AssignmentDAO, SessionDAO, UserDAO
from foxprofile_ext.storage.database import ExtensionDatabase
from src.api import app as api_app
from src.api import auth as api_auth
from src.api.routes.ext_auth import _check_login_transport

SERVICE_TOKEN = "trusted-service-token-that-is-long-enough"
PASSWORD = "correct horse battery staple"
ORIGIN = "https://panel.example.test"


class ProtectedApplication:
    def __init__(self, auth_service: AuthService):
        self.auth_service = auth_service
        self.principals: list[PrincipalDTO] = []

    def current_principal(self, principal: PrincipalDTO) -> PrincipalDTO:
        self.principals.append(principal)
        return self.auth_service.validate_principal(principal)


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
    users.create("user-id", "ordinary", hash_password(PASSWORD), Role.USER)
    auth_service = AuthService(
        users,
        sessions,
        assignments,
        lambda profile_id: profile_id == "profile-a",
    )
    protected = ProtectedApplication(auth_service)
    container = SimpleNamespace(
        auth_service=auth_service,
        protected_application=protected,
        _get_profile_manager=lambda: SimpleNamespace(),
        _get_browser_launcher=lambda: SimpleNamespace(),
        _get_proxy_service=lambda: SimpleNamespace(),
    )
    app = api_app.create_app(container, self_url="http://127.0.0.1:8000")
    return TestClient(app, base_url=ORIGIN), auth_service, users, protected


def _login(client: TestClient, username: str = "admin") -> tuple[dict, str]:
    response = client.post(
        "/api/v1/ext/auth/login",
        json={"username": username, "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return response.json(), response.cookies.get("foxprofile_session", "")


def test_login_issues_http_only_personal_cookie_without_returning_credentials(api):
    client, _, _, _ = api

    result, session_cookie = _login(client)
    response = client.post(
        "/api/v1/ext/auth/login",
        json={"username": "ordinary", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )

    assert session_cookie
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie
    assert "session_token" not in response.text
    assert SERVICE_TOKEN not in response.text
    assert result["user"]["role"] == "admin"
    assert result["csrf_token"]
    assert session_cookie not in response.text


def test_public_login_requires_https_outside_loopback(api):
    client, _, _, _ = api
    insecure = TestClient(client.app, base_url="http://panel.example.test")

    response = insecure.post(
        "/api/v1/ext/auth/login",
        json={"username": "admin", "password": PASSWORD},
        headers={"Origin": "http://panel.example.test"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize("client", [("127.0.0.1", 12345), None])
def test_public_http_login_is_rejected_even_with_local_or_missing_peer(client):
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("panel.example.test", 80),
            "client": client,
            "path": "/api/v1/ext/auth/login",
            "query_string": b"",
            "headers": [(b"origin", b"http://panel.example.test")],
        }
    )

    with pytest.raises(HTTPException) as denied:
        _check_login_transport(request)

    assert denied.value.status_code == 403


def test_cookie_state_changes_require_csrf_and_exact_origin(api):
    client, _, _, _ = api
    result, _ = _login(client)
    csrf = result["csrf_token"]

    missing = client.post("/api/v1/ext/auth/logout", headers={"Origin": ORIGIN})
    wrong_origin = client.post(
        "/api/v1/ext/auth/logout",
        headers={"Origin": "https://attacker.example.test", "X-CSRF-Token": csrf},
    )
    still_live = client.get("/api/v1/ext/auth/me")

    assert missing.status_code == 403
    assert wrong_origin.status_code == 403
    assert still_live.status_code == 200

    logged_out = client.post(
        "/api/v1/ext/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert logged_out.status_code == 200
    assert client.get("/api/v1/ext/auth/me").status_code == 401


def test_session_me_refreshes_and_disabled_account_is_denied(api):
    client, _, users, _ = api
    _login(client, "ordinary")
    assert client.get("/api/v1/ext/auth/me").json()["user"]["role"] == "user"

    users.set_active("user-id", False)
    response = client.get("/api/v1/ext/auth/me")

    assert response.status_code == 401


def test_service_principal_is_distinct_and_cannot_use_personal_admin_routes(api):
    client, _, _, protected = api
    result, _ = _login(client)
    personal = client.get("/api/v1/ext/admin/users")
    service = client.get(
        "/api/v1/ext/admin/users",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    service_me = client.get(
        "/api/v1/ext/auth/me",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert personal.status_code == 200
    assert service.status_code == 403
    assert service_me.status_code == 404
    assert protected.principals[-2].kind is PrincipalKind.SERVICE
    assert protected.principals[-1].kind is PrincipalKind.SERVICE
    assert result["user"]["role"] == "admin"


def test_empty_service_key_does_not_fall_back_to_personal_cookie(api):
    client, _, _, _ = api
    _login(client)

    response = client.get("/api/v1/ext/auth/me", headers={"X-API-Key": ""})

    assert response.status_code == 401


def test_public_allowlist_and_protected_routes(api):
    client, _, _, _ = api

    assert client.get("/api/v1/health").status_code == 200
    info = client.get("/api/v1/info")
    assert info.status_code == 200
    assert info.json()["auth_required"] is True
    panel = client.get("/")
    assert panel.status_code == 200
    assert SERVICE_TOKEN not in panel.text
    assert client.get("/api/v1/profiles").status_code == 401


def test_protected_mode_can_bind_publicly_without_a_service_token(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")

    api_auth.check_bind_safety("0.0.0.0", "")


def test_login_rejects_an_invalid_account_without_session(api):
    client, _, _, _ = api

    response = client.post(
        "/api/v1/ext/auth/login",
        json={"username": "missing", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 401
    assert not response.cookies.get("foxprofile_session")
    assert "password_hash" not in response.text


def test_admin_account_routes_never_return_password_material_and_reset_revokes_sessions(api):
    client, _, _, _ = api
    admin, _ = _login(client)
    created = client.post(
        "/api/v1/ext/admin/users",
        json={"username": "new-user", "password": "temporary password", "role": "user"},
        headers={"Origin": ORIGIN, "X-CSRF-Token": admin["csrf_token"]},
    )
    assert created.status_code == 201
    user_id = created.json()["user"]["user_id"]
    assert "password" not in created.text
    assert "password_hash" not in created.text

    user_client = TestClient(client.app, base_url=ORIGIN)
    user_response = user_client.post(
        "/api/v1/ext/auth/login",
        json={"username": "new-user", "password": "temporary password"},
        headers={"Origin": ORIGIN},
    )
    assert user_response.status_code == 200
    reset = client.post(
        f"/api/v1/ext/admin/users/{user_id}/reset-password",
        json={"password": "replacement password"},
        headers={"Origin": ORIGIN, "X-CSRF-Token": admin["csrf_token"]},
    )

    assert reset.status_code == 200
    assert "password" not in reset.text
    assert "password_hash" not in reset.text
    assert user_client.get("/api/v1/ext/auth/me").status_code == 401


def test_personal_cookie_and_unset_service_key_never_authorize_mcp(api, monkeypatch):
    client, _, _, _ = api
    _login(client)
    assert client.post("/mcp", json={}).status_code == 401
    monkeypatch.setattr(api_auth, "API_TOKEN", "")
    monkeypatch.setattr(api_app, "API_TOKEN", "")
    app = api_app.create_app(client.app.state.container)
    no_service = TestClient(app, base_url=ORIGIN)
    assert no_service.get("/api/v1/profiles").status_code == 401
    assert no_service.post("/mcp", json={}).status_code == 401
    assert (
        no_service.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
        ).status_code
        == 401
    )
    _login(no_service)
    assert no_service.get("/api/v1/ext/auth/me").status_code == 200
    assert no_service.post("/mcp", json={}).status_code == 401
