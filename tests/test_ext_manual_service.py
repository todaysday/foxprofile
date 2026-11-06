"""Manual-domain effects with real auth/lease SQLite and a gated fake backend."""

from dataclasses import asdict, replace
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from foxprofile_ext.auth import AuthService
from foxprofile_ext.dto import (
    BrowserRunRefDTO,
    ManualSessionState,
    PrincipalDTO,
    PrincipalKind,
    ProcessIdentity,
    ProfileRefDTO,
    Role,
    RuntimeDescriptor,
)
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.manual import BackendSession, ManualRuntime, ManualSessionService
from foxprofile_ext.policy import require_profile_access
from foxprofile_ext.storage import ExtensionDatabase, ManualLeaseDAO, SessionDAO, UserDAO
from foxprofile_ext.storage.manual import HelperLiveness


class FakeBackend:
    """Explicitly models gating and watchdog bounds; no real VNC/watchdog claim."""

    def __init__(self):
        self.starts = []
        self.events = []
        self.sessions = {}
        self.before_claim = None
        self.before_ready = None
        self.keep_alive = False
        self.fail_start = False
        self.fail_release = False
        self.fail_connect = False

    def start(self, session_id, runtime, claim_helper, **bounds):
        helper = ProcessIdentity(500 + len(self.starts), 9000 + len(self.starts))
        channel = object()
        entry = dict(bounds, helper=helper, channel=channel, live=True, cancelled=False)
        self.sessions[session_id] = entry
        self.starts.append((session_id, runtime))
        if self.before_claim:
            self.before_claim()
        claimed = claim_helper(helper)
        if not claimed or entry["cancelled"]:
            entry["live"] = False
            raise RuntimeError("gated start cancelled")
        self.events.append(("attach", session_id))
        if self.before_ready:
            self.before_ready()
        if self.fail_start:
            raise RuntimeError("private backend diagnostic")
        return BackendSession(helper, channel)

    def connect(self, session_id, helper):
        assert self.sessions[session_id]["helper"] == helper
        if self.fail_connect:
            raise RuntimeError("private backend diagnostic")
        return self.sessions[session_id]["channel"]

    def heartbeat(self, session_id, watchdog_deadline):
        entry = self.sessions[session_id]
        assert not entry["cancelled"]
        entry["watchdog_deadline"] = min(watchdog_deadline, entry["cleanup_deadline"])
        self.events.append(("heartbeat", session_id))

    def revoke(self, session_id):
        self.events.append(("revoke", session_id))

    def release_input(self, session_id):
        self.events.append(("release", session_id))
        if self.fail_release:
            raise RuntimeError("input channel lost")

    def stop(self, session_id, host_id, helper, cleanup_deadline):
        self.events.append(("stop", session_id, host_id, helper))
        entry = self.sessions.get(session_id)
        if entry:
            if helper is not None:
                assert helper == entry["helper"]
            entry["cancelled"] = True
            entry["cleanup_deadline"] = min(entry["cleanup_deadline"], cleanup_deadline)
            entry["watchdog_deadline"] = min(entry["watchdog_deadline"], cleanup_deadline)
            if not self.keep_alive:
                entry["live"] = False

    def probe(self, session_id, host_id, helper):
        entry = self.sessions.get(session_id)
        if entry and helper is not None:
            assert entry["helper"] == helper
        return HelperLiveness.LIVE if entry and entry["live"] else HelperLiveness.DEAD


