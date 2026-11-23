from dataclasses import asdict

import flet as ft
import pytest

from foxprofile_ext.dto import Role
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.workers.metrics import NodeMetrics
from foxprofile_ext.workers.placement import PlacementScheduler
from foxprofile_ext.workers.registry import WorkerRegistry
from src.core.strings import get_string, set_language
from tests.test_ext_access import access as access
from tests.test_ext_desktop import button, controls, login

pytest_plugins = ["tests.test_ext_desktop"]


def worker_registry(access):
    registry = WorkerRegistry(access["ledger"].database, "a" * 64)
    credential = registry.enroll("worker-fixture")
    worker = registry.register(
        "worker-fixture", credential, "boot-fixture", "a" * 64, preflight_ok=True
    )
    registry.heartbeat(
        worker.worker_id,
        worker.boot_id,
        worker.connection_generation,
        NodeMetrics(8 << 30, None, 8 << 30, ("camoufox",)),
    )
    return registry, credential


def test_worker_admin_facade_returns_redacted_web_shape(access):
    registry, credential = worker_registry(access)
    coordinator = access["coordinator"]
    coordinator.workers = registry
    coordinator.ledger = access["ledger"]
    profile = next(p for p in access["catalog"].list_profiles() if p.name == "Profile A")
    run = (
        PlacementScheduler(
            access["ledger"],
            registry,
            local_metrics=lambda: NodeMetrics(0, None, 8 << 30, ("camoufox",)),
            browser_memory_bytes=1 << 30,
            system_reserve_bytes=1 << 29,
        )
        .reserve((profile,), "master")
        .runs[0]
    )

    result = access["app"].admin_workers(access["admin"])

    worker = result["workers"][0]
    assert set(result) == {"workers"}
    assert set(worker) == {
        "worker_id",
        "state",
        "draining",
        "last_seen",
        "manifest_digest",
        "metrics",
        "runs",
    }
    assert worker["worker_id"] == "worker-fixture"
    assert worker["metrics"] == asdict(registry.list()[0].metrics)
    assert worker["runs"] == [
        {
            "profile_id": profile.profile_id,
            "run_id": run.ref.run_id,
            "state": "reserved",
            "reserved_memory_bytes": 1 << 30,
        }
    ]
    assert "boot-fixture" not in repr(result)
    assert credential not in repr(result)
    assert "connection_generation" not in repr(result)


def test_worker_admin_facade_requires_current_personal_admin_and_distributed_mode(access):
    app = access["app"]
    assert app.admin_workers(access["admin"]) is None
    with pytest.raises(DomainError) as disabled:
        app.set_worker_draining(access["admin"], "worker-fixture", True)
    assert disabled.value.code is ErrorCode.WORKER_UNAVAILABLE

    for principal in (access["user_a"], access["service"]):
        with pytest.raises(DomainError) as denied:
            app.admin_workers(principal)
        assert denied.value.code is ErrorCode.ADMIN_REQUIRED
        with pytest.raises(DomainError) as denied_mutation:
            app.set_worker_draining(principal, "worker-fixture", True)
        assert denied_mutation.value.code is ErrorCode.ADMIN_REQUIRED

    registry, _ = worker_registry(access)
    access["coordinator"].workers = registry
    access["coordinator"].ledger = access["ledger"]
    with pytest.raises(DomainError) as invalid:
        app.set_worker_draining(access["admin"], "worker-fixture", 1)
    assert invalid.value.code is ErrorCode.VALIDATION_ERROR


def test_worker_admin_facade_rechecks_revoked_admin(access):
    app = access["app"]
    registry, _ = worker_registry(access)
    access["coordinator"].workers = registry
    access["coordinator"].ledger = access["ledger"]
    access["auth"].create_user(access["admin"], "backup-admin", "backup password", Role.ADMIN)
    access["auth"].update_user_state(access["admin"], "admin-0", active=False)

    with pytest.raises(DomainError) as inventory:
        app.admin_workers(access["admin"])
    assert inventory.value.code is ErrorCode.AUTH_REQUIRED
    with pytest.raises(DomainError) as mutation:
        app.set_worker_draining(access["admin"], "worker-fixture", True)
    assert mutation.value.code is ErrorCode.AUTH_REQUIRED
    assert registry.list()[0].draining is False


def test_admin_dialog_drains_worker_and_revoked_callback_clears_session(desktop, monkeypatch):
    import time

    from foxprofile_ext.storage import SettingsDAO

    instance, page, access = desktop
    access["app"]._settings = SettingsDAO(access["ledger"].database)
    registry, _ = worker_registry(access)
    access["coordinator"].workers = registry
    access["coordinator"].ledger = access["ledger"]
    access["auth"].create_user(access["admin"], "backup-admin", "backup password", Role.ADMIN)
    set_language("en")
    login(instance, page, "admin", "admin password")
    binding = instance.personal.session.binding

    instance.personal.open_admin(binding)
    assert page.dialogs, instance.state.get_all_log_lines()
    dialog = page.dialogs[-1]
    assert get_string("personal_workers") in [
        control.value for control in controls(dialog) if isinstance(control, ft.Text)
    ]
    button(dialog, get_string("personal_worker_drain")).on_click(None)
    assert registry.list()[0].draining is True
    refreshed = page.dialogs[-1]
    resume = button(refreshed, get_string("personal_worker_resume"))
    monkeypatch.setattr(access["auth"], "_clock", lambda: time.time() + 13 * 3600)

    resume.on_click(None)

    assert registry.list()[0].draining is True
    assert instance.personal.session.binding is None
    assert not any(dialog.open for dialog in page.dialogs)

    login(instance, page, "backup-admin", "backup password")
    resume.on_click(None)
    assert registry.list()[0].draining is True
    assert instance.personal.session.binding is not None
