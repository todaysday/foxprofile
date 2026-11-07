"""Admission and recovery checks against SQLite and controlled OS children."""

import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxprofile_ext.capacity import CapacityService
from foxprofile_ext.dto import (
    BrowserRunRefDTO,
    PrincipalDTO,
    PrincipalKind,
    ProcessIdentity,
    ProfileRefDTO,
)
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.locking import catalog_lock
from foxprofile_ext.manual import helper as manual_helper
from foxprofile_ext.request_context import principal_scope
from foxprofile_ext.runtime.coordinator import RuntimeCoordinator
from foxprofile_ext.runtime.processes import (
    RuntimeSupervisor,
    _process_group_liveness,
    _runtime_liveness,
    _signal_group,
)
from foxprofile_ext.runtime.registry import probe_process, process_identity
from foxprofile_ext.storage import AssignmentDAO, CapacityLedger, ExtensionDatabase, SettingsDAO
from foxprofile_ext.storage.capacity import ProcessLiveness, RunStatus
from src.models.profile import Profile
from src.services.browser.launcher import BrowserLauncher

SERVICE = PrincipalDTO("test-service", PrincipalKind.SERVICE)
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="RuntimeSupervisor requires Linux process namespaces and /proc identities",
)


@pytest.fixture
def setup(tmp_path):
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    ledger = CapacityLedger(database)
    capacity = CapacityService(ledger, AssignmentDAO(database))
    profiles = [Profile(name, os_type="linux") for name in ("alpha", "beta", "gamma")]
    data = tmp_path / "data"
    for profile in profiles:
        (data / profile.name).mkdir(parents=True)
    runtime = RuntimeSupervisor(
        database,
        data_dir=data,
        code_root=Path.cwd(),
        runtime_paths=(Path(sys.prefix),),
        capacity=capacity,
        profile_by_name=lambda name: next(p for p in profiles if p.name == name),
        authorize=lambda actor, ref: actor,
        lock_path=str(tmp_path / "catalog.lock"),
    )
    return SimpleNamespace(
        database=database,
        ledger=ledger,
        capacity=capacity,
        profiles=profiles,
        runtime=runtime,
        lock=str(tmp_path / "catalog.lock"),
    )


def coordinator(setup, launcher, authorize=lambda actor, ref: actor):
    return RuntimeCoordinator(
        setup.capacity,
        setup.ledger,
        setup.runtime,
        launcher,
        profile_by_id=lambda identifier: next(
            p for p in setup.profiles if p.profile_id == identifier
        ),
        authorize=authorize,
        lock_path=setup.lock,
    )


def refs(setup):
    return tuple(ProfileRefDTO(p.profile_id, p.name) for p in setup.profiles)


def test_batch_preflight_forbidden_or_full_starts_nothing(setup):
    starts = []
    launcher = SimpleNamespace(start_thread=lambda *a, **k: starts.append(a))

    def authorize(actor, ref):
        if ref.name == "beta":
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return actor

    with pytest.raises(DomainError):
        coordinator(setup, launcher, authorize).reserve_and_start(SERVICE, refs(setup)[:2])
    assert not starts and not setup.ledger.active_runs()
    SettingsDAO(setup.database).set_capacity(1)
    with pytest.raises(DomainError) as error:
        coordinator(setup, launcher).reserve_and_start(SERVICE, refs(setup)[:2])
    assert error.value.code is ErrorCode.PROFILE_CAPACITY_EXCEEDED
    assert not starts and not setup.ledger.active_runs()


def test_duplicate_and_existing_run_only_start_newly_reserved(setup):
    starts = []
    existing = setup.ledger.reserve((setup.profiles[0].profile_id,), setup.runtime.host_id).runs[0]
    launcher = SimpleNamespace(start_thread=lambda p, *a, **k: starts.append(p.name) or True)
    results = coordinator(setup, launcher).reserve_and_start(
        SERVICE, [*refs(setup)[:2], refs(setup)[1]]
    )
    assert starts == ["beta"]
    assert len(results) == 2 and results[0].browser_run_id == existing.ref.run_id
    assert len(setup.ledger.active_runs()) == 2