@pytest.fixture
def env(tmp_path):
    now = [100.0]
    database = ExtensionDatabase(tmp_path / "manual.sqlite3")
    users, sessions = UserDAO(database), SessionDAO(database)
    users.bootstrap_admin("admin", "admin", "hash")
    users.create("alice", "alice", "hash")
    users.create("bob", "bob", "hash")
    for session, user in (("personal-a", "alice"), ("personal-a2", "alice"), ("personal-b", "bob")):
        sessions.create(session, user, f"digest-{session}", 10, 1000)
    auth = AuthService(users, sessions, clock=lambda: now[0])
    alice = PrincipalDTO("alice", PrincipalKind.PERSONAL, Role.USER, "personal-a")
    alice2 = replace(alice, session_id="personal-a2")
    bob = PrincipalDTO("bob", PrincipalKind.PERSONAL, Role.USER, "personal-b")
    grants = {("alice", "profile-a"), ("bob", "profile-a"), ("bob", "profile-b")}
    profiles = {name: ProfileRefDTO(name, name) for name in ("profile-a", "profile-b")}

    def access(actor, profile_id):
        auth.validate_principal(actor)
        return require_profile_access(
            actor, profiles.get(profile_id), lambda u, p: (u, p) in grants
        )

    def runtime(profile, run):
        return ManualRuntime(
            RuntimeDescriptor(
                BrowserRunRefDTO(profile, run),
                "host-a",
                ProcessIdentity(201, 1001),
                ProcessIdentity(202, 1002),
                ":101",
                "namespace-a",
                "/private/run-a",
                "/private/run-a/Xauthority",
                12345,
                "private-control-token",
            ),
            "camoufox",
            True,
        )

    runtimes = {p: runtime(p, f"run-{p}") for p in profiles}
    backend, leases = FakeBackend(), ManualLeaseDAO(database)
    service = ManualSessionService(
        auth, users, sessions, leases, access, runtimes.get, backend, clock=lambda: now[0]
    )
    return SimpleNamespace(**locals())


def error(code, function, *args):
    with pytest.raises(DomainError) as caught:
        function(*args)
    assert caught.value.code is code
    assert "private" not in str(caught.value)


def test_create_double_click_public_dto_and_private_channel(env):
    original = env.runtimes.copy()
    first = env.service.create(env.alice, "profile-a")
    assert env.service.create(env.alice, "profile-a") == first
    assert len(env.backend.starts) == 1
    assert first.state is ManualSessionState.ACTIVE
    assert set(asdict(first)) == {
        "session_id",
        "profile_id",
        "browser_run_id",
        "state",
        "websocket_path",
        "expires_at",
    }
    assert "private" not in repr(first) and "101" not in repr(first)
    channel = env.service.connect(env.alice, first.session_id, first.browser_run_id)
    assert channel is env.backend.sessions[first.session_id]["channel"]
    assert env.service.reconnect(env.alice, first.session_id, first.browser_run_id) is channel
    assert env.runtimes == original
    assert env.leases.current().helper == env.backend.sessions[first.session_id]["helper"]


@pytest.mark.parametrize("principal_name", ["alice2", "bob"])
def test_other_personal_session_cannot_read_connect_or_close_same_profile(env, principal_name):
    manual = env.service.create(env.alice, "profile-a")
    principal = getattr(env, principal_name)
    for function, args in (
        (env.service.status, (principal, manual.session_id)),
        (env.service.connect, (principal, manual.session_id, manual.browser_run_id)),
        (env.service.close, (principal, manual.session_id, manual.browser_run_id)),
    ):
        error(ErrorCode.FORBIDDEN_OR_NOT_FOUND, function, *args)
    error(ErrorCode.MANUAL_SESSION_BUSY, env.service.create, principal, "profile-a")
    assert env.leases.current().session_id == manual.session_id
    assert not any(event[0] == "stop" for event in env.backend.events)


@pytest.mark.parametrize("operation", ["create", "status", "connect", "close"])
def test_missing_service_and_forged_principals_denied(env, operation):
    manual = env.service.create(env.alice, "profile-a")
    args = {
        "create": ("profile-a",),
        "status": (manual.session_id,),
        "connect": (manual.session_id, manual.browser_run_id),
        "close": (manual.session_id, manual.browser_run_id),
    }[operation]
    function = getattr(env.service, operation)
    error(ErrorCode.AUTH_REQUIRED, function, None, *args)
    error(
        ErrorCode.FORBIDDEN_OR_NOT_FOUND,
        function,
        PrincipalDTO("mcp", PrincipalKind.SERVICE),
        *args,
    )
    error(ErrorCode.AUTH_REQUIRED, function, replace(env.alice, subject_id="bob"), *args)
    assert len(env.backend.starts) == 1


def test_guessed_locator_and_unauthorized_profile_return_no_metadata(env):
    error(ErrorCode.FORBIDDEN_OR_NOT_FOUND, env.service.status, env.alice, "guessed")
    error(ErrorCode.FORBIDDEN_OR_NOT_FOUND, env.service.create, env.alice, "profile-b")
    assert not env.backend.starts


