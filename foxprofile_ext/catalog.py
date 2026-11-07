"""Stable-identity and recovery boundary around the upstream profile catalog."""

import json
import time
import uuid
from pathlib import Path

from src.models.profile import Profile
from src.services.profile.manager import ProfileManager
from src.services.profile.transfer import read_profile_meta

from .locking import catalog_lock
from .storage.dao import (
    AssignmentDAO,
    CatalogOperationDAO,
    CatalogOperationRecord,
    TransferHandleDAO,
)


class CatalogOperationError(RuntimeError):
    """A profile catalog operation could not be completed safely."""


class ProfileCatalog:
    """Internal catalog adapter; actor IDs record ownership, never authorize calls.

    A trusted facade must authenticate and authorize the current principal at
    each effect boundary, using ``get_profile`` for pending-aware resolution.
    """

    def __init__(
        self,
        manager: ProfileManager,
        operations: CatalogOperationDAO,
        assignments: AssignmentDAO,
        lock_path: str | Path,
        transfer_handles: TransferHandleDAO | None = None,
    ):
        self.manager = manager
        self.operations = operations
        self.assignments = assignments
        self.transfer_handles = transfer_handles
        self.lock_path = Path(lock_path)
        self.migrate_profile_ids()

    def migrate_profile_ids(self) -> None:
        """Assign missing local IDs once, under the same mutation lock."""
        with catalog_lock(self.lock_path):
            self.manager.reload_profiles()
            catalog_path = self.manager.profiles_file
            raw = (
                json.loads(catalog_path.read_text(encoding="utf-8"))
                if catalog_path.exists()
                else {}
            )
            changed = self._repair_ids(raw)
            if changed:
                operation_id = str(uuid.uuid4())
                first = next(iter(self.manager.profiles.values()))
                self.operations.begin(
                    operation_id,
                    "identity_migration",
                    first.profile_id,
                    time.time(),
                )
                self.manager.save_profiles()
                self.operations.finish(operation_id, "complete", time.time())

    def list_profiles(self) -> tuple[Profile, ...]:
        with catalog_lock(self.lock_path):
            self.manager.reload_profiles()
            hidden = {record.profile_id for record in self.operations.pending()}
            return tuple(
                profile
                for profile in self.manager.list_profiles()
                if profile.profile_id not in hidden
            )

    def get_profile(self, profile_id: str) -> Profile:
        with catalog_lock(self.lock_path):
            self.manager.reload_profiles()
            return self._by_id(profile_id)

    def create(self, actor_user_id: str | None, **fields: object) -> Profile:
        with catalog_lock(self.lock_path):
            self.manager.reload_profiles()
            profile_id = str(uuid.uuid4())
            name = str(fields["name"])
            record = self._begin("create", profile_id, actor_user_id, destination=name)
            created = self.manager.add_profile(
                name,
                str(fields.get("proxy") or ""),
                str(fields.get("os_type") or "windows"),
                fields.get("timezone") if isinstance(fields.get("timezone"), str) else None,
                fields.get("locale") if isinstance(fields.get("locale"), str) else None,
                str(fields.get("engine") or "camoufox"),
                profile_id=profile_id,
            )
            if not created:
                self.operations.finish(record.operation_id, "failed", time.time())
                raise CatalogOperationError("profile could not be created")
            profile = self.manager.profiles[name]
            if actor_user_id is not None:
                self.assignments.grant(actor_user_id, profile_id)
            self.operations.finish(record.operation_id, "complete", time.time())
            return profile

    def rename(
        self, actor_user_id: str | None, profile_id: str, new_name: str, **fields: object
    ) -> Profile:
        with catalog_lock(self.lock_path):
            self.manager.reload_profiles()
            profile = self._by_id(profile_id)
            record = self._begin(
                "rename", profile_id, actor_user_id, source=profile.name, destination=new_name
            )
            updated = self.manager.update_profile(
                profile.name,
                new_name,
                str(fields.get("proxy", profile.proxy or "")),
                str(fields.get("os_type", profile.os_type)),
                fields.get("timezone", profile.timezone),
                fields.get("locale", profile.locale),
            )
            if not updated:
                self.operations.finish(record.operation_id, "failed", time.time())
                raise CatalogOperationError("profile could not be renamed")
            result = self.manager.profiles[new_name]
            self.operations.finish(record.operation_id, "complete", time.time())
            return result

    def import_archive(
        self,
        actor_user_id: str | None,
        archive_path: str,
        *,
        target_profile_id: str | None = None,
        new_profile_id: str | None = None,
    ) -> Profile:
        if read_profile_meta(archive_path) is None:
            raise CatalogOperationError("profile archive metadata is invalid")
        with catalog_lock(self.lock_path):
            self.manager.reload_profiles()
            target = self._by_id(target_profile_id) if target_profile_id else None
            record = self._begin(
                "import",
                target.profile_id if target else (new_profile_id or str(uuid.uuid4())),
                actor_user_id,
                source=target.name if target else None,
                destination=target.name if target else None,
                target_profile_id=target.profile_id if target else None,
            )
            succeeded, result = self.manager.import_profile(
                archive_path,
                overwrite=target is not None,
                target_name=target.name if target else None,
                profile_id=record.profile_id if target is None else None,
            )
            if not succeeded:
                self.operations.finish(record.operation_id, "failed", time.time())
                raise CatalogOperationError(str(result))
            profile = self.manager.profiles[str(result)]
            if target is None and actor_user_id is not None:
                self.assignments.grant(actor_user_id, profile.profile_id)
            self.operations.finish(record.operation_id, "complete", time.time())
            return profile

    def delete(self, actor_user_id: str | None, profile_id: str) -> None:
        with catalog_lock(self.lock_path):
            self.manager.reload_profiles()
            profile = self._by_id(profile_id)
            record = self._begin("delete", profile_id, actor_user_id, source=profile.name)
            if not self.manager.delete_profile(profile.name, operation_id=record.operation_id):
                self.operations.finish(record.operation_id, "failed", time.time())
                raise CatalogOperationError("profile could not be deleted")
            self.assignments.revoke_profile(profile_id)
            if self.transfer_handles is not None:
                self.transfer_handles.revoke_profile(profile_id)
            self.operations.finish(record.operation_id, "complete", time.time())

    def recover(self) -> tuple[str, ...]:
        """Complete or fail pending operations from persisted catalog state."""
        recovered: list[str] = []
        with catalog_lock(self.lock_path):
            self.manager.reload_profiles()
            for record in self.operations.pending():
                self._recover_one(record)
                recovered.append(record.operation_id)
        return tuple(recovered)

    def pending_profile_ids(self) -> frozenset[str]:
        return frozenset(record.profile_id for record in self.operations.pending())

    def _recover_one(self, record: CatalogOperationRecord) -> None:
        profile = next(
            (
                item
                for item in self.manager.profiles.values()
                if item.profile_id == record.profile_id
            ),
            None,
        )
        if record.kind in {"create", "import"}:
            if profile is not None:
                needs_creator_grant = record.kind == "create" or (
                    record.kind == "import" and record.target_profile_id is None
                )
                if record.actor_user_id is not None and needs_creator_grant:
                    self.assignments.grant(record.actor_user_id, record.profile_id)
                self.operations.finish(record.operation_id, "complete", time.time())
            else:
                self.operations.finish(record.operation_id, "failed", time.time())
        elif record.kind == "rename":
            if profile is not None and profile.name == record.destination_name:
                self.operations.finish(record.operation_id, "complete", time.time())
            elif profile is not None and profile.name == record.source_name:
                self.operations.finish(record.operation_id, "failed", time.time())
        elif record.kind == "delete":
            if profile is None and record.source_name:
                self.manager.quarantine_data(record.source_name, record.operation_id)
                self.assignments.revoke_profile(record.profile_id)
                if self.transfer_handles is not None:
                    self.transfer_handles.revoke_profile(record.profile_id)
                self.operations.finish(record.operation_id, "complete", time.time())
            elif profile is not None:
                self.operations.finish(record.operation_id, "failed", time.time())
        elif record.kind == "identity_migration":
            catalog_path = self.manager.profiles_file
            raw = (
                json.loads(catalog_path.read_text(encoding="utf-8"))
                if catalog_path.exists()
                else {}
            )
            changed = self._repair_ids(raw)
            if changed:
                self.manager.save_profiles()
            self.operations.finish(record.operation_id, "complete", time.time())

    def _repair_ids(self, raw: dict[str, object]) -> bool:
        seen: set[str] = set()
        changed = False
        for name, profile in self.manager.profiles.items():
            stored = raw.get(name)
            identity = stored.get("profile_id") if isinstance(stored, dict) else None
            if not isinstance(identity, str) or not identity or identity in seen:
                profile.profile_id = str(uuid.uuid4())
                changed = True
            seen.add(profile.profile_id)
        return changed

    def _by_id(self, profile_id: str | None) -> Profile:
        if profile_id in self.pending_profile_ids():
            raise CatalogOperationError("profile is unavailable")
        for profile in self.manager.profiles.values():
            if profile.profile_id == profile_id:
                return profile
        raise CatalogOperationError("profile is unavailable")

    def _begin(
        self,
        kind: str,
        profile_id: str,
        actor_user_id: str | None,
        *,
        source: str | None = None,
        destination: str | None = None,
        target_profile_id: str | None = None,
    ) -> CatalogOperationRecord:
        return self.operations.begin(
            str(uuid.uuid4()),
            kind,
            profile_id,
            time.time(),
            source_name=source,
            destination_name=destination,
            actor_user_id=actor_user_id,
            target_profile_id=target_profile_id,
        )
