"""Small contract between protected application use cases and launch runtime."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .dto import PrincipalDTO, ProfileRefDTO
from .errors import DomainError, ErrorCode

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class RunLaunchResult:
    """Redacted result for one profile launch; no process/runtime handles."""

    profile_id: str
    profile_name: str
    browser_run_id: str
    state: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        values = (self.profile_id, self.profile_name, self.browser_run_id)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        if not isinstance(self.state, str) or self.state not in {
            "starting",
            "running",
            "failed",
        }:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or not _ERROR_CODE.fullmatch(self.error_code)
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)


@dataclass(frozen=True, slots=True)
class RunStatusResult:
    """Redacted current-run state; excludes process/display/runtime details."""

    profile_id: str
    profile_name: str
    browser_run_id: str
    state: str

    def __post_init__(self) -> None:
        values = (self.profile_id, self.profile_name, self.browser_run_id)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        if not isinstance(self.state, str) or self.state not in {
            "reserved",
            "starting",
            "running",
            "stopping",
            "cancelled",
            "closed",
            "recovering",
            "failed",
        }:
            raise DomainError(ErrorCode.VALIDATION_ERROR)


class RunCoordinator(Protocol):
    """Trusted launch adapter. It rechecks current profile identity at admission."""

    def reserve_and_start(
        self,
        principal: PrincipalDTO,
        profiles: Sequence[ProfileRefDTO],
        *,
        wait: bool = False,
        timeout: float = 30,
    ) -> tuple[RunLaunchResult, ...]: ...

    def status(self, principal: PrincipalDTO, profile: ProfileRefDTO) -> RunStatusResult | None: ...

    def stop(
        self, principal: PrincipalDTO, profile: ProfileRefDTO, *, timeout: float = 30.0
    ) -> bool: ...

    def control(
        self,
        principal: PrincipalDTO,
        profile: ProfileRefDTO,
        action: str,
        params: dict[str, object] | None = None,
    ) -> object: ...

    def shutdown(self, *, timeout: float = 30.0) -> None: ...