@pytest.mark.parametrize("change", ["engine", "headless", "unconfined", "stopped"])
def test_unsupported_and_stopped_runs_never_launch_replacement(env, change):
    runtime = env.runtimes["profile-a"]
    if change == "engine":
        runtime = replace(runtime, engine="chrome")
    elif change == "headless":
        runtime = replace(runtime, descriptor=replace(runtime.descriptor, display=None))
    elif change == "unconfined":
        runtime = replace(runtime, confined=False)
    else:
        runtime = None
    env.runtimes["profile-a"] = runtime
    code = (
        ErrorCode.STALE_BROWSER_RUN if change == "stopped" else ErrorCode.MANUAL_UNSUPPORTED_RUNTIME
    )
    error(code, env.service.create, env.alice, "profile-a")
    assert env.leases.current() is None
    assert not env.backend.starts


def test_back_releases_input_before_helper_stop_and_stale_close_preserves_new_session(env):
    manual = env.service.create(env.alice, "profile-a")
    original = env.runtimes.copy()
    closed = env.service.close(env.alice, manual.session_id, manual.browser_run_id)
    assert closed.state is ManualSessionState.CLOSED
    actions = [event[0] for event in env.backend.events]
    assert actions.index("revoke") < actions.index("release") < actions.index("stop")
    newer = env.service.create(env.alice, "profile-a")
    count = len(env.backend.events)
    assert env.service.close(env.alice, manual.session_id, manual.browser_run_id) == closed
    assert len(env.backend.events) == count
    assert env.leases.current().session_id == newer.session_id
    assert env.runtimes == original


def test_live_helper_holds_lease_past_60_boundary_and_reconnect_cannot_extend(env):
    manual = env.service.create(env.alice, "profile-a")
    env.backend.keep_alive = True
    env.now[0] = 110
    error(
        ErrorCode.RUNTIME_RECOVERING,
        env.service.disconnect,
        env.alice,
        manual.session_id,
        manual.browser_run_id,
    )
    assert env.leases.current().cleanup_deadline == 170
    assert env.leases.current().state is ManualSessionState.RECOVERING
    for now in (169.999, 170, 171):
        env.now[0] = now
        error(
            ErrorCode.MANUAL_SESSION_CLOSED,
            env.service.reconnect,
            env.alice,
            manual.session_id,
            manual.browser_run_id,
        )
        assert env.leases.current().cleanup_deadline == 170
        error(ErrorCode.RUNTIME_RECOVERING, env.service.status, env.alice, manual.session_id)
        error(ErrorCode.MANUAL_SESSION_BUSY, env.service.create, env.bob, "profile-b")
    env.backend.keep_alive = False
    assert env.service.reconcile()
    assert env.leases.current() is None
    newer = env.service.create(env.bob, "profile-b")
    error(
        ErrorCode.MANUAL_SESSION_CLOSED,
        env.service.reconnect,
        env.alice,
        manual.session_id,
        manual.browser_run_id,
    )
    assert env.leases.current().session_id == newer.session_id


def test_watchdog_armed_before_first_heartbeat_and_expiry_caps_sliding_bound(env):
    manual = env.service.create(env.alice, "profile-a")
    entry = env.backend.sessions[manual.session_id]
    assert entry["watchdog_deadline"] == 160  # Not personal expiry (1000) + 60.
    assert not any(event[0] == "heartbeat" for event in env.backend.events)
    env.now[0] = 999
    env.service.before_forward(env.alice, manual.session_id, manual.browser_run_id)
    assert entry["watchdog_deadline"] == 1059
    env.now[0] = 1000
    error(
        ErrorCode.AUTH_REQUIRED,
        env.service.before_forward,
        env.alice,
        manual.session_id,
        manual.browser_run_id,
    )
    assert env.leases.get(manual.session_id).cleanup_deadline == 1060
    assert env.leases.current() is None
    assert entry["cancelled"]


def test_transport_detection_counts_toward_cleanup_and_reconnect_cannot_extend(env):
    manual = env.service.create(env.alice, "profile-a")
    env.backend.keep_alive = True
    # Loss starts at 100; transport spends its full 20-second detection budget.
    env.now[0] = 120
    with pytest.raises(DomainError) as caught:
        env.service.disconnect(
            env.alice, manual.session_id, manual.browser_run_id, detection_budget_seconds=20
        )
    assert caught.value.code is ErrorCode.RUNTIME_RECOVERING
    assert env.leases.current().cleanup_deadline == 160
    assert env.backend.sessions[manual.session_id]["cleanup_deadline"] == 160
    env.now[0] = 161
    error(
        ErrorCode.MANUAL_SESSION_CLOSED,
        env.service.reconnect,
        env.alice,
        manual.session_id,
        manual.browser_run_id,
    )
    assert env.leases.current().cleanup_deadline == 160
    error(ErrorCode.MANUAL_SESSION_BUSY, env.service.create, env.bob, "profile-b")


