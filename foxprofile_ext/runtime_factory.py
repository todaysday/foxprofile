"""Compose protected runtime from trusted server state without browser downloads."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from src.models.profile import Profile
from src.services.browser.launcher import BrowserLauncher

from .auth import AuthService
from .capacity import CapacityService
from .catalog import CatalogOperationError, ProfileCatalog
from .dto import PrincipalDTO, ProfileRefDTO
from .errors import DomainError, ErrorCode
from .policy import require_profile_access
from .runtime.cache import geoip_cache_directory, prepare_geoip_cache
from .runtime.coordinator import RuntimeCoordinator
from .runtime.processes import RuntimeSupervisor
from .storage import AssignmentDAO, CapacityLedger, ExtensionDatabase


def _installed_executable(engine: str) -> Path | None:
    configured = os.getenv(f"FOXPROFILE_{engine.upper()}_EXECUTABLE", "").strip()
    if configured:
        candidate = Path(configured)
        if (
            not candidate.is_absolute()
            or not candidate.is_file()
            or not os.access(candidate, os.X_OK)
        ):
            raise ValueError(
                f"FOXPROFILE_{engine.upper()}_EXECUTABLE must be an installed executable"
            )
        return candidate.resolve()
    if engine == "chrome":
        from chrome_engine.fetch import installed_executable

        candidate = installed_executable()
    else:
        from camoufox.exceptions import CamoufoxNotInstalled, UnsupportedVersion
        from camoufox.pkgman import camoufox_path, launch_path

        try:
            candidate = Path(launch_path(camoufox_path(download_if_missing=False)))
        except (CamoufoxNotInstalled, UnsupportedVersion):
            return None
    if candidate is None or not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    return candidate.resolve()


def build_run_coordinator(
    database: ExtensionDatabase,
    catalog: ProfileCatalog,
    auth_service: AuthService,
    assignments: AssignmentDAO,
    ledger: CapacityLedger,
) -> RuntimeCoordinator:
    """Build one launcher/supervisor pair and reconcile durable runs before use."""
    capacity = CapacityService(ledger, assignments)

    def profile_by_id(profile_id: str) -> Profile:
        try:
            return catalog.get_profile(profile_id)
        except CatalogOperationError:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND) from None

    def profile_by_name(name: str) -> Profile:
        profile = next((item for item in catalog.list_profiles() if item.name == name), None)
        if profile is None:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return profile

    def authorize(principal: PrincipalDTO, ref: ProfileRefDTO) -> PrincipalDTO:
        actor = auth_service.validate_principal(principal)
        current = profile_by_id(ref.profile_id)
        if current.name != ref.name:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        require_profile_access(
            actor, ProfileRefDTO(current.profile_id, current.name), assignments.is_assigned
        )
        return actor

    # Fixed system assets and the active Python environment are read-only.
    # Browser caches are added only at the exact installed binary's directory.
    paths = [
        Path(item)
        for item in (
            "/usr",
            "/bin",
            "/lib",
            "/lib64",
            "/etc/fonts",
            "/etc/ssl/certs",
            "/etc/resolv.conf",
            "/etc/hosts",
            "/etc/nsswitch.conf",
        )
    ]
    paths.append(Path(sys.prefix))
    paths.append(Path(sys.base_prefix))
    settings = {
        name: os.environ[name]
        for name in (
            "LANG",
            "LC_ALL",
            "TZ",
            "FOXPROFILE_LANG",
            "FOXPROFILE_RESTORE_TABS",
            "FOXPROFILE_PROXY_TIMEOUT",
        )
        if name in os.environ
    }
    settings["FOXPROFILE_LOG_DIR"] = "/tmp/logs"
    for engine in ("camoufox", "chrome"):
        executable = _installed_executable(engine)
        if executable is not None:
            paths.append(executable.parent)
            settings[f"FOXPROFILE_{engine.upper()}_EXECUTABLE"] = str(executable)
            if engine == "camoufox":
                cache = geoip_cache_directory()
                # Only create the empty mount point at startup. Database
                # downloads remain part of an explicit browser launch.
                cache.mkdir(parents=True, exist_ok=True)
                paths.append(cache)
                settings["XDG_CACHE_HOME"] = str(cache.parent.parent)

    def prepare_launch(profile: Profile) -> None:
        if profile.engine == "camoufox":
            prepare_geoip_cache()

    runtime = RuntimeSupervisor(
        database,
        data_dir=catalog.manager.data_dir,
        code_root=Path(__file__).resolve().parents[1],
        runtime_paths=tuple(dict.fromkeys(path for path in paths if path.exists())),
        settings=settings,
        capacity=capacity,
        profile_by_name=profile_by_name,
        authorize=authorize,
        lock_path=str(catalog.lock_path),
        prepare_launch=prepare_launch,
    )
    runtime.recover()
    launcher = BrowserLauncher(runtime_adapter=runtime)
    return RuntimeCoordinator(
        capacity,
        ledger,
        runtime,
        launcher,
        profile_by_id=profile_by_id,
        authorize=authorize,
        lock_path=str(catalog.lock_path),
    )
