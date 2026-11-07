import json
import os
import pathlib
import shutil
import stat
import zipfile
from datetime import datetime

from ...core.logging import get_logger
from ...models.profile import ENGINES, Profile
from ...utils.validation import (
    valid_or_none,
    validate_locale,
    validate_profile_name,
    validate_proxy_format,
    validate_timezone,
)
from ..browser.fingerprint import IDENTITY_FILES

logger = get_logger("profile.transfer")
_PROFILE_META_MAX_BYTES = 1024 * 1024
_COPY_CHUNK_SIZE = 64 * 1024


def _read_profile_meta(zipf: zipfile.ZipFile) -> dict:
    with zipf.open("profile.json") as source:
        data = source.read(_PROFILE_META_MAX_BYTES + 1)
    if len(data) > _PROFILE_META_MAX_BYTES:
        raise ValueError("profile.json exceeds 1 MiB")
    metadata = json.loads(data)
    if not isinstance(metadata, dict):
        raise ValueError("profile.json must contain an object")

    name = metadata.get("name")
    if not isinstance(name, str) or not validate_profile_name(name)[0]:
        raise ValueError("Invalid profile name in archive")

    proxy = metadata.get("proxy")
    if proxy is not None and (not isinstance(proxy, str) or not validate_proxy_format(proxy)[0]):
        raise ValueError("Invalid proxy in archive")
    os_type = metadata.get("os_type", "windows")
    if not isinstance(os_type, str) or os_type not in {"windows", "macos", "linux"}:
        raise ValueError("Invalid operating system in archive")
    engine = metadata.get("engine", "camoufox")
    if not isinstance(engine, str) or engine not in ENGINES:
        raise ValueError("Invalid engine in archive")
    for field, validate in (
        ("timezone", validate_timezone),
        ("locale", validate_locale),
    ):
        value = metadata.get(field)
        if value is not None and (not isinstance(value, str) or not validate(value)[0]):
            raise ValueError(f"Invalid {field} in archive")
    return metadata


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_ino != 0
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
    )


def _open_profile_data_dir(path: pathlib.Path) -> int:
    if path.is_symlink():
        raise OSError("profile data root is a symlink")
    canonical_parent = path.parent.resolve()
    canonical = path.resolve()
    if canonical.parent != canonical_parent or canonical.name != path.name:
        raise OSError("profile data root changed")

    flags = os.O_RDONLY | os.O_DIRECTORY
    descriptor = os.open(canonical.anchor, flags)
    try:
        for component in canonical.parts[1:]:
            next_descriptor = os.open(
                component,
                flags | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("profile data root is not a directory")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _write_descriptor_file(
    zipf: zipfile.ZipFile,
    directory_fd: int,
    name: str,
    archive_parts: tuple[str, ...],
    expected: os.stat_result | None = None,
) -> None:
    try:
        source_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
    except OSError:
        return

    try:
        actual = os.fstat(source_fd)
        if not stat.S_ISREG(actual.st_mode) or (
            expected is not None and not _same_file(expected, actual)
        ):
            return
        with os.fdopen(source_fd, "rb") as source:
            source_fd = -1
            with zipf.open("/".join(archive_parts), "w") as destination:
                shutil.copyfileobj(source, destination, _COPY_CHUNK_SIZE)
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def _write_descriptor_tree(
    zipf: zipfile.ZipFile,
    directory_fd: int,
    parts: tuple[str, ...],
    *,
    identities_only: bool = False,
) -> None:
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if identities_only and (parts or entry.name not in IDENTITY_FILES):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            archive_parts = ("data", *parts, entry.name)
            if stat.S_ISREG(info.st_mode):
                _write_descriptor_file(zipf, directory_fd, entry.name, archive_parts, info)
            elif stat.S_ISDIR(info.st_mode) and not identities_only:
                try:
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                except OSError:
                    continue
                try:
                    actual = os.fstat(child_fd)
                    if stat.S_ISDIR(actual.st_mode) and _same_file(info, actual):
                        _write_descriptor_tree(zipf, child_fd, (*parts, entry.name))
                finally:
                    os.close(child_fd)


def _portable_root(path: pathlib.Path) -> tuple[os.stat_result, pathlib.Path] | None:
    try:
        info = path.lstat()
        canonical_parent = path.parent.resolve()
        canonical = path.resolve()
    except (OSError, RuntimeError):
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or canonical.parent != canonical_parent
        or canonical.name != path.name
    ):
        return None
    return info, canonical


