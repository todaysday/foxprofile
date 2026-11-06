"""Authorize one temporary manual lease without launching or stopping browsers."""

from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone

from ..auth import AuthService
from ..dto import (
    ManualSessionDTO,
    ManualSessionState,
    PrincipalDTO,
    PrincipalKind,
    ProcessIdentity,
    ProfileRefDTO,
    RuntimeDescriptor,
)
from ..errors import DomainError, ErrorCode
from ..policy import require_personal
from ..storage.dao import SessionDAO, UserDAO
from ..storage.manual import HelperLiveness, ManualLeaseDAO, ManualLeaseRecord
from .contracts import BackendSession, ManualBackend, ManualRuntime

CLEANUP_BUDGET_SECONDS = 60


class ManualSessionService:
    """Current personal authority is checked at every effect boundary.

    profile_access must resolve IDs and current ACLs server-side (for example,
    ProtectedApplication.resolve_profile). resolve_runtime must verify the
    current generation's process identity and confinement, not just a display
    string. Backend callbacks and public methods can race across workers; the
    durable lease plus the backend's gated start/stop protocol own that race.
    """

    def __init__(
        self,
        auth: AuthService,
        users: UserDAO,
        sessions: SessionDAO,
        leases: ManualLeaseDAO,
        profile_access: Callable[[PrincipalDTO, str], ProfileRefDTO],
        resolve_runtime: Callable[[str], ManualRuntime | None],
        backend: ManualBackend,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._auth = auth
        self._users = users
        self._sessions = sessions
        self._leases = leases
        self._profile_access = profile_access
        self._resolve_runtime = resolve_runtime
        self._backend = backend
        self._clock = clock

    def create(self, principal: PrincipalDTO | None, profile_id: str) -> ManualSessionDTO:
        actor = self._personal(principal)
        self._profile_access(actor, profile_id)
        runtime = self._runtime(profile_id)
        self.reap_expired()
        now = self._now()
        session = self._sessions.get_valid_by_id(actor.session_id, now)
        if session is None or session.user_id != actor.subject_id:
            raise DomainError(ErrorCode.AUTH_REQUIRED)
        descriptor = runtime.descriptor
        acquired = self._leases.acquire(
            secrets.token_urlsafe(24),
            actor.subject_id,
            actor.session_id,
            profile_id,
            descriptor.run.run_id,
            descriptor.host_id,
            now,
            session.expires_at,
            session.expires_at + CLEANUP_BUDGET_SECONDS,
        )
        lease = acquired.record
        if not acquired.created:
            if lease.state in {ManualSessionState.CLOSING, ManualSessionState.RECOVERING}:
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)
            self._check_runtime(lease)
            return self._dto(lease)
        try:
            started = self._backend.start(
                lease.session_id,
                descriptor,
                lambda helper: self._claim_helper(lease, actor, descriptor, helper),
                expires_at=lease.expires_at,
                cleanup_deadline=lease.cleanup_deadline,
                watchdog_deadline=min(now + CLEANUP_BUDGET_SECONDS, lease.cleanup_deadline),
            )
            if not isinstance(started, BackendSession):
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)
            current = self._leases.get(lease.session_id)
            if current is None or current.helper != started.helper:
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)
            self._personal(actor)
            self._profile_access(actor, profile_id)
            if self._check_runtime(lease) != descriptor:
                raise DomainError(ErrorCode.STALE_BROWSER_RUN)
            if not self._transition(
                lease, ManualSessionState.PROVISIONING, ManualSessionState.ACTIVE
            ):
                raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
            return self._dto(self._leases.get(lease.session_id))
        except Exception as error:
            clean = self._cleanup(lease)
            if not clean or not isinstance(error, DomainError):
                raise DomainError(ErrorCode.RUNTIME_RECOVERING) from None
            raise

    def status(self, principal: PrincipalDTO | None, session_id: str) -> ManualSessionDTO:
        lease = self._owned(principal, session_id)
        if lease.state is not ManualSessionState.CLOSED:
            self._check_or_cleanup(lease)
            if self._now() >= lease.expires_at:
                self._cleanup(lease, trigger=lease.expires_at)
                raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
            if lease.state in {ManualSessionState.CLOSING, ManualSessionState.RECOVERING}:
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        return self._dto(lease)

    def close(
        self, principal: PrincipalDTO | None, session_id: str, browser_run_id: str
    ) -> ManualSessionDTO:
        lease = self._owned(principal, session_id)
        self._expected_run(lease, browser_run_id)
        if lease.state is ManualSessionState.CLOSED:
            return self._dto(lease)
        # A stale run may be cleaned, but no replacement descriptor is used.
        with suppress(DomainError):
            self._check_runtime(lease)
        if not self._cleanup(lease):
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        return self._dto(self._leases.get(session_id))

    def connect(
        self, principal: PrincipalDTO | None, session_id: str, browser_run_id: str
    ) -> object:
        """Return a server-only channel after validating the exact owner/run."""
        lease = self._forwarding_lease(principal, session_id, browser_run_id)
        try:
            channel = self._backend.connect(session_id, lease.helper)
        except Exception:
            self._cleanup(lease)
            raise DomainError(ErrorCode.RUNTIME_RECOVERING) from None
        # Recheck after backend connection; no bytes may be sent before this.
        self.before_forward(principal, session_id, browser_run_id)
        return channel

    def reconnect(
        self, principal: PrincipalDTO | None, session_id: str, browser_run_id: str
    ) -> object:
        """Zero reconnect grace: disconnected sessions need a new creation."""
        return self.connect(principal, session_id, browser_run_id)

    def before_forward(
        self, principal: PrincipalDTO | None, session_id: str, browser_run_id: str
    ) -> None:
        """Gateway must call before EACH incoming/outgoing chunk and heartbeat.

        The gateway also calls this while idle to detect passive revocation.
        No stream/pixel/input may bypass this hook. Backend watchdog handles
        gateway death independently; this function alone cannot enforce it.
        """
        try:
            lease = self._forwarding_lease(principal, session_id, browser_run_id)
            try:
                self._backend.heartbeat(
                    session_id, min(self._now() + CLEANUP_BUDGET_SECONDS, lease.cleanup_deadline)
                )
            except Exception:
                self._cleanup(lease)
                raise DomainError(ErrorCode.RUNTIME_RECOVERING) from None
        except DomainError:
            self.reap_expired()
            raise

    def disconnect(
        self,
        principal: PrincipalDTO | None,
        session_id: str,
        browser_run_id: str,
        *,
        detection_budget_seconds: float = 0,
    ) -> ManualSessionDTO:
        """Include trusted transport detection time in the first-loss deadline."""
        if (
            type(detection_budget_seconds) not in (int, float)
            or not math.isfinite(detection_budget_seconds)
            or not 0 <= detection_budget_seconds <= CLEANUP_BUDGET_SECONDS
        ):
            raise ValueError("detection budget must be finite and between 0 and 60 seconds")
        try:
            if detection_budget_seconds:
                lease = self._owned(principal, session_id)
                self._expected_run(lease, browser_run_id)
                now = self._now()
                self._leases.tighten_cleanup_deadline(
                    *self._binding(lease),
                    now + CLEANUP_BUDGET_SECONDS - detection_budget_seconds,
                    now,
                )
            return self.close(principal, session_id, browser_run_id)
        except DomainError:
            self.reap_expired()
            raise

    def reap_expired(self) -> bool:
        """Trusted periodic job: revoke expired/unauthorized/stale manual access."""
        lease = self._leases.current()
        if lease is None:
            return True
        try:
            user = self._users.get(lease.owner_user_id)
            if user is None:
                raise DomainError(ErrorCode.AUTH_REQUIRED)
            actor = PrincipalDTO(
                lease.owner_user_id, PrincipalKind.PERSONAL, user.role, lease.personal_session_id
            )
            self._personal(actor)
            self._profile_access(actor, lease.profile_id)
            self._check_runtime(lease)
            invalid = self._now() >= lease.expires_at or lease.state in {
                ManualSessionState.CLOSING,
                ManualSessionState.RECOVERING,
            }
            if lease.state is ManualSessionState.ACTIVE:
                try:
                    invalid = (
                        invalid
                        or self._backend.probe(lease.session_id, lease.host_id, lease.helper)
                        is not HelperLiveness.LIVE
                    )
                except Exception:
                    invalid = True
        except DomainError:
            invalid = True
        if not invalid:
            return True
        trigger = min(self._now(), lease.expires_at)
        return self._cleanup(lease, trigger=trigger)

    def reconcile(self) -> bool:
        """Gateway restart revokes known resources before any conflicting create.

        The backend retains its original watchdog/first-loss bound across this
        call; restarting the service must never extend that bound.
        """
        lease = self._leases.current()
        return lease is None or self._cleanup(lease)

    def _claim_helper(
        self,
        lease: ManualLeaseRecord,
        actor: PrincipalDTO,
        descriptor: RuntimeDescriptor,
        helper: ProcessIdentity,
    ) -> bool:
        # Persist identity even when close won, so recovery can prove its exit.
        registered = self._leases.register_helper(*self._binding(lease), helper, self._now())
        if not registered:
            return False
        current = self._leases.get(lease.session_id)
        if current is None or current.state is not ManualSessionState.PROVISIONING:
            return False
        try:
            self._personal(actor)
            self._profile_access(actor, lease.profile_id)
            return self._check_runtime(lease) == descriptor
        except DomainError:
            return False

    def _personal(self, principal: PrincipalDTO | None) -> PrincipalDTO:
        return require_personal(self._auth.validate_principal(principal))

    def _owned(self, principal: PrincipalDTO | None, session_id: str) -> ManualLeaseRecord:
        actor = self._personal(principal)
        lease = self._leases.get(session_id)
        if lease is None or (lease.owner_user_id, lease.personal_session_id) != (
            actor.subject_id,
            actor.session_id,
        ):
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        try:
            self._profile_access(actor, lease.profile_id)
        except DomainError:
            self._cleanup(lease)
            raise
        return lease

    def _runtime(self, profile_id: str) -> ManualRuntime:
        runtime = self._resolve_runtime(profile_id)
        if runtime is None or runtime.descriptor.run.profile_id != profile_id:
            raise DomainError(ErrorCode.STALE_BROWSER_RUN)
        descriptor = runtime.descriptor
        if (
            runtime.engine != "camoufox"
            or runtime.confined is not True
            or any(
                value is None
                for value in (
                    descriptor.browser,
                    descriptor.display,
                    descriptor.namespace,
                    descriptor.tmp_path,
                    descriptor.xauthority_path,
                )
            )
        ):
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        return runtime

    def _check_runtime(self, lease: ManualLeaseRecord) -> RuntimeDescriptor:
        descriptor = self._runtime(lease.profile_id).descriptor
        if (descriptor.run.run_id, descriptor.host_id) != (lease.browser_run_id, lease.host_id):
            raise DomainError(ErrorCode.STALE_BROWSER_RUN)
        return descriptor

    def _check_or_cleanup(self, lease: ManualLeaseRecord) -> None:
        try:
            self._check_runtime(lease)
        except DomainError:
            self._cleanup(lease)
            raise

    def _forwarding_lease(
        self, principal: PrincipalDTO | None, session_id: str, browser_run_id: str
    ) -> ManualLeaseRecord:
        lease = self._owned(principal, session_id)
        self._expected_run(lease, browser_run_id)
        if lease.state is not ManualSessionState.ACTIVE or self._now() >= lease.expires_at:
            if lease.state is not ManualSessionState.CLOSED:
                self._cleanup(lease, trigger=min(self._now(), lease.expires_at))
            raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
        self._check_or_cleanup(lease)
        if lease.helper is None:
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        return lease

    @staticmethod
    def _expected_run(lease: ManualLeaseRecord, browser_run_id: str) -> None:
        if browser_run_id != lease.browser_run_id:
            raise DomainError(ErrorCode.STALE_BROWSER_RUN)

    def _cleanup(self, lease: ManualLeaseRecord, *, trigger: float | None = None) -> bool:
        current = self._leases.get(lease.session_id)
        if current is None or current.state is ManualSessionState.CLOSED:
            return True
        now = self._now()
        deadline = (now if trigger is None else trigger) + CLEANUP_BUDGET_SECONDS
        self._leases.tighten_cleanup_deadline(*self._binding(lease), deadline, now)
        while current.state not in {
            ManualSessionState.CLOSING,
            ManualSessionState.RECOVERING,
        }:
            if current.state is ManualSessionState.CLOSED:
                return True
            if self._transition(current, current.state, ManualSessionState.CLOSING):
                break
            # Creation may have activated after our read. Preserve the close
            # request against that fresh state before touching the backend.
            current = self._leases.get(lease.session_id)
        # Each step is attempted even if an earlier best-effort step fails.
        with suppress(Exception):
            self._backend.revoke(lease.session_id)
        with suppress(Exception):
            self._backend.release_input(lease.session_id)
        current = self._leases.get(lease.session_id)
        with suppress(Exception):
            self._backend.stop(
                lease.session_id, lease.host_id, current.helper, current.cleanup_deadline
            )
        if self._leases.close_after_exit(
            *self._binding(lease),
            now,
            lambda host, helper: self._backend.probe(lease.session_id, host, helper),
        ):
            return True
        self._transition(current, ManualSessionState.CLOSING, ManualSessionState.RECOVERING)
        return self._leases.get(lease.session_id).state is ManualSessionState.CLOSED

    def _transition(
        self, lease: ManualLeaseRecord, expected: ManualSessionState, new: ManualSessionState
    ) -> bool:
        return self._leases.transition(*self._binding(lease), expected, new, self._now())

    @staticmethod
    def _binding(lease: ManualLeaseRecord) -> tuple[str, str, str, str, str, str]:
        return (
            lease.session_id,
            lease.owner_user_id,
            lease.personal_session_id,
            lease.profile_id,
            lease.browser_run_id,
            lease.host_id,
        )

    @staticmethod
    def _dto(lease: ManualLeaseRecord) -> ManualSessionDTO:
        return ManualSessionDTO(
            lease.session_id,
            lease.profile_id,
            lease.browser_run_id,
            lease.state,
            f"/api/v1/ext/manual-sessions/{lease.session_id}/ws",
            datetime.fromtimestamp(lease.expires_at, timezone.utc),
        )

    def _now(self) -> float:
        now = self._clock()
        if type(now) not in (float, int) or not math.isfinite(now):
            raise RuntimeError("clock must return finite Unix seconds")
        return float(now)
