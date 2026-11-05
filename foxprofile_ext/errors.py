"""Transport-independent failures with stable codes and redacted messages."""

from enum import Enum


class ErrorCode(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN_OR_NOT_FOUND = "FORBIDDEN_OR_NOT_FOUND"
    ADMIN_REQUIRED = "ADMIN_REQUIRED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PROFILE_CAPACITY_EXCEEDED = "PROFILE_CAPACITY_EXCEEDED"
    PROFILE_BUSY = "PROFILE_BUSY"
    MANUAL_SESSION_BUSY = "MANUAL_SESSION_BUSY"
    MANUAL_UNSUPPORTED_RUNTIME = "MANUAL_UNSUPPORTED_RUNTIME"
    STALE_BROWSER_RUN = "STALE_BROWSER_RUN"
    MANUAL_SESSION_CLOSED = "MANUAL_SESSION_CLOSED"
    RUNTIME_RECOVERING = "RUNTIME_RECOVERING"


_MESSAGES = {
    ErrorCode.AUTH_REQUIRED: "Authentication required.",
    ErrorCode.FORBIDDEN_OR_NOT_FOUND: "Resource unavailable.",
    ErrorCode.ADMIN_REQUIRED: "Administrator access required.",
    ErrorCode.VALIDATION_ERROR: "Invalid request or setting.",
    ErrorCode.PROFILE_CAPACITY_EXCEEDED: "Shared profile limit reached.",
    ErrorCode.PROFILE_BUSY: "Profile is busy.",
    ErrorCode.MANUAL_SESSION_BUSY: "Manual access is busy.",
    ErrorCode.MANUAL_UNSUPPORTED_RUNTIME: "Manual access is unavailable for this runtime.",
    ErrorCode.STALE_BROWSER_RUN: "Browser run is no longer current.",
    ErrorCode.MANUAL_SESSION_CLOSED: "Manual session is closed.",
    ErrorCode.RUNTIME_RECOVERING: "Runtime recovery is incomplete. Retry after recovery.",
}


class DomainError(Exception):
    """Adapters map the code to HTTP/MCP responses after authorization."""

    def __init__(self, code: ErrorCode):
        self.code = ErrorCode(code)
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return _MESSAGES[self.code]


class ProfileCapacityExceeded(DomainError):
    """The configured limit is public; foreign occupancy details are not."""

    def __init__(self, max_concurrent_profiles: int):
        if type(max_concurrent_profiles) is not int or max_concurrent_profiles < 1:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        self.max_concurrent_profiles = max_concurrent_profiles
        super().__init__(ErrorCode.PROFILE_CAPACITY_EXCEEDED)

    @property
    def message(self) -> str:
        return (
            f"Shared profile limit ({self.max_concurrent_profiles}) reached. "
            "Stop an accessible profile and retry, or contact an administrator."
        )
