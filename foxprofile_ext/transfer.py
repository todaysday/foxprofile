"""Managed, actor-scoped staging for portable profile archive transfers."""

import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from src.models.profile import Profile
from src.services.profile.transfer import export_to_zip, read_profile_meta

from .catalog import ProfileCatalog
from .dto import PrincipalDTO
from .errors import DomainError, ErrorCode
from .locking import catalog_lock
from .policy import require_personal
from .storage.dao import TransferHandleDAO


class ManagedProfileTransfer:
    def __init__(
        self,
        catalog: ProfileCatalog,
        handles: TransferHandleDAO,
        staging_dir: str | Path,
        resolve_authorized_profile: Callable[[PrincipalDTO, str], Profile],
        *,
        handle_lifetime_seconds: int = 300,
    ):
        if handle_lifetime_seconds < 1:
            raise ValueError("handle lifetime must be positive")
        self.catalog = catalog
        self.handles = handles
        self.staging_dir = Path(staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_dir.chmod(0o700)
        self.resolve_authorized_profile = resolve_authorized_profile
        self.handle_lifetime_seconds = handle_lifetime_seconds

    def stage_import(
        self,
        principal: PrincipalDTO,
        upload: BinaryIO,
        *,
        target_profile_id: str | None = None,
    ) -> tuple[str, str]:
        """Copy an uploaded stream into private staging and bind a one-use handle."""
        principal = require_personal(principal)
        self._cleanup_expired_handles()
        profile_id = target_profile_id or str(uuid.uuid4())
        if target_profile_id:
            with catalog_lock(self.catalog.lock_path):
                self._authorized_profile(principal, target_profile_id)
        handle_id = str(uuid.uuid4())
        path = self._path(handle_id, ".zip")
        try:
            with path.open("xb") as destination:
                shutil.copyfileobj(upload, destination)
            path.chmod(0o600)
            self.handles.issue(
                handle_id,
                principal.subject_id,
                profile_id,
                str(path),
                time.time() + self.handle_lifetime_seconds,
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return handle_id, profile_id

    def import_staged(
        self,
        principal: PrincipalDTO,
        handle_id: str,
        profile_id: str,
        *,
        overwrite: bool = False,
    ) -> Profile:
        principal = require_personal(principal)
        with catalog_lock(self.catalog.lock_path):
            if overwrite:
                self._authorized_profile(principal, profile_id)
            return self._import_staged(principal, handle_id, profile_id, overwrite=overwrite)

    def _import_staged(
        self,
        principal: PrincipalDTO,
        handle_id: str,
        profile_id: str,
        *,
        overwrite: bool,
    ) -> Profile:
        handle = self.handles.consume(handle_id, principal.subject_id, profile_id, time.time())
        if handle is None:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        path = self._managed_path(handle.server_path)
        try:
            metadata = read_profile_meta(str(path))
            if metadata is None:
                raise DomainError(ErrorCode.VALIDATION_ERROR)
            return self.catalog.import_archive(
                principal.subject_id,
                str(path),
                target_profile_id=profile_id if overwrite else None,
                new_profile_id=profile_id if not overwrite else None,
            )
        finally:
            path.unlink(missing_ok=True)

    def stage_export(
        self,
        principal: PrincipalDTO,
        profile_id: str,
        *,
        include_data: bool = True,
    ) -> str:
        principal = require_personal(principal)
        self._cleanup_expired_handles()
        with catalog_lock(self.catalog.lock_path):
            profile = self._authorized_profile(principal, profile_id)
            handle_id = str(uuid.uuid4())
            path = self._path(handle_id, ".zip")
            data_path = self.catalog.manager._data_path(profile.name)
            ok, result = export_to_zip(profile, data_path, str(self.staging_dir), include_data)
            if not ok:
                raise DomainError(ErrorCode.VALIDATION_ERROR)
            generated = Path(result)
            try:
                generated.replace(path)
                path.chmod(0o600)
                self.handles.issue(
                    handle_id,
                    principal.subject_id,
                    profile_id,
                    str(path),
                    time.time() + self.handle_lifetime_seconds,
                )
            except Exception:
                generated.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
                raise
            return handle_id

    @contextmanager
    def download(
        self, principal: PrincipalDTO, handle_id: str, profile_id: str
    ) -> Iterator[BinaryIO]:
        principal = require_personal(principal)
        with catalog_lock(self.catalog.lock_path):
            self._authorized_profile(principal, profile_id)
            handle = self.handles.consume(handle_id, principal.subject_id, profile_id, time.time())
            if handle is None:
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
            path = self._managed_path(handle.server_path)
            if not path.is_file():
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
            try:
                with path.open("rb") as source:
                    yield source
            finally:
                path.unlink(missing_ok=True)

    def _authorized_profile(self, principal: PrincipalDTO, profile_id: str) -> Profile:
        authorized = self.resolve_authorized_profile(principal, profile_id)
        current = self.catalog.get_profile(profile_id)
        if authorized.profile_id != current.profile_id:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return current

    def revoke_profile(self, profile_id: str) -> None:
        self.handles.revoke_profile(profile_id)

    def _path(self, handle_id: str, suffix: str) -> Path:
        path = self.staging_dir / f"{handle_id}{suffix}"
        if path.parent.resolve() != self.staging_dir.resolve():
            raise ValueError("invalid transfer handle")
        return path

    def _cleanup_expired_handles(self) -> None:
        now = time.time()
        for handle in self.handles.expired(now):
            try:
                path = self._expired_path(handle.server_path)
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    if not path.is_file():
                        continue
                    path.unlink()
                self.handles.remove_expired(handle.handle_id, now)
            except (OSError, ValueError):
                continue

    def _expired_path(self, server_path: str) -> Path:
        path = Path(server_path)
        if (
            path.parent.resolve() != self.staging_dir.resolve()
            or path.suffix != ".zip"
            or str(uuid.UUID(path.stem)) != path.stem
        ):
            raise ValueError("invalid expired transfer path")
        return path

    def _managed_path(self, server_path: str) -> Path:
        path = Path(server_path)
        if path.parent.resolve() != self.staging_dir.resolve() or path.is_symlink():
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return path
