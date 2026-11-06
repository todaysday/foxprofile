"""Offline checks for the extension's per-profile bubblewrap boundary."""

import os
import re
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxprofile_ext.runtime.confinement import (
    ConfinementPurpose,
    ConfinementSpec,
    ConfinementUnavailable,
    build_confined_command,
    build_confined_environment,
    probe_confinement,
)


def _spec(root: Path) -> ConfinementSpec:
    code_root = root / "application"
    for name in ("src", "chrome_engine", "foxprofile_ext"):
        code_dir = code_root / name
        code_dir.mkdir(parents=True)
        (code_dir / "marker.txt").write_text(name, encoding="utf-8")
    (code_root / ".env").write_text("FOXPROFILE_API_TOKEN=private-canary", encoding="utf-8")
    (code_root / "extension.sqlite3").write_text("sidecar-canary", encoding="utf-8")
    service_state = root / "service-state"
    service_state.mkdir()
    (service_state / "extension.sqlite3").write_text("sidecar-canary", encoding="utf-8")

    data_root = code_root / "camoufox_data"
    profile = data_root / "owned-profile"
    sibling = data_root / "sibling-profile"
    profile.mkdir(parents=True)
    sibling.mkdir()
    (profile / "cookies.sqlite").write_text("owned-profile-data", encoding="utf-8")
    (sibling / "cookies.sqlite").write_text("sibling-profile-canary", encoding="utf-8")
    (profile / "sibling-link").symlink_to(sibling, target_is_directory=True)

    private_tmp = code_root / ".runtime" / "tmp"
    private_tmp.mkdir(mode=0o700, parents=True)
    private_tmp.chmod(0o700)

    executable = Path(sys.executable)
    resolved_executable = executable.resolve()
    ldd = subprocess.run(("ldd", str(executable)), capture_output=True, text=True, check=False)
    if ldd.returncode != 0:
        pytest.skip("ldd could not identify the Python runtime libraries")
    libraries = {
        Path(match.group(1))
        for line in ldd.stdout.splitlines()
        if (match := re.search(r"(?:=>\s+|^\s*)(/\S+)", line)) is not None
        and Path(match.group(1)).exists()
    }
    python_paths = {
        Path(sysconfig.get_paths()[key])
        for key in ("stdlib", "platstdlib", "purelib", "platlib")
        if Path(sysconfig.get_paths()[key]).exists()
    }
    pyvenv_config = Path(sys.prefix) / "pyvenv.cfg"
    if pyvenv_config.exists():
        python_paths.add(pyvenv_config)
    runtime_paths = tuple(
        dict.fromkeys((*python_paths, executable, resolved_executable, *libraries))
    )
    return ConfinementSpec(profile, private_tmp, code_root, runtime_paths)


