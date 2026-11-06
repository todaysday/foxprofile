"""Small extension-owned configuration values."""

import os
from pathlib import Path


def extension_enabled() -> bool:
    """Return whether protected multi-user mode is explicitly enabled."""
    return os.getenv("FOXPROFILE_EXT_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def extension_database_path(profiles_file: str | Path) -> Path:
    """Place extension state beside the catalog unless explicitly relocated."""
    configured = os.getenv("FOXPROFILE_EXT_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(profiles_file).with_name("foxprofile_ext.sqlite3")


def service_transfer_roots() -> tuple[Path, ...]:
    """Explicit machine-only archive roots; an empty setting permits no paths."""
    configured = os.getenv("FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS", "")
    return tuple(
        dict.fromkeys(
            Path(value.strip()).expanduser().resolve()
            for value in configured.split(os.pathsep)
            if value.strip()
        )
    )
