"""Bounded browser-data snapshots and recoverable canonical profile commits.

The extension database stores only revision, transfer-grant and commit-journal
metadata. Browser files remain in the existing ``data_dir/<profile name>``
layout. Portable archives contain browser data only; the profile catalog,
Master database, ACLs, sessions and launch credentials are never serialized.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from src.services.profile.transfer import (
    _open_profile_data_dir,
    _portable_parents_match,
    _same_file,
)

from ..dto import BrowserRunRefDTO, ProcessIdentity
from ..errors import DomainError, ErrorCode
from ..storage.database import ExtensionDatabase

_ENGINES = {"camoufox", "chrome"}
_IDENTITY_FILES = {"camoufox": "fingerprint.json", "chrome": "chrome_persona.json"}
_GEO_FILE = "geo_locale.json"
_CHUNK_SIZE = 1024 * 1024
_DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_DEFAULT_MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_MAX_MEMBERS = 100_000
_MAX_COMPRESSION_RATIO = 1000
_TRANSIENT_DIRS = {
    "cache",
    "cache2",
    "code cache",
    "crash reports",
    "crashes",
    "crashpad",
    "dawncache",
    "gpucache",
    "grshadercache",
    "shadercache",
    "startupcache",
    "tmp",
    "temp",
}
_TRANSIENT_FILES = {
    ".parentlock",
    "parent.lock",
    "lock",
    "singletoncookie",
    "singletonlock",
    "singletonsockets",
    "devtoolsactiveport",
}


class ProfileStoreError(DomainError):
    """A profile synchronization failure with a stable public error code."""

    def __init__(self, code: str, message: str):
        self._detail = message
        super().__init__(ErrorCode(code))

    @property
    def message(self) -> str:
        return self._detail


class ArchiveStagingError(ProfileStoreError):
    """Temporary staging capacity failure; stopped source data remains retryable."""


class _DirectorySyncError(ProfileStoreError):
    """The filesystem could not confirm that profile directory entries are durable."""


@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    archive_digest: str
    size_bytes: int
    identity_digest: str
    expanded_bytes: int
    member_count: int
    content_digest: str


@dataclass(frozen=True, slots=True)
class UnpackedProfile:
    identity_digest: str
    expanded_bytes: int
    member_count: int
    content_digest: str
    directory_stat: os.stat_result = field(repr=False)


@dataclass(frozen=True, slots=True)
class Snapshot:
    profile_id: str
    run_id: str
    base_revision: int
    archive_digest: str
    path: Path
    size_bytes: int
    identity_digest: str


@dataclass(frozen=True, slots=True)
class TransferGrant:
    token: str = field(repr=False)
    profile_id: str
    run_id: str
    worker_id: str
    worker_boot_id: str
    connection_generation: int
    operation: str
    base_revision: int
    archive_digest: str
    expires_at: float
    max_archive_bytes: int


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    profile_id: str
    run_id: str
    revision: int
    archive_digest: str
    receipt_id: str


class ProfileStore:
    """Manage canonical data revisions and exact-run transfer authorization."""

    def __init__(
        self,
        database: ExtensionDatabase,
        data_dir: str | Path,
        staging_dir: str | Path,
        *,
        max_archive_bytes: int = _DEFAULT_MAX_ARCHIVE_BYTES,
        max_expanded_bytes: int = _DEFAULT_MAX_EXPANDED_BYTES,
        max_members: int = _DEFAULT_MAX_MEMBERS,
        clock=time.time,
        profile_by_id=None,
        staging_reserve_bytes: int = 0,
    ) -> None:
        for value, label in (
            (max_archive_bytes, "max_archive_bytes"),
            (max_expanded_bytes, "max_expanded_bytes"),
            (max_members, "max_members"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        self.database = database
        self.data_dir = Path(data_dir).resolve()
        self.staging_dir = Path(staging_dir).resolve()
        self.max_archive_bytes = max_archive_bytes
        self.max_expanded_bytes = max_expanded_bytes
        self.max_members = max_members
        self.clock = clock
        self.profile_by_id = profile_by_id
        if type(staging_reserve_bytes) is not int or staging_reserve_bytes < 0:
            raise ValueError("staging_reserve_bytes must be a nonnegative byte budget")
        self.staging_reserve_bytes = staging_reserve_bytes
        self._upload_lock = threading.RLock()
        self._uploads: dict[str, tuple[object, int]] = {}
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_directory(self.staging_dir)
        self._journal_dir = self.data_dir.with_name(f".{self.data_dir.name}-foxprofile-sync")
        self._secure_directory(self._journal_dir)
        if self._journal_dir.stat().st_dev != self.data_dir.stat().st_dev:
            raise ProfileStoreError(
                "SYNC_FAILED", "Profile journal and canonical data must share a filesystem."
            )
        # A prepared intent must be able to find both roots after restart.
        _fsync_directory(self._journal_dir.parent, required=True)
        self._initialize_tables()
        self._check_legacy_journal()

    def reserve_upload(self, run_id: str, size: int, reservation: object) -> None:
        """Allow one in-flight body per run and reserve its unwritten disk bytes."""
        if type(size) is not int or not 0 <= size <= self.max_archive_bytes:
            raise ProfileStoreError("TRANSFER_INVALID", "Upload size is invalid.")
        with self._upload_lock:
            if run_id in self._uploads:
                raise ProfileStoreError("TRANSFER_CONFLICT", "Upload is already in progress.")
            if size:
                self._check_staging_capacity(size)
            self._uploads[run_id] = (reservation, size)

    def _check_staging_capacity(self, additional: int = 0) -> None:
        required = self.staging_reserve_bytes + self._pending_upload_bytes() + additional
        if shutil.disk_usage(self.staging_dir).free < required:
            raise ArchiveStagingError("SYNC_FAILED", "Insufficient upload staging capacity.")

    def _pending_upload_bytes(self) -> int:
        return sum(remaining for _, remaining in self._uploads.values())

    def write_upload(self, run_id: str, reservation: object, stream, chunk: bytes) -> None:
        with self._upload_lock:
            current = self._uploads.get(run_id)
            if current is None or current[0] is not reservation or len(chunk) > current[1]:
                raise ProfileStoreError("TRANSFER_INVALID", "Upload exceeds reserved size.")
            self._check_staging_capacity()
            stream.write(chunk)
            self._uploads[run_id] = (reservation, current[1] - len(chunk))

    def release_upload(self, run_id: str, reservation: object) -> None:
        with self._upload_lock:
            current = self._uploads.get(run_id)
            if current is not None and current[0] is reservation:
                self._uploads.pop(run_id)

    def _check_legacy_journal(self) -> None:
        # The former path is also a legal tenant profile name. Never move or
        # delete it automatically, especially when a commit still references it.
        legacy = self.data_dir / ".foxprofile-sync"
        if not legacy.exists() and not legacy.is_symlink():
            return
        with self.database.read() as connection:
            entries = tuple(
                connection.execute("SELECT stage_name, backup_name FROM profile_sync_commits")
            )
        for entry in entries:
            for name, prefix in zip(entry, ("incoming-", "backup-"), strict=True):
                if name is None:
                    continue
                self._journal_child(name, prefix)
                if legacy.is_symlink() or (legacy / name).exists() or (legacy / name).is_symlink():
                    raise ProfileStoreError(
                        "SYNC_FAILED", "Legacy profile journal requires verified offline recovery."
                    )

    def _initialize_tables(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS profile_sync_revisions ("
                "profile_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK (revision >= 1), "
                "archive_digest TEXT NOT NULL, identity_digest TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS profile_archive_contents ("
                "archive_digest TEXT PRIMARY KEY, content_digest TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS profile_sync_grants ("
                "token_hash TEXT PRIMARY KEY, profile_id TEXT NOT NULL, run_id TEXT NOT NULL, "
                "worker_id TEXT NOT NULL, worker_boot_id TEXT NOT NULL, "
                "connection_generation INTEGER NOT NULL, operation TEXT NOT NULL "
                "CHECK (operation IN ('download', 'upload')), base_revision INTEGER NOT NULL, "
                "archive_digest TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at REAL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS profile_sync_grants_scope "
                "ON profile_sync_grants(profile_id, run_id, operation)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS profile_sync_snapshots ("
                "profile_id TEXT NOT NULL, run_id TEXT NOT NULL, base_revision INTEGER NOT NULL, "
                "archive_digest TEXT NOT NULL, identity_digest TEXT NOT NULL, archive_name TEXT NOT NULL, "
                "size_bytes INTEGER NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (profile_id, run_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS profile_sync_commits ("
                "profile_id TEXT NOT NULL, run_id TEXT NOT NULL, worker_id TEXT, "
                "worker_boot_id TEXT, connection_generation INTEGER, base_revision INTEGER NOT NULL, "
                "archive_digest TEXT NOT NULL, target_revision INTEGER NOT NULL, "
                "receipt_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL "
                "CHECK (status IN ('prepared', 'committed', 'failed')), "
                "stage_name TEXT, backup_name TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "PRIMARY KEY (profile_id, run_id))"
            )

    @staticmethod
    def _secure_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ProfileStoreError("TRANSFER_INVALID", "Private staging path is unsafe.")
        if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ProfileStoreError(
                "TRANSFER_INVALID", "Private staging directory permissions are too broad."
            )

    def initialize_identity(self, profile: object) -> str:
        """Initialize the engine identity on Master, then return its digest.

        This is deliberately the only generation path in this module. Worker
        helpers below validate the persisted identity and never call it.
        """
        profile_dir = self._profile_dir(profile, create=True)
        engine = self._engine(profile)
        os_type = getattr(profile, "os_type", None)
        if not isinstance(os_type, str) or not os_type:
            raise ProfileStoreError("TRANSFER_INVALID", "Profile engine identity is invalid.")
        try:
            if engine == "camoufox":
                identity_path = profile_dir / _IDENTITY_FILES[engine]
                if identity_path.exists() or identity_path.is_symlink():
                    digest, current = _read_identity(profile_dir, engine)
                    if current.get("os") == os_type:
                        return digest
                    # Canonical OS metadata was explicitly edited on Master.
                    # Reuse local launch semantics before freezing the snapshot;
                    # Worker extraction still cannot regenerate an identity.
                from src.services.browser.fingerprint import load_or_create

                load_or_create(str(profile_dir), os_type)
            else:
                identity_path = profile_dir / _IDENTITY_FILES[engine]
                timezone = getattr(profile, "timezone", None) or ""
                locale = getattr(profile, "locale", None) or ""
                if identity_path.exists() or identity_path.is_symlink():
                    from chrome_engine.persona import save

                    digest, persona = _read_identity(profile_dir, engine)
                    if persona.platform != os_type:
                        from chrome_engine.persona import load_or_create

                        load_or_create(profile_dir, os_type, timezone=timezone, locale=locale)
                        return _identity_digest(profile_dir, engine)
                    # Chrome's runner persists profile timezone/locale overrides
                    # on launch, including clearing those fields when the profile
                    # follows proxy geolocation. Apply the same values on Master
                    # before snapshotting, while retaining the saved device seed.
                    normalized = replace(
                        persona,
                        timezone=timezone or None,
                        locale=locale or None,
                    )
                    if normalized != persona:
                        save(profile_dir, normalized)
                        return _identity_digest(profile_dir, engine)
                    return digest
                from chrome_engine.persona import load_or_create

                load_or_create(profile_dir, os_type, timezone=timezone, locale=locale)
            return _identity_digest(profile_dir, engine)
        except ProfileStoreError:
            raise
        except Exception as exc:
            raise ProfileStoreError(
                "TRANSFER_INVALID", "Could not initialize the canonical browser identity."
            ) from exc

    def capture(self, profile: object, run: object) -> Snapshot:
        """Capture a deterministic snapshot for a pre-admitted stopped run."""
        profile_id = _text_attr(profile, "profile_id")
        run_id = _run_id(run)
        if _run_profile_id(run) != profile_id:
            raise ProfileStoreError("STALE_RUN", "Run does not own this profile.")
        row = self._ledger_row(profile_id, run_id)
        self._check_run_binding(row, run, profile_id, run_id)
        phase = _phase(row["status"])
        if phase not in {"reserved", "transferring"}:
            raise ProfileStoreError("STALE_RUN", "Run is not eligible for a startup snapshot.")
        self._require_remote_owner(row)
        existing = self.get_snapshot(run_id)
        if existing is not None:
            if existing.profile_id != profile_id:
                raise ProfileStoreError("STALE_RUN", "Snapshot belongs to another profile.")
            return existing

        identity_digest = self.initialize_identity(profile)
        profile_dir = self._profile_dir(profile)
        run_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
        target = self.staging_dir / f"snapshot-{run_key}.zip"
        if target.is_symlink():
            raise ProfileStoreError("TRANSFER_INVALID", "Snapshot staging path is unsafe.")
        if target.exists():
            try:
                if not stat.S_ISREG(target.lstat().st_mode):
                    raise ProfileStoreError("TRANSFER_INVALID", "Snapshot staging path is unsafe.")
                target.unlink()
            except OSError as exc:
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Snapshot staging path is unsafe."
                ) from exc
        packed = pack_browser_data(
            profile_dir,
            self._engine(profile),
            target,
            max_archive_bytes=self.max_archive_bytes,
            max_expanded_bytes=self.max_expanded_bytes,
            max_members=self.max_members,
        )
        if packed.identity_digest != identity_digest:
            target.unlink(missing_ok=True)
            raise ProfileStoreError(
                "TRANSFER_CONFLICT", "Profile identity changed during snapshot."
            )

        now = float(self.clock())
        try:
            with self.database.transaction() as connection:
                current_run = self._select_run(connection, profile_id, run_id)
                if current_run is None:
                    raise ProfileStoreError("STALE_RUN", "Run is no longer current.")
                self._check_run_binding(current_run, run, profile_id, run_id)
                if _phase(current_run["status"]) not in {"reserved", "transferring"}:
                    raise ProfileStoreError(
                        "STALE_RUN", "Run is no longer eligible for a snapshot."
                    )
                revision = _revision_with_content(connection, profile_id)
                _remember_archive_content(connection, packed.archive_digest, packed.content_digest)
                ledger_base = _row_value(current_run, "base_revision")
                if revision is None:
                    base_revision = 1
                    if ledger_base not in (None, 1):
                        raise ProfileStoreError(
                            "TRANSFER_CONFLICT", "Run was admitted against another revision."
                        )
                    connection.execute(
                        "INSERT INTO profile_sync_revisions "
                        "(profile_id, revision, archive_digest, identity_digest, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (profile_id, base_revision, packed.archive_digest, identity_digest, now),
                    )
                else:
                    previous_revision = revision["revision"]
                    base_revision = previous_revision
                    if ledger_base is not None and int(ledger_base) != previous_revision:
                        raise ProfileStoreError(
                            "TRANSFER_CONFLICT", "Run was admitted against another revision."
                        )
                    if (
                        revision["identity_digest"] != identity_digest
                        or revision["archive_digest"] != packed.archive_digest
                    ):
                        # Stopped Master-side data edits and explicit identity resets
                        # become a new canonical starting revision.
                        # A direct local revision may already describe these exact bytes.
                        if _revision_changed(revision, packed.content_digest, identity_digest):
                            base_revision = previous_revision + 1
                        connection.execute(
                            "UPDATE profile_sync_revisions SET revision = ?, archive_digest = ?, identity_digest = ?, "
                            "updated_at = ? WHERE profile_id = ?",
                            (
                                base_revision,
                                packed.archive_digest,
                                identity_digest,
                                now,
                                profile_id,
                            ),
                        )
                if ledger_base is None or int(ledger_base) != base_revision:
                    self._set_ledger_base_revision(
                        connection, current_run, profile_id, run_id, base_revision
                    )
                connection.execute(
                    "INSERT INTO profile_sync_snapshots "
                    "(profile_id, run_id, base_revision, archive_digest, identity_digest, archive_name, "
                    "size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        profile_id,
                        run_id,
                        base_revision,
                        packed.archive_digest,
                        identity_digest,
                        target.name,
                        packed.size_bytes,
                        now,
                    ),
                )
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return Snapshot(
            profile_id,
            run_id,
            base_revision,
            packed.archive_digest,
            target,
            packed.size_bytes,
            identity_digest,
        )

    def get_snapshot(self, run_id: str) -> Snapshot | None:
        """Read the durable immutable snapshot locator for an admitted run."""
        if not isinstance(run_id, str) or not run_id.strip():
            raise ProfileStoreError("TRANSFER_INVALID", "Run ID is invalid.")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM profile_sync_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        if Path(row["archive_name"]).name != row["archive_name"] or not row[
            "archive_name"
        ].startswith("snapshot-"):
            raise ProfileStoreError("TRANSFER_INVALID", "Snapshot locator is invalid.")
        path = self.staging_dir / row["archive_name"]
        if (
            _hash_file(path, self.max_archive_bytes) != row["archive_digest"]
            or path.stat().st_size != row["size_bytes"]
        ):
            raise ProfileStoreError("TRANSFER_INVALID", "Stored snapshot failed integrity checks.")
        return Snapshot(
            row["profile_id"],
            row["run_id"],
            row["base_revision"],
            row["archive_digest"],
            path,
            row["size_bytes"],
            row["identity_digest"],
        )

    def issue_grant(
        self,
        run: object,
        operation: str,
        *,
        snapshot: Snapshot | None = None,
        archive_digest: str | None = None,
        base_revision: int | None = None,
        ttl: float = 300,
    ) -> TransferGrant:
        if operation not in {"download", "upload"}:
            raise ProfileStoreError("TRANSFER_INVALID", "Unsupported transfer operation.")
        if type(ttl) not in (int, float) or ttl <= 0 or ttl > 3600:
            raise ProfileStoreError("TRANSFER_INVALID", "Transfer grant lifetime is invalid.")
        profile_id, run_id = _run_profile_id(run), _run_id(run)
        row = self._ledger_row(profile_id, run_id)
        self._check_run_binding(row, run, profile_id, run_id)
        required_phase = {"download": {"reserved", "transferring"}, "upload": {"syncing"}}[
            operation
        ]
        if _phase(row["status"]) not in required_phase:
            raise ProfileStoreError("STALE_RUN", "Run is not eligible for this transfer.")
        worker_id, boot_id, generation = self._require_remote_owner(row)
        if snapshot is not None:
            if snapshot.profile_id != profile_id or snapshot.run_id != run_id:
                raise ProfileStoreError("TRANSFER_CONFLICT", "Snapshot belongs to another run.")
            archive_digest = snapshot.archive_digest
            base_revision = snapshot.base_revision
        if not _valid_digest(archive_digest):
            raise ProfileStoreError("TRANSFER_INVALID", "Archive digest is invalid.")
        if type(base_revision) is not int or base_revision < 1:
            raise ProfileStoreError("TRANSFER_INVALID", "Base revision is invalid.")
        if operation == "download":
            saved = self.get_snapshot(run_id)
            if (
                saved is None
                or saved.archive_digest != archive_digest
                or saved.base_revision != base_revision
            ):
                raise ProfileStoreError(
                    "TRANSFER_CONFLICT", "Download grant does not match the run snapshot."
                )
        else:
            revision = self._revision(profile_id)
            if revision is None or revision["revision"] != base_revision:
                raise ProfileStoreError(
                    "TRANSFER_CONFLICT", "Upload grant has a stale base revision."
                )
        token = secrets.token_urlsafe(32)
        expires_at = float(self.clock()) + float(ttl)
        with self.database.transaction() as connection:
            current = self._select_run(connection, profile_id, run_id)
            if current is None or _phase(current["status"]) not in required_phase:
                raise ProfileStoreError("STALE_RUN", "Run is no longer eligible for transfer.")
            self._check_run_binding(current, run, profile_id, run_id)
            if self._require_remote_owner(current) != (worker_id, boot_id, generation):
                raise ProfileStoreError("STALE_RUN", "Worker generation is no longer current.")
            current_base = _row_value(current, "base_revision")
            if current_base is not None and int(current_base) != base_revision:
                raise ProfileStoreError("TRANSFER_CONFLICT", "Transfer base revision is stale.")
            connection.execute(
                "DELETE FROM profile_sync_grants WHERE expires_at <= ?", (self.clock(),)
            )
            connection.execute(
                "INSERT INTO profile_sync_grants "
                "(token_hash, profile_id, run_id, worker_id, worker_boot_id, "
                "connection_generation, operation, base_revision, archive_digest, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _token_hash(token),
                    profile_id,
                    run_id,
                    worker_id,
                    boot_id,
                    generation,
                    operation,
                    base_revision,
                    archive_digest,
                    expires_at,
                ),
            )
        return TransferGrant(
            token,
            profile_id,
            run_id,
            worker_id,
            boot_id,
            generation,
            operation,
            base_revision,
            archive_digest,
            expires_at,
            self.max_archive_bytes,
        )

    def validate_grant(
        self,
        token: str,
        *,
        operation: str,
        profile_id: str,
        run_id: str,
        worker_id: str,
        worker_boot_id: str,
        connection_generation: int,
        consume: bool = True,
    ) -> TransferGrant:
        if not isinstance(token, str) or len(token) < 32:
            raise ProfileStoreError("TRANSFER_INVALID", "Transfer authorization is invalid.")
        if operation not in {"download", "upload"} or type(connection_generation) is not int:
            raise ProfileStoreError("TRANSFER_INVALID", "Transfer authorization is invalid.")
        now = float(self.clock())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM profile_sync_grants WHERE token_hash = ?",
                (_token_hash(token),),
            ).fetchone()
            if (
                row is None
                or row["profile_id"] != profile_id
                or row["run_id"] != run_id
                or row["worker_id"] != worker_id
                or row["worker_boot_id"] != worker_boot_id
                or row["connection_generation"] != connection_generation
                or row["operation"] != operation
                or row["expires_at"] <= now
            ):
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Transfer authorization is invalid or expired."
                )
            run = self._select_run(connection, profile_id, run_id)
            if run is None:
                raise ProfileStoreError("STALE_RUN", "Run is no longer current.")
            self._check_row_owner(run, worker_id, worker_boot_id, connection_generation)
            allowed = (
                {"reserved", "transferring", "starting"} if operation == "download" else {"syncing"}
            )
            receipt = None
            if operation == "upload":
                receipt = connection.execute(
                    "SELECT * FROM profile_sync_commits WHERE profile_id = ? AND run_id = ?",
                    (profile_id, run_id),
                ).fetchone()
                if receipt is not None and (
                    receipt["status"] != "committed"
                    or receipt["archive_digest"] != row["archive_digest"]
                    or receipt["base_revision"] != row["base_revision"]
                    or _owner_values(receipt) != (worker_id, worker_boot_id, connection_generation)
                ):
                    raise ProfileStoreError(
                        "TRANSFER_CONFLICT", "Upload receipt does not match this grant."
                    )
            if receipt is None and (
                _phase(run["status"]) not in allowed or row["consumed_at"] is not None
            ):
                raise ProfileStoreError("STALE_RUN", "Run is no longer eligible for transfer.")
            if consume and operation == "upload" and receipt is None:
                connection.execute(
                    "UPDATE profile_sync_grants SET consumed_at = ? WHERE token_hash = ? "
                    "AND consumed_at IS NULL",
                    (now, row["token_hash"]),
                )
        return TransferGrant(
            token,
            row["profile_id"],
            row["run_id"],
            row["worker_id"],
            row["worker_boot_id"],
            row["connection_generation"],
            row["operation"],
            row["base_revision"],
            row["archive_digest"],
            row["expires_at"],
            self.max_archive_bytes,
        )

    def _verify_upload(
        self, archive_path: str | Path, base_revision: int, archive_digest: str
    ) -> None:
        if type(base_revision) is not int or base_revision < 1 or not _valid_digest(archive_digest):
            raise ProfileStoreError("TRANSFER_INVALID", "Commit identity is invalid.")
        if _hash_file(Path(archive_path), self.max_archive_bytes) != archive_digest:
            raise ProfileStoreError("TRANSFER_INVALID", "Archive digest does not match.")

    def replay_committed_receipt(
        self, run: object, archive_path: str | Path, base_revision: int, archive_digest: str
    ) -> CommitReceipt | None:
        """Verify an exact remote upload retry using its durable run and receipt."""
        receipt = self.committed_receipt(run, base_revision, archive_digest)
        if receipt is not None:
            self._verify_upload(archive_path, base_revision, archive_digest)
        return receipt

    def committed_receipt(
        self, run: object, base_revision: int, archive_digest: str
    ) -> CommitReceipt | None:
        """Resolve a durable receipt by exact ownership, revision and digest."""
        profile_id, run_id = _run_profile_id(run), _run_id(run)
        receipt = self._existing_receipt(profile_id, run_id)
        if receipt is None or receipt["status"] != "committed":
            return None
        ledger = self._ledger_row(profile_id, run_id)
        owner = self._require_remote_owner(ledger)
        self._check_row_owner(
            ledger,
            getattr(run, "worker_id", None),
            getattr(run, "worker_boot_id", None),
            getattr(run, "connection_generation", None),
        )
        if ledger["base_revision"] != base_revision:
            raise ProfileStoreError("TRANSFER_CONFLICT", "Run base revision does not match.")
        self._check_existing_commit(receipt, run, owner, base_revision, archive_digest)
        return self._receipt(receipt)

    def commit(
        self,
        profile: object,
        run: object,
        archive_path: str | Path,
        base_revision: int,
        archive_digest: str,
    ) -> CommitReceipt:
        """Validate and promote one stopped remote run, idempotently."""
        profile_id, run_id = _text_attr(profile, "profile_id"), _run_id(run)
        if _run_profile_id(run) != profile_id:
            raise ProfileStoreError("STALE_RUN", "Run does not own this profile.")
        self._verify_upload(archive_path, base_revision, archive_digest)
        ledger = self._ledger_row(profile_id, run_id)
        self._check_run_binding(ledger, run, profile_id, run_id)
        owner = _owner_values(ledger)
        receipt = self._existing_receipt(profile_id, run_id)
        if receipt is not None:
            self._check_existing_commit(receipt, run, owner, base_revision, archive_digest)
            if receipt["status"] == "committed":
                return self._receipt(receipt)
            if receipt["status"] == "failed":
                raise ProfileStoreError(
                    "SYNC_FAILED", "This run's profile commit previously failed."
                )
            if receipt["status"] == "prepared":
                revision = self._revision(profile_id)
                if revision is None:
                    raise ProfileStoreError("SYNC_FAILED", "Canonical revision record is missing.")
                self._promote(
                    profile,
                    receipt["stage_name"],
                    receipt["backup_name"],
                    archive_digest,
                    revision["identity_digest"],
                )
                return self._finish_commit(profile_id, run_id)

        if _phase(ledger["status"]) == "uncertain":
            raise ProfileStoreError("RUN_UNCERTAIN", "Run state is uncertain; upload is blocked.")
        if _phase(ledger["status"]) != "syncing":
            raise ProfileStoreError("STALE_RUN", "Run is not in the profile sync phase.")
        self._require_remote_owner(ledger)
        revision = self._revision(profile_id)
        if revision is None or revision["revision"] != base_revision:
            raise ProfileStoreError("TRANSFER_CONFLICT", "Canonical profile revision has changed.")

        expected_identity = revision["identity_digest"]
        stage_name = f"incoming-{uuid4().hex}"
        backup_name = f"backup-{uuid4().hex}"
        stage = self._journal_dir / stage_name
        with self._upload_lock:
            reserved = self.staging_reserve_bytes
            if self.staging_dir.stat().st_dev == self._journal_dir.stat().st_dev:
                reserved += self._pending_upload_bytes()
            unpacked = unpack_browser_data(
                archive_path,
                stage,
                archive_digest,
                self._engine(profile),
                expected_identity=expected_identity,
                max_archive_bytes=self.max_archive_bytes,
                max_expanded_bytes=self.max_expanded_bytes,
                max_members=self.max_members,
                min_free_bytes=reserved,
            )
        try:
            if unpacked.identity_digest != expected_identity:
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Upload changed the canonical identity."
                )

            now = float(self.clock())
            receipt_id = uuid4().hex
            with self.database.transaction() as connection:
                _remember_archive_content(connection, archive_digest, unpacked.content_digest)
                current = self._select_run(connection, profile_id, run_id)
                if current is None:
                    raise ProfileStoreError("STALE_RUN", "Run is no longer current.")
                self._check_run_binding(current, run, profile_id, run_id)
                self._require_remote_owner(current)
                if _phase(current["status"]) != "syncing":
                    raise ProfileStoreError("STALE_RUN", "Run is no longer syncing.")
                latest = connection.execute(
                    "SELECT * FROM profile_sync_revisions WHERE profile_id = ?", (profile_id,)
                ).fetchone()
                if latest is None or latest["revision"] != base_revision:
                    raise ProfileStoreError(
                        "TRANSFER_CONFLICT", "Canonical profile revision has changed."
                    )
                connection.execute(
                    "INSERT INTO profile_sync_commits "
                    "(profile_id, run_id, worker_id, worker_boot_id, connection_generation, "
                    "base_revision, archive_digest, target_revision, receipt_id, status, stage_name, "
                    "backup_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "'prepared', ?, ?, ?, ?)",
                    (
                        profile_id,
                        run_id,
                        *owner,
                        base_revision,
                        archive_digest,
                        base_revision + 1,
                        receipt_id,
                        stage_name,
                        backup_name,
                        now,
                        now,
                    ),
                )
        except Exception:
            # A transaction may already have published this exact recovery intent.
            # Keep its directory. Dispose only unclaimed extraction, outside the SQLite writer.
            with self.database.read() as connection:
                owned = connection.execute(
                    "SELECT 1 FROM profile_sync_commits WHERE stage_name=? AND status='prepared'",
                    (stage_name,),
                ).fetchone()
            if owned is None:
                shutil.rmtree(stage, ignore_errors=True)
            raise
        try:
            self._promote(
                profile,
                stage_name,
                backup_name,
                archive_digest,
                expected_identity,
                verified_directory=unpacked.directory_stat,
            )
            return self._finish_commit(profile_id, run_id)
        except _DirectorySyncError:
            # Either rename may already be visible. Its prepared intent owns both copies.
            raise
        except ProfileStoreError:
            self._restore_backup(profile, backup_name, verify_revision=revision)
            self._fail_intent(profile_id, run_id)
            raise
        except Exception as exc:
            self._restore_backup(profile, backup_name, verify_revision=revision)
            self._fail_intent(profile_id, run_id)
            raise ProfileStoreError(
                "SYNC_FAILED", "Profile commit could not be completed safely."
            ) from exc

    def record_local_stop(self, profile: object, run: object) -> CommitReceipt:
        """Record a confirmed local stop without replacing the canonical directory."""
        profile_id, run_id = _text_attr(profile, "profile_id"), _run_id(run)
        if _run_profile_id(run) != profile_id:
            raise ProfileStoreError("STALE_RUN", "Run does not own this profile.")
        prior = self._existing_receipt(profile_id, run_id)
        if prior is not None and prior["worker_id"] is None and prior["status"] == "committed":
            return self._receipt(prior)
        return record_local_session(
            self.database,
            BrowserRunRefDTO(profile_id, run_id),
            self._profile_dir(profile),
            self._engine(profile),
            clock=self.clock,
        )

    def recover(self) -> tuple[CommitReceipt, ...]:
        """Reconcile the private journal at exclusive Master startup, before accepting transfers."""
        with self.database.read() as connection:
            rows = tuple(
                connection.execute(
                    "SELECT * FROM profile_sync_commits WHERE status = 'prepared' ORDER BY created_at"
                )
            )
        self._remove_unclaimed_stages({row["stage_name"] for row in rows})
        recovered: list[CommitReceipt] = []
        for row in rows:
            profile_id = row["profile_id"]
            profile = self._profile_from_commit(profile_id)
            if profile is None:
                raise ProfileStoreError(
                    "SYNC_FAILED", "Profile metadata is unavailable for recovery."
                )
            stage_name, backup_name = row["stage_name"], row["backup_name"]
            revision = self._revision(profile_id)
            if revision is None or revision["revision"] != row["base_revision"]:
                raise ProfileStoreError("SYNC_FAILED", "Commit base revision is unavailable.")
            # Never roll back or discard an interrupted journal whose contents cannot be proven.
            self._archive_content(row["archive_digest"])
            try:
                self._promote(
                    profile,
                    stage_name,
                    backup_name,
                    row["archive_digest"],
                    revision["identity_digest"],
                )
            except _DirectorySyncError:
                raise
            except Exception:
                self._restore_backup(profile, backup_name, verify_revision=revision)
                self._fail_intent(profile_id, row["run_id"])
            else:
                recovered.append(self._finish_commit(profile_id, row["run_id"]))
        self._clean_committed_backups()
        return tuple(recovered)

    def _remove_unclaimed_stages(self, prepared: set[str]) -> None:
        # Only this journal owns generated incoming UUIDv4 names. Canonical data, backups,
        # and paths still referenced by a prepared intent never qualify.
        with os.scandir(self._journal_dir) as entries:
            for entry in entries:
                if (
                    re.fullmatch(r"incoming-[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", entry.name)
                    and entry.name not in prepared
                    and entry.is_dir(follow_symlinks=False)
                ):
                    shutil.rmtree(entry.path)

    def revision(self, profile_id: str) -> tuple[int, str, str] | None:
        row = self._revision(profile_id)
        return (
            (row["revision"], row["archive_digest"], row["identity_digest"])
            if row is not None
            else None
        )

    def cleanup_run(self, run_id: str) -> None:
        """Remove a closed run's immutable startup snapshot and obsolete grants.

        Committed receipts and unexpired upload grants remain available for an
        exact-digest retry after a lost HTTP acknowledgment. Download grants
        cannot be used once the run is closed and are removed immediately.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise ProfileStoreError("TRANSFER_INVALID", "Run ID is invalid.")
        with self.database.read() as connection:
            run = connection.execute(
                "SELECT status FROM browser_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            snapshot = connection.execute(
                "SELECT * FROM profile_sync_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        if run is None or run["status"] != "closed":
            raise ProfileStoreError("STALE_RUN", "Only a closed run can be cleaned up.")

        run_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
        archive_name = f"snapshot-{run_key}.zip"
        if snapshot is not None and snapshot["archive_name"] != archive_name:
            raise ProfileStoreError("TRANSFER_INVALID", "Snapshot locator is invalid.")
        removed_file = False
        for path in (
            self.staging_dir / archive_name,
            self.staging_dir / f"{archive_name}.part",
        ):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Snapshot cannot be cleaned safely."
                ) from exc
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ProfileStoreError("TRANSFER_INVALID", "Snapshot cannot be cleaned safely.")
            if path.name == archive_name:
                digest = _hash_file(path, self.max_archive_bytes)
                if snapshot is not None and (
                    info.st_size != snapshot["size_bytes"] or digest != snapshot["archive_digest"]
                ):
                    raise ProfileStoreError("TRANSFER_INVALID", "Snapshot changed before cleanup.")
            path.unlink()
            removed_file = True

        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT status FROM browser_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current is None or current["status"] != "closed":
                raise ProfileStoreError("STALE_RUN", "Run is no longer closed.")
            latest = connection.execute(
                "SELECT archive_name FROM profile_sync_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
            if latest is not None and latest["archive_name"] != archive_name:
                raise ProfileStoreError("TRANSFER_CONFLICT", "Snapshot changed during cleanup.")
            commit = connection.execute(
                "SELECT status FROM profile_sync_commits WHERE run_id = ?", (run_id,)
            ).fetchone()
            connection.execute("DELETE FROM profile_sync_snapshots WHERE run_id = ?", (run_id,))
            if commit is not None and commit["status"] == "committed":
                connection.execute(
                    "DELETE FROM profile_sync_grants WHERE run_id = ? AND operation = 'download'",
                    (run_id,),
                )
                connection.execute(
                    "DELETE FROM profile_sync_grants WHERE run_id = ? AND expires_at <= ?",
                    (run_id, self.clock()),
                )
            else:
                connection.execute("DELETE FROM profile_sync_grants WHERE run_id = ?", (run_id,))
        if removed_file:
            _fsync_directory(self.staging_dir)

    def _promote(
        self,
        profile: object,
        stage_name: str,
        backup_name: str,
        expected_digest: str,
        expected_identity: str,
        *,
        verified_directory: os.stat_result | None = None,
    ) -> None:
        stage = self._journal_child(stage_name, "incoming-")
        backup = self._journal_child(backup_name, "backup-")
        canonical = self._profile_dir(
            profile, missing_ok=backup.is_dir() and not backup.is_symlink()
        )
        engine = self._engine(profile)
        expected = (self._archive_content(expected_digest), expected_identity)
        if not stage.exists():
            # The stage has already been renamed into place before a crash.
            if not canonical.exists():
                raise ProfileStoreError("SYNC_FAILED", "Staged profile is missing.")
            if _profile_content(canonical, engine) != expected:
                raise ProfileStoreError("SYNC_FAILED", "Promoted profile failed integrity checks.")
            _sync_move_parents(stage, canonical)
            return
        if verified_directory is None:
            # After restart, verify the pending tree before promoting it.
            if _profile_content(stage, engine) != expected:
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Staged profile failed integrity checks."
                )
        elif not _same_file(verified_directory, stage.lstat()):
            raise ProfileStoreError("TRANSFER_INVALID", "Verified staging directory changed.")
        if canonical.exists() or canonical.is_symlink():
            if backup.exists() or backup.is_symlink():
                raise ProfileStoreError("SYNC_FAILED", "Profile backup path is already occupied.")
            _replace_directory(canonical, backup)
        else:
            # A prior attempt moved the old canonical copy but may not have synced that rename.
            _sync_move_parents(canonical, backup)
        try:
            _replace_directory(stage, canonical)
            if _profile_content(canonical, engine) != expected:
                raise ProfileStoreError("SYNC_FAILED", "Promoted profile failed integrity checks.")
        except Exception:
            if backup.exists() and not canonical.exists():
                _replace_directory(backup, canonical)
            raise

    def _finish_commit(self, profile_id: str, run_id: str) -> CommitReceipt:
        now = float(self.clock())
        with self.database.transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM profile_sync_commits WHERE profile_id = ? AND run_id = ?",
                (profile_id, run_id),
            ).fetchone()
            if intent is None:
                raise ProfileStoreError("SYNC_FAILED", "Commit journal entry is missing.")
            if intent["status"] == "committed":
                return self._receipt(intent)
            revision = connection.execute(
                "SELECT * FROM profile_sync_revisions WHERE profile_id = ?", (profile_id,)
            ).fetchone()
            if revision is None or revision["revision"] != intent["base_revision"]:
                raise ProfileStoreError(
                    "TRANSFER_CONFLICT", "Canonical revision changed during commit."
                )
            connection.execute(
                "UPDATE profile_sync_revisions SET revision = ?, archive_digest = ?, updated_at = ? "
                "WHERE profile_id = ? AND revision = ?",
                (
                    intent["target_revision"],
                    intent["archive_digest"],
                    now,
                    profile_id,
                    intent["base_revision"],
                ),
            )
            connection.execute(
                "UPDATE profile_sync_commits SET status = 'committed', updated_at = ? "
                "WHERE profile_id = ? AND run_id = ? AND status = 'prepared'",
                (now, profile_id, run_id),
            )
            completed = connection.execute(
                "SELECT * FROM profile_sync_commits WHERE profile_id = ? AND run_id = ?",
                (profile_id, run_id),
            ).fetchone()
        self._delete_backup(intent["backup_name"])
        return self._receipt(completed)

    def _fail_intent(self, profile_id: str, run_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE profile_sync_commits SET status = 'failed', updated_at = ? "
                "WHERE profile_id = ? AND run_id = ? AND status = 'prepared'",
                (self.clock(), profile_id, run_id),
            )

    def _archive_content(self, archive_digest: str) -> str:
        with self.database.read() as db:
            row = db.execute(
                "SELECT content_digest FROM profile_archive_contents WHERE archive_digest=?",
                (archive_digest,),
            ).fetchone()
        if row is None:
            raise ProfileStoreError(
                "SYNC_FAILED", "Prepared journal lacks verified content evidence."
            )
        return row["content_digest"]

    def _restore_backup(
        self, profile: object, backup_name: str, *, verify_revision: sqlite3.Row | None = None
    ) -> None:
        backup = self._journal_child(backup_name, "backup-")
        if backup.is_symlink() or (backup.exists() and not backup.is_dir()):
            raise ProfileStoreError("SYNC_FAILED", "Profile backup path is unsafe.")
        has_backup = backup.is_dir()
        canonical = self._profile_dir(profile, missing_ok=has_backup)
        if verify_revision is not None:
            digest = _profile_content(backup if has_backup else canonical, self._engine(profile))
            expected_content = self._archive_content(verify_revision["archive_digest"])
            if digest != (expected_content, verify_revision["identity_digest"]):
                raise ProfileStoreError(
                    "SYNC_FAILED", "Last canonical revision failed integrity checks."
                )
        if has_backup:
            if canonical.exists():
                shutil.rmtree(canonical)
            _replace_directory(backup, canonical)
        else:
            # A rollback rename can also be visible after a failed directory sync.
            _sync_move_parents(backup, canonical)

    def _delete_backup(self, backup_name: str | None) -> None:
        if not backup_name:
            return
        backup = self._journal_child(backup_name, "backup-")
        try:
            if backup.is_symlink():
                backup.unlink()
            elif backup.is_dir():
                shutil.rmtree(backup)
            elif backup.exists():
                raise OSError("unexpected backup type")
            _fsync_directory(self._journal_dir, required=True)
            with self.database.transaction() as db:
                db.execute(
                    "UPDATE profile_sync_commits SET backup_name=NULL WHERE status='committed' AND backup_name=?",
                    (backup_name,),
                )
        except (OSError, sqlite3.Error, _DirectorySyncError):
            # The revision is already committed. Retry outstanding cleanup on recovery.
            return

    def _clean_committed_backups(self) -> None:
        with self.database.read() as connection:
            names = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT backup_name FROM profile_sync_commits WHERE status = 'committed' "
                    "AND backup_name IS NOT NULL"
                )
            )
        for name in names:
            self._delete_backup(name)

    def _journal_child(self, name: str, prefix: str) -> Path:
        if not isinstance(name, str) or not name.startswith(prefix) or Path(name).name != name:
            raise ProfileStoreError("SYNC_FAILED", "Commit journal path is invalid.")
        return self._journal_dir / name

    def _profile_dir(
        self, profile: object, *, create: bool = False, missing_ok: bool = False
    ) -> Path:
        name = _text_attr(profile, "name")
        valid, _reason = _validate_profile_name(name)
        if not valid:
            raise ProfileStoreError("TRANSFER_INVALID", "Profile name is invalid.")
        path = self.data_dir / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            if create:
                path.mkdir(mode=0o700, parents=False)
                info = path.lstat()
            elif missing_ok:
                info = None
            else:
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Canonical profile data is missing."
                ) from None
        if info is not None and (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise ProfileStoreError("TRANSFER_INVALID", "Canonical profile data path is unsafe.")
        if path.resolve().parent != self.data_dir:
            raise ProfileStoreError("TRANSFER_INVALID", "Canonical profile data path is unsafe.")
        return path

    @staticmethod
    def _engine(profile: object) -> str:
        engine = getattr(profile, "engine", None)
        if engine not in _ENGINES:
            raise ProfileStoreError("TRANSFER_INVALID", "Profile engine is invalid.")
        return engine

    def _ledger_row(self, profile_id: str, run_id: str) -> sqlite3.Row:
        with self.database.read() as connection:
            row = self._select_run(connection, profile_id, run_id)
        if row is None:
            raise ProfileStoreError("STALE_RUN", "Run is no longer current.")
        return row

    @staticmethod
    def _select_run(
        connection: sqlite3.Connection, profile_id: str, run_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT r.*, p.worker_id AS worker_id, p.worker_boot_id AS worker_boot_id, "
            "p.connection_generation AS connection_generation, p.base_revision AS base_revision "
            "FROM browser_runs AS r LEFT JOIN browser_run_placements AS p ON p.run_id = r.run_id "
            "WHERE r.profile_id = ? AND r.run_id = ?",
            (profile_id, run_id),
        ).fetchone()

    def _check_run_binding(
        self, row: sqlite3.Row, run: object, profile_id: str, run_id: str
    ) -> None:
        if row["profile_id"] != profile_id or row["run_id"] != run_id:
            raise ProfileStoreError("STALE_RUN", "Run identity is stale.")
        for key in ("worker_id", "worker_boot_id", "connection_generation"):
            supplied = _object_value(run, key)
            actual = _row_value(row, key)
            if supplied is not None and actual is not None and supplied != actual:
                raise ProfileStoreError("STALE_RUN", "Run worker generation is stale.")

    @staticmethod
    def _require_remote_owner(row: sqlite3.Row) -> tuple[str, str, int]:
        worker_id, boot_id, generation = _owner_values(row)
        if not worker_id or not boot_id or type(generation) is not int or generation < 1:
            raise ProfileStoreError("STALE_RUN", "Run has no current remote worker owner.")
        return worker_id, boot_id, generation

    @staticmethod
    def _check_row_owner(row: sqlite3.Row, worker_id: str, boot_id: str, generation: int) -> None:
        if _owner_values(row) != (worker_id, boot_id, generation):
            raise ProfileStoreError("STALE_RUN", "Worker generation is stale.")

    def _set_ledger_base_revision(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        profile_id: str,
        run_id: str,
        base_revision: int,
    ) -> None:
        cursor = connection.execute(
            "UPDATE browser_run_placements SET base_revision = ? WHERE run_id = ? "
            "AND EXISTS (SELECT 1 FROM browser_runs WHERE run_id = ? AND profile_id = ? "
            "AND status IN ('reserved', 'transferring'))",
            (base_revision, run_id, run_id, profile_id),
        )
        if cursor.rowcount != 1 and _row_value(row, "base_revision") is None:
            raise ProfileStoreError("STALE_RUN", "Run placement is no longer current.")

    def _revision(self, profile_id: str) -> sqlite3.Row | None:
        with self.database.read() as connection:
            return connection.execute(
                "SELECT * FROM profile_sync_revisions WHERE profile_id = ?", (profile_id,)
            ).fetchone()

    def _existing_receipt(self, profile_id: str, run_id: str) -> sqlite3.Row | None:
        with self.database.read() as connection:
            return connection.execute(
                "SELECT * FROM profile_sync_commits WHERE profile_id = ? AND run_id = ?",
                (profile_id, run_id),
            ).fetchone()

    def _check_existing_commit(
        self,
        receipt: sqlite3.Row,
        run: object,
        owner: tuple[str | None, str | None, int | None],
        base_revision: int,
        digest: str,
    ) -> None:
        expected_owner = _owner_values(receipt)
        if (
            receipt["base_revision"] != base_revision
            or receipt["archive_digest"] != digest
            or expected_owner != owner
        ):
            raise ProfileStoreError("TRANSFER_CONFLICT", "Run already has a different commit.")
        self._check_run_binding(receipt, run, receipt["profile_id"], receipt["run_id"])

    @staticmethod
    def _receipt(row: sqlite3.Row) -> CommitReceipt:
        return CommitReceipt(
            row["profile_id"],
            row["run_id"],
            row["target_revision"],
            row["archive_digest"],
            row["receipt_id"],
        )

    def _profile_from_commit(self, profile_id: str) -> object | None:
        # Recovery cannot infer mutable names from profile IDs; composition
        # supplies the catalog resolver used by the runtime coordinator.
        resolver = self.profile_by_id
        if callable(resolver):
            try:
                return resolver(profile_id)
            except Exception:
                return None
        return None


def _local_digest(content_digest: str) -> str:
    # A locally written revision need not have a transferable ZIP representation.
    return f"local:{content_digest}"


def _revision_with_content(db, profile_id):
    return db.execute(
        "SELECT r.*, a.content_digest FROM profile_sync_revisions r LEFT JOIN profile_archive_contents a "
        "ON a.archive_digest=r.archive_digest WHERE r.profile_id=?",
        (profile_id,),
    ).fetchone()


def _revision_changed(revision, content, identity):
    return revision["identity_digest"] != identity or (
        revision["archive_digest"] != _local_digest(content)
        and revision["content_digest"] != content
    )


def _remember_archive_content(db, archive_digest, content_digest):
    db.execute(
        "INSERT OR IGNORE INTO profile_archive_contents VALUES (?, ?)",
        (archive_digest, content_digest),
    )


def record_local_session(
    database,
    ref: BrowserRunRefDTO,
    directory: Path,
    engine: str,
    *,
    supervisor: ProcessIdentity | None = None,
    clock=time.time,
) -> CommitReceipt:
    """Journal the trusted, stopped session while it still owns the canonical bytes."""

    def eligible(db):
        prior = db.execute(
            "SELECT * FROM profile_sync_commits WHERE profile_id=? AND run_id=? "
            "AND worker_id IS NULL AND status='committed'",
            (ref.profile_id, ref.run_id),
        ).fetchone()
        if prior is not None:
            return ProfileStore._receipt(prior)
        row = db.execute(
            "SELECT r.status, r.pid, r.start_ticks FROM browser_runs r JOIN browser_run_placements p "
            "ON p.run_id=r.run_id WHERE r.profile_id=? AND r.run_id=? AND p.worker_id IS NULL "
            "AND p.storage_outcome='unsaved' AND NOT EXISTS (SELECT 1 FROM browser_runs later "
            "WHERE later.profile_id=r.profile_id AND later.rowid>r.rowid)",
            (ref.profile_id, ref.run_id),
        ).fetchone()
        if supervisor is None:
            valid = row is not None and row["status"] == "closed"
        else:
            valid = (
                row is not None
                and row["status"] in {"running", "stopping"}
                and (row["pid"], row["start_ticks"]) == (supervisor.pid, supervisor.start_ticks)
            )
        if not valid:
            raise ProfileStoreError("STALE_RUN", "Local browser session no longer owns this data.")
        return None

    with database.read() as db:
        prior = eligible(db)
    if prior is not None:
        return prior
    content, identity = _profile_content(directory, engine)
    digest = _local_digest(content)
    now = clock()
    with database.transaction() as db:
        prior = eligible(db)
        if prior is not None:
            return prior
        revision = _revision_with_content(db, ref.profile_id)
        base = revision["revision"] if revision else 0
        changed = revision is None or _revision_changed(revision, content, identity)
        target = base + int(changed)
        if changed:
            db.execute(
                "INSERT INTO profile_sync_revisions (profile_id,revision,archive_digest,identity_digest,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(profile_id) DO UPDATE SET revision=excluded.revision, "
                "archive_digest=excluded.archive_digest, identity_digest=excluded.identity_digest, updated_at=excluded.updated_at",
                (ref.profile_id, target, digest, identity, now),
            )
        receipt = CommitReceipt(ref.profile_id, ref.run_id, target, digest, uuid4().hex)
        db.execute(
            "INSERT INTO profile_sync_commits "
            "(profile_id,run_id,base_revision,archive_digest,target_revision,receipt_id,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'committed',?,?)",
            (ref.profile_id, ref.run_id, base, digest, target, receipt.receipt_id, now, now),
        )
        db.execute(
            "UPDATE browser_run_placements SET storage_outcome='saved' WHERE run_id=?",
            (ref.run_id,),
        )
    return receipt


def pack_browser_data(
    profile_path: str | Path,
    engine: str,
    target_path: str | Path,
    *,
    max_archive_bytes: int = _DEFAULT_MAX_ARCHIVE_BYTES,
    max_expanded_bytes: int = _DEFAULT_MAX_EXPANDED_BYTES,
    max_members: int = _DEFAULT_MAX_MEMBERS,
) -> ArchiveInfo:
    """Pack persistent engine data as a deterministic, data-only ZIP archive."""
    root = Path(profile_path)
    target = Path(target_path)
    _validate_limits(max_archive_bytes, max_expanded_bytes, max_members)
    if engine not in _ENGINES:
        raise ProfileStoreError("TRANSFER_INVALID", "Profile engine is invalid.")
    part = target.with_name(target.name + ".part")
    try:
        tree = _ProfileTree(root)
        members = tree.files(engine, max_members, max_expanded_bytes)
        expanded_bytes = sum(info.st_size for _, info in members)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ProfileStore._secure_directory(target.parent)
        if shutil.disk_usage(target.parent).free < expanded_bytes:
            raise ArchiveStagingError("TRANSFER_INVALID", "Insufficient staging disk space.")
        if target.exists() or target.is_symlink() or part.exists() or part.is_symlink():
            raise ProfileStoreError("TRANSFER_CONFLICT", "Archive destination already exists.")
        with zipfile.ZipFile(
            part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            content, identity_digest = _read_profile(
                tree, engine, members, archive, max_archive_bytes
            )
            stored = {
                info.filename
                for info in archive.infolist()
                if info.file_size
                and (
                    not info.compress_size
                    or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
                )
            }
        if stored:
            with zipfile.ZipFile(
                part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                retried = _read_profile(tree, engine, members, archive, max_archive_bytes, stored)
                if retried != (content, identity_digest):
                    raise ProfileStoreError("TRANSFER_INVALID", "Profile changed during snapshot.")
        size = part.stat().st_size
        if size > max_archive_bytes:
            raise ProfileStoreError("TRANSFER_INVALID", "Profile archive exceeds the size limit.")
        os.chmod(part, 0o600)
        os.replace(part, target)
        _fsync_directory(target.parent)
        digest = _hash_file(target, max_archive_bytes)
        return ArchiveInfo(digest, size, identity_digest, expanded_bytes, len(members), content)
    except ProfileStoreError:
        part.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        part.unlink(missing_ok=True)
        if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise ArchiveStagingError(
                "TRANSFER_INVALID", "Insufficient staging disk space."
            ) from exc
        raise ProfileStoreError(
            "TRANSFER_INVALID", "Could not create a safe profile archive."
        ) from exc


def _profile_content(root: Path, engine: str) -> tuple[str, str]:
    if engine not in _ENGINES:
        raise ProfileStoreError("TRANSFER_INVALID", "Profile engine is invalid.")
    try:
        tree = _ProfileTree(root)
        return _read_profile(tree, engine, tree.files(engine))
    except (OSError, ValueError) as exc:
        raise ProfileStoreError(
            "TRANSFER_INVALID", "Canonical browser data could not be read safely."
        ) from exc


def _hash_member_header(digest, relative: str, size: int) -> None:
    name = relative.encode("utf-8")
    digest.update(len(name).to_bytes(8, "big") + name + size.to_bytes(8, "big"))


def _read_profile(tree, engine, members, archive=None, archive_limit=None, stored=()):
    """Hash unambiguous names, lengths and bytes from the same descriptors used for the ZIP."""
    digest = hashlib.sha256()
    identity = None
    for parts, original in members:
        relative = "/".join(parts)
        _hash_member_header(digest, relative, original.st_size)
        identity_bytes = bytearray() if relative == _IDENTITY_FILES[engine] else None
        with tree.open_file(parts, original) as source:
            destination = nullcontext(None)
            if archive is not None:
                info = zipfile.ZipInfo(f"data/{relative}", (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = (
                    zipfile.ZIP_STORED if info.filename in stored else zipfile.ZIP_DEFLATED
                )
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                destination = archive.open(info, "w")
            with destination as target:
                actual = 0
                while chunk := source.read(_CHUNK_SIZE):
                    actual += len(chunk)
                    if actual > original.st_size:
                        raise ProfileStoreError(
                            "TRANSFER_INVALID", "Profile file changed during snapshot."
                        )
                    digest.update(chunk)
                    if target is not None:
                        target.write(chunk)
                    if identity_bytes is not None:
                        identity_bytes.extend(chunk)
            if (
                actual != original.st_size
                or os.fstat(source.fileno()).st_mtime_ns != original.st_mtime_ns
            ):
                raise ProfileStoreError("TRANSFER_INVALID", "Profile file changed during snapshot.")
        if identity_bytes is not None:
            identity, _ = _validate_identity_bytes(bytes(identity_bytes), engine)
        if archive is not None and archive.fp.tell() > archive_limit:
            raise ProfileStoreError("TRANSFER_INVALID", "Profile archive exceeds the size limit.")
    if identity is None:
        raise ProfileStoreError(
            "TRANSFER_INVALID", "Required browser identity is missing or invalid."
        )
    return digest.hexdigest(), identity


def unpack_browser_data(
    archive_path: str | Path,
    destination: str | Path,
    expected_digest: str,
    engine: str,
    expected_identity: str | None = None,
    *,
    max_archive_bytes: int = _DEFAULT_MAX_ARCHIVE_BYTES,
    max_expanded_bytes: int = _DEFAULT_MAX_EXPANDED_BYTES,
    max_members: int = _DEFAULT_MAX_MEMBERS,
    min_free_bytes: int = 0,
) -> UnpackedProfile:
    """Validate an archive completely, then extract into a new private directory."""
    archive_path, destination = Path(archive_path), Path(destination)
    _validate_limits(max_archive_bytes, max_expanded_bytes, max_members)
    if engine not in _ENGINES or not _valid_digest(expected_digest):
        raise ProfileStoreError("TRANSFER_INVALID", "Archive identity is invalid.")
    if type(min_free_bytes) is not int or min_free_bytes < 0:
        raise ProfileStoreError("TRANSFER_INVALID", "Disk reserve is invalid.")
    if destination.exists() or destination.is_symlink():
        raise ProfileStoreError("TRANSFER_CONFLICT", "Extraction destination already exists.")

    descriptor = _open_regular_file(archive_path, max_archive_bytes)
    try:
        with os.fdopen(descriptor, "rb") as archive_stream:
            descriptor = -1
            if _hash_stream(archive_stream, max_archive_bytes) != expected_digest:
                raise ProfileStoreError("TRANSFER_INVALID", "Archive digest does not match.")
            archive_stream.seek(0)
            with zipfile.ZipFile(archive_stream, "r") as archive:
                infos = archive.infolist()
                if len(infos) > max_members:
                    raise ProfileStoreError("TRANSFER_INVALID", "Archive has too many members.")
                planned = _validate_members(infos, engine, max_expanded_bytes, max_members)
                expanded_bytes = sum(info.file_size for info in planned if not info.is_dir())
                if expanded_bytes > max_expanded_bytes:
                    raise ProfileStoreError(
                        "TRANSFER_INVALID", "Archive exceeds the expanded-size limit."
                    )
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                ProfileStore._secure_directory(destination.parent)
                if shutil.disk_usage(destination.parent).free < expanded_bytes + min_free_bytes:
                    raise ProfileStoreError("TRANSFER_INVALID", "Insufficient staging disk space.")
                destination.mkdir(mode=0o700)
                os.chmod(destination, 0o700)
                expanded_actual = 0
                content = hashlib.sha256()
                directory_stat = destination.lstat()
                directories = {destination}
                try:
                    for member in planned:
                        relative = _archive_relative(member.filename, member.is_dir())
                        target = destination / Path(*relative.parts)
                        parent = target if member.is_dir() else target.parent
                        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        while parent not in directories:
                            directories.add(parent)
                            parent = parent.parent
                        if member.is_dir():
                            continue
                        _hash_member_header(content, relative.as_posix(), member.file_size)
                        actual = 0
                        with archive.open(member, "r") as source, target.open("xb") as output:
                            os.chmod(target, 0o600)
                            while True:
                                chunk = source.read(_CHUNK_SIZE)
                                if not chunk:
                                    break
                                actual += len(chunk)
                                expanded_actual += len(chunk)
                                if (
                                    actual > member.file_size
                                    or expanded_actual > max_expanded_bytes
                                ):
                                    raise ProfileStoreError(
                                        "TRANSFER_INVALID",
                                        "Archive expands past its declared size.",
                                    )
                                content.update(chunk)
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        if actual != member.file_size:
                            raise ProfileStoreError(
                                "TRANSFER_INVALID", "Archive member size is invalid."
                            )
                    identity_digest = _identity_digest(destination, engine)
                    if expected_identity is not None and identity_digest != expected_identity:
                        raise ProfileStoreError(
                            "TRANSFER_INVALID", "Archive changed the canonical identity."
                        )
                    # File fsync alone cannot persist a newly created nested name.
                    for directory in sorted(
                        directories, key=lambda path: len(path.parts), reverse=True
                    ):
                        _fsync_directory(directory, required=True)
                    _fsync_directory(destination.parent, required=True)
                    return UnpackedProfile(
                        identity_digest,
                        expanded_actual,
                        len(planned),
                        content.hexdigest(),
                        directory_stat,
                    )
                except Exception:
                    shutil.rmtree(destination, ignore_errors=True)
                    raise
    except ProfileStoreError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, ValueError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise ProfileStoreError("TRANSFER_INVALID", "Profile archive is invalid.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_members(
    infos: list[zipfile.ZipInfo], engine: str, max_expanded_bytes: int, max_members: int
) -> list[zipfile.ZipInfo]:
    seen: dict[str, bool] = {}
    planned: list[zipfile.ZipInfo] = []
    total = 0
    identity_seen = False
    # CURRENT identifies LevelDB stores even when Chromium uses a feature-specific directory name.
    leveldb_dirs = {
        info.filename.removesuffix("/CURRENT")
        for info in infos
        if info.filename.endswith("/CURRENT")
    }
    for info in infos:
        if len(planned) >= max_members:
            raise ProfileStoreError("TRANSFER_INVALID", "Archive has too many members.")
        relative = _archive_relative(info.filename, info.is_dir())
        normalized = relative.as_posix().casefold()
        is_directory = info.is_dir()
        previous = seen.get(normalized)
        if previous is not None:
            if previous != is_directory:
                raise ProfileStoreError("TRANSFER_INVALID", "Archive has conflicting paths.")
            raise ProfileStoreError("TRANSFER_INVALID", "Archive has duplicate members.")
        seen[normalized] = is_directory
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFDIR if is_directory else stat.S_IFREG}:
            raise ProfileStoreError("TRANSFER_INVALID", "Archive contains a link or special file.")
        parts = relative.parts
        if _excluded_path(parts, engine, leveldb=info.filename.rpartition("/")[0] in leveldb_dirs):
            raise ProfileStoreError(
                "TRANSFER_INVALID", "Archive contains a transient or control file."
            )
        total += info.file_size
        if total > max_expanded_bytes:
            raise ProfileStoreError("TRANSFER_INVALID", "Archive exceeds the expanded-size limit.")
        if info.file_size and info.compress_size == 0:
            raise ProfileStoreError("TRANSFER_INVALID", "Archive compression metadata is invalid.")
        if info.compress_size and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
            raise ProfileStoreError("TRANSFER_INVALID", "Archive compression ratio is excessive.")
        if not is_directory and len(parts) == 1 and parts[0] == _IDENTITY_FILES[engine]:
            identity_seen = True
        planned.append(info)
    if not identity_seen:
        raise ProfileStoreError("TRANSFER_INVALID", "Archive has no engine identity file.")
    for path, _is_directory in seen.items():
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in seen and not seen[parent]:
                raise ProfileStoreError("TRANSFER_INVALID", "Archive file is used as a directory.")
    # Directory traversal and archive member order must describe the same canonical digest.
    return sorted(
        planned,
        key=lambda item: tuple(
            (part.casefold(), part) for part in PurePosixPath(item.filename).parts
        ),
    )


def _archive_relative(filename: str, is_directory: bool) -> PurePosixPath:
    if (
        not isinstance(filename, str)
        or not filename
        or "\\" in filename
        or "\x00" in filename
        or filename.startswith("/")
        or filename.startswith("\\")
    ):
        raise ProfileStoreError("TRANSFER_INVALID", "Archive path is unsafe.")
    clean = filename[:-1] if is_directory and filename.endswith("/") else filename
    parts = clean.split("/")
    if any(part in {"", ".", ".."} for part in parts) or not parts or parts[0] != "data":
        raise ProfileStoreError("TRANSFER_INVALID", "Archive path is unsafe.")
    if len(parts) < 2:
        raise ProfileStoreError("TRANSFER_INVALID", "Archive path is outside browser data.")
    return PurePosixPath(*parts[1:])


class _ProfileTree:
    """Open every selected directory and file against the inspected root and ancestry."""

    def __init__(self, root):
        self.root = Path(root)
        self.directories = {(): self.root.lstat()}
        if not stat.S_ISDIR(self.directories[()].st_mode):
            raise ProfileStoreError("TRANSFER_INVALID", "Profile data path is unsafe.")

    @contextmanager
    def directory(self, parts):
        if os.name == "nt":
            # Win32 lacks dir_fd. Revalidate each inspected ancestor before and after open.
            ancestors = [
                (self.root.joinpath(*parts[:i]), self.directories[parts[:i]])
                for i in range(len(parts) + 1)
            ]
            if not _portable_parents_match(ancestors, self.root.resolve()):
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Profile directory changed during snapshot."
                )
            yield self.root.joinpath(*parts)
            if not _portable_parents_match(ancestors, self.root.resolve()):
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Profile directory changed during snapshot."
                )
            return
        fd = _open_profile_data_dir(self.root)
        try:
            if not _same_file(os.fstat(fd), self.directories[()]):
                raise ProfileStoreError("TRANSFER_INVALID", "Profile root changed during snapshot.")
            for i, name in enumerate(parts, 1):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                if not _same_file(os.fstat(fd), self.directories[parts[:i]]):
                    raise ProfileStoreError(
                        "TRANSFER_INVALID", "Profile directory changed during snapshot."
                    )
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def open_file(self, parts, expected=None):
        with self.directory(parts[:-1]) as parent:
            parent_fd = parent if isinstance(parent, int) else None
            name = parts[-1] if parent_fd is not None else parent / parts[-1]
            if expected is None:
                expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            fd = os.open(name, flags, dir_fd=parent_fd)
            with os.fdopen(fd, "rb") as stream:
                actual = os.fstat(fd)
                if not stat.S_ISREG(actual.st_mode) or not _same_file(actual, expected):
                    raise ProfileStoreError(
                        "TRANSFER_INVALID", "Profile file changed during snapshot."
                    )
                yield stream

    def files(self, engine, max_members=None, max_expanded_bytes=None):
        result = []
        folded_paths = set()
        total = visited = 0

        def visit(parts):
            nonlocal total, visited
            if len(parts) > 256:
                raise ProfileStoreError(
                    "TRANSFER_INVALID", "Profile data directory nesting is excessive."
                )
            with self.directory(parts) as directory, os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda item: (item.name.casefold(), item.name))
                leveldb = engine == "chrome" and any(entry.name == "CURRENT" for entry in entries)
                for entry in entries:
                    child = (*parts, entry.name)
                    if _excluded_path(child, engine, leveldb=leveldb):
                        continue
                    visited += 1
                    if max_members is not None and visited > max_members:
                        raise ProfileStoreError(
                            "TRANSFER_INVALID", "Profile data has too many members."
                        )
                    if "\\" in entry.name or "\x00" in entry.name:
                        raise ProfileStoreError(
                            "TRANSFER_INVALID", "Profile contains an unsafe file name."
                        )
                    # On Windows DirEntry.stat may omit device and inode fields.
                    info = (
                        (directory / entry.name).lstat()
                        if isinstance(directory, Path)
                        else entry.stat(follow_symlinks=False)
                    )
                    if stat.S_ISDIR(info.st_mode):
                        self.directories[child] = info
                        visit(child)
                    elif stat.S_ISREG(info.st_mode):
                        folded = "/".join(child).casefold()
                        if folded in folded_paths:
                            raise ProfileStoreError(
                                "TRANSFER_INVALID", "Profile data has case-colliding paths."
                            )
                        folded_paths.add(folded)
                        result.append((child, info))
                        total += info.st_size
                        if max_expanded_bytes is not None and total > max_expanded_bytes:
                            raise ProfileStoreError(
                                "TRANSFER_INVALID", "Profile data exceeds configured limits."
                            )
                    elif stat.S_ISLNK(info.st_mode):
                        raise ProfileStoreError(
                            "TRANSFER_INVALID", "Profile data contains a symbolic link."
                        )
                    else:
                        raise ProfileStoreError(
                            "TRANSFER_INVALID", "Profile data contains a special file."
                        )

        visit(())
        return result


def _excluded_path(parts: tuple[str, ...], engine: str, *, leveldb: bool) -> bool:
    if not parts:
        return False
    lowered = [part.casefold() for part in parts]
    if any(part in _TRANSIENT_DIRS for part in lowered[:-1]):
        return True
    name = lowered[-1]
    if name in _TRANSIENT_FILES or name.startswith("singleton"):
        return True
    if len(parts) == 1 and name in {"profile.json", "worker.json", "runtime.json", "lease.json"}:
        return True
    if engine == "chrome" and leveldb:
        if name in {"log", "log.old"}:
            return True
        if re.fullmatch(r"[0-9]{6,}\.log", parts[-1]):
            return False
    if name.endswith((".lock", ".tmp", ".temp", ".log", ".pid", ".sock", ".socket")):
        return True
    if name.startswith(".") and name.endswith((".tmp", ".lock", ".log")):
        return True
    if engine == "camoufox" and len(parts) == 1 and name == "chrome_persona.json":
        return True
    return engine == "chrome" and len(parts) == 1 and name == "fingerprint.json"


def _identity_digest(profile_dir: Path, engine: str) -> str:
    digest, _ = _read_identity(profile_dir, engine)
    return digest


def _read_identity(profile_dir: Path, engine: str):
    if engine not in _ENGINES:
        raise ProfileStoreError("TRANSFER_INVALID", "Profile engine is invalid.")
    try:
        tree = _ProfileTree(profile_dir)
        with tree.open_file((_IDENTITY_FILES[engine],)) as source:
            return _validate_identity_bytes(source.read(), engine)
    except OSError as exc:
        raise ProfileStoreError(
            "TRANSFER_INVALID", "Required browser identity is missing or invalid."
        ) from exc


def _validate_identity_bytes(data, engine):
    try:
        parsed = json.loads(data.decode("utf-8"))
        if engine == "camoufox":
            if (
                not isinstance(parsed, dict)
                or parsed.get("os") not in {"windows", "macos", "linux"}
                or not isinstance(parsed.get("fingerprint"), dict)
                or not parsed["fingerprint"]
            ):
                raise ValueError("invalid fingerprint format")
        else:
            from chrome_engine.persona import Persona

            if not isinstance(parsed, dict):
                raise ValueError("invalid persona format")
            parsed = Persona.from_json(parsed)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ProfileStoreError(
            "TRANSFER_INVALID", "Required browser identity is missing or invalid."
        ) from exc
    return hashlib.sha256(engine.encode("ascii") + b"\0" + data).hexdigest(), parsed


def _hash_file(path: Path, limit: int) -> str:
    descriptor = _open_regular_file(path, limit)
    with os.fdopen(descriptor, "rb") as source:
        return _hash_stream(source, limit)


def _open_regular_file(path: Path, limit: int) -> int:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > limit:
            raise ProfileStoreError(
                "TRANSFER_INVALID", "Archive is missing or exceeds the size limit."
            )
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(opened, current)
            or opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
            or opened.st_size > limit
        ):
            os.close(descriptor)
            raise ProfileStoreError("TRANSFER_INVALID", "Archive changed while being opened.")
        return descriptor
    except ProfileStoreError:
        raise
    except OSError as exc:
        raise ProfileStoreError("TRANSFER_INVALID", "Archive is unavailable.") from exc


