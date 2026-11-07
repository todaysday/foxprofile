"""Baseline and deliberate drift checks against disposable candidate copies."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import foxprofile_ext.compatibility as compatibility
from foxprofile_ext.compatibility import (
    ROOT,
    check_compatibility,
    check_source_contracts,
    load_register,
    require_compatible_hooks,
    route_inventory,
    signature,
)

SCRIPT = ROOT / "tools/check_extension_compatibility.py"


@pytest.fixture
def protected_app(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    from src.api.app import create_app

    return create_app(object())


@pytest.fixture
def candidate(tmp_path):
    for package in ("src", "chrome_engine", "foxprofile_ext"):
        shutil.copytree(
            ROOT / package,
            tmp_path / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    return tmp_path


def replace_source(candidate: Path, relative: str, original: str, replacement: str) -> None:
    path = candidate / relative
    source = path.read_text(encoding="utf-8")
    assert source.count(original) == 1, "synthetic candidate fixture needs adaptation"
    path.write_text(source.replace(original, replacement), encoding="utf-8")


def test_baseline_route_and_source_contracts(protected_app):
    assert check_compatibility(ROOT, protected_app) == []
    require_compatible_hooks()
    routes = {entry["key"]: entry for entry in load_register()["routes"]}
    for key in (
        "HTTP GET /api/v1/profiles/{name}/data-dir",
        "HTTP POST /api/v1/profiles/{name}/export",
        "HTTP GET /api/v1/browser/{name}/page/screenshot",
        "HTTP POST /api/v1/browser/{name}/launch",
        "WEBSOCKET * /api/v1/ext/manual-sessions/{session_id}/ws",
    ):
        assert routes[key]["classification"].startswith("protected")
    for route in routes.values():
        if route["classification"] == "protected":
            assert "src.api.dependencies.get_request_principal" in route["dependencies"]
    assert any(entry["classification"] == "public" for entry in routes.values())
    assert any(entry["classification"] == "protected-service" for entry in routes.values())


def test_source_contract_gate_parses_each_inventory_file_once(candidate, monkeypatch):
    parsed = 0
    original_parse = ast.parse

    def count_parse(*args, **kwargs):
        nonlocal parsed
        parsed += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(compatibility.ast, "parse", count_parse)
    source_files = [
        path
        for package in compatibility.PACKAGES
        for path in (candidate / package).rglob("*.py")
        if not {"tests", "e2e", "__pycache__"}.intersection(path.relative_to(candidate).parts)
        and path.relative_to(candidate).as_posix() != "foxprofile_ext/compatibility.py"
    ]

    assert check_source_contracts(candidate) == []
    assert parsed == len(source_files)

    path = candidate / "src/api/routes/unreviewed.py"
    path.write_text(
        "from fastapi import APIRouter\nrouter=APIRouter()\n"
        '@router.get("/unmounted-profile-export")\ndef export_data():\n    return {}\n',
        encoding="utf-8",
    )
    parsed = 0
    assert check_source_contracts(candidate) == [
        "unclassified source_routes: src/api/routes/unreviewed.py:export_data:router.get('/unmounted-profile-export')"
    ]
    assert parsed == len(source_files) + 1


def test_register_records_real_baseline_and_finite_covering_contracts():
    register = load_register()
    assert register["upstream"]["commit"] == "530b993ff3a7d211aaa0904fba0817b77c75d4c9"
    assert register["candidate"]["status"] in {"uncommitted-draft", "locked"}
    assert register["schema"]["version"] == 5
    assert register["hooks"] and register["launch_calls"] and register["service_methods"]
    for hook in register["hooks"]:
        assert hook["purpose"] and hook["contract"] and hook["signature"]
        assert hook["baseline_commit"] == register["upstream"]["commit"]
        filename, _, symbol = hook["covering_test"].partition("::")
        source = (ROOT / filename).read_text(encoding="utf-8")
        if symbol:
            assert symbol in {
                node.name
                for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }


@pytest.mark.parametrize(
    "path", ["/new-data", "/new-export", "/new-screenshot", "/new-browser-launch"]
)
def test_unclassified_http_surface_fails_even_with_authentication(protected_app, path):
    from fastapi import Depends

    from src.api.dependencies import get_request_principal

    protected_app.add_api_route(path, lambda: {}, dependencies=[Depends(get_request_principal)])
    assert f"unclassified route: HTTP GET {path}" in check_compatibility(ROOT, protected_app)


def test_unclassified_websocket_fails(protected_app):
    async def unreviewed(websocket):
        pass

    protected_app.add_api_websocket_route("/new-socket", unreviewed)
    assert "unclassified route: WEBSOCKET * /new-socket" in check_compatibility(ROOT, protected_app)


def test_route_dependency_drift_is_specific(protected_app, monkeypatch):
    inventory = route_inventory(protected_app)
    target = next(entry for entry in inventory if entry["key"] == "HTTP GET /api/v1/profiles")
    target["dependencies"] = []
    monkeypatch.setattr("foxprofile_ext.compatibility.route_inventory", lambda app: inventory)
    assert "route adapter drift: HTTP GET /api/v1/profiles" in check_compatibility(
        ROOT, protected_app
    )


def test_unmounted_route_is_still_unclassified(candidate):
    path = candidate / "src/api/routes/unreviewed.py"
    path.write_text(
        "from fastapi import APIRouter\nrouter=APIRouter()\n"
        '@router.get("/unmounted-profile-export")\ndef export_data():\n    return {}\n',
        encoding="utf-8",
    )
    assert any(
        "unclassified source_routes: src/api/routes/unreviewed.py:export_data" in problem
        for problem in check_source_contracts(candidate)
    )


@pytest.mark.parametrize(
    "call",
    [
        "subprocess.Popen(['browser'])",
        "AsyncCamoufox()",
        "engine.launch_persistent_context('/profile')",
    ],
)
def test_new_browser_factory_is_unclassified(candidate, call):
    path = candidate / "src/services/browser/unreviewed.py"
    path.write_text(
        f"import subprocess\ndef create_browser():\n    return {call}\n", encoding="utf-8"
    )
    assert any(
        "unclassified launch_calls: src/services/browser/unreviewed.py:create_browser:" in problem
        for problem in check_source_contracts(candidate)
    )


def test_new_direct_service_method_is_unclassified(candidate):
    path = candidate / "foxprofile_ext/application.py"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n    def expose_profile(self):\n        return self._catalog\n")
    assert (
        "unclassified service_methods: foxprofile_ext/application.py:ProtectedApplication.expose_profile"
        in check_source_contracts(candidate)
    )


def test_adapter_signature_drift_is_specific(candidate):
    replace_source(
        candidate,
        "foxprofile_ext/runtime/contracts.py",
        "def spawn_browser(self, profile: Profile, profile_id: str, run_id: str)",
        "def spawn_browser(self, profile: Profile, profile_id: str)",
    )
    assert (
        "signature drift: foxprofile_ext/runtime/contracts.py:BrowserRuntimeAdapter.spawn_browser"
        in check_source_contracts(candidate)
    )


def test_missing_launch_admission_call_fails(candidate):
    replace_source(
        candidate,
        "foxprofile_ext/runtime/coordinator.py",
        "self.capacity.reserve(current_actor, refs, self.host_id)",
        "self.capacity.unreviewed_reserve(current_actor, refs, self.host_id)",
    )
    assert (
        "missing required call: foxprofile_ext/runtime/coordinator.py:RuntimeCoordinator.reserve_and_start -> self.capacity.reserve"
        in check_source_contracts(candidate)
    )
    with pytest.raises(RuntimeError, match="^Protected extension compatibility check failed$"):
        require_compatible_hooks(candidate)


def test_missing_confinement_call_fails(candidate):
    replace_source(
        candidate,
        "foxprofile_ext/runtime/supervisor.py",
        "build_confined_command(spec, gated_command, settings)",
        "unreviewed_command(spec, gated_command, settings)",
    )
    assert (
        "missing required call: foxprofile_ext/runtime/supervisor.py:supervise -> build_confined_command"
        in check_source_contracts(candidate)
    )


def test_non_contract_comment_change_is_compatible(candidate):
    path = candidate / "src/services/browser/process.py"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n# Harmless upstream documentation change.\n")
    assert check_source_contracts(candidate) == []


def test_protected_startup_rejects_missing_hook_before_services(candidate):
    replace_source(
        candidate,
        "foxprofile_ext/runtime/coordinator.py",
        "self.capacity.reserve(current_actor, refs, self.host_id)",
        "self.capacity.unreviewed_reserve(current_actor, refs, self.host_id)",
    )
    code = """