@pytest.mark.parametrize("wait", [False, True])
def test_actual_launcher_swallowed_spawn_failure_releases_unclaimed(setup, monkeypatch, wait):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")

    def fail(*args):
        raise OSError("stand-in startup failure")

    monkeypatch.setattr(setup.runtime, "spawn_browser", fail)
    launcher = BrowserLauncher(setup.runtime)
    result = coordinator(setup, launcher).reserve_and_start(SERVICE, refs(setup)[:1], wait=wait)
    assert result[0].state == "failed"
    assert result[0].error_code == "LAUNCH_FAILED"
    assert not setup.ledger.active_runs()


def test_preparation_failure_releases_admitted_run_without_spawn(setup, monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")

    def fail(_profile):
        raise OSError("controlled preparation failure")

    setup.runtime.prepare_launch = fail
    monkeypatch.setattr(
        "foxprofile_ext.runtime.processes.subprocess.Popen",
        lambda *a, **k: pytest.fail("failed preparation spawned a supervisor"),
    )
    result = coordinator(setup, BrowserLauncher(setup.runtime)).reserve_and_start(
        SERVICE, refs(setup)[:1]
    )
    assert result[0].state == "failed"
    assert not setup.ledger.active_runs()


@pytest.mark.parametrize("claimed", [False, True])
def test_failed_supervisor_watcher_keeps_only_unconfirmed_claims_owned(setup, monkeypatch, claimed):
    from foxprofile_ext.runtime import processes as runtime_processes

    ref = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    identity = ProcessIdentity(700010, 81)
    spawned = []

    class Stdin:
        def write(self, _data):
            if claimed:
                assert setup.ledger.claim(ref, setup.runtime.host_id, identity)

        def close(self):
            return None

    class Process:
        def __init__(self):
            self.stdin = Stdin()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    def popen(*_args, **_kwargs):
        process = Process()
        spawned.append(process)
        return process

    def fail_start(_thread):
        raise RuntimeError("watcher start failed")

    monkeypatch.setattr(runtime_processes.subprocess, "Popen", popen)
    monkeypatch.setattr(
        runtime_processes.threading.Thread,
        "start",
        fail_start,
    )
    if claimed:
        monkeypatch.setattr(setup.runtime, "_probe_run", lambda *_: ProcessLiveness.LIVE)

    with pytest.raises(RuntimeError, match="watcher start failed"):
        setup.runtime.spawn_browser(setup.profiles[0], ref.profile_id, ref.run_id)

    assert spawned and spawned[0].poll() == 0
    if claimed:
        assert setup.ledger.get(ref).status is RunStatus.STARTING
        assert setup.runtime._owned_runs[ref.run_id] == ref
        assert (setup.runtime.data_dir / ".foxprofile-runtime" / ref.run_id).is_dir()
        monkeypatch.setattr(setup.runtime, "_probe_run", lambda *_: ProcessLiveness.DEAD)
        setup.runtime._watch(ref, spawned[0])
        assert setup.ledger.get(ref).status is RunStatus.CLOSED
    else:
        assert setup.ledger.get(ref).status is RunStatus.CANCELLED
    assert ref.run_id not in setup.runtime._owned_runs
    assert not (setup.runtime.data_dir / ".foxprofile-runtime" / ref.run_id).exists()
    setup.runtime.shutdown(0)


def test_partial_batch_failure_preserves_success_and_wait_timeout_capacity(setup):
    def start(profile, log, **options):
        if profile.name == "beta":
            options["on_stop"]()
        return True

    launcher = SimpleNamespace(start_thread=start, wait_for_launch=lambda *a: (False, "timeout"))
    results = coordinator(setup, launcher).reserve_and_start(
        SERVICE, refs(setup)[:2], wait=True, timeout=0
    )
    assert [r.state for r in results] == ["starting", "failed"]
    assert [r.ref.profile_id for r in setup.ledger.active_runs()] == [setup.profiles[0].profile_id]


def test_wait_does_not_hold_catalog_lock(setup):
    acquired = threading.Event()

    def wait(*args):
        def take_lock():
            with catalog_lock(setup.lock):
                acquired.set()

        thread = threading.Thread(target=take_lock)
        thread.start()
        assert acquired.wait(2)
        thread.join()
        return False, "timeout"

    launcher = SimpleNamespace(start_thread=lambda *a, **k: True, wait_for_launch=wait)
    coordinator(setup, launcher).reserve_and_start(SERVICE, refs(setup)[:1], wait=True)


def test_launch_preparation_does_not_hold_catalog_lock(setup):
    preparing = threading.Event()
    resume = threading.Event()
    acquired = threading.Event()
    starts = []

    def prepare(_profile):
        preparing.set()
        assert resume.wait(5)

    def start(profile, *args, **kwargs):
        starts.append(profile.name)
        return True

    def take_lock():
        with catalog_lock(setup.lock):
            acquired.set()

    setup.runtime.prepare_launch = prepare
    launcher = SimpleNamespace(start_thread=start)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            coordinator(setup, launcher).reserve_and_start(SERVICE, refs(setup)[:1])
        )
    )
    thread.start()
    try:
        assert preparing.wait(2)
        lock_thread = threading.Thread(target=take_lock)
        lock_thread.start()
        assert acquired.wait(2)
        lock_thread.join(2)
    finally:
        resume.set()
        thread.join(5)
        setup.runtime.shutdown(0)
    assert not thread.is_alive()
    assert starts == ["alpha"]
    assert results[0][0].state == "starting"


