"""Authorized batch admission over the shared durable capacity ledger."""

from collections.abc import Iterable

from .dto import PrincipalDTO, ProfileRefDTO
from .policy import require_profile_access
from .storage.capacity import AdmissionResult, CapacityLedger
from .storage.dao import AssignmentDAO


class CapacityService:
    """Authorize whole batch before atomically reserving its new profile runs."""

    def __init__(self, ledger: CapacityLedger, assignments: AssignmentDAO):
        self.ledger = ledger
        self.assignments = assignments

    def reserve(
        self, principal: PrincipalDTO | None, profiles: Iterable[ProfileRefDTO], host_id: str
    ) -> AdmissionResult:
        unique: dict[str, ProfileRefDTO] = {}
        for profile in profiles:
            if not isinstance(profile, ProfileRefDTO):
                # The common policy emits the same redacted resource denial.
                require_profile_access(principal, profile, self.assignments.is_assigned)
            unique.setdefault(profile.profile_id, profile)
        for profile in unique.values():
            require_profile_access(principal, profile, self.assignments.is_assigned)
        return self.ledger.reserve((profile.profile_id for profile in unique.values()), host_id)