def test_child_environment_is_an_allowlist_and_never_inherits_service_secrets():
    environment = build_confined_environment(
        {
            "FOXPROFILE_API_TOKEN": "service-token-canary",
            "FOXPROFILE_SESSION": "personal-session-canary",
            "AWS_SECRET_ACCESS_KEY": "cloud-secret-canary",
            "PATH": "/private/bin",
            "HOME": "/private/home",
            "DISPLAY": ":101",
            "FOXPROFILE_HEADLESS": "virtual",
            "UNKNOWN_SETTING": "ignored",
        }
    )

    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["HOME"] == "/tmp"
    assert environment["TMPDIR"] == "/tmp"
    assert environment["PYTHONPATH"] == "/opt/foxprofile"
    assert environment["DISPLAY"] == ":101"
    assert environment["FOXPROFILE_HEADLESS"] == "virtual"
    assert "FOXPROFILE_API_TOKEN" not in environment
    assert "FOXPROFILE_SESSION" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "UNKNOWN_SETTING" not in environment


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_bubblewrap_missing_fails_closed_without_fallback(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(ConfinementUnavailable, match="bubblewrap executable is missing"):
        build_confined_command(spec, (sys.executable, "-S", "-c", "pass"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_bubblewrap_namespace_failure_is_an_actionable_fail_closed_error(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, b"", b"denied"),
    )

    with pytest.raises(ConfinementUnavailable, match="could not establish its required namespaces"):
        probe_confinement(spec, (sys.executable, "-S", "-c", "pass"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
@pytest.mark.parametrize(
    "runtime_path_kind", ["application-root", "working-directory", "application-state"]
)
def test_broad_application_mounts_are_rejected(tmp_path, runtime_path_kind):
    spec = _spec(tmp_path)
    if runtime_path_kind == "application-root":
        broad_path = spec.code_root
    elif runtime_path_kind == "working-directory":
        broad_path = Path.cwd()
    else:
        broad_path = spec.code_root / "state"
        broad_path.mkdir()
    broad_spec = replace(spec, runtime_paths=(broad_path,))

    with pytest.raises(ConfinementUnavailable, match="application|virtual environment"):
        build_confined_command(broad_spec, (sys.executable, "-S", "-c", "pass"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_exact_venv_path_may_be_mounted_read_only_from_application_root(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    venv = spec.code_root / ".venv"
    executable = venv / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("stand-in", encoding="utf-8")
    venv_spec = replace(spec, runtime_paths=(venv,))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/true")

    command, _ = build_confined_command(venv_spec, (str(executable), "-S", "-c", "pass"))

    assert str(venv) in command
    assert "/opt/foxprofile/.venv/bin/python" in command


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_root_owned_installed_code_root_is_accepted(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    original_lstat = Path.lstat

    def lstat_with_root_owned_code(path):
        info = original_lstat(path)
        if path == spec.code_root:
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(Path, "lstat", lstat_with_root_owned_code)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/true")

    command, _ = build_confined_command(spec, (sys.executable, "-S", "-c", "pass"))

    assert str(spec.code_root / "src") in command
    assert str(spec.code_root / "camoufox_data") not in command
    assert str(spec.code_root / ".runtime") not in command


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
@pytest.mark.parametrize("private_path", [Path("/proc"), Path("/run")])
def test_host_private_namespaces_cannot_be_readonly_runtime_mounts(tmp_path, private_path):
    spec = replace(_spec(tmp_path), runtime_paths=(private_path,))

    with pytest.raises(ConfinementUnavailable, match="private namespace or service path"):
        build_confined_command(spec, (sys.executable, "-S", "-c", "pass"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_bubblewrap_isolates_siblings_configuration_and_foreign_processes(tmp_path):
    spec = _spec(tmp_path)
    code = "\n".join(
        (
            "import os, pathlib, sys",
            "host_pid = int(sys.argv[1])",
            "assert os.environ['HOME'] == '/tmp'",
            "assert 'FOXPROFILE_API_TOKEN' not in os.environ",
            "assert 'FOXPROFILE_SESSION' not in os.environ",
            "assert 'AWS_SECRET_ACCESS_KEY' not in os.environ",
            "assert b'service-token-canary' not in pathlib.Path('/proc/self/environ').read_bytes()",
            "assert pathlib.Path('/opt/foxprofile/src/marker.txt').read_text() == 'src'",
            "assert pathlib.Path('/opt/foxprofile/chrome_engine/marker.txt').read_text() == 'chrome_engine'",
            "assert pathlib.Path('/opt/foxprofile/foxprofile_ext/marker.txt').read_text() == 'foxprofile_ext'",
            "assert not pathlib.Path('/opt/foxprofile/.env').exists()",
            "assert not pathlib.Path('/opt/foxprofile/extension.sqlite3').exists()",
            "assert not pathlib.Path('/service-state/extension.sqlite3').exists()",
            "assert not pathlib.Path('/profile/sibling-profile/cookies.sqlite').exists()",
            "assert not pathlib.Path('/profile/sibling-link/cookies.sqlite').exists()",
            "assert pathlib.Path('/profile/cookies.sqlite').read_text() == 'owned-profile-data'",
            "try: pathlib.Path(f'/proc/{host_pid}/environ').read_bytes()",
            "except (FileNotFoundError, PermissionError): pass",
            "else: raise AssertionError('foreign process environment is visible')",
            "pathlib.Path('/profile/written-by-sandbox').write_text('persistent')",
            "pathlib.Path('/tmp/written-by-sandbox').write_text('private')",
            "try: pathlib.Path('/opt/foxprofile/root-write').write_text('forbidden')",
            "except OSError: pass",
            "else: raise AssertionError('sandbox root is writable')",
        )
    )
    environment = {
        "FOXPROFILE_API_TOKEN": "service-token-canary",
        "FOXPROFILE_SESSION": "personal-session-canary",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret-canary",
    }

    foreign_process = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        env={**os.environ, "FOXPROFILE_FOREIGN_PROCESS_SECRET": "foreign-process-canary"},
    )
    try:
        try:
            probe_confinement(spec, (sys.executable, "-S", "-c", "pass"), timeout=10)
        except ConfinementUnavailable as error:
            pytest.skip(str(error))

        command, child_environment = build_confined_command(
            spec,
            (sys.executable, "-S", "-c", code, str(foreign_process.pid)),
            environment,
        )
        result = subprocess.run(
            command,
            env=child_environment,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        foreign_process.terminate()
        foreign_process.wait(timeout=5)

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert (spec.profile_path / "written-by-sandbox").read_text() == "persistent"
    assert (spec.private_tmp_path / "written-by-sandbox").read_text() == "private"
    assert not (spec.code_root / "root-write").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_vnc_helper_shares_run_tmp_but_never_mounts_profile_data(tmp_path, monkeypatch):
    spec = replace(_spec(tmp_path), purpose=ConfinementPurpose.VNC_HELPER)
    monkeypatch.setattr(shutil, "which", lambda name: sys.executable)
    command, environment = build_confined_command(
        spec,
        (sys.executable, "-S", "-c", "pass"),
        {"DISPLAY": ":101", "XAUTHORITY": "/tmp/Xauthority"},
    )

    assert "--unshare-net" not in command
    assert "--bind" in command
    assert str(spec.private_tmp_path) in command
    assert str(spec.profile_path) not in command
    assert "/profile" not in command
    assert environment["DISPLAY"] == ":101"
    assert environment["XAUTHORITY"] == "/tmp/Xauthority"


def test_trusted_browser_executable_path_needs_approved_runtime_mount(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/true")
    command, environment = build_confined_command(
        spec,
        (sys.executable, "-c", "pass"),
        {
            "FOXPROFILE_CAMOUFOX_EXECUTABLE": sys.executable,
            "FOXPROFILE_CHROME_EXECUTABLE": sys.executable,
        },
    )
    assert environment["FOXPROFILE_CAMOUFOX_EXECUTABLE"] == command[command.index("--") + 1]
    assert (
        environment["FOXPROFILE_CHROME_EXECUTABLE"] == environment["FOXPROFILE_CAMOUFOX_EXECUTABLE"]
    )
    assert "FOXPROFILE_CAMOUFOX_EXECUTABLE" not in build_confined_environment(
        {"FOXPROFILE_CAMOUFOX_EXECUTABLE": "/private/untrusted"}
    )
    with pytest.raises(ConfinementUnavailable):
        build_confined_command(
            spec,
            (sys.executable, "-c", "pass"),
            {"FOXPROFILE_CAMOUFOX_EXECUTABLE": str(spec.profile_path / "cookies.sqlite")},
        )


def test_resolver_exact_alias_is_preserved_but_arbitrary_private_alias_rejected(
    tmp_path, monkeypatch
):
    import foxprofile_ext.runtime.confinement as confinement

    spec = _spec(tmp_path)
    private = tmp_path / "private-run"
    private.mkdir()
    target = private / "resolver"
    target.write_text("nameserver 192.0.2.1\n")
    target.chmod(0o644)
    original = Path.resolve
    alias = tmp_path / "unrelated-alias"
    alias.symlink_to(target)

    def resolve(path, *args, **kwargs):
        return target if path == Path("/etc/resolv.conf") else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(
        confinement, "_PRIVATE_HOST_PATHS", (*confinement._PRIVATE_HOST_PATHS, private)
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/true")
    command, _ = build_confined_command(
        replace(spec, runtime_paths=(*spec.runtime_paths, Path("/etc/resolv.conf"))),
        (sys.executable, "-c", "pass"),
    )
    assert command[command.index("/etc/resolv.conf") - 1] == "--ro-bind"
    assert command[command.index("/etc/resolv.conf") + 1] == "/etc/resolv.conf"
    with pytest.raises(ConfinementUnavailable, match="private namespace"):
        build_confined_command(
            replace(spec, runtime_paths=(*spec.runtime_paths, alias)),
            (sys.executable, "-c", "pass"),
        )


def test_real_confined_venv_import_fixed_script_and_localhost_resolution(tmp_path):
    from foxprofile_ext.runtime.supervisor import _confined_arguments

    spec = _spec(tmp_path)
    resolvers = tuple(
        path
        for path in (Path("/etc/resolv.conf"), Path("/etc/hosts"), Path("/etc/nsswitch.conf"))
        if path.is_file()
    )
    spec = replace(spec, runtime_paths=(*spec.runtime_paths, *resolvers))
    script = spec.code_root / "src" / "venv_smoke.py"
    script.write_text(
        "import pathlib,pytest,socket,sys\n"
        "assert 'site-packages' in pytest.__file__\n"
        "assert socket.getaddrinfo('localhost',80)\n"
        "pathlib.Path('/profile/venv-script-ok').write_text(sys.prefix)\n"
    )
    command = _confined_arguments([sys.executable, str(script)], spec.code_root)
    try:
        argv, environment = build_confined_command(spec, command)
    except ConfinementUnavailable as error:
        pytest.skip(str(error))
    result = subprocess.run(argv, env=environment, capture_output=True, timeout=10)
    if result.returncode != 0 and b"No permissions" in result.stderr:
        pytest.skip(result.stderr.decode())
    assert result.returncode == 0, result.stderr.decode()
    assert (spec.profile_path / "venv-script-ok").read_text() == sys.prefix
