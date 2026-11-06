"""Actual supervisor/child IPC checks with a deterministic bwrap stand-in."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from foxprofile_ext.runtime.processes import RuntimeSupervisor
from foxprofile_ext.runtime.registry import current_host_id, process_identity
from foxprofile_ext.runtime.supervisor import _confined_arguments, _runtime_details
from foxprofile_ext.storage import CapacityLedger, ExtensionDatabase
from foxprofile_ext.storage.capacity import ProcessLiveness, RunStatus

ROOT = Path(__file__).resolve().parents[1]


def start_supervisor(tmp_path, code, *, cancelled=False, helper_stdin=None):
    database = ExtensionDatabase(tmp_path / "state.sqlite3")
    ledger = CapacityLedger(database)
    ref = ledger.reserve(("profile-a",), current_host_id()).newly_reserved[0]
    if cancelled:
        ledger.cancel_unclaimed(ref)
    profile = tmp_path / "data" / "alpha"
    profile.mkdir(parents=True)
    private_tmp = tmp_path / "data" / ".foxprofile-runtime" / ref.run_id
    private_tmp.mkdir(mode=0o700, parents=True)
    wrapper = tmp_path / "bwrap_standin.py"
    wrapper.write_text("import os,sys\na=sys.argv[sys.argv.index('--')+1:]\nos.execv(a[0],a)\n")
    config = dict(
        database_path=str(database.path),
        host_id=current_host_id(),
        profile_id=ref.profile_id,
        run_id=ref.run_id,
        profile_path=str(profile),
        private_tmp_path=str(private_tmp),
        code_root=str(ROOT),
        runtime_paths=[str(Path(sys.prefix))],
        settings={},
        command=[sys.executable, "-c", code],
        helper_stdin=helper_stdin,
    )
    bootstrap = "\n".join(
        (
            "import importlib,sys,os",
            "m=importlib.import_module('foxprofile_ext.runtime.supervisor')",
            f"wrapper={str(wrapper)!r}",
            "def build(spec,command,settings):",
            " return [sys.executable,wrapper,'--bind',str(spec.profile_path),'/profile','--',*command],os.environ.copy()",
            "m.build_confined_command=build",
            "m._verified_namespace=lambda boundary,ns: True",
            "m._runtime_liveness=lambda boundary,ns: m._process_group_liveness(boundary)",
            "sys.exit(m.supervise(sys.stdin,sys.stdout))",
        )
    )
    child = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    child.stdin.write(json.dumps(config))
    child.stdin.close()
    child.stdin = None
    return database, ledger, ref, child, private_tmp


def line(process):
    results = queue.Queue()
    threading.Thread(target=lambda: results.put(process.stdout.readline()), daemon=True).start()
    try:
        result = results.get(timeout=5)
    except queue.Empty:
        pytest.fail("supervisor protocol timed out")
    assert result, process.stderr.read() if process.stderr is not None else "supervisor exited"
    return result.rstrip()


def wait_status(ledger, ref, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ledger.get(ref).status is expected:
            return
        time.sleep(0.02)
    assert ledger.get(ref).status is expected


def stop_owned(process, runtime=None, ref=None):
    if runtime is not None and ref is not None:
        runtime.request_stop(ref.profile_id, ref.run_id, 2)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=8)


def test_durable_claim_precedes_controlled_child_effect_and_private_input(tmp_path):
    marker = tmp_path / "effect"
    # Child verifies durable parent registration before its first observable effect.
    code = "\n".join(
        (
            "import sqlite3,os,pathlib,sys,json",
            f"db=sqlite3.connect({str(tmp_path / 'state.sqlite3')!r})",
            "r=db.execute('SELECT status,pid FROM browser_runs').fetchone()",
            "d=db.execute('SELECT supervisor_pid FROM runtime_descriptors').fetchone()",
            "assert r[0]=='starting' and r[1]==d[0] and r[1]!=os.getpid()",
            f"pathlib.Path({str(marker)!r}).write_text(sys.stdin.read())",
            "print('RESULT:'+json.dumps({'ok':True}),flush=True)",
        )
    )
    _, ledger, ref, process, private_tmp = start_supervisor(
        tmp_path, code, helper_stdin="cookie-input-canary"
    )
    output, error = process.communicate(timeout=10)
    assert process.returncode == 0, error
    assert marker.read_text() == "cookie-input-canary"
    assert output.strip() == 'RESULT:{"ok": true}'
    assert ledger.get(ref).status is RunStatus.CLOSED
    assert not private_tmp.exists()


def test_late_cancelled_supervisor_creates_no_browser_effect(tmp_path):
    marker = tmp_path / "effect"
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('spawned')"
    _, ledger, ref, process, _ = start_supervisor(tmp_path, code, cancelled=True)
    process.communicate(timeout=10)
    assert process.returncode == 3
    assert not marker.exists()
    assert ledger.get(ref).status is RunStatus.CANCELLED


def test_parent_stdout_loss_retains_browser_and_restart_adopts_private_control(tmp_path):
    code = "\n".join(
        (
            "import http.server,json,threading,time",
            "class Handler(http.server.BaseHTTPRequestHandler):",
            " def do_POST(self):",
            "  assert self.headers['X-FoxProfile-Control-Token']=='private-control-canary'",
            '  self.send_response(200); self.end_headers(); self.wfile.write(b\'{"result":"same-run"}\')',
            " def log_message(self,*args): pass",
            "server=http.server.HTTPServer(('127.0.0.1',0),Handler)",
            "threading.Thread(target=server.serve_forever,daemon=True).start()",
            "print('RUNTIME:'+json.dumps({'display':None,'namespace':__import__('os').readlink('/proc/self/ns/pid'),'tmp_path':'/tmp','xauthority_path':None}),flush=True)",
            "print(f'CONTROL:{server.server_port}:private-control-canary',flush=True)",
            "print('BROWSER_STARTED',flush=True)",
            "while True: print('heartbeat',flush=True); time.sleep(.05)",
        )
    )
    database, ledger, ref, process, _ = start_supervisor(tmp_path, code)
    runtime = RuntimeSupervisor(
        database, data_dir=tmp_path / "data", code_root=ROOT, runtime_paths=(Path(sys.prefix),)
    )
    try:
        assert line(process).startswith("CONTROL:")
        # Close the manager pipe while supervisor still receives child output.
        process.stdout.close()
        wait_status(ledger, ref, RunStatus.RUNNING)
        time.sleep(0.15)
        assert process.poll() is None
        report = runtime.recover()
        assert report.confirmed_live == 1 and report.uncertain == 0
        assert runtime.control(ref.profile_id, ref.run_id, "probe") == "same-run"
        assert len(ledger.active_runs()) == 1
    finally:
        stop_owned(process, runtime, ref)
    assert ledger.get(ref).status is RunStatus.CLOSED


def test_child_startup_failure_releases_only_after_supervisor_cleanup(tmp_path):
    _, ledger, ref, process, _ = start_supervisor(tmp_path, "raise SystemExit(17)")
    process.communicate(timeout=10)
    assert process.returncode == 17
    assert ledger.get(ref).status is RunStatus.CLOSED


def test_stale_callbacks_never_release_new_generation(tmp_path):
    database, ledger, old, process, _ = start_supervisor(tmp_path, "pass")
    process.communicate(timeout=10)
    old_identity = ledger.get(old).process
    new = ledger.reserve((old.profile_id,), current_host_id()).newly_reserved[0]
    assert not ledger.release_after_exit(old, old_identity, lambda *args: ProcessLiveness.DEAD)
    assert not ledger.mark_running(old)
    assert ledger.get(new).status is RunStatus.RESERVED
    assert len(ledger.active_runs()) == 1


def test_sigkill_supervisor_keeps_live_browser_counted(tmp_path):
    database, ledger, ref, process, _ = start_supervisor(
        tmp_path, "import time; print('child-live',flush=True); time.sleep(30)"
    )
    runtime = RuntimeSupervisor(
        database, data_dir=tmp_path / "data", code_root=ROOT, runtime_paths=(Path(sys.prefix),)
    )
    browser = None
    try:
        assert line(process) == "child-live"
        browser = runtime.registry.get(ref).descriptor.boundary
        process.kill()
        process.wait(timeout=5)
        assert runtime.recover().confirmed_live == 1
        assert len(ledger.active_runs()) == 1
        assert process_identity(browser.pid) == browser
    finally:
        if browser is not None and process_identity(browser.pid) == browser:
            os.killpg(browser.pid, signal.SIGKILL)
        stop_owned(process)


def test_fixed_host_script_is_remapped_without_mutating_proxy_arguments():
    command = [
        sys.executable,
        str(ROOT / "src/services/browser/runner.py"),
        "alpha",
        "/tmp/private-proxy",
        "linux",
    ]
    result = _confined_arguments(command, ROOT)
    assert result[1] == "/opt/foxprofile/src/services/browser/runner.py"
    assert result[3] == "/tmp/private-proxy"


@pytest.mark.parametrize("path", ["/tmp/../outside", "/outside", "/tmp/a/../../outside"])
def test_runtime_xauthority_cannot_escape_private_run(tmp_path, path):
    with pytest.raises(ValueError):
        _runtime_details(
            json.dumps(dict(display=":101", tmp_path="/tmp", xauthority_path=path)), tmp_path
        )


def test_real_bwrap_boundary_browser_pid_mapping_and_detached_descendant_cleanup(tmp_path):
    from src.models.profile import Profile

    database = ExtensionDatabase(tmp_path / "actual.sqlite3")
    data = tmp_path / "data"
    profile = Profile("actual", os_type="linux")
    (data / profile.name).mkdir(parents=True)
    paths = tuple(
        path
        for path in (
            Path(sys.prefix),
            Path(sys.base_prefix),
            Path("/usr"),
            Path("/lib"),
            Path("/lib64"),
        )
        if path.exists()
    )
    runtime = RuntimeSupervisor(
        database,
        data_dir=data,
        code_root=ROOT,
        runtime_paths=paths,
        settings={"FOXPROFILE_CAMOUFOX_EXECUTABLE": sys.executable},
    )
    ref = runtime.ledger.reserve((profile.profile_id,), runtime.host_id).newly_reserved[0]
    code = "\n".join(
        (
            "import subprocess,sys,os,json,time,pathlib",
            "engine=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)",
            "stat=pathlib.Path(f'/proc/{engine.pid}/stat').read_text(); fields=stat[stat.rfind(\")\")+2:].split()",
            "print('RUNTIME:'+json.dumps({'display':None,'xauthority_path':None,'tmp_path':'/tmp','namespace':os.readlink('/proc/self/ns/pid'),'browser_process':{'pid':engine.pid,'start_ticks':int(fields[19])}}),flush=True)",
            "print('CONTROL:9001:private-control-canary',flush=True)",
            "print('BROWSER_STARTED',flush=True)",
            "time.sleep(30)",
        )
    )
    process = runtime._spawn(profile, profile.profile_id, ref.run_id, [sys.executable, "-c", code])
    browser = boundary = None
    try:
        assert line(process).startswith("CONTROL:")
        assert line(process) == "BROWSER_STARTED"
        descriptor = runtime.registry.get(ref).descriptor
        browser, boundary = descriptor.browser, descriptor.boundary
        assert browser is not None and boundary is not None and browser != boundary
        assert os.readlink(f"/proc/{browser.pid}/ns/pid") == descriptor.namespace
        assert Path(f"/proc/{browser.pid}").stat().st_uid == os.getuid()
        assert process_identity(browser.pid) == browser
        assert os.getpgid(browser.pid) != boundary.pid
        assert runtime.request_stop(ref.profile_id, ref.run_id, 5)
        wait_status(runtime.ledger, ref, RunStatus.CLOSED)
        assert not Path(descriptor.tmp_path).exists()
    finally:
        runtime.shutdown(5)
        stop_owned(process)


def test_private_gate_eof_never_executes_tool(tmp_path):
    marker = tmp_path / "gate-effect"
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('unsafe')"
    result = subprocess.run(
        [sys.executable, "-m", "foxprofile_ext.runtime.child_gate", "--", "-c", code],
        input="",
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=5,
    )
    assert result.returncode == 3
    assert result.stdout.startswith("BOUNDARY:pid:[")
    assert not marker.exists()


def test_browser_mapping_rejects_wrong_executable_or_generation(tmp_path):
    from foxprofile_ext.runtime.supervisor import _map_browser_process

    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        boundary = process_identity(os.getpid())
        identity = process_identity(child.pid)
        namespace = os.readlink(f"/proc/{child.pid}/ns/pid")
        value = {"pid": child.pid, "start_ticks": identity.start_ticks}
        # In this test's namespace NSpid may include an outer host hierarchy;
        # the final component remains the child's locally visible PID.
        assert _map_browser_process(boundary, namespace, value, sys.executable) == identity
        assert _map_browser_process(boundary, namespace, value, "/bin/false") is None
        assert (
            _map_browser_process(
                boundary,
                namespace,
                {**value, "start_ticks": identity.start_ticks + 1},
                sys.executable,
            )
            is None
        )
    finally:
        child.terminate()
        child.wait(timeout=5)
