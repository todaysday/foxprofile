import json
import zipfile

import pytest

from src.services.browser.launcher import BrowserLauncher, ProfileBusyError, _LaunchResult
from src.services.profile.transfer import import_from_zip


def _archive(path, name, files):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("profile.json", json.dumps({"name": name, "os_type": "windows"}))
        for arcname, data in files.items():
            z.writestr(arcname, data)
    return str(path)


def test_import_rejects_traversal_profile_name(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "..", {})
    ok, msg = import_from_zip(zip_path, str(tmp_path / "data"))
    assert not ok and "name" in msg


def test_import_rejects_zip_slip_entry(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "good", {"data/../../escaped.txt": "x"})
    ok, msg = import_from_zip(zip_path, str(tmp_path / "data"))
    assert not ok and "Unsafe path" in msg
    assert not (tmp_path / "escaped.txt").exists()


def test_import_extracts_normal_archive(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "good", {"data/fingerprint.json": "{}"})
    ok, profile = import_from_zip(zip_path, str(tmp_path / "data"))
    assert ok and profile.name == "good"
    assert (tmp_path / "data" / "good" / "fingerprint.json").read_text() == "{}"


def test_import_rejects_malformed_proxy_before_extracting_data(tmp_path):
    zip_path = tmp_path / "bad-proxy.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("profile.json", json.dumps({"name": "bad-proxy", "proxy": "not-a-proxy"}))
        archive.writestr("data/Cookies", "private")

    from src.services.profile.transfer import read_profile_meta

    assert read_profile_meta(str(zip_path)) is None
    ok, message = import_from_zip(str(zip_path), str(tmp_path / "data"))

    assert not ok and "proxy" in message
    assert not (tmp_path / "data" / "bad-proxy").exists()


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {"name": 123},
        {"name": "../bad"},
        {"name": "bad-engine-type", "engine": []},
        {"name": "bad-engine-value", "engine": "unknown"},
        {"name": "bad-os-type", "os_type": {}},
        {"name": "bad-os-value", "os_type": "other"},
        {"name": "bad-timezone-type", "timezone": []},
        {"name": "bad-timezone-value", "timezone": "Not/AZone"},
        {"name": "bad-locale-type", "locale": {}},
        {"name": "bad-locale-value", "locale": "invalid"},
    ],
)
def test_profile_metadata_requires_valid_profile_fields(tmp_path, metadata):
    from src.services.profile.transfer import read_profile_meta

    zip_path = tmp_path / "bad-meta.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("profile.json", json.dumps(metadata))

    assert read_profile_meta(str(zip_path)) is None
    ok, _ = import_from_zip(str(zip_path), str(tmp_path / "data"))
    assert not ok


