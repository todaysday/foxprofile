"""Protected application facade for profile discovery and browser starts."""

from __future__ import annotations

import math
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TypeVar

from src.models.profile import ENGINES, Profile
from src.services.profile.transfer import export_to_zip, read_profile_meta
from src.utils.validation import (
    validate_locale,
    validate_profile_name,
    validate_proxy_format,
    validate_timezone,
)

from .auth import AuthService
from .catalog import CatalogOperationError, ProfileCatalog
from .config import service_transfer_roots
from .dto import PrincipalDTO, PrincipalKind, ProfileRefDTO, Role
from .errors import DomainError, ErrorCode
from .locking import catalog_lock
from .policy import require_admin, require_personal, require_profile_access, require_service
from .request_context import principal_scope
from .run_contracts import RunCoordinator, RunLaunchResult, RunStatusResult
from .storage.capacity import CapacityLedger
from .storage.dao import AssignmentDAO, SettingsDAO
from .transfer import ManagedProfileTransfer

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class VisibleProfileDTO:
    """Profile fields safe for general discovery responses."""

    profile_id: str
    name: str


@dataclass(frozen=True, slots=True)
class RunningProfileDTO:
    """Filtered profile identity and lifecycle state; excludes run internals."""

    profile_id: str
    name: str
    state: str


@dataclass(frozen=True, slots=True)
class CapacitySnapshotDTO:
    max_concurrent_profiles: int
    occupied: int


@contextmanager
def _service_transfer_path(supplied: str | None, *, directory: bool) -> Iterator[tuple[int, Path]]:
    """Pin an allowlisted path without following any symlink component."""
    roots = service_transfer_roots()
    if not roots:
        raise PermissionError(
            "Configure FOXPROFILE_EXT_SERVICE_TRANSFER_ROOTS for service archives"
        )
    if supplied is None and directory:
        path = roots[0]
    elif isinstance(supplied, str) and supplied.strip():
        path = Path(os.path.abspath(os.path.expanduser(supplied)))
    else:
        raise PermissionError("A path inside a configured service transfer root is required")
    if not any(path.is_relative_to(root) for root in roots):
        raise PermissionError("Path is outside configured service transfer roots")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise PermissionError("Confined service archive paths are unavailable on this platform")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory or index < len(path.parts) - 2:
                flags |= os.O_DIRECTORY
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise PermissionError("Service archive path has an unsupported file type")
        yield descriptor, path
    except OSError:
        raise PermissionError(
            "Service archive path must exist without symlink components"
        ) from None
    finally:
        os.close(descriptor)


