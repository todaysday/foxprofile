from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import types

import pytest

from foxprofile_ext.dto import PrincipalDTO, PrincipalKind
from foxprofile_ext.errors import DomainError, ErrorCode
from foxprofile_ext.request_context import principal_scope
from src.models.profile import Profile
from src.services.browser import cookie_tool, cookies, fingerprint, process, runner
from src.services.browser import launcher as launcher_mod
from src.services.browser.launcher import BrowserLauncher
from src.services.browser.process import helper_command


class FakeProc:
    def __init__(self, lines: str = "") -> None:
        self.pid = 1234
        self.stdout = io.StringIO(lines)

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class FakeAdapter:
    def __init__(self, *procs: FakeProc) -> None:
        self.procs = list(procs)
        self.spawned = []
        self.stops = []
        self.helpers = []
        self.controls = []

    def spawn_browser(self, profile, profile_id, run_id):
        self.spawned.append((profile_id, run_id))
        return self.procs.pop(0)

    def request_stop(self, profile_id, run_id, timeout):
        self.stops.append((profile_id, run_id, timeout))
        return True

    def shutdown(self, timeout):
        return None

    def control(self, profile_id, run_id, action, params, timeout):
        self.controls.append((profile_id, run_id, action, params, timeout))
        return {"ok": True}

    def run_helper(self, action, profile_name, os_type, engine, stdin, timeout):
        self.helpers.append((action, profile_name, os_type, engine, stdin, timeout))
        return {"cookies": []}


