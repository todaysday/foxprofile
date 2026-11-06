"""Offline maintenance for one stopped FoxProfile installation; never starts browsers."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .errors import DomainError
from .locking import catalog_lock


class MaintenanceError(RuntimeError):
    """A private, offline operation cannot safely proceed."""


@dataclass(frozen=True)
class Installation:
    profiles_file: Path
    data_dir: Path
    database: Path
    configuration_dir: Path
    release_dir: Path
    dependencies_file: Path
    release_revision: str

    @classmethod
    def load(cls, path: Path) -> Installation:
        path = _safe_path(path)
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or set(values) != set(cls.__dataclass_fields__):
            raise MaintenanceError("invalid installation descriptor")
        if not re.fullmatch(r"[a-f0-9]{40}", values["release_revision"]):
            raise MaintenanceError("release_revision must be a locked Git commit")
        if any(
            not Path(value).is_absolute()
            for key, value in values.items()
            if key != "release_revision"
        ):
            raise MaintenanceError("installation paths must be absolute")
        paths = {
            key: _safe_path(Path(value))
            for key, value in values.items()
            if key != "release_revision"
        }
        for key, candidate in paths.items():
            if any(
                candidate == other or candidate in other.parents or other in candidate.parents
                for other_key, other in paths.items()
                if other_key != key
            ):
                raise MaintenanceError("installation paths must be separate, non-overlapping roots")
        return cls(**paths, release_revision=values["release_revision"])

    @property
    def lock_path(self) -> Path:
        return self.profiles_file.with_name("profiles.lock")


def _safe_path(path: Path) -> Path:
    """Reject symlinks, including ancestors; snapshots never follow links."""
    if ".." in path.parts:
        raise MaintenanceError("parent path traversal is not supported")
    path = Path(os.path.abspath(path))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise MaintenanceError("symlinks are not supported in maintenance inputs")
    return path


def _inventory(root: Path) -> dict[str, dict[str, str | bool]]:
    result = {}
    _safe_path(root)
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            result[path.relative_to(root).as_posix()] = {"directory": True}
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise MaintenanceError(
                "snapshot inputs must contain only directories and unlinked regular files"
            )
        result[path.relative_to(root).as_posix()] = {
            "sha256": _digest(path),
            "executable": bool(info.st_mode & 0o100),
        }
    return result


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy(source: Path, destination: Path) -> None:
    _safe_path(source)
    info = source.lstat()
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir(mode=0o700)
        for child in source.iterdir():
            _copy(child, destination / child.name)
    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        with source.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        destination.chmod(0o600 | (info.st_mode & 0o100))
    else:
        raise MaintenanceError("snapshot input is not a regular file or directory")


def _assert_closed(database: Path) -> None:
    if not database.exists():
        return  # Initial upstream installation has no extension database yet.
    _safe_path(database)
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
    ) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MaintenanceError("extension database integrity check failed")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if (
            version == 0
            and not connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        ):
            return  # An interrupted first migration rolled back all schema DDL.
        if version not in range(1, 6):
            raise MaintenanceError("unsupported extension schema")
        checks = []
        if version >= 3:
            checks.extend(
                (
                    "SELECT 1 FROM browser_runs WHERE status NOT IN ('closed', 'cancelled') OR (status = 'cancelled' AND pid IS NOT NULL) LIMIT 1",
                    "SELECT 1 FROM capacity_recovery WHERE blocked != 0 LIMIT 1",
                )
            )
        if version >= 4:
            checks.append("SELECT 1 FROM manual_leases WHERE state != 'closed' LIMIT 1")
        if version >= 5:
            checks.append("SELECT 1 FROM runtime_descriptors LIMIT 1")
        if any(connection.execute(query).fetchone() is not None for query in checks):
            raise MaintenanceError(
                "active or uncertain runtime state; reconcile with the owning manager first"
            )


def _validate_inputs(installation: Installation) -> None:
    for directory in (
        installation.data_dir,
        installation.configuration_dir,
        installation.release_dir,
    ):
        if not directory.is_dir():
            raise MaintenanceError("required installation directory is missing")
        _inventory(directory)
    for path in (
        installation.profiles_file,
        installation.dependencies_file,
        installation.lock_path,
    ):
        _safe_path(path)
    raw = json.loads(installation.profiles_file.read_text(encoding="utf-8"))
    from src.utils.validation import validate_profile_name

    if not isinstance(raw, dict) or any(
        not validate_profile_name(name)[0]
        or not isinstance(value, dict)
        or value.get("name") != name
        for name, value in raw.items()
    ):
        raise MaintenanceError("invalid profile catalog; refusing a lossy migration")
    for directory in installation.data_dir.rglob("*"):
        if directory.name in {
            "parent.lock",
            ".parentlock",
            "lock",
            "SingletonLock",
            "SingletonSocket",
        }:
            raise MaintenanceError(
                "browser lock marker present; verify browser shutdown before maintenance"
            )
    if not installation.dependencies_file.is_file():
        raise MaintenanceError("matching dependency lock/report is missing")
    _assert_closed(installation.database)


def migrate(installation: Installation) -> None:
    """Reuse the additive schema, stable-ID migration and catalog recovery APIs."""
    # Configuration must be selected before any upstream manager import.
    os.environ["FOXPROFILE_PROFILES_FILE"] = str(installation.profiles_file)
    os.environ["FOXPROFILE_DATA_DIR"] = str(installation.data_dir)
    from src.core.config import DATA_DIR, PROFILES_FILE

    if Path(DATA_DIR) != installation.data_dir or Path(PROFILES_FILE) != installation.profiles_file:
        raise MaintenanceError("upstream configuration was imported before maintenance selection")
    from src.services.profile.manager import ProfileManager

    from .catalog import ProfileCatalog
    from .storage import (
        AssignmentDAO,
        CatalogOperationDAO,
        ExtensionDatabase,
        SettingsDAO,
        TransferHandleDAO,
    )

    with catalog_lock(installation.lock_path, timeout=10):
        _validate_inputs(installation)
        database = ExtensionDatabase(installation.database)
        SettingsDAO(database).get_capacity()  # Invalid saved limits must fail, never reset.
        catalog = ProfileCatalog(
            ProfileManager(),
            CatalogOperationDAO(database),
            AssignmentDAO(database),
            installation.lock_path,
            TransferHandleDAO(database),
        )
        catalog.recover()


def backup(installation: Installation, destination: Path) -> None:
    """Create a private matched snapshot; an incomplete copy is never published."""
    destination = _safe_path(destination)
    sources = (
        installation.data_dir,
        installation.configuration_dir,
        installation.release_dir,
        installation.database,
        installation.profiles_file,
        installation.dependencies_file,
    )
    if any(destination == source or source in destination.parents for source in sources):
        raise MaintenanceError("snapshot destination must be outside installation inputs")
    if destination.exists():
        raise MaintenanceError("backup destination must not exist")
    with catalog_lock(installation.lock_path, timeout=10):
        _validate_inputs(installation)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=".foxprofile-backup-", dir=destination.parent))
        try:
            for source, name in (
                (installation.profiles_file, "catalog.json"),
                (installation.data_dir, "profiles"),
                (installation.configuration_dir, "config"),
                (installation.release_dir, "release"),
                (installation.dependencies_file, "dependencies.txt"),
            ):
                _copy(source, staging / name)
            present = installation.database.exists()
            if present:
                deadline = time.monotonic() + 10

                def progress(status: int, remaining: int, total: int) -> None:
                    if time.monotonic() >= deadline:
                        raise MaintenanceError("database backup exceeded its deadline")

                with (
                    closing(
                        sqlite3.connect(installation.database.as_uri() + "?mode=ro", uri=True)
                    ) as source,
                    closing(sqlite3.connect(staging / "state.sqlite3")) as target,
                ):
                    source.backup(target, pages=256, progress=progress, sleep=0.05)
                (staging / "state.sqlite3").chmod(0o600)
            manifest = {
                "format": 1,
                "release_revision": installation.release_revision,
                "database_present": present,
                "files": _inventory(staging),
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            manifest_path.chmod(0o600)
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def restore(snapshot: Path, destination: Path) -> None:
    """Restore only into an explicitly supplied empty disposable directory."""
    snapshot, destination = _safe_path(snapshot), _safe_path(destination)
    if (
        snapshot == destination
        or snapshot in destination.parents
        or destination in snapshot.parents
    ):
        raise MaintenanceError("restore destination and snapshot must be separate")
    if not destination.is_dir() or any(destination.iterdir()):
        raise MaintenanceError("restore requires an existing empty disposable destination")
    files = _inventory(snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    files.pop("manifest.json", None)
    if manifest.get("format") != 1 or files != manifest.get("files"):
        raise MaintenanceError("snapshot identity check failed")
    if not re.fullmatch(r"[a-f0-9]{40}", manifest.get("release_revision", "")):
        raise MaintenanceError("snapshot release identity is missing")
    expected = {
        "manifest.json",
        "catalog.json",
        "profiles",
        "config",
        "release",
        "dependencies.txt",
    }
    if manifest.get("database_present") is True:
        expected.add("state.sqlite3")
    if {path.name for path in snapshot.iterdir()} != expected:
        raise MaintenanceError("snapshot layout is invalid")
    _assert_closed(snapshot / "state.sqlite3")
    staging = Path(tempfile.mkdtemp(prefix=".foxprofile-restore-", dir=destination.parent))
    try:
        for child in snapshot.iterdir():
            _copy(child, staging / child.name)
        descriptor = Installation(
            destination / "catalog.json",
            destination / "profiles",
            destination / "state.sqlite3",
            destination / "config",
            destination / "release",
            destination / "dependencies.txt",
            manifest["release_revision"],
        )
        values = {key: str(value) for key, value in vars(descriptor).items()}
        (staging / "installation.json").write_text(json.dumps(values, indent=2), encoding="utf-8")
        (staging / "installation.json").chmod(0o600)
        # Recheck immediately before replacing the empty directory.
        if any(destination.iterdir()) or destination.is_symlink():
            raise MaintenanceError("restore destination changed during copy")
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "backup"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--installation", required=True, type=Path)
        subparser.add_argument(
            "--stopped-and-closed",
            required=True,
            action="store_true",
            help="attest manager stopped, admission fenced, all profile browsers closed",
        )
        if command == "backup":
            subparser.add_argument("--destination", required=True, type=Path)
    subparser = commands.add_parser("restore")
    subparser.add_argument("--snapshot", required=True, type=Path)
    subparser.add_argument("--empty-disposable-destination", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)  # Catalog errors can include private paths/profile data.
    previous_umask = os.umask(0o077)
    try:
        if args.command == "restore":
            restore(args.snapshot, args.empty_disposable_destination)
        else:
            installation = Installation.load(args.installation)
            if args.command == "migrate":
                migrate(installation)
            else:
                backup(installation, args.destination)
    except (
        MaintenanceError,
        DomainError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        RuntimeError,
    ):
        print(
            "Maintenance refused or failed; keep installation stopped and inspect private inputs.",
            file=sys.stderr,
        )
        return 1
    finally:
        os.umask(previous_umask)
    print(f"{args.command}: complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
