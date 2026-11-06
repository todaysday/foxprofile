"""Authorized batch admission and lifecycle coordination for browser runs."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence

from src.models.profile import Profile

from ..capacity import CapacityService
from ..dto import PrincipalDTO, ProfileRefDTO
from ..errors import DomainError, ErrorCode
from ..locking import catalog_lock
from ..run_contracts import RunLaunchResult, RunStatusResult
from ..storage.capacity import CapacityLedger, RunStatus
from .processes import RuntimeSupervisor


class RuntimeCoordinator:
    """Recheck identity, atomically admit batches, then start only new runs."""

    def __init__(
        self,
        capacity: CapacityService,
        ledger: CapacityLedger,
        runtime: RuntimeSupervisor,
        launcher: object,
        *,
        profile_by_id: Callable[[str], Profile],
        authorize: Callable[[PrincipalDTO, ProfileRefDTO], PrincipalDTO],
        lock_path: str,
    ) -> None:
        self.capacity = capacity
        self.ledger = ledger
        self.runtime = runtime
        self.launcher = launcher
        self.profile_by_id = profile_by_id
        self.authorize = authorize
        self.lock_path = lock_path
        self.host_id = runtime.host_id

    def reserve_and_start(
        self,
        principal: PrincipalDTO,
        profiles: Sequence[ProfileRefDTO],
        *,
        wait: bool = False,
        timeout: float = 30,
    ) -> tuple[RunLaunchResult, ...]:
        if (
            type(wait) is not bool
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        if isinstance(profiles, (str, bytes)) or not profiles:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        targets = _unique_profiles(profiles)
        with catalog_lock(self.lock_path):
            current_actor = principal
            current_profiles: dict[str, Profile] = {}
            refs: list[ProfileRefDTO] = []
            for ref in targets:
                if not isinstance(ref, ProfileRefDTO):
                    raise DomainError(ErrorCode.VALIDATION_ERROR)
                profile = self.profile_by_id(ref.profile_id)
                if profile.profile_id != ref.profile_id or profile.name != ref.name:
                    raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
                current_ref = ProfileRefDTO(profile.profile_id, profile.name)
                current_actor = self.authorize(current_actor, current_ref)
                current_profiles[ref.profile_id] = profile
                refs.append(current_ref)
            admission = self.capacity.reserve(current_actor, refs, self.host_id)
            new_ids = {ref.profile_id: ref.run_id for ref in admission.newly_reserved}
            for ref in admission.newly_reserved:
                profile = current_profiles[ref.profile_id]
                try:
                    started = self.launcher.start_thread(
                        profile,
                        lambda _message: None,
                        on_ready=lambda item=ref: self.ledger.mark_running(item),
                        on_stop=lambda item=ref: self._launch_stopped(item),
                        run_id=ref.run_id,
                        profile_id=ref.profile_id,
                    )
                    if not started:
                        self.ledger.cancel_unclaimed(ref)
                except Exception:
                    self.ledger.cancel_unclaimed(ref)
                    record = self.ledger.get(ref)
                    if record is not None and record.status in {
                        RunStatus.STARTING,
                        RunStatus.RUNNING,
                    }:
                        self.runtime.request_stop(ref.profile_id, ref.run_id, 1.0)

        deadline = time.monotonic() + float(timeout)
        results: list[RunLaunchResult] = []
        for run in admission.runs:
            ref = ProfileRefDTO(run.ref.profile_id, current_profiles[run.ref.profile_id].name)
            state = _launch_state(run.status)
            error_code = None
            if ref.profile_id in new_ids:
                record = self.ledger.get(run.ref)
                if record is not None:
                    state = _launch_state(record.status)
                if wait and state == "starting":
                    remaining = max(0.0, deadline - time.monotonic())
                    ok, error = self.launcher.wait_for_launch(ref.name, remaining)
                    record = self.ledger.get(run.ref)
                    if ok:
                        if record is not None and record.status is RunStatus.STARTING:
                            self.ledger.mark_running(run.ref)
                        state = "running"
                    elif error != "timeout":
                        state = "failed"
                        error_code = "LAUNCH_FAILED"
                    else:
                        state = "starting"
                elif state == "failed":
                    state = "failed"
                    error_code = "LAUNCH_FAILED"
            results.append(
                RunLaunchResult(
                    ref.profile_id,
                    ref.name,
                    run.ref.run_id,
                    state,
                    error_code,
                )
            )
        return tuple(results)

    def _launch_stopped(self, ref) -> None:
        if not self.ledger.cancel_unclaimed(ref):
            self.ledger.mark_stopping(ref)

    def status(self, principal: PrincipalDTO, profile: ProfileRefDTO) -> RunStatusResult | None:
        current, run = self._current_run(principal, profile)
        if run is None:
            return None
        return RunStatusResult(current.profile_id, current.name, run.ref.run_id, run.status.value)

    def stop(
        self, principal: PrincipalDTO, profile: ProfileRefDTO, *, timeout: float = 30.0
    ) -> bool:
        with catalog_lock(self.lock_path):
            current, run = self._current_run(principal, profile)
            if run is None:
                return False
            if run.status is RunStatus.RESERVED and self.ledger.cancel_unclaimed(run.ref):
                return True
        return self.runtime.request_stop(run.ref.profile_id, run.ref.run_id, timeout)

    def control(
        self,
        principal: PrincipalDTO,
        profile: ProfileRefDTO,
        action: str,
        params: dict[str, object] | None = None,
    ) -> object:
        with catalog_lock(self.lock_path):
            current, run = self._current_run(principal, profile)
            if run is None:
                raise DomainError(ErrorCode.STALE_BROWSER_RUN)
            return self.runtime.control(run.ref.profile_id, run.ref.run_id, action, params)

    def shutdown(self, *, timeout: float = 30.0) -> None:
        self.runtime.shutdown(timeout)

    def _current_run(
        self, principal: PrincipalDTO, ref: ProfileRefDTO
    ) -> tuple[ProfileRefDTO, object | None]:
        if not isinstance(ref, ProfileRefDTO):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with catalog_lock(self.lock_path):
            actor = self.authorize(principal, ref)
            profile = self.profile_by_id(ref.profile_id)
            if profile.profile_id != ref.profile_id or profile.name != ref.name:
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
            current = ProfileRefDTO(profile.profile_id, profile.name)
            self.authorize(actor, current)
            run = next(
                (
                    item
                    for item in self.ledger.active_runs()
                    if item.ref.profile_id == ref.profile_id
                ),
                None,
            )
            return current, run


def _unique_profiles(profiles: Sequence[ProfileRefDTO]) -> tuple[ProfileRefDTO, ...]:
    unique: dict[str, ProfileRefDTO] = {}
    for profile in profiles:
        if not isinstance(profile, ProfileRefDTO):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        existing = unique.get(profile.profile_id)
        if existing is not None and existing.name != profile.name:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        unique.setdefault(profile.profile_id, profile)
    return tuple(unique.values())


def _launch_state(status: RunStatus) -> str:
    if status is RunStatus.RUNNING:
        return "running"
    if status in {RunStatus.CANCELLED, RunStatus.CLOSED}:
        return "failed"
    return "starting"
