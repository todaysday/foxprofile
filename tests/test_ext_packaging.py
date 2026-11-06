"""Offline distribution checks; build tools must already be installed."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("src", "chrome_engine", "foxprofile_ext")


def run_python(code: str, cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=cwd,
        env={**os.environ, "PIP_NO_INDEX": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture(scope="module")
def distributions(tmp_path_factory):
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("offline packaging checks require setuptools>=77 and wheel")
    pytest.importorskip("wheel")
    source = tmp_path_factory.mktemp("packaging-source")
    for name in PACKAGES:
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in (
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "LICENSE",
        "foxprofile_mcp.py",
        "requirements.txt",
        "requirements-extension.txt",
    ):
        shutil.copyfile(ROOT / name, source / name)

    # Realistic accidental local files must never become application payload.
    for name in (
        ".env",
        "profiles.json",
        "camoufox_data/private/cookies.json",
        "logs/private.log",
        "tests/fixtures/private.json",
        "src/assets/private.sqlite",
        "foxprofile_ext/assets/private.json",
        "foxprofile_ext/assets/novnc/private.bin",
        "chrome_engine/browser/private.bin",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("packaging exclusion fixture", encoding="utf-8")

    output = tmp_path_factory.mktemp("packaging-output")
    run_python(
        "from setuptools.build_meta import build_sdist, build_wheel; "
        "import sys; destination = sys.argv[1]; build_sdist(destination); build_wheel(destination)",
        source,
        str(output),
    )
    return source, output


def expected_payload(source: Path) -> set[str]:
    paths = {
        path.relative_to(source).as_posix()
        for package in PACKAGES
        for path in (source / package).rglob("*.py")
        if not {"tests", "e2e"}.intersection(path.relative_to(source).parts)
    }
    for directory in ("src/web", "src/assets", "foxprofile_ext/assets"):
        paths.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return paths | {"foxprofile_mcp.py", "foxprofile_ext/compatibility.json"}


def test_archives_include_complete_runtime_only(distributions):
    source, output = distributions
    expected = expected_payload(source)
    assert len([name for name in expected if name.startswith("foxprofile_ext/assets/novnc/")]) == 62
    assert {
        "foxprofile_ext/assets/panel.js",
        "foxprofile_ext/assets/panel-manual.js",
        "foxprofile_ext/assets/panel.css",
    } <= expected
    with zipfile.ZipFile(next(output.glob("*.whl"))) as wheel:
        payload = {name for name in wheel.namelist() if ".dist-info/" not in name}
        assert payload == expected
        metadata_path = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        assert metadata["Requires-Python"] == ">=3.10"
        assert metadata.get_all("Requires-Dist", []) == []
        assert metadata.get_all("Provides-Extra", []) == []
        assert not any(name.endswith("entry_points.txt") for name in wheel.namelist())

    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        names = {
            Path(member.name).relative_to(archive.getnames()[0]).as_posix()
            for member in archive.getmembers()
            if member.isfile()
        }
    assert expected <= names
    allowed = expected | {
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "LICENSE",
        "PKG-INFO",
        "setup.cfg",
        "requirements.txt",
        "requirements-extension.txt",
    }
    assert {name for name in names if not name.startswith("foxprofile.egg-info/")} <= allowed


def test_sdist_rebuild_and_isolated_installed_resources(distributions, tmp_path):
    source, output = distributions
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        for member in archive.getmembers():
            if member.isfile():
                destination = tmp_path / "unpacked" / member.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read())
    unpacked = next((tmp_path / "unpacked").iterdir())
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    run_python(
        "from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])",
        unpacked,
        str(rebuilt),
    )
    with (
        zipfile.ZipFile(next(output.glob("*.whl"))) as direct,
        zipfile.ZipFile(next(rebuilt.glob("*.whl"))) as rebuilt_archive,
    ):
        assert {name: direct.read(name) for name in expected_payload(source)} == {
            name: rebuilt_archive.read(name) for name in expected_payload(source)
        }

    pytest.importorskip("pip")
    installed = tmp_path / "installed"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--target",
            str(installed),
            str(next(rebuilt.glob("*.whl"))),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    code = """
import importlib, importlib.util, json, pathlib, sys
sys.path.insert(0, sys.argv[1])
modules = [importlib.import_module(name) for name in ('src', 'chrome_engine', 'foxprofile_ext')]
for name in ('src.server', 'src.main', 'src.mcp_server.server', 'chrome_engine.runner',
             'foxprofile_ext.application', 'foxprofile_ext.manual.http', 'foxprofile_mcp'):
    assert importlib.util.find_spec(name), name
print(json.dumps({module.__name__: module.__file__ for module in modules}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(installed)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(Path(path).is_relative_to(installed) for path in json.loads(result.stdout).values())
    for name in expected_payload(source):
        assert (source / name).read_bytes() == (installed / name).read_bytes(), name
