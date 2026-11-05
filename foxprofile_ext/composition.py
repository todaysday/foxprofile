"""Small composition boundary for adapters that already resolved authority."""

from dataclasses import dataclass

from .dto import CapacitySettings, PrincipalDTO, ProfileRefDTO
from .policy import (
    AssignmentLookup,
    require_authenticated,
    require_capacity,
    require_profile_access,
)


@dataclass(frozen=True, slots=True)
class ExtensionComposition:
    """Bind pure policies to an assignment lookup supplied by the application."""

    is_assigned: AssignmentLookup

    def principal(self, principal: PrincipalDTO | None) -> PrincipalDTO:
        """Validate an already-authenticated principal; never authenticate credentials."""
        return require_authenticated(principal)

    def profile(self, principal: PrincipalDTO | None, profile: ProfileRefDTO) -> ProfileRefDTO:
        return require_profile_access(principal, profile, self.is_assigned)

    def capacity(self, settings: CapacitySettings, occupied: int, requested: int = 1) -> None:
        require_capacity(settings, occupied, requested)


def create_extension_composition(is_assigned: AssignmentLookup) -> ExtensionComposition:
    """Create the policy seam around an application-owned assignment lookup."""
    if not callable(is_assigned):
        raise TypeError("is_assigned must be callable")
    return ExtensionComposition(is_assigned)
