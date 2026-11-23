"""Master validation and local/remote ownership regressions."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from foxprofile_ext.dto import BrowserRunRefDTO, PrincipalDTO, PrincipalKind, ProcessIdentity, Role
from foxprofile_ext.runtime.registry import RuntimeRegistry
from foxprofile_ext.storage import CapacityLedger, ExtensionDatabase, ProcessLiveness
from foxprofile_ext.workers import http
from foxprofile_ext.workers.execution import WorkerExecutionDatabase
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.placement import PlacementScheduler
from foxprofile_ext.workers.registry import WorkerRegistry
from src.api.dependencies import get_request_principal, require_cookie_csrf
from src.models.profile import Profile
from tests.test_ext_worker_agent import _worker_owner


@pytest.mark.parametrize(
    "host", ["worker:fixture-boot", "WORKER:fixture-boot", "node:fixture-boot"]
)
def test_local_hostname_worker_keeps_local_admission_and_lifecycle(tmp_path, host):
    database = WorkerExecutionDatabase(tmp_path / "worker.sqlite")
    ledger = CapacityLedger(database)
    registry = RuntimeRegistry(database, ledger)
    ref = BrowserRunRefDTO("fixture-profile", "fixture-run")
    identity = ProcessIdentity(900004, 126)
    cancelled = BrowserRunRefDTO("cancelled-profile", "cancelled-run")
    assert database.reserve_exact(cancelled, host, context=_worker_owner(cancelled))
    assert ledger.cancel_unclaimed(cancelled)
    assert database.reserve_exact(ref, host, context=_worker_owner(ref))
    assert registry.register_supervisor(ref, host, identity)
    assert ledger.mark_running(ref)
    assert ledger.mark_stopping(ref)
    assert ledger.release_after_exit(ref, identity, lambda *_: ProcessLiveness.DEAD)
    master = ExtensionDatabase(tmp_path / "master.sqlite")
    master_ledger = CapacityLedger(master)
    assert master_ledger.reserve(("existing-profile",), host).newly_reserved
    scheduler = PlacementScheduler(
        master_ledger,
        WorkerRegistry(master, "a" * 64),
        local_metrics=lambda: NodeMetrics(4096, None, 4096, ("camoufox",)),
        browser_memory_bytes=128,
        system_reserve_bytes=128,
        staging_reserve_bytes=128,
    )
    (run,) = scheduler.reserve((Profile("alpha"),), host).runs
    assert run.worker_id is None and run.host_id == host


def test_admin_revoke_and_fence_map_expected_errors(tmp_path):
    app = FastAPI()
    database = ExtensionDatabase(tmp_path / "master.sqlite")
    coordinator = SimpleNamespace(
        workers=WorkerRegistry(database, "a" * 64),
        ledger=CapacityLedger(database),
    )
    admin = PrincipalDTO("fixture-admin", PrincipalKind.PERSONAL, Role.ADMIN, "fixture-session")
    app.state.container = SimpleNamespace(
        worker_coordinator=coordinator,
        protected_application=SimpleNamespace(current_principal=lambda principal: principal),
    )
    app.include_router(http.router, prefix="/api/v1")
    app.dependency_overrides[get_request_principal] = lambda: admin
    app.dependency_overrides[require_cookie_csrf] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as client:
        absent = client.post("/api/v1/workers/absent/revoke")
        assert absent.status_code == 503
        assert absent.json()["detail"]["code"] == "WORKER_UNAVAILABLE"
        malformed = client.post("/api/v1/workers/bad%20id/revoke")
        assert malformed.status_code == 422
        blank = client.post(
            "/api/v1/workers/runs/fixture-run/fence",
            json={
                "profile_id": " ",
                "host_fenced": True,
                "evidence": "fixture host stopped",
            },
        )
        assert blank.status_code == 422
        missing = client.post(
            "/api/v1/workers/runs/fixture-run/fence",
            json={
                "profile_id": "absent-profile",
                "host_fenced": True,
                "evidence": "fixture host stopped",
            },
        )
        assert missing.status_code == 404