def _hash_stream(source, limit: int) -> str:
    source.seek(0)
    info = os.fstat(source.fileno())
    if info.st_size > limit:
        raise ProfileStoreError("TRANSFER_INVALID", "Archive exceeds the size limit.")
    digest = hashlib.sha256()
    actual = 0
    while True:
        chunk = source.read(_CHUNK_SIZE)
        if not chunk:
            break
        actual += len(chunk)
        if actual > limit:
            raise ProfileStoreError("TRANSFER_INVALID", "Archive exceeds the size limit.")
        digest.update(chunk)
    if actual != info.st_size:
        raise ProfileStoreError("TRANSFER_INVALID", "Archive changed while being read.")
    return digest.hexdigest()


def _validate_limits(archive: int, expanded: int, members: int) -> None:
    if any(type(value) is not int or value < 1 for value in (archive, expanded, members)):
        raise ProfileStoreError("TRANSFER_INVALID", "Archive limits are invalid.")


def _validate_profile_name(name: str) -> tuple[bool, str]:
    from src.utils.validation import validate_profile_name

    return validate_profile_name(name)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
        return value == value.lower()
    except ValueError:
        return False


def _phase(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _text_attr(value: object, key: str) -> str:
    result = getattr(value, key, None)
    if not isinstance(result, str) or not result.strip():
        raise ProfileStoreError("TRANSFER_INVALID", f"{key} is invalid.")
    return result


def _run_id(run: object) -> str:
    ref = getattr(run, "ref", None)
    return _text_attr(ref, "run_id") if ref is not None else _text_attr(run, "run_id")


def _run_profile_id(run: object) -> str:
    ref = getattr(run, "ref", None)
    return _text_attr(ref, "profile_id") if ref is not None else _text_attr(run, "profile_id")


def _object_value(value: object, key: str) -> Any:
    result = getattr(value, key, None)
    if key == "connection_generation" and result is None:
        return None
    return getattr(result, "value", result)


def _row_value(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _owner_values(row: sqlite3.Row) -> tuple[str | None, str | None, int | None]:
    generation = _row_value(row, "connection_generation")
    return (
        _row_value(row, "worker_id"),
        _row_value(row, "worker_boot_id"),
        generation if type(generation) is int else None,
    )


def _replace_directory(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _sync_move_parents(source, destination)


def _sync_move_parents(source: Path, destination: Path) -> None:
    _fsync_directory(destination.parent, required=True)
    _fsync_directory(source.parent, required=True)


def _fsync_directory(path: Path, *, required: bool = False) -> None:
    # The deployed Master is Linux. Python has no portable Win32 directory-fsync equivalent.
    if os.name != "posix":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        if required:
            raise _DirectorySyncError(
                "SYNC_FAILED", "Profile directory sync has not completed."
            ) from exc
