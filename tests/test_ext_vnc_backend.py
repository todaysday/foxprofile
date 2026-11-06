"""Private adapter contracts plus opt-in real Xvfb/x11vnc process tests.

Set FOXPROFILE_RUN_NATIVE_X_TESTS=1, make the native binaries available on PATH,
and optionally set FOXPROFILE_NATIVE_LIBRARY_PATH for a locally extracted build.
This harness launches no browser and reserves no profile capacity.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import ctypes.util
import hashlib
import json
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxprofile_ext.dto import BrowserRunRefDTO, ProcessIdentity, RuntimeDescriptor
from foxprofile_ext.manual import helper as helper_module
from foxprofile_ext.manual.backend import PrivateVNCBackend, VNCBackendUnavailable
from foxprofile_ext.manual.private_channel import PrivateChannel
from foxprofile_ext.runtime.confinement import ConfinementSpec, build_confined_command
from foxprofile_ext.runtime.registry import current_host_id, process_identity
from foxprofile_ext.storage.manual import HelperLiveness


def eventually(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        select.select([], [], [], 0.02)
    assert predicate()


def ended(identity):
    if process_identity(identity.pid) != identity:
        return True
    # An orphaned test supervisor can be a zombie until PID 1 reaps it.
    stat = Path(f"/proc/{identity.pid}/stat").read_text()
    return stat[stat.rfind(")") + 2 :].split()[0] == "Z"


def read_exact(connection, count):
    value = b""
    while len(value) < count:
        chunk = connection.recv(count - len(value))
        assert chunk, "RFB stream ended early"
        value += chunk
    return value


def handshake(channel):
    connection = channel.socket
    connection.settimeout(4)
    assert read_exact(connection, 12) == b"RFB 003.008\n"
    connection.sendall(b"RFB 003.008\n")
    methods = read_exact(connection, read_exact(connection, 1)[0])
    assert methods == b"\x01"  # No credential on an already private connected FD.
    connection.sendall(b"\x01")
    assert read_exact(connection, 4) == b"\x00" * 4
    connection.sendall(b"\x01")
    header = read_exact(connection, 24)
    assert struct.unpack("!HH", header[:4]) == (640, 480)
    name = read_exact(connection, struct.unpack("!I", header[20:])[0])
    assert name == b"FoxProfile"
    return connection


def test_private_channel_revocation_is_synchronous_idempotent_and_late_safe():
    server, peer = socket.socketpair()
    channel = PrivateChannel(server)
    seen = []
    channel.on_revoke(lambda: seen.append("first"))
    channel.on_revoke(lambda: 1 / 0)
    channel.on_revoke(lambda: seen.append("last"))
    channel.revoke()
    channel.revoke()
    channel.on_revoke(lambda: seen.append("late"))
    assert channel.revoked and seen == ["first", "last", "late"]
    assert peer.recv(1) == b""
    peer.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fork/prctl helper gate")
def test_receipt_failure_reaps_gated_child_without_exec(tmp_path, monkeypatch):
    identities = []

    def identity(pid):
        value = process_identity(pid)
        identities.append(value)
        return value

    def unavailable(*_args):
        raise OSError("injected state write failure")

    monkeypatch.setattr(helper_module, "process_identity", identity)
    monkeypatch.setattr(helper_module, "write_receipt", unavailable)
    server, peer = socket.socketpair()
    control, control_peer = socket.socketpair()
    try:
        with pytest.raises(OSError, match="injected"):
            helper_module._spawn_gated({"receipt": str(tmp_path / "receipt")}, server, control, {})
        assert identities and all(ended(value) for value in identities)
    finally:
        for connection in (server, peer, control, control_peer):
            connection.close()


@pytest.fixture
def native(tmp_path):
    if not sys.platform.startswith("linux") or os.getenv("FOXPROFILE_RUN_NATIVE_X_TESTS") != "1":
        pytest.skip("opt-in Linux native-X harness")
    binaries = {name: shutil.which(name) for name in ("Xvfb", "x11vnc", "xauth", "bwrap")}
    assert all(binaries.values()), binaries
    profile, temporary, state = (tmp_path / name for name in ("profile", "run", "state"))
    for path in (profile, temporary, state):
        path.mkdir(mode=0o700)
    libraries = tuple(
        Path(value).resolve()
        for value in os.getenv("FOXPROFILE_NATIVE_LIBRARY_PATH", "").split(":")
        if value
    )
    runtime_paths = tuple(
        dict.fromkeys(
            (
                Path("/usr"),
                Path("/bin"),
                Path("/lib"),
                Path("/lib64"),
                Path(sys.base_prefix),
                Path(sys.prefix),
                *(Path(path).resolve().parent for path in binaries.values()),
                *libraries,
            )
        )
    )
    spec = ConfinementSpec(profile, temporary, Path(__file__).resolve().parents[1], runtime_paths)
    authority = temporary / "Xauthority"
    (temporary / ".X11-unix").mkdir(mode=0o700)
    authority.touch(mode=0o600)
    # Server loads the same cookie before its dynamically allocated display is known.
    cookie = os.urandom(16).hex()
    subprocess.run(
        [binaries["xauth"], "-f", str(authority), "add", ":65530", ".", cookie],
        check=True,
        capture_output=True,
    )
    command, environment = build_confined_command(
        spec,
        [
            binaries["Xvfb"],
            "-displayfd",
            "1",
            "-auth",
            "/tmp/Xauthority",
            "-nolisten",
            "tcp",
            "-screen",
            "0",
            "640x480x24",
            "-noreset",
        ],
    )
    if libraries:
        environment["LD_LIBRARY_PATH"] = ":".join(map(str, libraries))
    display_process = subprocess.Popen(
        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert select.select([display_process.stdout], [], [], 5)[0], "Xvfb did not start"
    number = display_process.stdout.readline().decode().strip()
    if not number.isdigit():
        display_process.wait(timeout=5)
        pytest.fail(display_process.stderr.read().decode())
    display = f":{number}"
    subprocess.run(
        [binaries["xauth"], "-f", str(authority), "add", display, ".", cookie],
        check=True,
        capture_output=True,
    )
    identity = process_identity(display_process.pid)
    runtime = RuntimeDescriptor(
        BrowserRunRefDTO("profile-test", "run-test"),
        current_host_id(),
        identity,
        browser=process_identity(os.getpid()),
        display=display,
        namespace=os.readlink(f"/proc/{display_process.pid}/ns/pid"),
        tmp_path=str(temporary),
        xauthority_path=str(authority),
    )
    backend = PrivateVNCBackend(
        state_root=state,
        confinement_for=lambda _: spec,
        executable=binaries["x11vnc"],
        executable_sha256=hashlib.sha256(Path(binaries["x11vnc"]).read_bytes()).hexdigest(),
        library_paths=libraries,
    )
    harness = SimpleNamespace(
        backend=backend, runtime=runtime, spec=spec, process=display_process, binaries=binaries
    )
    try:
        yield harness
    finally:
        for session_id, entry in list(backend._sessions.items()):
            backend.stop(session_id, runtime.host_id, entry.helper, time.time() + 2)
            if entry.process is not None:
                entry.process.wait(timeout=5)
        display_process.terminate()
        display_process.wait(timeout=5)
        display_process.stdout.close()
        display_process.stderr.close()


def start(harness, session_id="session", claim=lambda _: True, watchdog=30):
    now = time.time()
    return harness.backend.start(
        session_id,
        harness.runtime,
        claim,
        expires_at=now + 120,
        cleanup_deadline=now + 180,
        watchdog_deadline=now + watchdog,
    )


def wait_dead(harness, session_id, helper):
    eventually(
        lambda: (
            harness.backend.probe(session_id, harness.runtime.host_id, helper)
            is HelperLiveness.DEAD
        )
    )
    receipt = harness.backend._read_receipt(session_id)
    assert receipt["phase"] == "dead"
    if receipt["child"] is not None:
        assert ended(ProcessIdentity(*receipt["child"]))
    assert harness.process.poll() is None


def test_real_connected_rfb_has_no_listener_and_stop_preserves_display(native):
    started = start(native)
    connection = handshake(started.channel)
    assert native.backend.connect("session", started.helper) is started.channel
    receipt = native.backend._read_receipt("session")
    # The confined child is bwrap; inspect it and every namespace descendant.
    owned = {receipt["child"][0]}
    for _ in range(4):
        for pid in list(owned):
            children = Path(f"/proc/{pid}/task/{pid}/children")
            if children.exists():
                owned.update(map(int, children.read_text().split()))
    socket_inodes = set()
    for pid in owned:
        for fd in Path(f"/proc/{pid}/fd").glob("*"):
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                socket_inodes.add(target[8:-1])
    assert socket_inodes
    for table in ("tcp", "tcp6"):
        for line in Path(f"/proc/net/{table}").read_text().splitlines()[1:]:
            columns = line.split()
            assert not (columns[3] == "0A" and columns[9] in socket_inodes)
    native.backend.heartbeat("session", time.time() + 1000)
    eventually(
        lambda: (
            native.backend._read_receipt("session")["watchdog_deadline"]
            > receipt["watchdog_deadline"]
        )
    )
    assert native.backend._read_receipt("session")["watchdog_deadline"] <= time.time() + 60
    native.backend.stop("session", native.runtime.host_id, started.helper, time.time() + 2)
    wait_dead(native, "session", started.helper)
    assert started.channel.revoked
    with pytest.raises(OSError):
        connection.send(b"input")
    native.backend.stop("session", native.runtime.host_id, started.helper, time.time() + 1)
    with pytest.raises(VNCBackendUnavailable):
        start(native)


def test_real_start_is_gated_until_durable_claim_and_close_cancels(native):
    claims = []

    def close_before_claim(helper):
        claims.append(helper)
        eventually(lambda: native.backend._read_receipt("session") is not None)
        assert native.backend._read_receipt("session")["phase"] == "gated"
        assert native.backend._read_receipt("session")["child"] is None
        native.backend.stop("session", native.runtime.host_id, None, time.time() + 2)
        return True

    with pytest.raises(VNCBackendUnavailable, match="cancelled"):
        start(native, claim=close_before_claim)
    wait_dead(native, "session", claims[0])


def test_close_during_runtime_validation_cannot_create_late_helper(native, monkeypatch):
    import threading

    entered, resume = threading.Event(), threading.Event()
    original = native.backend._commands

    def blocked(runtime):
        entered.set()
        assert resume.wait(4)
        return original(runtime)

    monkeypatch.setattr(native.backend, "_commands", blocked)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(start, native)
        assert entered.wait(4)
        assert (
            native.backend.probe("session", native.runtime.host_id, None) is HelperLiveness.UNKNOWN
        )
        native.backend.stop("session", native.runtime.host_id, None, time.time() + 2)
        resume.set()
        with pytest.raises(VNCBackendUnavailable, match="cancelled"):
            future.result(timeout=5)
    assert native.backend._sessions["session"].process is None
    assert native.backend.probe("session", native.runtime.host_id, None) is HelperLiveness.DEAD


@pytest.mark.parametrize("failure", ["claim", "authority", "binary"])
def test_real_partial_start_failure_is_confirmed_clean(native, failure):
    if failure == "authority":
        empty = Path(native.runtime.tmp_path) / "empty-Xauthority"
        empty.touch(mode=0o600)
        native.runtime = replace(native.runtime, xauthority_path=str(empty))
    elif failure == "binary":
        native.backend._sha256 = "0" * 64
    with pytest.raises(VNCBackendUnavailable):
        start(native, claim=lambda _: failure != "claim", watchdog=3)
    entry = native.backend._sessions["session"]
    eventually(
        lambda: (
            native.backend.probe("session", native.runtime.host_id, entry.helper)
            is HelperLiveness.DEAD
        )
    )
    assert native.process.poll() is None


def test_real_independent_watchdog_before_first_heartbeat(native):
    began = time.time()
    started = start(native, watchdog=3)
    receipt = native.backend._read_receipt("session")
    assert receipt["watchdog_deadline"] <= began + 3.1
    handshake(started.channel)
    wait_dead(native, "session", started.helper)
    assert time.time() - began < 4


def test_wrong_host_or_pid_generation_cannot_kill_helper(native):
    started = start(native)
    handshake(started.channel)
    wrong = ProcessIdentity(started.helper.pid, started.helper.start_ticks + 1)
    native.backend.stop("session", native.runtime.host_id, wrong, time.time())
    native.backend.stop("session", "other-host:boot", started.helper, time.time())
    assert native.backend.probe("session", native.runtime.host_id, wrong) is HelperLiveness.UNKNOWN
    assert (
        native.backend.probe("session", native.runtime.host_id, started.helper)
        is HelperLiveness.LIVE
    )
    # Recovery has no in-memory session: receipt and host/start ticks remain required.
    recovered = PrivateVNCBackend(
        state_root=native.backend.state_root,
        confinement_for=lambda _: native.spec,
        executable_sha256=native.backend._sha256,
    )
    recovered.stop("session", native.runtime.host_id, wrong, time.time())
    assert recovered.probe("session", native.runtime.host_id, started.helper) is HelperLiveness.LIVE
    recovered.stop("session", native.runtime.host_id, started.helper, time.time() + 2)
    wait_dead(native, "session", started.helper)


@pytest.mark.parametrize("phase", ["gated", "attached"])
def test_real_gateway_sigkill_before_first_heartbeat_still_cleans_independently(native, phase):
    # Separate OS gateway: SIGKILL prevents every gateway finally/atexit hook.
    script = """