from pathlib import Path
from src.core.container import Container
marker = Path('services-composed')
def build(self):
    marker.touch()
    raise AssertionError('service activation must not happen')
Container._build_extension_services = build
try:
    Container()
except RuntimeError as error:
    assert str(error) == 'Protected extension compatibility check failed', str(error)
else:
    raise AssertionError('protected startup accepted missing admission hook')
assert not marker.exists()
assert not list(Path('.').glob('*.sqlite3'))
print('redacted startup rejection before services')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=candidate,
        env={**os.environ, "FOXPROFILE_EXT_ENABLED": "true", "PYTHONPATH": str(candidate)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "redacted startup rejection before services" in result.stdout


def test_disposable_cli_rehearsal_detects_and_restores_each_drift():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--rehearse"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("REJECTED:") == 4
    assert "restored disposable candidate; original checkout unchanged" in result.stdout


def test_installed_shape_without_tests_or_e2e_activates_from_unrelated_cwd(candidate, tmp_path):
    for package in ("src", "chrome_engine", "foxprofile_ext"):
        for directory in list((candidate / package).rglob("*")):
            if directory.is_dir() and directory.name in {"tests", "e2e"}:
                shutil.rmtree(directory)
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from foxprofile_ext.compatibility import ROOT, require_compatible_hooks
assert ROOT == Path(sys.argv[1])
require_compatible_hooks()
print('installed-shape activation PASS')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(candidate)],
        cwd=unrelated,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "installed-shape activation PASS" in result.stdout