@pytest.mark.parametrize("change", ["stop", "revoke", "rename", "delete"])
def test_preparation_revalidates_exact_reservation_and_profile_grant(setup, change):
    preparing = threading.Event()
    resume = threading.Event()
    revoked = False
    deleted = False
    starts = []

    def prepare(_profile):
        preparing.set()
        assert resume.wait(5)

    def profile_by_id(profile_id):
        if deleted:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return next(profile for profile in setup.profiles if profile.profile_id == profile_id)

    def authorize(actor, _ref):
        if revoked:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return actor

    launcher = SimpleNamespace(
        start_thread=lambda profile, *args, **kwargs: starts.append(profile.name) or True
    )
    run_coordinator = RuntimeCoordinator(
        setup.capacity,
        setup.ledger,
        setup.runtime,
        launcher,
        profile_by_id=profile_by_id,
        authorize=authorize,
        lock_path=setup.lock,
    )
    setup.runtime.prepare_launch = prepare
    results = []
    thread = threading.Thread(
        target=lambda: results.append(run_coordinator.reserve_and_start(SERVICE, refs(setup)[:1]))
    )
    thread.start()
    try:
        assert preparing.wait(2)
        if change == "stop":
            assert run_coordinator.stop(SERVICE, refs(setup)[0])
        elif change == "revoke":
            revoked = True
        elif change == "rename":
            setup.profiles[0].name = "renamed"
        else:
            deleted = True
        resume.set()
        thread.join(5)
    finally:
        resume.set()
        thread.join(5)
        setup.runtime.shutdown(0)
    assert not thread.is_alive()
    assert starts == []
    assert results[0][0].state == "failed"
    assert (
        setup.ledger.get(
            BrowserRunRefDTO(refs(setup)[0].profile_id, results[0][0].browser_run_id)
        ).status
        is RunStatus.CANCELLED
    )


def test_wait_returns_the_exact_closed_generation_after_ready(setup):
    identity = ProcessIdentity(700001, 77)
    callbacks = {}

    def start(profile, _log, *, on_ready, run_id, profile_id, **_kwargs):
        ref = BrowserRunRefDTO(profile_id, run_id)
        assert setup.ledger.claim(ref, setup.runtime.host_id, identity)
        callbacks[run_id] = on_ready
        return True

    def wait(_name, _timeout, run_id):
        ref = BrowserRunRefDTO(setup.profiles[0].profile_id, run_id)
        callbacks[run_id]()
        assert setup.ledger.mark_stopping(ref)
        assert setup.ledger.release_after_exit(ref, identity, lambda *_: ProcessLiveness.DEAD)
        return True, None

    result = coordinator(
        setup, SimpleNamespace(start_thread=start, wait_for_launch=wait)
    ).reserve_and_start(SERVICE, refs(setup)[:1], wait=True)

    assert result[0].state == "failed"
    assert result[0].error_code == "LAUNCH_FAILED"
    assert (
        setup.ledger.get(BrowserRunRefDTO(result[0].profile_id, result[0].browser_run_id)).status
        is RunStatus.CLOSED
    )