def test_import_streams_large_compressed_member_in_bounded_reads(tmp_path, monkeypatch):
    from src.services.profile import transfer

    zip_path = tmp_path / "large.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("profile.json", json.dumps({"name": "good", "os_type": "windows"}))
        archive.writestr("data/large.bin", "x" * (40 * 1024 * 1024))
    assert zip_path.stat().st_size < 1024 * 1024

    read_sizes = []
    original_read = zipfile.ZipExtFile.read

    def tracked_read(source, size=-1):
        read_sizes.append(size)
        return original_read(source, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", tracked_read)
    ok, profile = import_from_zip(zip_path, str(tmp_path / "data"))

    assert ok and profile.name == "good"
    assert (tmp_path / "data" / "good" / "large.bin").stat().st_size == 40 * 1024 * 1024
    assert len(read_sizes) > 2
    assert all(0 < size <= transfer._COPY_CHUNK_SIZE for size in read_sizes[1:])


def test_oversized_profile_metadata_is_rejected_by_both_read_paths(tmp_path, monkeypatch):
    from src.services.profile import transfer

    zip_path = tmp_path / "oversized-meta.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "profile.json",
            json.dumps({"name": "good", "padding": "x" * transfer._PROFILE_META_MAX_BYTES}),
        )

    read_sizes = []
    original_read = zipfile.ZipExtFile.read

    def tracked_read(source, size=-1):
        read_sizes.append(size)
        return original_read(source, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", tracked_read)
    assert transfer.read_profile_meta(str(zip_path)) is None
    ok, message = import_from_zip(str(zip_path), str(tmp_path / "data"))

    assert not ok and "profile.json exceeds 1 MiB" in message
    assert read_sizes == [transfer._PROFILE_META_MAX_BYTES + 1] * 2


def test_exclusive_blocks_second_holder():
    bl = BrowserLauncher()
    with bl.exclusive("p"), pytest.raises(ProfileBusyError), bl.exclusive("p"):
        pass
    with bl.exclusive("p"):  # released after the block
        pass


def test_launch_result_settles_once():
    r = _LaunchResult()
    r.settle(True)
    r.settle(False, "late failure")
    assert r.ok is True and r.error is None


def test_export_without_data_keeps_the_identity(tmp_path):
    from src.models.profile import Profile
    from src.services.profile.transfer import export_to_zip

    data = tmp_path / "data"
    (data / "Default").mkdir(parents=True)
    for name in ("fingerprint.json", "chrome_persona.json", "geo_locale.json"):
        (data / name).write_text("{}", encoding="utf-8")
    (data / "Default" / "Cookies").write_text("x", encoding="utf-8")
    ok, zip_path = export_to_zip(Profile(name="alice"), str(data), str(tmp_path), False)
    assert ok
    names = set(zipfile.ZipFile(zip_path).namelist())
    assert {"data/fingerprint.json", "data/chrome_persona.json", "data/geo_locale.json"} <= names
    assert "data/Default/Cookies" not in names


def test_export_without_data_skips_identity_symlink(tmp_path):
    from src.models.profile import Profile
    from src.services.profile.transfer import export_to_zip

    data = tmp_path / "profile-data"
    data.mkdir()
    canary = b"outside profile data"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(canary)
    (data / "fingerprint.json").symlink_to(outside)

    ok, archive_path = export_to_zip(Profile(name="safe"), str(data), str(tmp_path), False)

    assert ok
    with zipfile.ZipFile(archive_path) as archive:
        assert "data/fingerprint.json" not in archive.namelist()
        assert all(canary not in archive.read(name) for name in archive.namelist())


def test_export_without_data_portable_fallback_keeps_regular_identity_and_skips_symlink(
    tmp_path, monkeypatch
):
    from src.models.profile import Profile
    from src.services.profile import transfer
    from src.services.profile.transfer import export_to_zip

    data = tmp_path / "profile-data"
    data.mkdir()
    (data / "fingerprint.json").write_text("legitimate identity", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside profile data")
    (data / "chrome_persona.json").symlink_to(outside)
    monkeypatch.delattr(transfer.os, "O_NOFOLLOW")

    ok, archive_path = export_to_zip(Profile(name="safe"), str(data), str(tmp_path), False)

    assert ok
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("data/fingerprint.json") == b"legitimate identity"
        assert "data/chrome_persona.json" not in archive.namelist()


def test_export_with_data_portable_fallback_verifies_nested_files(tmp_path, monkeypatch):
    from src.models.profile import Profile
    from src.services.profile import transfer
    from src.services.profile.transfer import export_to_zip

    data = tmp_path / "profile-data"
    nested = data / "Default"
    nested.mkdir(parents=True)
    (nested / "Cookies").write_bytes(b"legitimate profile data")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside profile data")
    (nested / "Preferences").symlink_to(outside)
    monkeypatch.delattr(transfer.os, "O_NOFOLLOW")

    ok, archive_path = export_to_zip(Profile(name="safe"), str(data), str(tmp_path), True)

    assert ok
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("data/Default/Cookies") == b"legitimate profile data"
        assert "data/Default/Preferences" not in archive.namelist()


@pytest.mark.parametrize("swap_at", ["leaf", "parent"])
def test_export_with_data_does_not_follow_symlink_installed_after_scan(
    tmp_path, monkeypatch, swap_at
):
    from src.models.profile import Profile
    from src.services.profile import transfer
    from src.services.profile.transfer import export_to_zip

    data = tmp_path / "profile-data"
    nested = data / "Default"
    nested.mkdir(parents=True)
    target = nested / "Cookies"
    target.write_bytes(b"own profile data")
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "Cookies"
    canary.write_bytes(b"outside profile data")
    original_open = transfer.os.open
    swapped = False

    def replace_with_symlink(path, flags, *args, **kwargs):
        nonlocal swapped
        expected_name = "Cookies" if swap_at == "leaf" else "Default"
        if path == expected_name and kwargs.get("dir_fd") is not None and not swapped:
            if swap_at == "leaf":
                target.unlink()
                target.symlink_to(canary)
            else:
                nested.rename(data / "Default-original")
                nested.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(transfer.os, "open", replace_with_symlink)
    monkeypatch.setattr(
        transfer.os,
        "supports_dir_fd",
        transfer.os.supports_dir_fd | {replace_with_symlink},
    )
    ok, archive_path = export_to_zip(Profile(name="safe"), str(data), str(tmp_path), True)

    assert ok and swapped
    with zipfile.ZipFile(archive_path) as archive:
        assert not any(name.endswith("/Cookies") for name in archive.namelist())
        assert all(b"outside profile data" not in archive.read(name) for name in archive.namelist())


@pytest.mark.parametrize("include_data", [False, True])
def test_export_skips_profile_data_root_symlink(tmp_path, include_data):
    from src.models.profile import Profile
    from src.services.profile.transfer import export_to_zip

    outside = tmp_path / "outside-data"
    outside.mkdir()
    (outside / "Cookies").write_bytes(b"outside profile data")
    (outside / "fingerprint.json").write_text("outside identity", encoding="utf-8")
    profile_data = tmp_path / "profile-data"
    profile_data.symlink_to(outside, target_is_directory=True)

    ok, archive_path = export_to_zip(
        Profile(name="safe"), str(profile_data), str(tmp_path), include_data
    )

    assert ok
    with zipfile.ZipFile(archive_path) as archive:
        assert "data/Cookies" not in archive.namelist()
        assert "data/fingerprint.json" not in archive.namelist()