def test_alias_cannot_hide_new_subprocess_factory(candidate):
    path = candidate / "src/services/browser/unreviewed.py"
    path.write_text(
        "from subprocess import Popen as unreviewed\n"
        "def create_browser():\n    return unreviewed(['browser'])\n",
        encoding="utf-8",
    )
    assert (
        "unclassified launch_calls: src/services/browser/unreviewed.py:create_browser:subprocess.Popen"
        in check_source_contracts(candidate)
    )


def test_dropping_direct_service_principal_is_signature_drift(candidate):
    replace_source(
        candidate,
        "foxprofile_ext/application.py",
        "def running_profiles(self, principal: PrincipalDTO | None)",
        "def running_profiles(self)",
    )
    assert (
        "signature drift: foxprofile_ext/application.py:ProtectedApplication.running_profiles"
        in check_source_contracts(candidate)
    )


def test_explicit_review_of_protected_route_recovers_focused_gate(candidate, protected_app):
    from fastapi import Depends

    from src.api.dependencies import get_request_principal

    protected_app.add_api_route(
        "/reviewed-profile-data", lambda: {}, dependencies=[Depends(get_request_principal)]
    )
    assert "unclassified route: HTTP GET /reviewed-profile-data" in check_compatibility(
        candidate, protected_app
    )
    record = load_register(candidate)
    added = next(
        entry
        for entry in route_inventory(protected_app)
        if entry["key"] == "HTTP GET /reviewed-profile-data"
    )
    added.update(
        classification="protected",
        purpose="Synthetic reviewed principal-required adapter; no profile side effects.",
        covering_test="tests/test_ext_compatibility_gate.py::test_explicit_review_of_protected_route_recovers_focused_gate",
    )
    record["routes"].append(added)
    (candidate / "foxprofile_ext/compatibility.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    assert check_compatibility(candidate, protected_app) == []


def test_adapted_browser_factory_requires_principal_and_admitted_coordinator(candidate):
    from foxprofile_ext.dto import PrincipalDTO, PrincipalKind
    from foxprofile_ext.errors import DomainError, ErrorCode

    path = candidate / "src/services/browser/unreviewed.py"
    path.write_text(
        "import subprocess\ndef create_browser():\n    return subprocess.Popen(['browser'])\n",
        encoding="utf-8",
    )
    assert any("unclassified launch_calls:" in item for item in check_source_contracts(candidate))
    path.write_text(
        "from foxprofile_ext.policy import require_authenticated\n"
        "def create_browser(principal, profiles, coordinator):\n"
        "    actor = require_authenticated(principal)\n"
        "    return coordinator.reserve_and_start(actor, profiles)\n",
        encoding="utf-8",
    )
    assert any("unclassified launch_calls:" in item for item in check_source_contracts(candidate))
    record = load_register(candidate)
    record["launch_calls"].append(
        "src/services/browser/unreviewed.py:create_browser:coordinator.reserve_and_start"
    )
    record["hooks"].append(
        {
            "file": "src/services/browser/unreviewed.py",
            "symbol": "create_browser",
            "purpose": "Synthetic reviewed browser facade delegates to shared admission.",
            "contract": "Principal validation precedes admission; no direct subprocess creation.",
            "covering_test": "tests/test_ext_compatibility_gate.py::test_adapted_browser_factory_requires_principal_and_admitted_coordinator",
            "baseline_commit": record["upstream"]["commit"],
            "signature": signature(ast.parse(path.read_text()).body[1]),
            "required_calls": [
                {"callee": "require_authenticated", "arguments": ["principal"]},
                {"callee": "coordinator.reserve_and_start", "arguments": ["actor", "profiles"]},
            ],
            "call_order": ["require_authenticated", "coordinator.reserve_and_start"],
        }
    )
    (candidate / "foxprofile_ext/compatibility.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    assert check_source_contracts(candidate) == []
    spec = importlib.util.spec_from_file_location("synthetic_browser_factory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    effects = []

    class AdmittedCoordinator:
        def reserve_and_start(self, principal, profiles):
            effects.append((principal, profiles))
            return "admitted"

    coordinator = AdmittedCoordinator()
    with pytest.raises(DomainError) as error:
        module.create_browser(None, (), coordinator)
    assert error.value.code is ErrorCode.AUTH_REQUIRED
    assert effects == []
    service = PrincipalDTO("synthetic-service", PrincipalKind.SERVICE)
    assert module.create_browser(service, (), coordinator) == "admitted"
    assert effects == [(service, ())]


def test_readiness_protocol_drift_is_specific(candidate):
    replace_source(
        candidate,
        "src/services/browser/runner.py",
        '_safe_print("BROWSER_STARTED")',
        '_safe_print("UNREVIEWED_READY_EVENT")',
    )
    assert (
        "protocol drift: src/services/browser/runner.py:run_browser -> BROWSER_STARTED"
        in check_source_contracts(candidate)
    )


def test_empty_admission_context_cannot_keep_existing_launch_call(candidate):
    replace_source(
        candidate,
        "foxprofile_ext/runtime/coordinator.py",
        "run_id=ref.run_id,",
        "run_id=None,",
    )
    assert (
        "missing required call: foxprofile_ext/runtime/coordinator.py:RuntimeCoordinator._prepare_and_start -> self.launcher.start_thread"
        in check_source_contracts(candidate)
    )
