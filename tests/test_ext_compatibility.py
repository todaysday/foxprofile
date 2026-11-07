"""Compatibility checks at the current upstream auth, API, catalog, and runner seams."""

import inspect
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient

from foxprofile_ext import create_extension_composition
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind, ProfileRefDTO, Role
from foxprofile_ext.errors import DomainError, ErrorCode
from src.api import auth
from src.api.routes.browser import launch_browser
from src.models.profile import Profile
from src.services.browser import runner
from src.services.browser.launcher import BrowserLauncher, _LaunchResult
from src.services.profile import manager as manager_module

TOKEN = "s" * 32


def test_existing_service_token_dependency_and_extension_principal_seam(monkeypatch):
    monkeypatch.setattr(auth, "API_TOKEN", TOKEN)
    app = FastAPI()

    @app.get("/service", dependencies=[Depends(auth.require_token)])
    def service_route():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/service").status_code == 401
    assert client.get("/service", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200

    composition = create_extension_composition(lambda user_id, profile_id: False)
    service = PrincipalDTO("automation", PrincipalKind.SERVICE)
    assert composition.principal(service) is service
    profile = ProfileRefDTO("profile-a", "Example")
    assert composition.profile(service, profile) is profile

    with pytest.raises(DomainError) as error:
        composition.principal(None)
    assert error.value.code is ErrorCode.AUTH_REQUIRED

    ordinary = PrincipalDTO("user-a", PrincipalKind.PERSONAL, Role.USER, "session-a")
    with pytest.raises(DomainError) as error:
        composition.profile(ordinary, profile)
    assert error.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND


def test_existing_browser_route_keeps_launcher_service_call_shape():
    profile = Profile("example")
    calls = []

    class Profiles:
        profiles = {"example": profile}

    class Launcher:
        def is_running(self, name):
            return False

        def start_thread(
            self, profile_arg, log_callback, on_start=None, on_ready=None, on_stop=None
        ):
            calls.append((profile_arg, log_callback, on_start, on_ready, on_stop))
            return True

    class Events:
        count = 0

        def emit(self):
            self.count += 1

    response = Response()
    request = Request({"type": "http", "app": FastAPI()})
    events = Events()
    result = launch_browser(
        "example", response, request, wait=False, pm=Profiles(), bl=Launcher(), bus=events
    )

    assert response.status_code == 202
    assert result.success is True
    assert len(calls) == 1
    passed_profile, log_callback, on_start, on_ready, on_stop = calls[0]
    assert passed_profile is profile
    assert callable(log_callback)
    assert on_start is None
    assert callable(on_ready) and callable(on_stop)
    on_ready()
    on_stop()
    assert events.count == 3


def test_local_profile_serializer_keeps_known_fields_and_ignores_unknowns(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text(
        json.dumps(
            {
                "example": {
                    "name": "example",
                    "proxy": "http://user:pass@192.0.2.10:8080",
                    "os_type": "linux",
                    "timezone": "Asia/Tokyo",
                    "locale": "ja-JP",
                    "engine": "camoufox",
                    "profile_id": "stable-local-id",
                    "role": "admin",
                    "unknown": "ignored",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(tmp_path / "browser-data"))

    profile = manager_module.ProfileManager().profiles["example"]

    expected = {
        "name": "example",
        "proxy": "http://user:pass@192.0.2.10:8080",
        "os_type": "linux",
        "timezone": "Asia/Tokyo",
        "locale": "ja-JP",
        "engine": "camoufox",
    }
    local = profile.to_dict()
    assert {key: local[key] for key in expected} == expected
    assert not {"role", "unknown"} & local.keys()
    if "profile_id" in local:
        assert local["profile_id"] == "stable-local-id"

    portable_serializer = getattr(profile, "to_portable_dict", profile.to_dict)
    portable = portable_serializer()
    assert "profile_id" not in portable
    assert portable == expected


def test_runner_control_event_precedes_ready_and_launcher_registers_it():
    source = inspect.getsource(runner.run_browser)
    control_emit = source.index('_safe_print(f"CONTROL:{port}:{token}")')
    ready_emit = source.index('_safe_print("BROWSER_STARTED")')
    assert control_emit < ready_emit

    token = "example-private-control-token"
    process = SimpleNamespace(stdout=io.StringIO(f"CONTROL:12345:{token}\nBROWSER_STARTED\n"))
    launcher = BrowserLauncher()
    result = _LaunchResult()
    logs = []
    ready_saw_control = []

    def on_ready():
        ready_saw_control.append(launcher._controls.get("example"))

    launcher._monitor_process(
        process,
        "example",
        logs.append,
        on_ready,
        lambda: None,
        result,
    )

    assert result.settled.is_set() and result.ok is True
    assert ready_saw_control == [(12345, token)]
    assert all(token not in message for message in logs)
