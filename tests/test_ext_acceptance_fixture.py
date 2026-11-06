from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from foxprofile_ext.acceptance import (
    SmokeFailure,
    _staging_origin,
    credentials,
    input_server,
    smoke,
)
from foxprofile_ext.auth import AuthService
from foxprofile_ext.storage.dao import AssignmentDAO, SessionDAO, SettingsDAO, UserDAO
from foxprofile_ext.storage.database import ExtensionDatabase

REPOSITORY = Path(__file__).resolve().parents[1]
# Deliberately fake, disposable fixture credentials; never an actual account.
SECRETS = {
    "admin0_password": "example-admin-only",
    "a_password": "example-a-only",
    "b_password": "example-b-only",
    "service_token": "example-disposable-service-token-for-tests",
}


def _prepare(destination: Path, secret_file: Path):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "foxprofile_ext.acceptance",
            "prepare",
            "--destination",
            str(destination),
            "--credentials-file",
            str(secret_file),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = tmp_path_factory.mktemp("acceptance")
    secret_file = root / "credentials.json"
    secret_file.write_text(json.dumps(SECRETS), encoding="utf-8")
    secret_file.chmod(0o600)
    destination = root / "fixture"
    result = _prepare(destination, secret_file)
    assert result.returncode == 0, result.stderr
    return destination, secret_file, json.loads((destination / "fixture.json").read_text())


def test_prepared_fixture_uses_real_catalog_users_sessions_and_assignments(prepared):
    destination, _, manifest = prepared
    raw_profiles = json.loads((destination / "profiles.json").read_text())
    database = ExtensionDatabase(destination / "foxprofile_ext.sqlite3")
    users, assignments = UserDAO(database), AssignmentDAO(database)
    auth = AuthService(users, SessionDAO(database))
    assert len(raw_profiles) == 11
    assert len({item["profile_id"] for item in raw_profiles.values()}) == 11
    assert all(
        item["os_type"] == "linux" and item["engine"] == "camoufox" and not item["proxy"]
        for item in raw_profiles.values()
    )
    for name, label in (("A", "PA"), ("B", "PB")):
        session = auth.login(name, SECRETS[f"{name.lower()}_password"])
        assert auth.resolve_session(session.session_token).user.user_id == manifest["users"][name]
        assert assignments.profiles_for_user(session.user.user_id) == (
            manifest["profiles"][label]["profile_id"],
        )
        auth.logout(session.principal)
    assert SettingsDAO(database).get_capacity().max_concurrent_profiles == 10
    # Reopening the persistent database/catalog retains identities and grants.
    assert json.loads((destination / "profiles.json").read_text()) == raw_profiles
    assert {user.username for user in users.list_accounts()} == {"admin0", "A", "B"}
    if os.name == "posix":
        for path in (destination, *destination.rglob("*")):
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    for path in destination.rglob("*"):
        if path.is_file():
            assert all(
                value.encode() not in path.read_bytes()
                for key, value in SECRETS.items()
                if key != "service_token"
            )


def test_prepare_refuses_existing_nonempty_directory_and_symlinks(prepared, tmp_path):
    destination, secret_file, _ = prepared
    before = (destination / "profiles.json").read_bytes()
    assert _prepare(destination, secret_file).returncode == 1
    assert (destination / "profiles.json").read_bytes() == before
    link = tmp_path / "alias"
    link.symlink_to(destination, target_is_directory=True)
    assert _prepare(link, secret_file).returncode == 1
    parent_link = tmp_path / "parent"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    assert _prepare(parent_link / "fresh", secret_file).returncode == 1
    assert not (tmp_path / "fresh").exists()


def test_credentials_require_private_regular_file_and_never_echo_secrets(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(SECRETS))
    path.chmod(0o600)
    assert credentials(path) == SECRETS
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        credentials(link)
    if os.name == "posix":
        path.chmod(0o644)
        result = _prepare(tmp_path / "destination", path)
        assert result.returncode == 1
        assert not (tmp_path / "destination").exists()
        assert all(value not in result.stdout + result.stderr for value in SECRETS.values())


