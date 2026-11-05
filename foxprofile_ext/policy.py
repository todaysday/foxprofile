"""Small domain checks; callers own authentication, transactions, and effects."""

from collections.abc import Callable

from .dto import CapacitySettings, PrincipalDTO, PrincipalKind, ProfileRefDTO, Role
from .errors import DomainError, ErrorCode, ProfileCapacityExceeded

# Inject a concrete assignment DAO's lookup; do not cache grants in a principal.
AssignmentLookup = Callable[[str, str], bool]


def require_authenticated(principal: PrincipalDTO | None) -> PrincipalDTO:
    if not isinstance(principal, PrincipalDTO):
        raise DomainError(ErrorCode.AUTH_REQUIRED)
    return principal


def require_personal(principal: PrincipalDTO | None) -> PrincipalDTO:
    principal = require_authenticated(principal)
    if principal.kind is not PrincipalKind.PERSONAL:
        raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
    return principal


def require_service(principal: PrincipalDTO | None) -> PrincipalDTO:
    principal = require_authenticated(principal)
    if principal.kind is not PrincipalKind.SERVICE:
        raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
    return principal


def require_admin(principal: PrincipalDTO | None) -> PrincipalDTO:
    principal = require_authenticated(principal)
    if principal.kind is not PrincipalKind.PERSONAL or principal.role is not Role.ADMIN:
        raise DomainError(ErrorCode.ADMIN_REQUIRED)
    return principal


def require_profile_access(
    principal: PrincipalDTO | None,
    profile: ProfileRefDTO | None,
    is_assigned: AssignmentLookup,
) -> ProfileRefDTO:
    """Resolve the profile server-side; recheck grants at each effect boundary."""
    principal = require_authenticated(principal)
    if not isinstance(profile, ProfileRefDTO):
        raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
    if (
        principal.kind is PrincipalKind.SERVICE
        or principal.role is Role.ADMIN
        or is_assigned(principal.subject_id, profile.profile_id) is True
    ):
        return profile
    raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)


def require_capacity(settings: CapacitySettings, occupied: int, requested: int = 1) -> None:
    """Evaluate counts inside the caller's admission transaction; reserve nothing.

    Call after whole-request authorization and deduplication. Zero new places
    preserves access to existing runs even after a limit decrease.
    """
    if not isinstance(settings, CapacitySettings) or any(
        type(count) is not int or count < 0 for count in (occupied, requested)
    ):
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    if requested and occupied + requested > settings.max_concurrent_profiles:
        raise ProfileCapacityExceeded(settings.max_concurrent_profiles)
