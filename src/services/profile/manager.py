import contextlib
import json
import os
import pathlib
import shutil
import tempfile
import threading
import time
import uuid

from ...core.config import DATA_DIR, PROFILES_FILE
from ...core.logging import get_logger
from ...models.profile import ENGINES, Profile
from ...utils.validation import (
    valid_or_none,
    validate_locale,
    validate_profile_name,
    validate_timezone,
)
from .transfer import export_to_zip, import_from_zip, read_profile_meta

logger = get_logger("profile.manager")


def _engine_or_default(value: object) -> str:
    return value if value in ENGINES else "camoufox"


def _replace(src: pathlib.Path, dst: pathlib.Path, attempts: int = 10) -> None:
    """os.replace, retried briefly: on Windows it fails with a sharing violation
    while another process (antivirus, indexer, a reader) has the target open."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))


class ProfileManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.data_dir = pathlib.Path(DATA_DIR)
        self.profiles_file = pathlib.Path(PROFILES_FILE)
        self.profiles: dict[str, Profile] = {}
        self._load_profiles()
        self.data_dir.mkdir(exist_ok=True, parents=True)

    def _find_by_casefold(self, name: str) -> str | None:
        """Return the existing profile key that case-folds to *name*, or None.

        Used to enforce case-insensitive uniqueness across all platforms so that
        a profiles.json exported on Linux (case-sensitive FS) can be imported on
        Windows/macOS (case-insensitive FS) without data-dir collisions.
        """
        lower = name.casefold()
        return next((k for k in self.profiles if k.casefold() == lower), None)

    def _data_path(self, name: str) -> str:
        return str(self.data_dir / name)

    def _load_profiles(self, *, strict: bool = False) -> None:
        if self.profiles_file.exists():
            try:
                with self.profiles_file.open(encoding="utf-8") as f:
                    data = json.load(f)
                    for name, p_data in data.items():
                        if not validate_profile_name(name)[0] or p_data.get("name") != name:
                            logger.warning("Skipping profile with unsafe name: %r", name)
                            continue
                        clean_data = {
                            "name": p_data.get("name"),
                            "proxy": p_data.get("proxy"),
                            "os_type": p_data.get(
                                "os_type",
                                p_data.get("config", {}).get("os", "windows"),
                            ),
                            "timezone": valid_or_none(p_data.get("timezone"), validate_timezone),
                            "locale": valid_or_none(p_data.get("locale"), validate_locale),
                            "engine": _engine_or_default(p_data.get("engine")),
                            "profile_id": p_data.get("profile_id") or str(uuid.uuid4()),
                        }
                        self.profiles[name] = Profile(**clean_data)
                logger.info("Loaded %d profiles", len(self.profiles))
            except Exception as e:
                logger.exception("Error loading profiles: %s", e)
                if strict:
                    raise

    def reload_profiles(self) -> None:
        """Discard the process cache and load the current on-disk catalog."""
        with self._lock:
            self.profiles.clear()
            self._load_profiles(strict=True)

    def save_profiles(self) -> None:
        """Atomic write: write to a temp file then os.replace."""
        path = self.profiles_file
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as f:
                tmp = pathlib.Path(f.name)
                json.dump(
                    {name: p.to_dict() for name, p in self.profiles.items()},
                    f,
                    indent=4,
                )
                f.flush()
                os.fsync(f.fileno())
            _replace(tmp, path)
            try:
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                # Some platforms do not support fsync on directory handles.
                pass
            logger.debug("Profiles saved")
        except Exception as e:
            logger.exception("Error saving profiles: %s", e)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
            raise

    def add_profile(
        self,
        name: str,
        proxy: str,
        os_type: str,
        timezone: str | None = None,
        locale: str | None = None,
        engine: str = "camoufox",
        profile_id: str | None = None,
    ) -> bool:
        with self._lock:
            if self._find_by_casefold(name) is not None:
                return False
            # Create directory before touching profiles.json so a mkdir failure
            # does not leave an entry with no data dir on disk.
            try:
                pathlib.Path(self._data_path(name)).mkdir(parents=True, exist_ok=False)
            except OSError as e:
                logger.warning("Could not create data dir for %s: %s", name, e)
                return False
            profile = Profile(
                name=name,
                proxy=proxy or None,
                os_type=os_type,
                timezone=timezone or None,
                locale=locale or None,
                engine=engine,
                profile_id=profile_id or str(uuid.uuid4()),
            )
            self.profiles[name] = profile
            try:
                self.save_profiles()
            except Exception:
                self.profiles.pop(name, None)
                with contextlib.suppress(OSError):
                    pathlib.Path(self._data_path(name)).rmdir()
                raise
            logger.info("Created profile: %s", name)
            return True

    def update_profile(
        self,
        original_name: str,
        new_name: str,
        new_proxy: str,
        new_os: str,
        new_timezone: str | None = None,
        new_locale: str | None = None,
    ) -> bool:
        """Replace every field; a None/empty timezone or locale means automatic."""
        with self._lock:
            if original_name not in self.profiles:
                return False

            if new_name != original_name:
                # Case-insensitive uniqueness: a case-only rename of the profile
                # itself is allowed; renaming to another profile's casefold is not.
                conflicting = self._find_by_casefold(new_name)
                if conflicting is not None and conflicting != original_name:
                    return False

            # Rename the data directory BEFORE mutating in-memory state so that
            # an OSError leaves memory and JSON untouched.
            if new_name != original_name:
                old_dir = pathlib.Path(self._data_path(original_name))
                new_dir = pathlib.Path(self._data_path(new_name))
                if old_dir.exists():
                    if new_dir.exists():
                        # On case-insensitive filesystems (Windows/macOS) a
                        # case-only rename sees the target as existing because
                        # both paths point to the same directory entry.  Allow
                        # the rename only in that situation.
                        try:
                            same = old_dir.samefile(new_dir)
                        except OSError:
                            same = False
                        if not same:
                            # Refuse: do NOT delete potential user data.
                            logger.warning(
                                "Cannot rename %s -> %s: target data dir already exists",
                                original_name,
                                new_name,
                            )
                            return False
                    try:
                        old_dir.rename(new_dir)
                    except OSError as e:
                        logger.warning("Could not rename data dir for %s: %s", original_name, e)
                        return False

            profile = self.profiles[original_name]
            previous = (
                profile.name,
                profile.proxy,
                profile.os_type,
                profile.timezone,
                profile.locale,
            )
            profile.name = new_name
            profile.proxy = new_proxy or None
            profile.os_type = new_os
            profile.timezone = new_timezone or None
            profile.locale = new_locale or None

            if new_name != original_name:
                del self.profiles[original_name]
                self.profiles[new_name] = profile

            try:
                self.save_profiles()
            except Exception:
                if new_name != original_name:
                    with contextlib.suppress(OSError):
                        pathlib.Path(self._data_path(new_name)).rename(
                            pathlib.Path(self._data_path(original_name))
                        )
                    self.profiles.pop(new_name, None)
                    self.profiles[original_name] = profile
                (
                    profile.name,
                    profile.proxy,
                    profile.os_type,
                    profile.timezone,
                    profile.locale,
                ) = previous
                raise
            logger.info("Updated profile: %s -> %s", original_name, new_name)
            return True

    def quarantine_data(self, name: str, operation_id: str) -> None:
        data_path = pathlib.Path(self._data_path(name))
        if data_path.exists():
            quarantine = data_path.with_name(f".{name}.deleted-{operation_id}")
            if quarantine.exists():
                raise FileExistsError(f"quarantine target already exists for {name}")
            data_path.rename(quarantine)
            # Remove private staged data when cleanup succeeds. If it fails, the
            # operation remains pending and the renamed directory stays hidden.
            shutil.rmtree(quarantine)

    def delete_profile(self, name: str, operation_id: str | None = None) -> bool:
        with self._lock:
            if name not in self.profiles:
                return False
            profile = self.profiles.pop(name)
            try:
                self.save_profiles()
            except Exception:
                self.profiles[name] = profile
                raise
            # Inside the lock: a profile re-created under the same (or, on a
            # case-insensitive file system, a case-variant) name must not have
            # its fresh data dir removed by this delete.
            try:
                self.quarantine_data(name, operation_id or uuid.uuid4().hex)
            except OSError:
                logger.exception("Could not quarantine deleted profile data: %s", name)
                raise
        logger.info("Deleted profile: %s", name)
        return True

    def list_profiles(self) -> list[Profile]:
        with self._lock:
            return list(self.profiles.values())

    def export_profile(
        self,
        name: str,
        export_path: str,
        include_data: bool = True,
    ) -> tuple[bool, str]:
        with self._lock:
            if name not in self.profiles:
                return False, "Profile not found"
            profile = self.profiles[name]
            data_path = self._data_path(name)
        return export_to_zip(profile, data_path, export_path, include_data)

    def import_profile(
        self,
        zip_path: str,
        overwrite: bool = False,
        target_name: str | None = None,
        profile_id: str | None = None,
    ) -> tuple[bool, str]:
        # Decide before extracting: an archive must not write into an existing
        # profile's data unless overwriting, and an overwrite that changes the
        # engine starts from an empty directory (the engines' data do not mix).
        meta = read_profile_meta(zip_path)
        if meta is None:
            return False, "Invalid profile archive metadata"
        name = target_name or meta["name"]
        if not isinstance(name, str) or not validate_profile_name(name)[0]:
            return False, "Invalid profile name in archive"
        if target_name is not None and not overwrite:
            return False, "An explicit import target requires overwrite"
        with self._lock:
            # Case-insensitive collision check: find any existing profile whose
            # name casefolds to the imported name so the check is consistent on
            # all platforms (Windows/macOS case-insensitive FS, Linux sensitive).
            existing_key = self._find_by_casefold(name) if name else None
            if target_name is not None and existing_key is None:
                return False, f"Profile '{target_name}' not found"
            if existing_key is not None:
                if not overwrite:
                    return False, f"Profile '{name}' already exists"
            elif pathlib.Path(self._data_path(name)).exists():
                return False, "Residual profile data directory exists"
            profile_name = target_name or name
            staging = self.data_dir / f".import-{uuid.uuid4().hex}"
            staging.mkdir(mode=0o700, parents=True)
            success, result = import_from_zip(zip_path, str(staging))
            if not success:
                shutil.rmtree(staging, ignore_errors=True)
                return False, result

            profile = result
            assert isinstance(profile, Profile)
            imported_name = profile.name
            profile.name = profile_name or profile.name
            if existing_key is not None:
                profile.profile_id = self.profiles[existing_key].profile_id
            elif profile_id is not None:
                profile.profile_id = profile_id
            data_staging = staging / imported_name
            destination = pathlib.Path(self._data_path(profile.name))
            backup = destination.with_name(f".{profile.name}.replaced-{uuid.uuid4().hex}")
            previous = self.profiles.get(existing_key) if existing_key is not None else None
            case_change = existing_key is not None and existing_key != profile.name
            old_directory = pathlib.Path(self._data_path(existing_key)) if case_change else None
            old_backup = (
                old_directory.with_name(f".{existing_key}.replaced-{uuid.uuid4().hex}")
                if old_directory is not None
                else None
            )
            replace_data = data_staging.exists() or (
                previous is not None and previous.engine != profile.engine
            )
            try:
                if old_directory is not None and old_directory.exists():
                    if old_backup is None:
                        raise RuntimeError("missing case-change backup path")
                    old_directory.rename(old_backup)
                if case_change and destination.exists():
                    raise FileExistsError("residual profile data directory exists")
                if replace_data:
                    if destination.exists() and (existing_key is None or case_change):
                        raise FileExistsError("residual profile data directory exists")
                    if destination.exists():
                        destination.rename(backup)
                    if data_staging.exists():
                        data_staging.rename(destination)
                    else:
                        destination.mkdir(parents=True)
                elif not destination.exists():
                    destination.mkdir(parents=True)
            except OSError as e:
                if backup.exists() and not destination.exists():
                    backup.rename(destination)
                if old_backup is not None and old_backup.exists():
                    if destination.exists():
                        shutil.rmtree(destination)
                    old_backup.rename(old_directory)
                shutil.rmtree(staging, ignore_errors=True)
                return False, str(e)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

            # An overwrite under a different case replaces the old entry rather
            # than leaving two names for one data directory.
            if existing_key is not None and existing_key != profile.name:
                del self.profiles[existing_key]
            self.profiles[profile.name] = profile
            try:
                self.save_profiles()
            except Exception:
                if previous is not None:
                    self.profiles.pop(profile.name, None)
                    self.profiles[existing_key] = previous
                    if backup.exists():
                        with contextlib.suppress(OSError):
                            shutil.rmtree(destination)
                        backup.rename(destination)
                    if old_backup is not None and old_backup.exists():
                        with contextlib.suppress(OSError):
                            shutil.rmtree(destination)
                        old_backup.rename(old_directory)
                else:
                    self.profiles.pop(profile.name, None)
                    if destination.exists():
                        self.quarantine_data(profile.name, uuid.uuid4().hex)
                raise
            if backup.exists():
                backup.rename(
                    backup.with_name(f".{profile.name}.replaced-quarantine-{uuid.uuid4().hex}")
                )
            if old_backup is not None and old_backup.exists():
                old_backup.rename(
                    old_backup.with_name(f".{existing_key}.replaced-quarantine-{uuid.uuid4().hex}")
                )
            logger.info("Registered imported profile: %s", profile.name)
            return True, profile.name