@pytest.mark.parametrize("budget", [-1, 61, float("inf"), float("nan"), True, "20"])
def test_invalid_internal_detection_budget_has_no_effect(env, budget):
    manual = env.service.create(env.alice, "profile-a")
    before = env.leases.current()
    with pytest.raises(ValueError, match="detection budget"):
        env.service.disconnect(
            env.alice, manual.session_id, manual.browser_run_id, detection_budget_seconds=budget
        )
    assert env.leases.current() == before


def test_detection_budget_does_not_authorize_foreign_or_stale_disconnect(env):
    manual = env.service.create(env.alice, "profile-a")
    before = env.leases.current()
    for actor, run in (
        (env.alice2, manual.browser_run_id),
        (env.bob, manual.browser_run_id),
        (env.alice, "stale-run"),
    ):
        with pytest.raises(DomainError):
            env.service.disconnect(actor, manual.session_id, run, detection_budget_seconds=20)
        assert env.leases.current() == before
    assert not any(event[0] == "stop" for event in env.backend.events)


@pytest.mark.parametrize("revoke", ["logout", "disabled", "grant", "password"])
def test_passive_reaper_and_stream_hook_revoke_current_authority_only(env, revoke):
    manual = env.service.create(env.alice, "profile-a")
    original = env.runtimes.copy()
    if revoke == "logout":
        env.auth.logout(env.alice)
    elif revoke == "disabled":
        env.users.update_state("alice", active=False)
    elif revoke == "grant":
        env.grants.remove(("alice", "profile-a"))
    else:
        env.users.set_password_hash("alice", "changed-hash")
    code = ErrorCode.FORBIDDEN_OR_NOT_FOUND if revoke == "grant" else ErrorCode.AUTH_REQUIRED
    error(code, env.service.before_forward, env.alice, manual.session_id, manual.browser_run_id)
    assert env.leases.current() is None
    assert env.runtimes == original
    assert env.backend.sessions[manual.session_id]["cancelled"]


def test_idle_reaper_cleans_revoked_session_without_client_request(env):
    manual = env.service.create(env.alice, "profile-a")
    env.sessions.revoke(env.alice.session_id)
    assert env.service.reap_expired()
    assert env.leases.get(manual.session_id).state is ManualSessionState.CLOSED


def test_display_reuse_rejects_old_run_and_never_targets_replacement(env):
    manual = env.service.create(env.alice, "profile-a")
    old_descriptor = env.runtimes["profile-a"].descriptor
    new_descriptor = replace(old_descriptor, run=BrowserRunRefDTO("profile-a", "replacement"))
    env.runtimes["profile-a"] = replace(env.runtimes["profile-a"], descriptor=new_descriptor)
    error(
        ErrorCode.STALE_BROWSER_RUN,
        env.service.connect,
        env.alice,
        manual.session_id,
        manual.browser_run_id,
    )
    assert env.leases.current() is None
    newer = env.service.create(env.alice, "profile-a")
    assert newer.browser_run_id == "replacement"
    count = len(env.backend.events)
    env.service.close(env.alice, manual.session_id, manual.browser_run_id)
    assert len(env.backend.events) == count
    assert env.backend.starts == [
        (manual.session_id, old_descriptor),
        (newer.session_id, new_descriptor),
    ]