def _portable_parents_match(
    ancestors: tuple[tuple[pathlib.Path, os.stat_result], ...],
    canonical_root: pathlib.Path,
) -> bool:
    try:
        for path, expected in ancestors:
            current = path.lstat()
            if (
                not stat.S_ISDIR(current.st_mode)
                or not _same_file(expected, current)
                or not path.resolve().is_relative_to(canonical_root)
            ):
                return False
        return True
    except (OSError, RuntimeError):
        return False


def _write_portable_file(
    zipf: zipfile.ZipFile,
    path: pathlib.Path,
    canonical_root: pathlib.Path,
    ancestors: tuple[tuple[pathlib.Path, os.stat_result], ...],
    expected: os.stat_result,
    archive_parts: tuple[str, ...],
) -> None:
    if not stat.S_ISREG(expected.st_mode):
        return
    source = None
    try:
        if not _same_file(expected, path.lstat()):
            return
        if not path.resolve().is_relative_to(canonical_root):
            return
        source = path.open("rb")
        opened = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_file(expected, opened)
            or not _portable_parents_match(ancestors, canonical_root)
            or not _same_file(opened, path.lstat())
            or not path.resolve().is_relative_to(canonical_root)
        ):
            source.close()
            return
    except (OSError, RuntimeError):
        if source is not None:
            source.close()
        return
    with source, zipf.open("/".join(archive_parts), "w") as destination:
        shutil.copyfileobj(source, destination, _COPY_CHUNK_SIZE)


def _write_portable_tree(
    zipf: zipfile.ZipFile,
    root: pathlib.Path,
    canonical_root: pathlib.Path,
    ancestors: tuple[tuple[pathlib.Path, os.stat_result], ...],
    parts: tuple[str, ...],
    *,
    identities_only: bool = False,
) -> None:
    try:
        entries = os.scandir(root)
    except OSError:
        return
    with entries:
        for entry in entries:
            if identities_only and (parts or entry.name not in IDENTITY_FILES):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = pathlib.Path(entry.path)
            archive_parts = ("data", *parts, entry.name)
            if stat.S_ISREG(info.st_mode):
                _write_portable_file(zipf, path, canonical_root, ancestors, info, archive_parts)
            elif stat.S_ISDIR(info.st_mode) and not identities_only:
                nested = (*ancestors, (path, info))
                if _portable_parents_match(nested, canonical_root):
                    _write_portable_tree(
                        zipf,
                        path,
                        canonical_root,
                        nested,
                        (*parts, entry.name),
                    )


def _write_profile_data(
    zipf: zipfile.ZipFile, profile_data_dir: str, *, identities_only: bool = False
) -> None:
    path = pathlib.Path(profile_data_dir)
    descriptor_paths = (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.scandir in os.supports_fd
    )
    if descriptor_paths:
        try:
            directory_fd = _open_profile_data_dir(path)
        except OSError:
            return
        try:
            _write_descriptor_tree(zipf, directory_fd, (), identities_only=identities_only)
        finally:
            os.close(directory_fd)
        return

    root = _portable_root(path)
    if root is None:
        return
    info, canonical_root = root
    _write_portable_tree(
        zipf,
        path,
        canonical_root,
        ((path, info),),
        (),
        identities_only=identities_only,
    )