def test_control_releases_catalog_lock_and_keeps_exact_generation(setup, monkeypatch):
    old = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    identity = ProcessIdentity(700002, 78)
    assert setup.ledger.claim(old, setup.runtime.host_id, identity)
    assert setup.ledger.mark_running(old)
    entered = threading.Event()
    resume = threading.Event()
    acquired = threading.Event()
    calls = []

    def control(profile_id, run_id, action, params):
        calls.append((profile_id, run_id, action, params))
        entered.set()
        assert resume.wait(5)
        return "controlled"

    monkeypatch.setattr(setup.runtime, "control", control)
    run_coordinator = coordinator(setup, SimpleNamespace())
    results = []
    thread = threading.Thread(
        target=lambda: results.append(run_coordinator.control(SERVICE, refs(setup)[0], "click"))
    )
    thread.start()
    try:
        assert entered.wait(2)

        def take_lock():
            with catalog_lock(setup.lock):
                acquired.set()

        lock_thread = threading.Thread(target=take_lock)
        lock_thread.start()
        assert acquired.wait(2)
        lock_thread.join(2)
        assert setup.ledger.mark_stopping(old)
        assert setup.ledger.release_after_exit(old, identity, lambda *_: ProcessLiveness.DEAD)
        replacement = setup.ledger.reserve((old.profile_id,), setup.runtime.host_id).newly_reserved[
            0
        ]
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results == ["controlled"]
    assert calls == [(old.profile_id, old.run_id, "click", None)]
    assert replacement.run_id != old.run_id
    setup.ledger.cancel_unclaimed(replacement)
    setup.runtime.shutdown(0)


def test_runtime_stop_backoff_reprobes_live_then_dead_orphan(setup, monkeypatch):
    from foxprofile_ext.runtime import processes as runtime_processes

    ref = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    identity = ProcessIdentity(700003, 79)
    assert setup.ledger.claim(ref, setup.runtime.host_id, identity)
    assert setup.ledger.mark_running(ref)
    descriptor = SimpleNamespace(
        host_id=setup.runtime.host_id,
        supervisor=identity,
        boundary=None,
        namespace=None,
    )
    monkeypatch.setattr(
        setup.runtime.registry, "get", lambda _ref: SimpleNamespace(descriptor=descriptor)
    )
    monkeypatch.setattr(runtime_processes, "probe_process", lambda *_: ProcessLiveness.LIVE)
    monkeypatch.setattr(runtime_processes, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(runtime_processes.os, "kill", lambda *_: None)
    liveness = iter(
        (
            ProcessLiveness.LIVE,
            ProcessLiveness.LIVE,
            ProcessLiveness.DEAD,
            ProcessLiveness.DEAD,
        )
    )
    probes = []

    def probe(*_args):
        probes.append(True)
        return next(liveness)

    monkeypatch.setattr(setup.runtime, "_probe_run", probe)

    class Clock:
        now = 100.0
        sleeps = []

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, delay):
            cls.sleeps.append(delay)
            cls.now += delay

    Clock.sleeps = []
    monkeypatch.setattr(runtime_processes, "time", Clock)
    try:
        assert setup.runtime.request_stop(ref.profile_id, ref.run_id, 1)
    finally:
        setup.runtime.shutdown(0)
    assert Clock.sleeps == pytest.approx([0.05, 0.1])
    assert len(probes) == 4
    assert setup.ledger.get(ref).status is RunStatus.CLOSED


def test_runtime_stop_timeout_is_bounded_by_deadline(setup, monkeypatch):
    from foxprofile_ext.runtime import processes as runtime_processes

    ref = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    identity = ProcessIdentity(700004, 80)
    assert setup.ledger.claim(ref, setup.runtime.host_id, identity)
    assert setup.ledger.mark_running(ref)
    descriptor = SimpleNamespace(
        host_id=setup.runtime.host_id,
        supervisor=identity,
        boundary=None,
        namespace=None,
    )
    monkeypatch.setattr(
        setup.runtime.registry, "get", lambda _ref: SimpleNamespace(descriptor=descriptor)
    )
    monkeypatch.setattr(runtime_processes, "probe_process", lambda *_: ProcessLiveness.LIVE)
    monkeypatch.setattr(runtime_processes, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(runtime_processes.os, "kill", lambda *_: None)
    monkeypatch.setattr(setup.runtime, "_probe_run", lambda *_: ProcessLiveness.LIVE)

    class Clock:
        now = 0.0
        sleeps = []

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, delay):
            cls.sleeps.append(delay)
            cls.now += delay

    Clock.sleeps = []
    monkeypatch.setattr(runtime_processes, "time", Clock)
    try:
        assert not setup.runtime.request_stop(ref.profile_id, ref.run_id, 0.15)
    finally:
        setup.runtime.shutdown(0)
    assert Clock.sleeps == pytest.approx([0.05, 0.1])
    assert Clock.now == pytest.approx(0.15)