@pytest.mark.parametrize("phase", ["before_claim", "before_ready"])
def test_close_during_start_cancels_gated_child_and_late_readiness_cannot_reopen(env, phase):
    entered, resume = Event(), Event()

    def pause():
        entered.set()
        assert resume.wait(10)

    setattr(env.backend, phase, pause)
    outcomes = []

    def create():
        try:
            outcomes.append(env.service.create(env.alice, "profile-a"))
        except DomainError as failure:
            outcomes.append(failure.code)

    thread = Thread(target=create)
    thread.start()
    assert entered.wait(10)
    lease = env.leases.current()
    duplicate = env.service.create(env.alice, "profile-a")
    assert duplicate.session_id == lease.session_id
    assert duplicate.state is ManualSessionState.PROVISIONING
    env.service.close(env.alice, lease.session_id, lease.browser_run_id)
    resume.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcomes[0] in {ErrorCode.RUNTIME_RECOVERING, ErrorCode.MANUAL_SESSION_CLOSED}
    assert env.leases.get(lease.session_id).state is ManualSessionState.CLOSED
    assert len(env.backend.starts) == 1
    if phase == "before_claim":
        assert not any(event[0] == "attach" for event in env.backend.events)


def test_start_failure_and_input_release_failure_still_confirm_helper_exit(env):
    env.backend.fail_start = True
    env.backend.fail_release = True
    error(ErrorCode.RUNTIME_RECOVERING, env.service.create, env.alice, "profile-a")
    assert env.leases.current() is None
    assert [event[0] for event in env.backend.events] == ["attach", "revoke", "release", "stop"]


def test_connect_failure_is_redacted_and_cleans_only_owned_helper(env):
    manual = env.service.create(env.alice, "profile-a")
    env.backend.fail_connect = True
    error(
        ErrorCode.RUNTIME_RECOVERING,
        env.service.connect,
        env.alice,
        manual.session_id,
        manual.browser_run_id,
    )
    assert env.leases.current() is None


def test_restart_reconciliation_freezes_prior_loss_deadline_and_never_revives_channel(env):
    manual = env.service.create(env.alice, "profile-a")
    env.backend.keep_alive = True
    error(
        ErrorCode.RUNTIME_RECOVERING,
        env.service.close,
        env.alice,
        manual.session_id,
        manual.browser_run_id,
    )
    assert env.leases.current().cleanup_deadline == 160
    env.now[0] = 150
    assert not env.service.reconcile()
    assert env.leases.current().cleanup_deadline == 160
    assert env.backend.sessions[manual.session_id]["watchdog_deadline"] == 160
    env.backend.keep_alive = False
    assert env.service.reconcile()
    assert env.leases.current() is None


def test_idle_reaper_cleans_exited_helper_and_keeps_unknown_exit_occupied(env):
    manual = env.service.create(env.alice, "profile-a")
    env.backend.sessions[manual.session_id]["live"] = False
    assert env.service.reap_expired()
    newer = env.service.create(env.alice, "profile-a")

    def unavailable_probe(*args):
        raise OSError("private process probe unavailable")

    env.backend.probe = unavailable_probe
    assert not env.service.reap_expired()
    assert env.leases.current().session_id == newer.session_id
    assert env.leases.current().state is ManualSessionState.RECOVERING


def test_close_retries_when_create_activates_after_cleanup_reads_provisioning(env):
    ready, activate, created = Event(), Event(), Event()
    env.backend.keep_alive = True

    def before_ready():
        ready.set()
        assert activate.wait(10)

    env.backend.before_ready = before_ready
    original_transition = env.leases.transition

    def transition(*args):
        if args[6:8] == (ManualSessionState.PROVISIONING, ManualSessionState.CLOSING):
            # Cleanup already read PROVISIONING; let creation win its ACTIVE CAS.
            activate.set()
            assert created.wait(10)
        return original_transition(*args)

    env.leases.transition = transition
    outcomes = []

    def create():
        try:
            outcomes.append(env.service.create(env.alice, "profile-a"))
        finally:
            created.set()

    thread = Thread(target=create)
    thread.start()
    assert ready.wait(10)
    lease = env.leases.current()
    error(
        ErrorCode.RUNTIME_RECOVERING,
        env.service.close,
        env.alice,
        lease.session_id,
        lease.browser_run_id,
    )
    thread.join(10)
    assert not thread.is_alive()
    assert outcomes[0].state is ManualSessionState.ACTIVE
    assert env.leases.current().state is ManualSessionState.RECOVERING
    assert env.leases.current().cleanup_deadline == 160
    error(ErrorCode.RUNTIME_RECOVERING, env.service.status, env.alice, lease.session_id)
    error(
        ErrorCode.MANUAL_SESSION_CLOSED,
        env.service.before_forward,
        env.alice,
        lease.session_id,
        lease.browser_run_id,
    )
    assert not any(event[0] == "heartbeat" for event in env.backend.events)
