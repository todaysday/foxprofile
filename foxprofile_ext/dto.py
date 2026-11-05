"""Validated values, explicit public DTOs, and private runtime descriptors.

Authentication adapters construct principals from verified server-side state.
These values do not authenticate credentials, check liveness, or persist rows.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TypeVar

from .errors import DomainError, ErrorCode

_Enum = TypeVar("_Enum", bound=Enum)


def _text(*values: object) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise DomainError(ErrorCode.VALIDATION_ERROR)


def _enum_value(enum_type: type[_Enum], value: object) -> _Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        raise DomainError(ErrorCode.VALIDATION_ERROR) from None


class Role(str, Enum):
    ADMIN = "admin"
    USER = "user"


class PrincipalKind(str, Enum):
    PERSONAL = "personal"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class PrincipalDTO:
    """Authenticated identity; session_id is a server reference, never a token."""

    subject_id: str
    kind: PrincipalKind
    role: Role | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.subject_id)
        object.__setattr__(self, "kind", _enum_value(PrincipalKind, self.kind))
        if self.kind is PrincipalKind.PERSONAL:
            object.__setattr__(self, "role", _enum_value(Role, self.role))
            _text(self.session_id)
        elif self.role is not None or self.session_id is not None:
            raise DomainError(ErrorCode.VALIDATION_ERROR)


@dataclass(frozen=True, slots=True)
class ProfileRefDTO:
    """Server-owned local identity; the name is a mutable catalog label."""

    profile_id: str
    name: str

    def __post_init__(self) -> None:
        _text(self.profile_id, self.name)


@dataclass(frozen=True, slots=True)
class BrowserRunRefDTO:
    """One server-allocated launch generation, independent of display/PID reuse."""

    profile_id: str
    run_id: str

    def __post_init__(self) -> None:
        _text(self.profile_id, self.run_id)


class ManualSessionState(str, Enum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    CLOSING = "closing"
    RECOVERING = "recovering"
    CLOSED = "closed"

    @property
    def occupies_slot(self) -> bool:
        """Only confirmed cleanup permits the service to persist CLOSED."""
        return self is not ManualSessionState.CLOSED


@dataclass(frozen=True, slots=True)
class ManualSessionDTO:
    """Public manual response. Its locator is not connection authorization."""

    session_id: str
    profile_id: str
    browser_run_id: str
    state: ManualSessionState
    websocket_path: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.session_id, self.profile_id, self.browser_run_id, self.websocket_path)
        object.__setattr__(self, "state", _enum_value(ManualSessionState, self.state))
        if (
            not self.websocket_path.startswith("/")
            or self.websocket_path.startswith("//")
            or any(
                ord(char) <= 32 or ord(char) == 127 or char in "\\?#"
                for char in self.websocket_path
            )
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        if self.expires_at is not None and (
            not isinstance(self.expires_at, datetime) or self.expires_at.utcoffset() is None
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Private host PID and /proc start ticks, populated by a trusted adapter."""

    pid: int
    start_ticks: int

    def __post_init__(self) -> None:
        if (
            type(self.pid) is not int
            or self.pid < 1
            or type(self.start_ticks) is not int
            or self.start_ticks < 0
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """Server-only registration; never serialize this into a public response.

    Supervisor registration precedes browser creation outside confinement.
    Adapters verify process/context ownership; this value checks structure only.
    """

    run: BrowserRunRefDTO
    host_id: str
    supervisor: ProcessIdentity = field(repr=False)
    browser: ProcessIdentity | None = field(default=None, repr=False)
    display: str | None = field(default=None, repr=False)
    namespace: str | None = field(default=None, repr=False)
    tmp_path: str | None = field(default=None, repr=False)
    xauthority_path: str | None = field(default=None, repr=False)
    control_port: int | None = field(default=None, repr=False)
    control_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _text(self.host_id)
        if (
            not isinstance(self.run, BrowserRunRefDTO)
            or not isinstance(self.supervisor, ProcessIdentity)
            or (self.browser is not None and not isinstance(self.browser, ProcessIdentity))
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        for value in (self.display, self.namespace, self.tmp_path, self.xauthority_path):
            if value is not None:
                _text(value)
        if self.control_port is None:
            if self.control_token is not None:
                raise DomainError(ErrorCode.VALIDATION_ERROR)
        else:
            if type(self.control_port) is not int or not 1 <= self.control_port <= 65535:
                raise DomainError(ErrorCode.VALIDATION_ERROR)
            _text(self.control_token)


@dataclass(frozen=True, slots=True)
class CapacitySettings:
    """Default applies only when storage has no saved setting."""

    max_concurrent_profiles: int = 10

    def __post_init__(self) -> None:
        if type(self.max_concurrent_profiles) is not int or self.max_concurrent_profiles < 1:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