def test_request_stop_honors_real_timeout_boundary(setup, monkeypatch):
    from foxprofile_ext.runtime import processes as runtime_processes

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    original_kill = os.kill
    try:
        identity = process_identity(child.pid)
        assert identity is not None
        ref = setup.ledger.reserve(
            (setup.profiles[0].profile_id,), setup.runtime.host_id
        ).newly_reserved[0]
        assert setup.ledger.claim(ref, setup.runtime.host_id, identity)
        assert setup.ledger.mark_running(ref)
        descriptor = SimpleNamespace(
            host_id=setup.runtime.host_id,
            supervisor=identity,
            boundary=None,
            namespace=None,
        )
        monkeypatch.setattr(
            setup.runtime.registry,
            "get",
            lambda _ref: SimpleNamespace(descriptor=descriptor),
        )
        monkeypatch.setattr(runtime_processes.os, "kill", lambda *_: None)
        started = time.monotonic()
        assert not setup.runtime.request_stop(ref.profile_id, ref.run_id, 0.15)
        elapsed = time.monotonic() - started
        assert 0.14 <= elapsed < 0.5
        assert child.poll() is None
    finally:
        monkeypatch.setattr(runtime_processes.os, "kill", original_kill)
        child.terminate()
        child.wait(timeout=5)
        setup.runtime.shutdown(0)


def test_manual_runtime_liveness_rejects_unreaped_zombie():
    if not hasattr(os, "fork"):
        pytest.skip("requires Linux /proc process state")
    child = os.fork()
    if child == 0:
        os._exit(0)
    try:
        deadline = time.monotonic() + 2
        identity = None
        while time.monotonic() < deadline:
            identity = process_identity(child)
            if identity is not None:
                state = Path(f"/proc/{child}/stat").read_text(encoding="ascii")
                if state[state.rfind(")") + 2 :].split()[0] == "Z":
                    break
            time.sleep(0.01)
        assert identity is not None
        assert state[state.rfind(")") + 2 :].split()[0] == "Z"
        host_id = manual_helper.current_host_id()
        assert manual_helper._alive([child, identity.start_ticks])
        assert (
            manual_helper._runtime_alive(
                {"host_id": host_id, "runtime_processes": [[child, identity.start_ticks]]}
            )
            is False
        )
        current = process_identity(os.getpid())
        assert current is not None
        assert manual_helper._runtime_alive(
            {"host_id": host_id, "runtime_processes": [[os.getpid(), current.start_ticks]]}
        )
    finally:
        os.waitpid(child, 0)