def test_controlled_input_page_real_loopback_http_has_no_directory_or_secret_access():
    server = input_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            response = client.get("/")
            assert response.status_code == 200
            assert "connect-src 'none'" in response.headers["content-security-policy"]
            for marker in (
                "text-input",
                "drag-source",
                "drop-target",
                "scroll-box",
                "geometry",
                "held-input",
                "next-click",
                "acceptanceState",
            ):
                assert marker in response.text
            assert "fetch(" not in response.text
            assert "XMLHttpRequest" not in response.text
            assert "WebSocket" not in response.text
            assert client.get("/fixture.json").status_code == 404
            assert client.get("/../runtime-env.json").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def real_http_app(prepared, monkeypatch):
    destination, _, manifest = prepared
    environment = json.loads((destination / "runtime-env.json").read_text())
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    from src.api import app as api_app
    from src.api import auth as api_auth
    from src.core.container import Container
    from src.services.profile import manager as manager_module

    monkeypatch.setattr(manager_module, "PROFILES_FILE", destination / "profiles.json")
    monkeypatch.setattr(manager_module, "DATA_DIR", destination / "browserdata")
    monkeypatch.setattr(api_auth, "API_TOKEN", SECRETS["service_token"])
    monkeypatch.setattr(api_app, "API_TOKEN", SECRETS["service_token"])
    # Actual Container/auth/facade/catalog/DAOs/routes. No browser subprocess is needed.
    container = Container(
        run_coordinator=SimpleNamespace(status=lambda *_: None, shutdown=lambda **_: None)
    )
    app = api_app.create_app(container)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield origin, manifest
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        container.shutdown(timeout=0)


def test_smoke_over_real_http_uses_real_disposable_application(real_http_app, prepared):
    origin, manifest = real_http_app
    assert len(smoke(manifest, manifest["fixture_id"], origin, SECRETS)) == 4
    database = ExtensionDatabase(prepared[0] / "foxprofile_ext.sqlite3")
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM browser_runs").fetchone()[0] == 0
    assert len(json.loads((prepared[0] / "profiles.json").read_text())) == 11


def test_smoke_wrong_fixture_fails_before_http(prepared):
    with pytest.raises(SmokeFailure, match="identity confirmation"):
        smoke(prepared[2], "wrong-id", "http://127.0.0.1:1", SECRETS)


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.0.2.1",
        "https://user:pass@example.test",
        "https://example.test/path",
        "https://example.test?secret=example",
        "file:///tmp/example",
    ],
)
def test_smoke_rejects_unsafe_or_ambiguous_target_origins(origin):
    with pytest.raises(ValueError):
        _staging_origin(origin)


def test_smoke_cli_real_http_is_executable_and_secret_free(real_http_app, prepared):
    origin, manifest = real_http_app
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "foxprofile_ext.acceptance",
            "smoke",
            "--fixture",
            str(prepared[0]),
            "--expect-fixture",
            manifest["fixture_id"],
            "--base-url",
            origin,
            "--credentials-file",
            str(prepared[1]),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("PASS ") == 4
    assert "Browser/noVNC/source acceptance: NOT RUN" in result.stdout
    assert all(value not in result.stdout + result.stderr for value in SECRETS.values())


def test_smoke_rejects_wrong_target_ids_and_cleans_its_sessions(real_http_app, prepared):
    origin, manifest = real_http_app
    altered = json.loads(json.dumps(manifest))
    altered["users"]["A"] = "wrong-target-user"
    with pytest.raises(SmokeFailure, match="target user identity"):
        smoke(altered, altered["fixture_id"], origin, SECRETS)
    altered = json.loads(json.dumps(manifest))
    altered["profiles"]["PA"]["name"] = "wrong-target-profile"
    with pytest.raises(SmokeFailure, match="target fixture catalog identity"):
        smoke(altered, altered["fixture_id"], origin, SECRETS)
    database = ExtensionDatabase(prepared[0] / "foxprofile_ext.sqlite3")
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_prepare_is_offline_even_when_socket_connections_are_forbidden(prepared, tmp_path):
    script = """
import runpy, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError("fixture preparation attempted network access")
socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
sys.argv = ["foxprofile_ext.acceptance", "prepare", "--destination", sys.argv[1],
            "--credentials-file", sys.argv[2]]
runpy.run_module("foxprofile_ext.acceptance", run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "offline"), str(prepared[1])],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "no target contacted" in result.stdout


def test_smoke_cli_connection_failure_is_redacted(prepared):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "foxprofile_ext.acceptance",
            "smoke",
            "--fixture",
            str(prepared[0]),
            "--expect-fixture",
            prepared[2]["fixture_id"],
            "--base-url",
            "http://127.0.0.1:1",
            "--credentials-file",
            str(prepared[1]),
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "http://127.0.0.1:1" not in result.stderr
    assert all(value not in result.stdout + result.stderr for value in SECRETS.values())
