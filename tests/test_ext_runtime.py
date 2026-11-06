"""Admission and recovery checks against SQLite and controlled OS children."""

import io
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxprofile_ext.capacity import CapacityService
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind, ProcessIdentity, ProfileRefDTO
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.locking import catalog_lock
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