import json, signal, sys, time
from foxprofile_ext.dto import BrowserRunRefDTO, ProcessIdentity, RuntimeDescriptor
from foxprofile_ext.manual.backend import PrivateVNCBackend
from foxprofile_ext.runtime.confinement import ConfinementSpec
from pathlib import Path
c = json.load(sys.stdin)
r = RuntimeDescriptor(BrowserRunRefDTO('profile-test', 'run-test'), c['host'],
    ProcessIdentity(*c['supervisor']), browser=ProcessIdentity(*c['browser']),
    display=c['display'], namespace=c['namespace'], tmp_path=c['temporary'],
    xauthority_path=c['authority'])
s = ConfinementSpec(Path(c['profile']), Path(c['temporary']), Path(c['code']),
    tuple(map(Path, c['runtime_paths'])))
b = PrivateVNCBackend(state_root=c['state'], confinement_for=lambda _: s,
    executable=c['executable'], executable_sha256=c['sha256'],
    library_paths=c['libraries'])
def claim(helper):
    if c['phase'] == 'gated':
        print(json.dumps([helper.pid, helper.start_ticks]), flush=True)
        signal.pause()
    return True
now = time.time()
started = b.start('gateway-session', r, claim, expires_at=now+120,
    cleanup_deadline=now+180, watchdog_deadline=now+3)