class ProtectedApplication:
    """Shared direct-service/API boundary; no implicit principal or raw services."""

    def __init__(
        self,
        auth_service: AuthService,
        catalog: ProfileCatalog,
        assignments: AssignmentDAO,
        capacity_ledger: CapacityLedger,
        run_coordinator: RunCoordinator | None = None,
        *,
        settings: SettingsDAO | None = None,
        transfers: ManagedProfileTransfer | None = None,
    ) -> None:
        self._auth = auth_service
        self._catalog = catalog
        self._assignments = assignments
        self._capacity = capacity_ledger
        self._run_coordinator = run_coordinator
        self._settings = settings
        self._transfers = transfers

    def admin_settings(self, principal: PrincipalDTO | None) -> CapacitySnapshotDTO:
        actor = self.current_principal(principal)
        require_admin(actor)
        if self._settings is None:
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        settings = self._settings.get_capacity()
        return CapacitySnapshotDTO(
            settings.max_concurrent_profiles, len(self._capacity.active_runs())
        )

    def update_admin_settings(
        self, principal: PrincipalDTO | None, max_concurrent_profiles: int
    ) -> CapacitySnapshotDTO:
        actor = self.current_principal(principal)
        require_admin(actor)
        if self._settings is None:
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        self._settings.set_capacity(max_concurrent_profiles)
        return self.admin_settings(actor)

    def export_service_archive(
        self,
        principal: PrincipalDTO | None,
        identifier: str,
        export_dir: str | None,
        *,
        include_data: bool = True,
    ) -> str:
        """Preserve machine exports only within administrator-configured roots."""
        if type(include_data) is not bool:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with catalog_lock(self._catalog.lock_path):
            actor = require_service(self.current_principal(principal))
            profile = self._resolve_profile_locked(actor, identifier)
            with (
                _service_transfer_path(export_dir, directory=True) as (directory_fd, directory),
                tempfile.TemporaryDirectory(prefix="foxprofile-service-export-") as staging,
            ):
                ok, result = export_to_zip(
                    profile,
                    self._catalog.manager._data_path(profile.name),
                    staging,
                    include_data,
                )
                if not ok:
                    raise DomainError(ErrorCode.VALIDATION_ERROR)
                generated = Path(result)
                filename = f"{generated.stem}_{uuid.uuid4().hex}.zip"
                # Keep the opened directory pinned even if its pathname is
                # replaced while the portable archive is being generated.
                with _service_transfer_path(str(directory), directory=True) as (current_fd, _):
                    original = os.fstat(directory_fd)
                    current = os.fstat(current_fd)
                    if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                        raise PermissionError("Service export directory changed during transfer")
                destination_fd = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    with (
                        os.fdopen(destination_fd, "wb") as destination,
                        generated.open("rb") as source,
                    ):
                        shutil.copyfileobj(source, destination)
                except Exception:
                    os.unlink(filename, dir_fd=directory_fd)
                    raise
                return str(directory / filename)

    def import_service_archive(
        self,
        principal: PrincipalDTO | None,
        zip_path: str,
        *,
        overwrite: bool = False,
    ) -> Profile:
        """Copy one pinned service input to private staging before native import."""
        if type(overwrite) is not bool:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with catalog_lock(self._catalog.lock_path):
            actor = require_service(self.current_principal(principal))
            with (
                _service_transfer_path(zip_path, directory=False) as (source_fd, _),
                tempfile.TemporaryDirectory(prefix="foxprofile-service-import-") as staging,
            ):
                staged = Path(staging) / "profile.zip"
                with (
                    os.fdopen(os.dup(source_fd), "rb") as source,
                    staged.open("xb") as destination,
                ):
                    shutil.copyfileobj(source, destination)
                metadata = read_profile_meta(str(staged))
                name = metadata.get("name") if isinstance(metadata, dict) else None
                if not isinstance(name, str) or not validate_profile_name(name)[0]:
                    raise DomainError(ErrorCode.VALIDATION_ERROR)
                target_id = None
                if overwrite:
                    existing = next(
                        (
                            profile
                            for profile in self._catalog.list_profiles()
                            if profile.name.casefold() == name.casefold()
                        ),
                        None,
                    )
                    if existing is not None:
                        target = self._resolve_profile_locked(actor, existing.profile_id)
                        self._require_stopped(target.profile_id)
                        target_id = target.profile_id
                return self._catalog.import_archive(None, str(staged), target_profile_id=target_id)

    def import_profile_archive(
        self,
        principal: PrincipalDTO | None,
        upload: BinaryIO,
        *,
        target_profile_id: str | None = None,
    ) -> Profile:
        with catalog_lock(self._catalog.lock_path):
            actor = require_personal(self.current_principal(principal))
            if target_profile_id is not None:
                ref = self._ref_by_id_locked(actor, target_profile_id)
                self._require_stopped(ref.profile_id)
            if self._transfers is None:
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)
            handle_id, profile_id = self._transfers.stage_import(
                actor, upload, target_profile_id=target_profile_id
            )
            return self._transfers.import_staged(
                actor, handle_id, profile_id, overwrite=target_profile_id is not None
            )

    def export_profile_archive(
        self, principal: PrincipalDTO | None, profile_id: str, *, include_data: bool = True
    ) -> str:
        if type(include_data) is not bool:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with catalog_lock(self._catalog.lock_path):
            actor = require_personal(self.current_principal(principal))
            self._ref_by_id_locked(actor, profile_id)
            if self._transfers is None:
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)
            return self._transfers.stage_export(actor, profile_id, include_data=include_data)

    @contextmanager
    def download_profile_archive(
        self, principal: PrincipalDTO | None, download_id: str, profile_id: str
    ) -> Iterator[BinaryIO]:
        with catalog_lock(self._catalog.lock_path):
            actor = require_personal(self.current_principal(principal))
            self._ref_by_id_locked(actor, profile_id)
            if self._transfers is None:
                raise DomainError(ErrorCode.RUNTIME_RECOVERING)
            with self._transfers.download(actor, download_id, profile_id) as source:
                yield source

    def current_principal(self, principal: PrincipalDTO | None) -> PrincipalDTO:
        """Refresh live personal-session authority for this interaction."""
        return self._auth.validate_principal(principal)

    def list_profiles(self, principal: PrincipalDTO | None) -> tuple[VisibleProfileDTO, ...]:
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            visible = []
            for profile in self._catalog.list_profiles():
                ref = ProfileRefDTO(profile.profile_id, profile.name)
                try:
                    require_profile_access(actor, ref, self._assignments.is_assigned)
                except DomainError as error:
                    if error.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND:
                        continue
                    raise
                visible.append(VisibleProfileDTO(ref.profile_id, ref.name))
            return tuple(visible)

    def profile_count(self, principal: PrincipalDTO | None) -> int:
        return len(self.list_profiles(principal))

    def resolve_profile(self, principal: PrincipalDTO | None, name_or_id: str) -> ProfileRefDTO:
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            profile = self._resolve_profile_locked(actor, name_or_id)
            return ProfileRefDTO(profile.profile_id, profile.name)

    def perform_profile(
        self,
        principal: PrincipalDTO | None,
        identifier: str,
        effect: Callable[[Profile], _T],
    ) -> _T:
        """Authorize and run one trusted synchronous adapter under catalog lock."""
        if not callable(effect):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            profile = self._resolve_profile_locked(actor, identifier)
            with principal_scope(actor):
                return effect(profile)

    def perform_authenticated(self, principal: PrincipalDTO | None, effect: Callable[[], _T]) -> _T:
        """Run a trusted synchronous adapter that needs identity but no profile."""
        if not callable(effect):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        actor = self.current_principal(principal)
        with principal_scope(actor):
            return effect()

    def create_profile(
        self,
        principal: PrincipalDTO | None,
        *,
        name: str,
        proxy: str = "",
        os_type: str | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        engine: str = "camoufox",
    ) -> Profile:
        """Create one profile and its ordinary creator grant under the catalog lock."""
        if os_type is None:
            os_type = "windows" if engine == "chrome" else "linux"
        self._validate_profile_fields(name, proxy, os_type, timezone, locale, engine)
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            creator_id = actor.subject_id if actor.kind is PrincipalKind.PERSONAL else None
            return self._catalog.create(
                creator_id,
                name=name,
                proxy=proxy,
                os_type=os_type,
                timezone=timezone,
                locale=locale,
                engine=engine,
            )

    def update_profile(
        self,
        principal: PrincipalDTO | None,
        identifier: str,
        fields: Mapping[str, object],
    ) -> Profile:
        if not isinstance(fields, Mapping) or not set(fields).issubset(
            {"name", "proxy", "os_type", "timezone", "locale"}
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            profile = self._resolve_profile_locked(actor, identifier)
            values: dict[str, object] = {
                "name": profile.name,
                "proxy": profile.proxy or "",
                "os_type": profile.os_type,
                "timezone": profile.timezone,
                "locale": profile.locale,
            }
            values.update(fields)
            name = values["name"]
            proxy = values["proxy"] or ""
            os_type = values["os_type"]
            timezone = values["timezone"]
            locale = values["locale"]
            self._validate_profile_fields(name, proxy, os_type, timezone, locale, profile.engine)
            if name != profile.name:
                self._require_stopped(profile.profile_id)
            actor_user_id = actor.subject_id if actor.kind is PrincipalKind.PERSONAL else None
            return self._catalog.rename(
                actor_user_id,
                profile.profile_id,
                name,
                proxy=proxy,
                os_type=os_type,
                timezone=timezone,
                locale=locale,
            )

    def delete_profile(self, principal: PrincipalDTO | None, identifier: str) -> None:
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            profile = self._resolve_profile_locked(actor, identifier)
            self._require_stopped(profile.profile_id)
            actor_user_id = actor.subject_id if actor.kind is PrincipalKind.PERSONAL else None
            self._catalog.delete(actor_user_id, profile.profile_id)

    @staticmethod
    def _validate_profile_fields(
        name: object,
        proxy: object,
        os_type: object,
        timezone: object,
        locale: object,
        engine: object,
    ) -> None:
        if (
            not isinstance(name, str)
            or not validate_profile_name(name)[0]
            or not isinstance(proxy, str)
            or not validate_proxy_format(proxy)[0]
            or not isinstance(os_type, str)
            or os_type not in {"windows", "macos", "linux"}
            or (timezone is not None and not isinstance(timezone, str))
            or not validate_timezone(timezone or "")[0]
            or (locale is not None and not isinstance(locale, str))
            or not validate_locale(locale or "")[0]
            or not isinstance(engine, str)
            or engine not in ENGINES
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)

    def replace_assignments(
        self,
        principal: PrincipalDTO | None,
        user_id: str,
        profile_ids: Sequence[str],
    ) -> tuple[str, ...]:
        """Serialize grant replacement with catalog resolution and profile effects."""
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            require_admin(actor)
            if isinstance(profile_ids, (str, bytes)) or not isinstance(profile_ids, Sequence):
                raise DomainError(ErrorCode.VALIDATION_ERROR)
            ids = tuple(profile_ids)
            for profile_id in ids:
                if not isinstance(profile_id, str) or not profile_id.strip():
                    raise DomainError(ErrorCode.VALIDATION_ERROR)
                try:
                    self._catalog.get_profile(profile_id)
                except CatalogOperationError:
                    raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND) from None
            return self._auth.replace_assignments(actor, user_id, ids)

    def running_profiles(self, principal: PrincipalDTO | None) -> tuple[RunningProfileDTO, ...]:
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            running: list[RunningProfileDTO] = []
            for run in self._capacity.active_runs():
                if not self._may_access_id(actor, run.ref.profile_id):
                    continue
                try:
                    profile = self._catalog.get_profile(run.ref.profile_id)
                except CatalogOperationError:
                    continue
                ref = ProfileRefDTO(profile.profile_id, profile.name)
                try:
                    require_profile_access(actor, ref, self._assignments.is_assigned)
                except DomainError as error:
                    if error.code is ErrorCode.FORBIDDEN_OR_NOT_FOUND:
                        continue
                    raise
                running.append(RunningProfileDTO(ref.profile_id, ref.name, run.status.value))
            return tuple(running)

    def running_count(self, principal: PrincipalDTO | None) -> int:
        return len(self.running_profiles(principal))

    def start_profile(
        self,
        principal: PrincipalDTO | None,
        name_or_id: str,
        *,
        wait: bool = False,
        timeout: float = 30,
    ) -> RunLaunchResult:
        results = self.start_profiles(principal, (name_or_id,), wait=wait, timeout=timeout)
        return results[0]

    def start_profiles(
        self,
        principal: PrincipalDTO | None,
        names_or_ids: Sequence[str],
        *,
        wait: bool = False,
        timeout: float = 30,
    ) -> tuple[RunLaunchResult, ...]:
        self._validate_launch_options(wait, timeout)
        if isinstance(names_or_ids, (str, bytes)) or not isinstance(names_or_ids, Sequence):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        identifiers = tuple(names_or_ids)
        if not identifiers or any(
            not isinstance(identifier, str) or not identifier.strip() for identifier in identifiers
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)

        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            refs = self._resolve_profiles_locked(actor, identifiers)
        coordinator = self._run_coordinator
        reserve_and_start = getattr(coordinator, "reserve_and_start", None)
        if not callable(reserve_and_start):
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)

        # The coordinator repeats live identity, current profile, and grant
        # checks under its admission lock. It releases that lock before any
        # optional readiness wait, so unrelated catalog calls remain available.
        results = reserve_and_start(actor, refs, wait=wait, timeout=timeout)
        if (
            not isinstance(results, tuple)
            or len(results) != len(refs)
            or any(not isinstance(result, RunLaunchResult) for result in results)
            or any(
                result.profile_id != ref.profile_id or result.profile_name != ref.name
                for result, ref in zip(results, refs, strict=True)
            )
        ):
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        return results

    def run_status(self, principal: PrincipalDTO | None, identifier: str) -> RunStatusResult | None:
        return self._coordinated_profile_call(principal, identifier, "status")

    def stop_run(
        self,
        principal: PrincipalDTO | None,
        identifier: str,
        *,
        timeout: float = 30,
    ) -> bool:
        self._validate_launch_options(False, timeout)
        result = self._coordinated_profile_call(principal, identifier, "stop", timeout=timeout)
        if type(result) is not bool:
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        return result

    def control_run(
        self,
        principal: PrincipalDTO | None,
        identifier: str,
        action: str,
        params: dict[str, object] | None = None,
    ) -> object:
        if (
            not isinstance(action, str)
            or not action.strip()
            or (params is not None and not isinstance(params, dict))
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        return self._coordinated_profile_call(
            principal, identifier, "control", action=action, params=params
        )

    def _coordinated_profile_call(
        self,
        principal: PrincipalDTO | None,
        identifier: str,
        operation: str,
        **options: object,
    ) -> object:
        with catalog_lock(self._catalog.lock_path):
            actor = self.current_principal(principal)
            profile = self._resolve_profile_locked(actor, identifier)
            ref = ProfileRefDTO(profile.profile_id, profile.name)
        coordinator = self._run_coordinator
        call = getattr(coordinator, operation, None)
        if not callable(call):
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        result = call(actor, ref, **options)
        if operation == "status" and (
            result is not None
            and (
                not isinstance(result, RunStatusResult)
                or result.profile_id != ref.profile_id
                or result.profile_name != ref.name
            )
        ):
            raise DomainError(ErrorCode.RUNTIME_RECOVERING)
        return result

    def _resolve_profiles_locked(
        self, principal: PrincipalDTO, identifiers: Sequence[str]
    ) -> tuple[ProfileRefDTO, ...]:
        profiles = self._catalog.list_profiles()
        resolved: list[ProfileRefDTO] = []
        seen: set[str] = set()
        for identifier in identifiers:
            by_id = next((item for item in profiles if item.profile_id == identifier), None)
            candidate = by_id or next((item for item in profiles if item.name == identifier), None)
            if candidate is None:
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
            try:
                current = self._catalog.get_profile(candidate.profile_id)
            except CatalogOperationError:
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND) from None
            if current.profile_id != candidate.profile_id:
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
            ref = ProfileRefDTO(current.profile_id, current.name)
            require_profile_access(principal, ref, self._assignments.is_assigned)
            if ref.profile_id not in seen:
                seen.add(ref.profile_id)
                resolved.append(ref)
        return tuple(resolved)

    def _resolve_profile_locked(self, principal: PrincipalDTO, identifier: str) -> Profile:
        if not isinstance(identifier, str) or not identifier.strip():
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        refs = self._resolve_profiles_locked(principal, (identifier,))
        try:
            profile = self._catalog.get_profile(refs[0].profile_id)
        except CatalogOperationError:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND) from None
        if profile.profile_id != refs[0].profile_id:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return profile

    def _ref_by_id_locked(self, principal: PrincipalDTO, profile_id: str) -> ProfileRefDTO:
        try:
            profile = self._catalog.get_profile(profile_id)
        except CatalogOperationError:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND) from None
        ref = ProfileRefDTO(profile.profile_id, profile.name)
        return require_profile_access(principal, ref, self._assignments.is_assigned)

    def _require_stopped(self, profile_id: str) -> None:
        if any(run.ref.profile_id == profile_id for run in self._capacity.active_runs()):
            raise DomainError(ErrorCode.PROFILE_BUSY)

    def _may_access_id(self, principal: PrincipalDTO, profile_id: str) -> bool:
        return (
            principal.kind is PrincipalKind.SERVICE
            or principal.role is Role.ADMIN
            or self._assignments.is_assigned(principal.subject_id, profile_id)
        )

    @staticmethod
    def _validate_launch_options(wait: bool, timeout: float) -> None:
        if (
            type(wait) is not bool
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