def test_protected_launch_without_adapter_or_admitted_ids_never_spawns(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    calls = []
    monkeypatch.setattr(launcher_mod, "spawn_browser", lambda profile: calls.append(profile))
    launcher = BrowserLauncher()

    with pytest.raises(RuntimeError, match="admitted run"):
        launcher.start_thread(Profile(name="p"), lambda _message: None)

    with pytest.raises(RuntimeError, match="runtime adapter"), launcher.exclusive("p"):
        pass
    assert calls == []

    adapter = FakeAdapter()
    admitted = BrowserLauncher(adapter)
    for identifiers in ({}, {"profile_id": "id-p"}, {"run_id": "run-1"}):
        with pytest.raises(RuntimeError, match="admitted run"):
            admitted.start_thread(Profile(name="p"), lambda _: None, **identifiers)
    assert adapter.spawned == []


def test_direct_spawn_cannot_bypass_protected_admission(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    calls = []
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: calls.append(a))

    with pytest.raises(RuntimeError, match="runtime adapter"):
        process.spawn_browser(Profile(name="p"))
    assert calls == []


def test_protected_cookie_helper_requires_principal_and_exclusive_context(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    subprocess_calls = []
    monkeypatch.setattr(cookies.subprocess, "run", lambda *a, **k: subprocess_calls.append(a))
    principal = PrincipalDTO("service-test", PrincipalKind.SERVICE)

    with pytest.raises(DomainError) as missing_principal:
        cookies._call_tool("export", "p", "linux")
    assert missing_principal.value.code is ErrorCode.AUTH_REQUIRED
    with principal_scope(principal), pytest.raises(cookies.CookieError, match="exclusive"):
        cookies._call_tool("export", "p", "linux")

    adapter = FakeAdapter()
    launcher = BrowserLauncher(adapter)
    with principal_scope(principal), launcher.exclusive("p"):
        assert cookies._call_tool("export", "p", "linux") == {"cookies": []}

    assert len(adapter.helpers) == 1
    assert subprocess_calls == []
    with principal_scope(principal), pytest.raises(cookies.CookieError, match="exclusive"):
        cookies._call_tool("export", "p", "linux")
    with pytest.raises(DomainError) as reset_principal:
        cookies._call_tool("export", "p", "linux")
    assert reset_principal.value.code is ErrorCode.AUTH_REQUIRED


def test_exclusive_context_cannot_launch_a_different_profile_helper(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    adapter = FakeAdapter()
    launcher = BrowserLauncher(adapter)
    principal = PrincipalDTO("service-test", PrincipalKind.SERVICE)

    with (
        principal_scope(principal),
        launcher.exclusive("p"),
        pytest.raises(cookies.CookieError, match="exclusive"),
    ):
        cookies._call_tool("export", "sibling", "linux")
    assert adapter.helpers == []


def test_protected_stop_is_bound_to_exact_run_and_stale_exit_keeps_replacement(
    monkeypatch,
):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    adapter = FakeAdapter(FakeProc(), FakeProc())
    callbacks = []
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda p, n, cb, **kw: callbacks.append(cb))
    monkeypatch.setattr(BrowserLauncher, "_monitor_process", lambda *args: None)
    launcher = BrowserLauncher(adapter)

    launcher.start_thread(
        Profile(name="p"), lambda _message: None, profile_id="id-p", run_id="run-1"
    )
    assert launcher.stop_profile("p", timeout=4)
    launcher.start_thread(
        Profile(name="p"), lambda _message: None, profile_id="id-p", run_id="run-2"
    )
    callbacks[0]()

    assert adapter.stops == [("id-p", "run-1", 4)]
    assert launcher._run_bindings["p"] == ("id-p", "run-2")
    assert launcher.is_running("p")
    launcher._active_sessions.clear()


def test_private_runtime_and_control_lines_are_not_logged(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    adapter = FakeAdapter()
    launcher = BrowserLauncher(adapter)
    logs = []
    ready = []
    result = launcher_mod._LaunchResult()
    proc = FakeProc(
        'RUNTIME:{"display":":91","xauthority_path":"/private/auth-secret",'
        '"tmp_path":"/private/run-secret","namespace":null}\n'
        "CONTROL:9001:secret-control-token\nBROWSER_STARTED\n"
    )

    launcher._monitor_process(
        proc, "p", logs.append, lambda: ready.append(True), lambda: None, result
    )

    assert result.ok
    assert ready == [True]
    assert launcher._controls == {}
    assert all(
        secret not in "\n".join(logs)
        for secret in ("auth-secret", "run-secret", "secret-control-token")
    )


@pytest.mark.parametrize("protocol", ["BROWSER_CLOSED", "LAUNCH_FAILED: private-detail"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_protected_protocol_stop_never_uses_host_pid_cleanup(monkeypatch, protocol, confirmed):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    adapter = FakeAdapter()
    stops = []
    adapter.request_stop = lambda *args: stops.append(args) or confirmed
    launcher = BrowserLauncher(adapter)
    monkeypatch.setattr(launcher_mod, "terminate", lambda *a, **k: pytest.fail("raw terminate"))
    monkeypatch.setattr(process.display, "stop_for_runner", lambda *a: pytest.fail("host PID"))
    callbacks = []
    logs = []

    launcher._monitor_process(
        FakeProc(protocol + "\n"),
        "p",
        logs.append,
        None,
        lambda: callbacks.append(True),
        launcher_mod._LaunchResult(),
        ("id-p", "run-old"),
    )

    assert stops == [("id-p", "run-old", 1.0)]
    assert callbacks == ([True] if confirmed else [])
    assert "private-detail" not in "\n".join(logs)


def test_runner_emits_private_runtime_line_only_in_protected_mode(monkeypatch, capsys):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("DISPLAY", ":91")
    monkeypatch.setenv("XAUTHORITY", "/tmp/Xauthority")
    monkeypatch.setattr(runner.os, "readlink", lambda path: "pid:[12345]")

    runner._emit_runtime_metadata(None)

    line = capsys.readouterr().out.strip()
    assert line.startswith("RUNTIME:")
    assert json.loads(line.removeprefix("RUNTIME:")) == {
        "display": ":91",
        "xauthority_path": "/tmp/Xauthority",
        "tmp_path": "/tmp",
        "namespace": "pid:[12345]",
    }


@pytest.mark.parametrize("headless", [False, True, "virtual"])
def test_cookie_helper_uses_configured_camoufox_headless_and_humanize(
    monkeypatch, protected_executables, headless
):
    seen = {}

    class FakeCamoufox:
        def __init__(self, **options):
            seen.update(options)

    monkeypatch.setattr(cookie_tool, "AsyncCamoufox", FakeCamoufox)
    monkeypatch.setattr(cookie_tool, "HEADLESS", headless)
    monkeypatch.setattr(cookie_tool, "load_or_create", lambda *_args: {"stable": True})

    cookie_tool._open("/profile/p", "linux", "camoufox")

    assert seen["headless"] == headless
    assert seen["humanize"] is True
    assert seen["fingerprint"] == {"stable": True}


@pytest.mark.parametrize("headless", [False, True, "virtual"])
@pytest.mark.parametrize(
    "protected,engine", [(False, "camoufox"), (False, "chrome"), (True, "chrome")]
)
def test_helper_preserves_baseline_options_outside_protected_camoufox(
    monkeypatch, protected_executables, headless, engine, protected
):
    seen = {}

    class Engine:
        def __init__(self, *args, **options):
            seen.update(options)

    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", str(protected).lower())
    monkeypatch.setattr(cookie_tool, "HEADLESS", headless)
    monkeypatch.setattr(cookie_tool, "AsyncCamoufox", Engine)
    monkeypatch.setattr("chrome_engine.ChromeEngine", Engine)
    monkeypatch.setattr(cookie_tool, "load_or_create", lambda *_: {"stable": True})
    cookie_tool._open("/profile/p", "linux", engine)
    assert seen["headless"] is True
    assert "humanize" not in seen


def test_helper_command_is_fixed_to_cookie_tool_actions():
    command = helper_command("import", "profile-a", "linux", "camoufox")

    assert command[1].endswith("/src/services/browser/cookie_tool.py")
    assert command[2:] == ["import", "profile-a", "linux", "camoufox"]
    with pytest.raises(ValueError, match="unknown cookie helper action"):
        helper_command("shell", "profile-a", "linux", "camoufox")


def test_protected_browser_control_uses_exact_adapter_binding(monkeypatch):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    adapter = FakeAdapter(FakeProc())
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda *a, **k: None)
    monkeypatch.setattr(BrowserLauncher, "_monitor_process", lambda *args: None)
    launcher = BrowserLauncher(adapter)
    launcher.start_thread(
        Profile(name="p"), lambda _message: None, profile_id="id-p", run_id="run-9"
    )

    assert launcher.control("p", "navigate", {"url": "https://example.test"}) == {"ok": True}
    assert adapter.controls[0][:3] == ("id-p", "run-9", "navigate")
    launcher._active_sessions.clear()


@pytest.fixture
def protected_executables(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    executable = tmp_path / "installed-browser"
    executable.write_text("stand-in, never executed", encoding="utf-8")
    executable.chmod(0o700)
    for engine in ("CAMOUFOX", "CHROME"):
        monkeypatch.setenv(f"FOXPROFILE_{engine}_EXECUTABLE", str(executable))
    return str(executable)


@pytest.mark.parametrize("engine", ["camoufox", "chrome"])
def test_protected_runtime_never_downloads_a_missing_browser(monkeypatch, engine):
    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.delenv(f"FOXPROFILE_{engine.upper()}_EXECUTABLE", raising=False)
    with pytest.raises(RuntimeError, match="installed runtime executable"):
        runner.runtime_executable(engine)


def test_new_linux_native_baseline_and_saved_geometry_stay_distinct(
    monkeypatch, tmp_path, protected_executables
):
    import camoufox.fingerprints as native

    generated = {
        "screen": {"width": 1366, "height": 768},
        "window": {"screenX": 0, "screenY": 0, "outerWidth": 1300, "outerHeight": 740},
    }
    calls = []
    monkeypatch.setattr(native, "generate_fingerprint", lambda **kw: calls.append(kw) or generated)

    first = fingerprint.load_or_create(str(tmp_path / "new"), "linux")
    assert first == generated
    assert calls[0]["os"] == "linux"
    screen = calls[0]["screen"]
    assert (screen.min_width, screen.max_width, screen.min_height, screen.max_height) == (
        1366,
        1366,
        768,
        768,
    )

    saved = tmp_path / "saved"
    saved.mkdir()
    old = {"screen": {"width": 1920, "height": 1080}, "window": {"outerWidth": 1800}}
    document = json.dumps({"os": "windows", "fingerprint": old})
    (saved / "fingerprint.json").write_text(document, encoding="utf-8")
    assert fingerprint.load_or_create(str(saved), "windows") == old
    assert (saved / "fingerprint.json").read_text(encoding="utf-8") == document
    assert len(calls) == 1


def _fake_display(monkeypatch):
    displays = []
    records = []

    class FakeDisplay:
        def __init__(self, **options):
            self.options = options
            self.proc = types.SimpleNamespace(pid=42)
            self.authority_path = "/tmp/private-Xauthority"
            self.killed = 0
            self.authorized = []
            displays.append(self)

        def get(self):
            return ":91"

        def authorize(self, display):
            self.authorized.append(display)

        def kill(self):
            self.killed += 1

    monkeypatch.setattr(runner, "_PrivateVirtualDisplay", FakeDisplay)
    monkeypatch.setattr(runner.xvfb, "sweep_stale", lambda: None)
    monkeypatch.setattr(runner.xvfb, "record", lambda *args: records.append(args))
    monkeypatch.setattr(runner.xvfb, "clear", lambda: None)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(
        runner, "browser_process_metadata", lambda *_: {"pid": 43, "start_ticks": 7}
    )
    monkeypatch.setattr(
        cookie_tool, "browser_process_metadata", lambda *_: {"pid": 43, "start_ticks": 7}
    )
    return displays, records


def test_saved_geometry_controls_framebuffer_without_rewriting_fingerprint(
    monkeypatch, protected_executables
):
    displays, records = _fake_display(monkeypatch)
    old = {
        "screen": {"width": 1920, "height": 1080, "devicePixelRatio": 1.5},
        "window": {"screenX": 80, "screenY": 40, "outerWidth": 1900, "outerHeight": 1060},
    }
    original = copy.deepcopy(old)
    options = {"headless": "virtual", "fingerprint": old}

    display = runner.start_virtual_display(options)

    assert display is displays[0]
    assert display.options == {"screen": "2970x1650x24"}
    assert display.authorized == [":91"]
    assert os.environ["XAUTHORITY"] == display.authority_path
    assert options["headless"] is False
    assert options["virtual_display"] == ":91"
    assert old == original
    assert records == [(42, ":91")]


def test_runner_private_metadata_precedes_control_and_ready_with_one_display(
    monkeypatch, tmp_path, protected_executables, capsys
):
    from foxprofile_ext.runtime import cache

    monkeypatch.setattr(cache, "require_geoip_cache", lambda: None)
    displays, _ = _fake_display(monkeypatch)
    options = {}

    class Context:
        pages = [types.SimpleNamespace(on=lambda *args: None)]

        def on(self, event, callback):
            if event == "close":
                callback()

    class Camoufox:
        def __init__(self, **kwargs):
            options.update(kwargs)

        async def __aenter__(self):
            return Context()

        async def __aexit__(self, *_args):
            return None

    async def serve(context):
        async def cleanup():
            return None

        return types.SimpleNamespace(cleanup=cleanup), 9001, "private-control-token"

    monkeypatch.setattr(runner, "AsyncCamoufox", Camoufox)
    monkeypatch.setattr(runner, "HEADLESS", "virtual")
    monkeypatch.setattr(runner, "RESTORE_TABS", False)
    monkeypatch.setattr(runner, "_shutdown", asyncio.Event())
    monkeypatch.setattr(runner, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        runner, "load_or_create", lambda *_: {"screen": {"width": 1366, "height": 768}}
    )
    monkeypatch.setattr(runner, "exit_ip", lambda *_: "203.0.113.8")
    monkeypatch.setattr(runner, "pin_geoip_locale", lambda *_: None)
    monkeypatch.setattr(runner.control, "serve", serve)

    assert asyncio.run(runner.run_browser("p", "None", "linux", "UTC", "en-US")) == 0

    lines = capsys.readouterr().out.splitlines()
    runtime_index = next(i for i, line in enumerate(lines) if line.startswith("RUNTIME:"))
    control_index = next(i for i, line in enumerate(lines) if line.startswith("CONTROL:"))
    assert runtime_index < control_index < lines.index("BROWSER_STARTED")
    runtime_lines = [
        json.loads(line.removeprefix("RUNTIME:")) for line in lines if line.startswith("RUNTIME:")
    ]
    assert "browser_process" not in runtime_lines[0]
    assert runtime_lines[1]["browser_process"] == {"pid": 43, "start_ticks": 7}
    assert lines.index(next(line for line in lines if '"browser_process"' in line)) < control_index
    assert options["executable_path"] == protected_executables
    assert options["headless"] is False and options["humanize"] is True
    assert options["os"] == "linux" and options["locale"] == "en-US"
    assert options["config"] == {"timezone": "UTC"}
    assert len(displays) == 1 and displays[0].killed == 1


def test_helper_virtual_runtime_uses_one_owned_display_and_keeps_payload_private(
    monkeypatch, protected_executables, capsys
):
    displays, _ = _fake_display(monkeypatch)
    options = {}
    imported = []

    class Camoufox:
        def __init__(self, **kwargs):
            options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def add_cookies(self, payload):
            imported.extend(payload)

    monkeypatch.setattr(cookie_tool, "AsyncCamoufox", Camoufox)
    monkeypatch.setattr(cookie_tool, "HEADLESS", "virtual")
    monkeypatch.setattr(cookie_tool, "load_or_create", lambda *_: {"stable": True})
    monkeypatch.setattr(cookie_tool.sys, "argv", ["cookie_tool.py", "import", "p", "linux"])
    monkeypatch.setattr(cookie_tool.sys, "stdin", io.StringIO('[{"value":"private-cookie"}]'))

    assert cookie_tool.main() == 0

    output = capsys.readouterr().out
    assert output.index("RUNTIME:") < output.index("RESULT:")
    assert "private-cookie" not in output
    assert imported == [{"value": "private-cookie"}]
    assert options["headless"] is False and options["virtual_display"] == ":91"
    assert options["humanize"] is True
    assert options["executable_path"] == protected_executables
    assert len(displays) == 1 and displays[0].killed == 1


def test_chrome_uses_trusted_executable_without_cache_download(monkeypatch, protected_executables):
    from chrome_engine import runner as chrome_runner

    monkeypatch.setattr(
        chrome_runner.fetch, "installed_executable", lambda: pytest.fail("cache lookup")
    )
    monkeypatch.setattr(chrome_runner.fetch, "install", lambda **kw: pytest.fail("download"))
    asyncio.run(chrome_runner.ensure_installed())
    engine = cookie_tool._open("/profile/p", "linux", "chrome")
    assert engine._executable == protected_executables


def test_actual_browser_metadata_selects_root_engine_process_and_rejects_mismatch(monkeypatch):
    from textwrap import dedent

    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "true")
    monkeypatch.setenv("FOXPROFILE_CAMOUFOX_EXECUTABLE", sys.executable)
    browser_script = dedent("""
        import subprocess
        import sys

        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
        )
        try:
            print("ready", flush=True)
            sys.stdin.read()
        finally:
            child.communicate(timeout=5)
            assert child.returncode == 0
    """)
    collector_script = dedent("""
        import os
        import subprocess
        import sys

        import pytest

        from src.services.browser import runner

        child = subprocess.Popen(
            [sys.executable, "-c", sys.argv[1]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout.readline().strip() == "ready"
            metadata = runner.browser_process_metadata("camoufox")
            assert metadata["pid"] == child.pid
            assert metadata["start_ticks"] > 0
            os.environ["FOXPROFILE_CAMOUFOX_EXECUTABLE"] = "/bin/true"
            with pytest.raises(RuntimeError, match="process could not be verified"):
                runner.browser_process_metadata("camoufox")
        finally:
            child.communicate(timeout=5)
            assert child.returncode == 0
    """)
    result = subprocess.run(
        [sys.executable, "-c", collector_script, browser_script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_private_x_cookie_uses_stdin_and_never_argv_or_logs(monkeypatch, capsys):
    calls = []
    canary = "12" * 16
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _: canary)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda args, **options: (
            calls.append((args, options)) or types.SimpleNamespace(returncode=0)
        ),
    )
    display = runner._PrivateVirtualDisplay(screen="1366x768x24")
    directory = os.path.dirname(display.authority_path)
    try:
        display.authorize(":91")
        assert (os.stat(directory).st_mode & 0o777) == 0o700
        assert all(canary not in " ".join(args) for args, _options in calls)
        assert all(options["input"].endswith(canary + "\n") for _args, options in calls)
        assert canary not in capsys.readouterr().out
    finally:
        display.kill()
    assert not os.path.exists(directory)


@pytest.mark.skipif(
    os.getenv("FOXPROFILE_RUN_NATIVE_X_TESTS") != "1",
    reason="explicit offline native-X harness only; no browser is launched",
)
def test_native_xauthority_denies_missing_wrong_and_sibling_cookies(tmp_path):
    for executable in ("Xvfb", "xauth", "xdpyinfo", "strace"):
        assert shutil.which(executable), f"offline native-X harness requires {executable}"
    displays = []
    processes = []
    names = []
    try:
        for _ in range(2):
            display = runner._PrivateVirtualDisplay(screen="1366x768x24")
            displays.append(display)
            display_name = display.get()
            names.append(display_name)
            display.authorize(display_name)
            processes.append(display.proc)
            assert "-ac" not in display.xvfb_args
            assert (os.stat(display.authority_path).st_mode & 0o777) == 0o600

        selected, sibling = displays
        display_name = names[0]
        environment = dict(os.environ, XAUTHORITY=selected.authority_path)
        own = subprocess.run(
            ["xdpyinfo", "-display", display_name],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert own.returncode == 0
        assert "1366x768 pixels" in own.stdout

        empty = tmp_path / "empty-Xauthority"
        empty.touch(mode=0o600)
        wrong = tmp_path / "wrong-Xauthority"
        wrong.touch(mode=0o600)
        subprocess.run(
            ["xauth", "-f", str(wrong), "add", display_name, "MIT-MAGIC-COOKIE-1", "00" * 16],
            capture_output=True,
            timeout=5,
            check=True,
        )
        for label, authority in (
            ("missing", empty),
            ("wrong", wrong),
            ("sibling", sibling.authority_path),
        ):
            trace = tmp_path / f"{label}-connect.trace"
            denied = subprocess.run(
                [
                    "strace",
                    "-qq",
                    "-e",
                    "connect",
                    "-o",
                    str(trace),
                    "xdpyinfo",
                    "-display",
                    display_name,
                ],
                env=dict(os.environ, XAUTHORITY=str(authority)),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert denied.returncode != 0, f"{label} cookie accessed foreign X server"
            # Both servers share the host network namespace. Prove denial
            # after connecting the abstract socket, not filesystem invisibility.
            assert 'sun_path=@"/tmp/.X11-unix/X' in trace.read_text()
    finally:
        for display in reversed(displays):
            display.kill()
        assert all(process.poll() is not None for process in processes)
        assert all(not os.path.exists(display.authority_path) for display in displays)