def test_registry_claim_atomic_and_late_cancelled_child_cannot_claim(setup):
    ref = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    identity = process_identity(os.getpid())
    assert setup.runtime.registry.register_supervisor(ref, setup.runtime.host_id, identity)
    with setup.database.read() as connection:
        row = connection.execute(
            "SELECT * FROM runtime_descriptors WHERE run_id = ?", (ref.run_id,)
        ).fetchone()
    assert row["supervisor_pid"] == identity.pid
    assert not setup.runtime.registry.register_supervisor(ref, setup.runtime.host_id, identity)
    second = setup.ledger.reserve(
        (setup.profiles[1].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    assert setup.ledger.cancel_unclaimed(second)
    assert not setup.runtime.registry.register_supervisor(second, setup.runtime.host_id, identity)


def test_missing_descriptor_after_supervisor_death_stays_counted_and_blocks_admission(setup):
    ref = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    assert setup.ledger.claim(ref, setup.runtime.host_id, ProcessIdentity(99999999, 0))
    report = setup.runtime.recover()
    assert report.uncertain == 1
    assert len(setup.ledger.active_runs()) == 1
    with pytest.raises(DomainError) as error:
        setup.ledger.reserve((setup.profiles[1].profile_id,), setup.runtime.host_id)
    assert error.value.code is ErrorCode.RUNTIME_RECOVERING


def test_foreign_host_or_unknown_identity_is_never_signalled(setup, monkeypatch):
    ref = setup.ledger.reserve((setup.profiles[0].profile_id,), "other-boot").newly_reserved[0]
    identity = process_identity(os.getpid())
    assert setup.runtime.registry.register_supervisor(ref, "other-boot", identity)
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("foreign process signalled"))
    assert not setup.runtime.request_stop(ref.profile_id, ref.run_id, 0)
    assert setup.runtime.recover().uncertain == 1


def test_pid_generation_guard_prevents_foreign_group_signal(setup, monkeypatch):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        identity = process_identity(child.pid)
        wrong = ProcessIdentity(child.pid, identity.start_ticks + 1)
        monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("reused PID group signalled"))
        assert not _signal_group(wrong, signal.SIGTERM)
        assert _process_group_liveness(wrong) is ProcessLiveness.UNKNOWN
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_helper_no_context_full_capacity_and_parameter_mismatch_spawn_nothing(setup, monkeypatch):
    monkeypatch.setattr(setup.runtime, "_spawn", lambda *a, **k: pytest.fail("helper spawned"))
    with pytest.raises(DomainError) as error:
        setup.runtime.run_helper("export", "alpha", "linux", "camoufox", "", 5)
    assert error.value.code is ErrorCode.AUTH_REQUIRED
    with principal_scope(SERVICE):
        with pytest.raises(DomainError) as error:
            setup.runtime.run_helper("export", "alpha", "windows", "camoufox", "", 5)
        assert error.value.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND
        SettingsDAO(setup.database).set_capacity(1)
        setup.ledger.reserve((setup.profiles[1].profile_id,), setup.runtime.host_id)
        with pytest.raises(DomainError) as error:
            setup.runtime.run_helper("export", "alpha", "linux", "camoufox", "", 5)
        assert error.value.code is ErrorCode.PROFILE_CAPACITY_EXCEEDED
    assert len(setup.ledger.active_runs()) == 1


def test_helper_admitted_before_controlled_child_and_private_result(setup, monkeypatch, caplog):
    def spawn(profile, profile_id, run_id, command, *, helper_stdin):
        assert (
            setup.ledger.get(
                next(r.ref for r in setup.ledger.active_runs() if r.ref.run_id == run_id)
            ).status
            is RunStatus.RESERVED
        )
        assert helper_stdin == "private-cookie-payload"
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                'print(\'RESULT:{"cookies":[{"value":"cookie-secret-canary"}]}\')',
            ],
            stdout=subprocess.PIPE,
            text=True,
        )

    monkeypatch.setattr(setup.runtime, "_spawn", spawn)
    with principal_scope(SERVICE):
        result = setup.runtime.run_helper(
            "export", "alpha", "linux", "camoufox", "private-cookie-payload", 5
        )
    assert result["cookies"][0]["value"] == "cookie-secret-canary"
    assert "cookie-secret-canary" not in caplog.text
    assert not setup.ledger.active_runs()


def test_shutdown_preserves_independent_manager_same_host_run(setup, monkeypatch):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        ref = setup.ledger.reserve(
            (setup.profiles[0].profile_id,), setup.runtime.host_id
        ).newly_reserved[0]
        assert setup.runtime.registry.register_supervisor(
            ref, setup.runtime.host_id, process_identity(child.pid)
        )
        # This instance did not spawn/adopt ownership of that generation.
        setup.runtime.shutdown(1)
        assert child.poll() is None
        assert len(setup.ledger.active_runs()) == 1
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_group_signal_rejects_nonleader_pid(setup, monkeypatch):
    identity = process_identity(os.getpid())
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(
        os, "killpg", lambda *args: pytest.fail("non-owned process group signalled")
    )
    assert not _signal_group(identity, signal.SIGTERM)


