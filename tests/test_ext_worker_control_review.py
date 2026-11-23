"""Distributed Stop, exact local dispatch, and manual capability preflight."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from foxprofile_ext.dto import (
    PrincipalDTO,
    PrincipalKind,
    ProcessIdentity,
    Role,
    RuntimeDescriptor,
)
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.manual.factory import build_manual_gateway
from foxprofile_ext.storage import ManualLeaseDAO, SessionDAO, UserDAO
from foxprofile_ext.storage.capacity import ProcessLiveness, RunStatus
from src.api.routes.ext_browser import BatchProfilesRequest, stop_batch
from tests.test_ext_distributed_integration import ACTOR, launch
from tests.test_ext_distributed_integration import cluster as cluster
from tests.test_ext_worker_agent import _agent


@pytest.mark.parametrize("use_batch, expected_timeout", [(True, 30), (False, 60)])
def test_remote_stop_uses_valid_worker_budget(cluster, monkeypatch, use_batch, expected_timeout):
    run = launch(cluster)
    command = cluster.hub.command
    budgets = []

    def dispatch(selected, operation, payload=None, **options):
        if operation == "stop":
            budgets.append(payload["timeout"])
            if payload["timeout"] > 60:
                raise DomainError(ErrorCode.VALIDATION_ERROR)
        return command(selected, operation, payload, **options)

    monkeypatch.setattr(cluster.hub, "command", dispatch)
    if use_batch:

        def stop_runs(actor, ids, *, timeout=30):
            assert ids == [f"id:{cluster.ref.profile_id}"]
            assert cluster.coordinator.stop(actor, cluster.ref, timeout=timeout)
            return ()

        container = SimpleNamespace(
            protected_application=SimpleNamespace(stop_runs=stop_runs),
            event_bus=SimpleNamespace(emit=lambda: None),
        )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=container)))
        stop_batch(BatchProfilesRequest(profile_ids=[cluster.ref.profile_id]), request, ACTOR)
    else:
        assert cluster.coordinator.stop(ACTOR, cluster.ref, timeout=90)
    assert budgets == [expected_timeout]
    assert cluster.ledger.get(run.ref).status is RunStatus.CLOSED
    assert cluster.store.revision(run.ref.profile_id)[0] == 2


def _local_run(cluster, running=True):
    (ref,) = cluster.ledger.reserve([cluster.ref.profile_id], "master").newly_reserved
    if running:
        assert cluster.ledger.claim(ref, "master", ProcessIdentity(101, 201))
        assert cluster.ledger.mark_running(ref)
    return cluster.ledger.get(ref)


@pytest.mark.parametrize("operation, running", [("control", True), ("stop", True), ("stop", False)])
def test_local_dispatch_keeps_first_selected_run_after_replacement(
    cluster, monkeypatch, operation, running
):
    old = _local_run(cluster, running)
    selected = []
    replacements = []
    current_run = cluster.coordinator._current_run

    def replace_after_selection(*args):
        current, run = current_run(*args)
        selected.append(run.ref)
        if run.ref == old.ref:
            if running:
                assert cluster.ledger.release_after_exit(
                    old.ref, old.process, lambda *_: ProcessLiveness.DEAD
                )
            else:
                assert cluster.ledger.cancel_unclaimed(old.ref)
            replacements.append(_local_run(cluster, running))
        return current, run

    effects = []
    monkeypatch.setattr(cluster.coordinator, "_current_run", replace_after_selection)
    cluster.local.runtime.control = lambda profile_id, run_id, *args: effects.append(run_id)

    def stop_exact(profile_id, run_id, _timeout):
        effects.append(run_id)
        return True

    cluster.local.runtime.request_stop = stop_exact
    if operation == "control":
        cluster.coordinator.control(ACTOR, cluster.ref, "screenshot")
    else:
        cluster.coordinator.stop(ACTOR, cluster.ref)
    assert selected == [old.ref]
    assert effects == [old.ref.run_id]
    (replacement,) = replacements
    assert cluster.ledger.get(replacement.ref).status is replacement.status


def _gateway_for(cluster):
    now = time.time()
    UserDAO(cluster.db).bootstrap_admin("operator", "operator", "fixture-hash")
    SessionDAO(cluster.db).create("session", "operator", "fixture-token-digest", now - 1, now + 600)
    actor = PrincipalDTO("operator", PrincipalKind.PERSONAL, Role.ADMIN, "session")
    catalog = SimpleNamespace(get_profile=lambda _: cluster.profile)
    auth = SimpleNamespace(validate_principal=lambda who: who)
    application = SimpleNamespace(resolve_profile=lambda *_: cluster.ref)
    gateway = build_manual_gateway(cluster.db, catalog, auth, application, cluster.coordinator)
    return gateway, actor


@pytest.mark.parametrize("placement", ["local", "remote", "legacy-remote"])
def test_missing_manual_backend_cannot_claim_a_helperless_lease(
    cluster, tmp_path, monkeypatch, placement
):
    monkeypatch.delenv("FOXPROFILE_X11VNC_EXECUTABLE", raising=False)
    monkeypatch.delenv("FOXPROFILE_X11VNC_SHA256", raising=False)
    run = _local_run(cluster) if placement == "local" else launch(cluster)
    descriptor = RuntimeDescriptor(
        run.ref,
        run.host_id,
        ProcessIdentity(101, 201),
        ProcessIdentity(102, 202),
        ":101",
        "pid:[456]",
        "/private/run",
        "/private/run/Xauthority",
    )
    cluster.local.runtime.verified_descriptor = lambda exact: (
        descriptor if exact == run.ref else None
    )
    worker = _agent(tmp_path, runtime=SimpleNamespace(descriptor=lambda _: descriptor))
    operations = []

    def command(selected, operation, *args, **kwargs):
        operations.append(operation)
        if operation == "describe":
            reply = worker._describe_command(selected.ref)
            if placement == "legacy-remote":
                reply.pop("manual_available", None)
            return reply
        raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)

    monkeypatch.setattr(cluster.hub, "command", command)
    gateway, actor = _gateway_for(cluster)
    try:
        with pytest.raises(DomainError) as blocked:
            gateway.service.create(actor, run.ref.profile_id)
        assert blocked.value.code is ErrorCode.MANUAL_UNSUPPORTED_RUNTIME
        assert ManualLeaseDAO(cluster.db).current() is None
        assert operations == ([] if placement == "local" else ["describe"])
    finally:
        gateway.shutdown()


def test_remote_manual_capability_belongs_to_verified_descriptor(cluster, tmp_path, monkeypatch):
    run = launch(cluster)
    descriptor = RuntimeDescriptor(
        run.ref, run.host_id, ProcessIdentity(101, 201), ProcessIdentity(102, 202)
    )
    worker = _agent(tmp_path, runtime=SimpleNamespace(descriptor=lambda _: descriptor))
    worker._manual_backend = object()
    reply = worker._describe_command(run.ref)
    assert reply["manual_available"] is True
    monkeypatch.setattr(cluster.hub, "command", lambda *a, **k: reply)
    assert cluster.coordinator.verified_remote_descriptor(run) == descriptor

    def disconnected(_run):
        raise DomainError(ErrorCode.WORKER_UNAVAILABLE)

    monkeypatch.setattr(cluster.hub, "connection_for", disconnected)
    with pytest.raises(DomainError) as stale:
        cluster.coordinator.verified_remote_descriptor(run)
    assert stale.value.code is ErrorCode.WORKER_UNAVAILABLE


def test_manual_authorization_rechecks_worker_runtime_after_first_descriptor(
    cluster, tmp_path, monkeypatch
):
    run = launch(cluster)
    descriptor = RuntimeDescriptor(
        run.ref, run.host_id, ProcessIdentity(101, 201), ProcessIdentity(102, 202)
    )
    current = [descriptor]
    worker = _agent(tmp_path, runtime=SimpleNamespace(descriptor=lambda _: current[0]))
    worker._manual_backend = object()
    monkeypatch.setattr(
        cluster.hub, "command", lambda selected, *a, **k: worker._describe_command(selected.ref)
    )
    assert cluster.coordinator.verified_remote_descriptor(run) == descriptor
    current[0] = None
    with pytest.raises(DomainError) as stale:
        cluster.coordinator.verified_remote_descriptor(run)
    assert stale.value.code is ErrorCode.STALE_BROWSER_RUN


def test_worker_manual_heartbeat_does_not_renew_unverified_runtime(tmp_path):
    from foxprofile_ext.dto import BrowserRunRefDTO

    ref = BrowserRunRefDTO("profile", "run")
    renewed = []
    worker = _agent(tmp_path, runtime=SimpleNamespace(descriptor=lambda _: None))
    worker._manual_backend = SimpleNamespace(heartbeat=lambda *args: renewed.append(args))
    worker._manual["manual-session"] = SimpleNamespace(run=ref, closing=False, descriptor=object())
    with pytest.raises(DomainError) as stale:
        asyncio.run(
            worker._manual_command(
                "manual_heartbeat",
                ref,
                {"session_id": "manual-session", "watchdog_deadline": time.time() + 10},
            )
        )
    assert stale.value.code is ErrorCode.MANUAL_SESSION_CLOSED
    assert renewed == []


def test_supported_remote_open_without_reply_keeps_lease_in_recovery(
    cluster, tmp_path, monkeypatch
):
    run = launch(cluster)
    descriptor = RuntimeDescriptor(
        run.ref,
        run.host_id,
        ProcessIdentity(101, 201),
        ProcessIdentity(102, 202),
        ":101",
        "pid:[456]",
        "/private/run",
        "/private/run/Xauthority",
    )
    worker = _agent(tmp_path, runtime=SimpleNamespace(descriptor=lambda _: descriptor))
    worker._manual_backend = object()
    commands = []

    def command(selected, operation, *args, **kwargs):
        commands.append(operation)
        if operation == "describe":
            return worker._describe_command(selected.ref)
        raise DomainError(ErrorCode.WORKER_UNAVAILABLE)

    monkeypatch.setattr(cluster.hub, "command", command)
    gateway, actor = _gateway_for(cluster)
    try:
        with pytest.raises(DomainError) as unavailable:
            gateway.service.create(actor, run.ref.profile_id)
        assert unavailable.value.code is ErrorCode.RUNTIME_RECOVERING
        lease = ManualLeaseDAO(cluster.db).current()
        assert lease.browser_run_id == run.ref.run_id and lease.state.value == "recovering"
        assert lease.helper is None and "manual_open" in commands and "manual_probe" in commands
    finally:
        gateway.shutdown()
