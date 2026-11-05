"""Runtime process adapters owned by the FoxProfile extension."""

from .confinement import (
    ConfinementPurpose,
    ConfinementSpec,
    ConfinementUnavailable,
    build_confined_command,
    build_confined_environment,
    probe_confinement,
)

__all__ = [
    "ConfinementPurpose",
    "ConfinementSpec",
    "ConfinementUnavailable",
    "build_confined_command",
    "build_confined_environment",
    "probe_confinement",
]
