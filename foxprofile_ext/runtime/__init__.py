"""Runtime process adapters owned by the FoxProfile extension."""

from .confinement import (
    ConfinementPurpose,
    ConfinementSpec,
    ConfinementUnavailable,
    build_confined_command,
    build_confined_environment,
    probe_confinement,
)
from .coordinator import RuntimeCoordinator
from .processes import RuntimeSupervisor
from .registry import RuntimeRecord, RuntimeRegistry

__all__ = [
    "ConfinementPurpose",
    "ConfinementSpec",
    "ConfinementUnavailable",
    "RuntimeCoordinator",
    "RuntimeRecord",
    "RuntimeRegistry",
    "RuntimeSupervisor",
    "build_confined_command",
    "build_confined_environment",
    "probe_confinement",
]