def test_runtime_paths_preserve_lexical_venv_and_resolver_aliases(setup):
    runtime = RuntimeSupervisor(
        ExtensionDatabase(setup.runtime.data_dir / "second.sqlite3"),
        data_dir=setup.runtime.data_dir,
        code_root=Path.cwd(),
        runtime_paths=(Path(sys.executable), Path("/etc/resolv.conf")),
    )
    assert runtime.runtime_paths == (sys.executable, "/etc/resolv.conf")


def test_single_lifecycle_manager_lock_is_not_inherited_and_releases_on_shutdown(setup):
    assert not os.get_inheritable(setup.runtime._manager_file.fileno())
    with pytest.raises(RuntimeError, match="Another lifecycle manager"):
        RuntimeSupervisor(
            setup.database,
            data_dir=setup.runtime.data_dir,
            code_root=Path.cwd(),
            runtime_paths=(Path(sys.prefix),),
        )
    code = (
        "from pathlib import Path; from foxprofile_ext.runtime.processes import RuntimeSupervisor; "
        "from foxprofile_ext.storage import ExtensionDatabase; "
        f"RuntimeSupervisor(ExtensionDatabase({str(setup.database.path)!r}),data_dir={str(setup.runtime.data_dir)!r},code_root={str(Path.cwd())!r},runtime_paths=('/usr',))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0 and "Another lifecycle manager" in result.stderr
    setup.runtime.shutdown(0)
    replacement = RuntimeSupervisor(
        setup.database,
        data_dir=setup.runtime.data_dir,
        code_root=Path.cwd(),
        runtime_paths=(Path(sys.prefix),),
    )
    replacement.shutdown(0)


@pytest.mark.parametrize("boundary_state", list(ProcessLiveness))
def test_unreaped_supervisor_checks_boundary_before_recovery(setup, monkeypatch, boundary_state):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        identity = process_identity(child.pid)
        ref = setup.ledger.reserve(
            (setup.profiles[0].profile_id,), setup.runtime.host_id
        ).newly_reserved[0]
        assert setup.runtime.registry.register_supervisor(ref, setup.runtime.host_id, identity)
        boundary = ProcessIdentity(99999999, 1)
        assert setup.runtime.registry.publish_boundary(ref, identity, boundary)
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert Path(f"/proc/{child.pid}/stat").read_text().split(") ", 1)[1].startswith("Z ")
        assert probe_process(setup.runtime.host_id, identity) is ProcessLiveness.DEAD
        monkeypatch.setattr(
            "foxprofile_ext.runtime.processes._runtime_liveness", lambda *_: boundary_state
        )
        assert setup.runtime._probe_run(setup.runtime.host_id, identity) is boundary_state
        setup.runtime.recover()
        assert bool(setup.ledger.active_runs()) is (boundary_state is not ProcessLiveness.DEAD)
    finally:
        child.wait(timeout=5)


@pytest.mark.parametrize("owner", ["foreign", "same", "unknown"])
def test_unreadable_proc_entry_only_blocks_our_runtime_uid(monkeypatch, owner):
    identity = ProcessIdentity(99999999, 1)
    entry = Path("/proc/88888888")
    read_text = Path.read_text
    stat = Path.stat

    def proc_read(path, *args, **kwargs):
        if path == entry / "stat":
            raise PermissionError("controlled unreadable process")
        return read_text(path, *args, **kwargs)

    def proc_stat(path, *args, **kwargs):
        if path != entry:
            return stat(path, *args, **kwargs)
        if owner == "unknown":
            raise PermissionError("controlled unreadable owner")
        return SimpleNamespace(st_uid=os.getuid() if owner == "same" else os.getuid() + 1)

    monkeypatch.setattr("foxprofile_ext.runtime.processes.process_identity", lambda _: None)
    monkeypatch.setattr(Path, "iterdir", lambda _: iter((entry,)))
    monkeypatch.setattr(Path, "read_text", proc_read)
    monkeypatch.setattr(Path, "stat", proc_stat)
    expected = ProcessLiveness.DEAD if owner == "foreign" else ProcessLiveness.UNKNOWN
    assert _runtime_liveness(identity, "pid:[99999999]") is expected


def test_shutdown_cancels_paused_preparation_before_replacement_can_spawn(setup, monkeypatch):
    preparing = threading.Event()
    resume = threading.Event()
    errors = []
    popen_calls = []

    def prepare(_profile):
        preparing.set()
        assert resume.wait(5)

    def spawn():
        try:
            setup.runtime.spawn_browser(setup.profiles[0], ref.profile_id, ref.run_id)
        except Exception as error:
            errors.append(error)

    ref = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    setup.runtime.prepare_launch = prepare
    monkeypatch.setattr(
        "foxprofile_ext.runtime.processes.subprocess.Popen",
        lambda *a, **k: popen_calls.append((a, k)),
    )
    thread = threading.Thread(target=spawn)
    thread.start()
    replacement = None
    try:
        assert preparing.wait(2)
        setup.runtime.shutdown(0)
        replacement = RuntimeSupervisor(
            setup.database,
            data_dir=setup.runtime.data_dir,
            code_root=Path.cwd(),
            runtime_paths=(Path(sys.prefix),),
        )
        resume.set()
        thread.join(3)
        assert not thread.is_alive()
        assert popen_calls == []
        assert len(errors) == 1 and "shut down" in str(errors[0])
        assert setup.ledger.get(ref).status is RunStatus.CANCELLED
        assert not (setup.runtime.data_dir / ".foxprofile-runtime" / ref.run_id).exists()
    finally:
        resume.set()
        thread.join(5)
        if replacement is not None:
            replacement.shutdown(0)


def test_shutdown_waits_for_atomic_spawn_registration(setup, monkeypatch):
    spawning = threading.Event()
    resume = threading.Event()
    shutdown_started = threading.Event()
    errors = []
    process = SimpleNamespace(stdin=io.StringIO())
    ref = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]

    def popen(*args, **kwargs):
        spawning.set()
        assert resume.wait(5)
        return process

    def spawn():
        try:
            setup.runtime.spawn_browser(setup.profiles[0], ref.profile_id, ref.run_id)
        except Exception as error:
            errors.append(error)

    def shutdown():
        shutdown_started.set()
        setup.runtime.shutdown(0)

    cancel = setup.ledger.cancel_unclaimed

    def cancel_registered(run):
        assert setup.runtime._handles[run.run_id] is process
        return cancel(run)

    monkeypatch.setattr("foxprofile_ext.runtime.processes.subprocess.Popen", popen)
    monkeypatch.setattr(setup.runtime, "_watch", lambda *_: None)
    monkeypatch.setattr(setup.runtime.ledger, "cancel_unclaimed", cancel_registered)
    thread = threading.Thread(target=spawn)
    stop = threading.Thread(target=shutdown)
    thread.start()
    try:
        assert spawning.wait(2)
        stop.start()
        assert shutdown_started.wait(2)
        assert not setup.runtime._lock.acquire(blocking=False)
        with pytest.raises(RuntimeError, match="Another lifecycle manager"):
            RuntimeSupervisor(
                setup.database,
                data_dir=setup.runtime.data_dir,
                code_root=Path.cwd(),
                runtime_paths=(Path(sys.prefix),),
            )
    finally:
        resume.set()
        thread.join(5)
        if stop.ident is not None:
            stop.join(5)
    assert not thread.is_alive() and not stop.is_alive()
    assert errors == []
    assert setup.runtime._manager_file.closed
    assert setup.ledger.get(ref).status is RunStatus.CANCELLED


def test_shutdown_stops_supervisor_claim_that_wins_reserved_cancellation(setup, monkeypatch):
    ref = setup.ledger.reserve(
        (setup.profiles[0].profile_id,), setup.runtime.host_id
    ).newly_reserved[0]
    setup.runtime._owned_runs[ref.run_id] = ref
    cancel = setup.runtime.ledger.cancel_unclaimed
    stops = []

    def claim_before_cancel(run):
        assert setup.runtime.registry.register_supervisor(
            ref, setup.runtime.host_id, process_identity(os.getpid())
        )
        return cancel(run)

    monkeypatch.setattr(setup.runtime.ledger, "cancel_unclaimed", claim_before_cancel)
    monkeypatch.setattr(setup.runtime, "request_stop", lambda *args: stops.append(args))
    setup.runtime.shutdown(0)
    assert [args[:2] for args in stops] == [(ref.profile_id, ref.run_id)]