def export_to_zip(
    profile: Profile,
    profile_data_dir: str,
    export_path: str,
    include_data: bool = True,
) -> tuple[bool, str]:
    """Create a ZIP archive of a profile and optionally its browser data."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_filename = f"{profile.name}_{timestamp}.zip"
        zip_path = os.path.join(export_path, zip_filename)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr("profile.json", json.dumps(profile.to_portable_dict(), indent=2))

            profile_data_path = pathlib.Path(profile_data_dir)
            if include_data and profile_data_path.exists() and not profile_data_path.is_symlink():
                _write_profile_data(zipf, profile_data_dir)
            else:
                # The fingerprint is the profile's identity, not browser data:
                # keep it so the imported profile presents the same device.
                _write_profile_data(zipf, profile_data_dir, identities_only=True)

        logger.info("Exported profile %s to %s", profile.name, zip_path)
        return True, zip_path
    except Exception as e:
        logger.exception("Error exporting profile %s: %s", profile.name, e)
        return False, str(e)


def read_profile_meta(zip_path: str) -> dict | None:
    """profile.json of an archive without extracting anything, or None if unreadable."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            return _read_profile_meta(zipf)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def import_from_zip(
    zip_path: str,
    data_dir: str,
) -> tuple[bool, Profile | str]:
    """Extract a profile from a ZIP archive.

    Returns (True, Profile) on success or (False, error_message) on failure.
    The caller is responsible for registering the profile in the manager.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            if "profile.json" not in zipf.namelist():
                return False, "Invalid profile archive (missing profile.json)"

            try:
                profile_data = _read_profile_meta(zipf)
            except ValueError as e:
                return False, str(e)
            name = profile_data["name"]

            profile = Profile(
                name=name,
                proxy=profile_data.get("proxy"),
                os_type=profile_data.get("os_type", "windows"),
                timezone=valid_or_none(profile_data.get("timezone"), validate_timezone),
                locale=valid_or_none(profile_data.get("locale"), validate_locale),
                engine=profile_data.get("engine")
                if profile_data.get("engine") in ENGINES
                else "camoufox",
            )

            data_files = [f for f in zipf.namelist() if f.startswith("data/")]
            seen: set[str] = set()
            for member in zipf.infolist():
                member_name = member.filename
                if member_name in {"profile.json", ""}:
                    continue
                if "\\" in member_name or member_name.startswith("/"):
                    return False, f"Unsafe path in archive: {member_name}"
                parts = pathlib.PurePosixPath(member_name).parts
                if any(part in {"", ".", ".."} for part in parts):
                    return False, f"Unsafe path in archive: {member_name}"
                normalized = "/".join(parts).casefold()
                if normalized in seen:
                    return False, f"Duplicate path in archive: {member_name}"
                seen.add(normalized)
                if stat.S_ISLNK(member.external_attr >> 16):
                    return False, f"Symbolic links are not allowed in archives: {member_name}"
                if member_name != "profile.json" and not member_name.startswith("data/"):
                    return False, f"Unsupported archive entry: {member_name}"
            if data_files:
                profile_data_dir = os.path.join(data_dir, name)
                root = pathlib.Path(profile_data_dir).resolve()
                root.mkdir(exist_ok=True, parents=True)

                for file in data_files:
                    if file.endswith("/"):
                        continue
                    relative = pathlib.PurePosixPath(file).relative_to("data")
                    target_path = pathlib.Path(profile_data_dir) / pathlib.Path(*relative.parts)
                    # Reject entries such as data/../../evil that would be
                    # written outside the profile's own directory.
                    if not target_path.resolve().is_relative_to(root):
                        return False, f"Unsafe path in archive: {file}"
                    target_path.parent.mkdir(
                        exist_ok=True,
                        parents=True,
                    )
                    with zipf.open(file) as src, target_path.open("xb") as dst:
                        shutil.copyfileobj(src, dst, _COPY_CHUNK_SIZE)

            logger.info("Imported profile from zip: %s", name)
            return True, profile
    except zipfile.BadZipFile:
        return False, "Invalid zip file"
    except Exception as e:
        logger.exception("Error importing profile: %s", e)
        return False, str(e)