print(json.dumps([started.helper.pid, started.helper.start_ticks]), flush=True)
signal.pause()
"""
    runtime = native.runtime
    config = {
        "host": runtime.host_id,
        "supervisor": [runtime.supervisor.pid, runtime.supervisor.start_ticks],
        "browser": [runtime.browser.pid, runtime.browser.start_ticks],
        "display": runtime.display,
        "namespace": runtime.namespace,
        "temporary": runtime.tmp_path,
        "authority": runtime.xauthority_path,
        "profile": str(native.spec.profile_path),
        "code": str(native.spec.code_root),
        "runtime_paths": list(map(str, native.spec.runtime_paths)),
        "state": str(native.backend.state_root),
        "executable": native.backend._executable,
        "sha256": native.backend._sha256,
        "libraries": list(map(str, native.backend._library_paths)),
        "phase": phase,
    }
    gateway = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    helper = None
    try:
        gateway.stdin.write(json.dumps(config).encode())
        gateway.stdin.close()
        assert select.select([gateway.stdout], [], [], 5)[0]
        line = gateway.stdout.readline()
        assert line, gateway.stderr.read().decode()
        helper = ProcessIdentity(*json.loads(line))
        eventually(lambda: native.backend._read_receipt("gateway-session") is not None)
        receipt = native.backend._read_receipt("gateway-session")
        assert receipt["phase"] == phase
        assert receipt["watchdog_deadline"] <= time.time() + 3
        trigger = time.monotonic()
        gateway.kill()
        gateway.wait(timeout=3)
        wait_dead(native, "gateway-session", helper)
        eventually(lambda: ended(helper))
        assert time.monotonic() - trigger < 3
    finally:
        if gateway.poll() is None:
            gateway.kill()
            gateway.wait(timeout=3)
        if helper is not None:
            native.backend.stop("gateway-session", runtime.host_id, helper, time.time() + 2)
            eventually(lambda: ended(helper))
        gateway.stdout.close()
        gateway.stderr.close()


class XInputProbe:
    """Real XQueryKeymap/pointer state and ordinary native click observation."""

    def __init__(self, display):
        self.x = ctypes.CDLL(ctypes.util.find_library("X11"))
        self.t = ctypes.CDLL(ctypes.util.find_library("Xtst"))
        pointer, window = ctypes.c_void_p, ctypes.c_ulong
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x.XOpenDisplay.restype = pointer
        self.x.XDefaultRootWindow.argtypes = [pointer]
        self.x.XDefaultRootWindow.restype = window
        self.x.XKeysymToKeycode.argtypes = [pointer, ctypes.c_ulong]
        self.x.XQueryKeymap.argtypes = [pointer, ctypes.c_void_p]
        self.x.XQueryPointer.argtypes = [
            pointer,
            window,
            ctypes.POINTER(window),
            ctypes.POINTER(window),
            *([ctypes.POINTER(ctypes.c_int)] * 4),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.x.XCreateSimpleWindow.argtypes = [
            pointer,
            window,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            window,
            window,
        ]
        self.x.XCreateSimpleWindow.restype = window
        self.x.XSelectInput.argtypes = [pointer, window, ctypes.c_long]
        self.x.XMapWindow.argtypes = [pointer, window]
        self.x.XDestroyWindow.argtypes = [pointer, window]
        self.x.XSync.argtypes = [pointer, ctypes.c_int]
        self.x.XPending.argtypes = [pointer]
        self.x.XNextEvent.argtypes = [pointer, ctypes.c_void_p]
        self.x.XCloseDisplay.argtypes = [pointer]
        self.t.XTestFakeMotionEvent.argtypes = [
            pointer,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self.t.XTestFakeButtonEvent.argtypes = [
            pointer,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self.display = self.x.XOpenDisplay(display.encode())
        assert self.display
        self.root = self.x.XDefaultRootWindow(self.display)
        self.control_key = self.x.XKeysymToKeycode(self.display, 0xFFE3)
        self.window = self.x.XCreateSimpleWindow(self.display, self.root, 10, 10, 200, 200, 0, 0, 0)
        self.x.XSelectInput(self.display, self.window, (1 << 2) | (1 << 3))
        self.x.XMapWindow(self.display, self.window)
        self.x.XSync(self.display, 0)

    def state(self):
        keys = (ctypes.c_ubyte * 32)()
        self.x.XQueryKeymap(self.display, keys)
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        coordinates = [ctypes.c_int() for _ in range(4)]
        mask = ctypes.c_uint()
        assert self.x.XQueryPointer(
            self.display,
            self.root,
            ctypes.byref(root),
            ctypes.byref(child),
            *(ctypes.byref(value) for value in coordinates),
            ctypes.byref(mask),
        )
        return bool(keys[self.control_key // 8] & (1 << (self.control_key % 8))), mask.value

    def ordinary_click(self):
        # Same XTEST/native path ordinary browser input ultimately reaches.
        while self.x.XPending(self.display):
            self.x.XNextEvent(self.display, (ctypes.c_long * 24)())
        self.t.XTestFakeMotionEvent(self.display, -1, 60, 60, 0)
        self.t.XTestFakeButtonEvent(self.display, 1, 1, 0)
        self.t.XTestFakeButtonEvent(self.display, 1, 0, 0)
        self.x.XSync(self.display, 0)

        class ButtonEvent(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_int),
                ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong),
                ("root", ctypes.c_ulong),
                ("subwindow", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("x", ctypes.c_int),
                ("y", ctypes.c_int),
                ("x_root", ctypes.c_int),
                ("y_root", ctypes.c_int),
                ("state", ctypes.c_uint),
                ("button", ctypes.c_uint),
                ("same_screen", ctypes.c_int),
            ]

        events = []
        while self.x.XPending(self.display):
            event = (ctypes.c_long * 24)()
            self.x.XNextEvent(self.display, event)
            button = ctypes.cast(event, ctypes.POINTER(ButtonEvent)).contents
            if button.type in (4, 5):
                events.append((button.type, button.state, button.button))
        assert events == [(4, 0, 1), (5, 1 << 8, 1)]
        assert self.state() == (False, 0)

    def close(self):
        self.x.XDestroyWindow(self.display, self.window)
        self.x.XCloseDisplay(self.display)


@pytest.mark.parametrize("trigger", ["stop", "disconnect"])
def test_real_held_input_release_then_next_native_click(native, monkeypatch, trigger):
    monkeypatch.setenv("XAUTHORITY", native.runtime.xauthority_path)
    probe = XInputProbe(native.runtime.display)
    try:
        started = start(native)
        connection = handshake(started.channel)

        def hold():
            # Synchronize the RFB client's previous button state after release.
            connection.sendall(struct.pack("!BBHI", 4, 0, 0, 0xFFE3))
            connection.sendall(struct.pack("!BBHH", 5, 0, 60, 60))
            connection.sendall(struct.pack("!BBHI", 4, 1, 0, 0xFFE3))
            connection.sendall(struct.pack("!BBHH", 5, 1, 60, 60))
            eventually(lambda: probe.state() == (True, (1 << 2) | (1 << 8)))

        hold()
        native.backend.release_input("session")
        eventually(lambda: probe.state() == (False, 0))
        # Explicit release works while x11vnc still lives, before disposal.
        assert (
            native.backend.probe("session", native.runtime.host_id, started.helper)
            is HelperLiveness.LIVE
        )
        hold()
        if trigger == "stop":
            native.backend.stop("session", native.runtime.host_id, started.helper, time.time() + 2)
        else:
            started.channel.revoke()
        wait_dead(native, "session", started.helper)
        eventually(lambda: probe.state() == (False, 0))
        probe.ordinary_click()
    finally:
        probe.close()


def test_heartbeat_cannot_extend_initial_absolute_cleanup_bound(native):
    now = time.time()
    hard_deadline = now + 3
    started = native.backend.start(
        "session",
        native.runtime,
        lambda _: True,
        expires_at=now + 120,
        cleanup_deadline=hard_deadline,
        watchdog_deadline=now + 1.5,
    )
    handshake(started.channel)
    native.backend.heartbeat("session", now + 1000)
    eventually(lambda: native.backend._read_receipt("session")["watchdog_deadline"] > now + 1.5)
    assert native.backend._read_receipt("session")["watchdog_deadline"] <= hard_deadline
    wait_dead(native, "session", started.helper)
    assert time.time() < hard_deadline + 0.3


def test_old_close_cannot_stop_replacement_session_on_same_display(native):
    old = start(native, "old")
    handshake(old.channel)
    native.backend.stop("old", native.runtime.host_id, old.helper, time.time() + 2)
    wait_dead(native, "old", old.helper)
    new = start(native, "new")
    handshake(new.channel)
    native.backend.stop("old", native.runtime.host_id, old.helper, time.time() + 2)
    assert native.backend.connect("new", new.helper) is new.channel
    assert not new.channel.revoked
    assert native.process.poll() is None


def test_real_watchdog_still_kills_child_when_receipt_writes_fail(native):
    started = start(native)
    handshake(started.channel)
    child = ProcessIdentity(*native.backend._read_receipt("session")["child"])
    try:
        native.backend.state_root.chmod(0o500)
        native.backend.heartbeat("session", time.time() + 60)
        eventually(lambda: native.backend._sessions["session"].process.poll() is not None)
        assert ended(child)
        # Failed durable confirmation must retain recovery, even after physical exit.
        assert (
            native.backend.probe("session", native.runtime.host_id, started.helper)
            is HelperLiveness.UNKNOWN
        )
        assert native.process.poll() is None
    finally:
        native.backend.state_root.chmod(0o700)
