import asyncio
import contextlib
import json
import math
import os
import secrets
import signal
import subprocess
import sys
import tempfile

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from camoufox.async_api import AsyncCamoufox
from camoufox.virtdisplay import VirtualDisplay

from src.core.config import DATA_DIR, HEADLESS, RESTORE_TABS
from src.services.browser import control, session
from src.services.browser import display as xvfb
from src.services.browser.fingerprint import load_or_create
from src.services.proxy.geo_check import requests_proxy_url
from src.utils.proxy_parser import parse_proxy

_shutdown = asyncio.Event()

# How often the open tabs are snapshotted. The browser is stopped by killing
# this process, so whatever was saved last is what the next launch reopens.
TAB_SNAPSHOT_SECONDS = 2.0


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _safe_print(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        fallback = (message + "\n").encode("utf-8", errors="replace")
        sys.stdout.buffer.write(fallback)
        sys.stdout.flush()


def _emit_runtime_metadata(
    display_name: str | None, *, browser_process: dict | None = None
) -> None:
    """Announce sandbox-local display details for the trusted supervisor."""
    from foxprofile_ext.config import extension_enabled

    if not extension_enabled():
        return
    metadata = {
        "display": display_name or os.environ.get("DISPLAY"),
        "xauthority_path": os.environ.get("XAUTHORITY") or None,
        # This is the namespace-local mount point; the supervisor maps it to
        # its trusted private host run directory before persisting metadata.
        "tmp_path": os.environ.get("TMPDIR", "/tmp"),
        # Namespace inode identity is host-visible too; the supervisor verifies
        # that it belongs to this confined child's process tree before storing it.
        "namespace": os.readlink("/proc/self/ns/pid"),
    }
    if browser_process is not None:
        metadata["browser_process"] = browser_process
    _safe_print("RUNTIME:" + json.dumps(metadata, separators=(",", ":")))


def camoufox_runtime_options(headless: bool | str) -> dict:
    """Keep browser and stopped-profile helpers on the same native policy."""
    options = {"headless": headless, "humanize": True}
    executable = runtime_executable("camoufox")
    if executable is not None:
        options["executable_path"] = executable
    return options


def runtime_executable(engine: str) -> str | None:
    """Use only the installed executable selected by the trusted supervisor."""
    from foxprofile_ext.config import extension_enabled

    if not extension_enabled():
        return None
    executable = os.environ.get(f"FOXPROFILE_{engine.upper()}_EXECUTABLE", "")
    if (
        not os.path.isabs(executable)
        or not os.path.isfile(executable)
        or not os.access(executable, os.X_OK)
    ):
        raise RuntimeError(f"Protected {engine} requires an installed runtime executable")
    return executable


def browser_process_metadata(engine: str) -> dict | None:
    """Report the actual engine process, not its Python/Playwright launcher."""
    executable = runtime_executable(engine)
    if executable is None:
        return None
    executable = os.path.realpath(executable)
    runner_pid = os.getpid()
    processes = {}
    matching = []
    for name in os.listdir("/proc"):
        if not name.isdecimal():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
                data = stat.read()
            fields = data[data.rindex(")") + 2 :].split()
            processes[pid] = (int(fields[1]), int(fields[19]))
            if pid != runner_pid and os.readlink(f"/proc/{pid}/exe") == executable:
                matching.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    candidates = []
    for pid in matching:
        parent, depth = pid, 0
        visited = set()
        while parent in processes and parent != runner_pid and parent not in visited:
            visited.add(parent)
            parent = processes[parent][0]
            depth += 1
        if parent == runner_pid:
            candidates.append((depth, pid))
    candidates.sort()
    if not candidates or (len(candidates) > 1 and candidates[0][0] == candidates[1][0]):
        raise RuntimeError("Protected browser process could not be verified")
    pid = candidates[0][1]
    return {"pid": pid, "start_ticks": processes[pid][1]}


class _PrivateVirtualDisplay(VirtualDisplay):
    """Keep Xvfb cookie-protected, including its Linux abstract X socket."""

    def __init__(self, **options) -> None:
        super().__init__(**options)
        self._authority_dir = tempfile.mkdtemp(prefix="foxprofile-xauth-")
        descriptor, self.authority_path = tempfile.mkstemp(
            prefix="Xauthority-", dir=self._authority_dir
        )
        os.close(descriptor)
        self._cookie = secrets.token_hex(16)
        try:
            self.authorize(":0")
        except BaseException:
            os.unlink(self.authority_path)
            os.rmdir(self._authority_dir)
            raise

    @property
    def xvfb_args(self) -> tuple:
        return tuple(arg for arg in super().xvfb_args if arg != "-ac") + (
            "-auth",
            self.authority_path,
        )

    def authorize(self, display_name: str) -> None:
        # Xvfb reads the cookie before choosing its -displayfd number; clients
        # then need that same cookie associated with the resulting display.
        result = subprocess.run(
            [
                "xauth",
                "-f",
                self.authority_path,
                "source",
                "-",
            ],
            input=f"add {display_name} MIT-MAGIC-COOKIE-1 {self._cookie}\n",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Could not prepare private X authorization")

    def kill(self) -> None:
        try:
            super().kill()
        finally:
            if os.environ.get("XAUTHORITY") == self.authority_path:
                os.environ.pop("XAUTHORITY", None)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.authority_path)
            with contextlib.suppress(OSError):
                os.rmdir(self._authority_dir)


def start_virtual_display(options: dict) -> VirtualDisplay | None:
    """Own the single virtual display used by either Camoufox entry point."""
    if options["headless"] != "virtual":
        return None
    from foxprofile_ext.config import extension_enabled

    xvfb.sweep_stale()
    display_options = {}
    protected = extension_enabled()
    if protected:
        # Allocate the framebuffer around the saved device; never resize its
        # fingerprint to a client viewport or the upstream 1x1 virtual default.
        fingerprint = options.get("fingerprint", {})
        screen = fingerprint.get("screen", {})
        window = fingerprint.get("window", {})
        scale = screen.get("devicePixelRatio") or window.get("devicePixelRatio") or 1
        width = max(
            1366,
            screen.get("width") or 0,
            (window.get("screenX") or 0) + (window.get("outerWidth") or 0),
        )
        height = max(
            768,
            screen.get("height") or 0,
            (window.get("screenY") or 0) + (window.get("outerHeight") or 0),
        )
        width, height = math.ceil(width * scale), math.ceil(height * scale)
        if not 1 <= width <= 32768 or not 1 <= height <= 32768:
            raise ValueError("Saved browser geometry exceeds supported virtual display size")
        display_options["screen"] = f"{width}x{height}x24"
    display = (_PrivateVirtualDisplay if protected else VirtualDisplay)(**display_options)
    try:
        options["virtual_display"] = display.get()
        if protected:
            display.authorize(options["virtual_display"])
            os.environ["XAUTHORITY"] = display.authority_path
        options["headless"] = False
        xvfb.record(display.proc.pid, options["virtual_display"])
        return display
    except BaseException:
        display.kill()
        xvfb.clear()
        raise


def _compact_error(exc: Exception, limit: int = 700) -> str:
    text = " ".join(str(exc).split())
    if "Call log:" in text:
        text = text.split("Call log:", 1)[0].strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _setup_signals(loop: asyncio.AbstractEventLoop) -> None:
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown.set)
    except NotImplementedError:
        pass


def _open_urls(context: object) -> list[str]:
    return [p.url for p in context.pages if not p.is_closed()]


async def restore_tabs(context: object, urls: list[str]) -> int:
    """Reopen saved tabs: the first in the existing blank tab, the rest in new ones.

    Waits only for navigation to commit, so a slow site does not hold up the
    launch; a tab that fails to load stays open on whatever it reached.
    """
    if not urls:
        return 0
    first = context.pages[0] if context.pages else await context.new_page()
    pages = [first] + [await context.new_page() for _ in urls[1:]]

    async def go(page: object, url: str) -> None:
        try:
            await page.goto(url, wait_until="commit", timeout=15_000)
        except Exception as e:
            _safe_print(f"Could not restore tab {url}: {_compact_error(e, 120)}")

    await asyncio.gather(*(go(p, u) for p, u in zip(pages, urls, strict=True)))
    await pages[0].bring_to_front()
    return len(urls)


async def snapshot_tabs(context: object, profile_dir: str, stop: asyncio.Event) -> None:
    """Save the open tabs whenever they change, until `stop` is set."""
    last: list[str] = []
    while not stop.is_set():
        # asyncio.TimeoutError, not the builtin: they only became one in 3.11.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TAB_SNAPSHOT_SECONDS)
        if stop.is_set():
            # The last tab closing ends the session; keep the previous snapshot
            # instead of saving the now-empty window.
            return
        urls = session.restorable(_open_urls(context))
        if urls and urls != last and session.save(profile_dir, urls):
            last = urls


def geo_overrides(timezone: str, locale: str) -> dict:
    """Launch options that pin the timezone / locale instead of following the IP.

    Camoufox's geoip only fills in the timezone and locale when they are unset
    (setdefault for the config key, and `locale=` is applied after geoip), so the
    pinned values win while geolocation and the WebRTC IP still follow the proxy.
    """
    options: dict = {}
    if timezone:
        options["config"] = {"timezone": timezone}
    if locale:
        options["locale"] = locale
    return options


def exit_ip(proxy_str: str) -> str:
    """The exit IP Camoufox's geoip should use, looked up with DNS through the proxy.

    With `geoip=True` Camoufox looks it up itself, and for a socks5:// or
    socks4:// proxy that resolves the lookup hosts with the machine's own
    resolver at every launch.
    """
    from camoufox.ip import public_ip

    try:
        return public_ip(requests_proxy_url(proxy_str))
    except Exception as e:
        # The raw error is a urllib3 retry chain (class names, object addresses,
        # the lookup host); log it, but report something a user can act on.
        if not proxy_str or proxy_str == "None":
            raise
        _safe_print(f"Exit IP lookup failed: {_compact_error(e, 300)}")
        parsed = parse_proxy(proxy_str)
        server = parsed["server"].split("://", 1)[-1] if parsed else "the proxy"
        raise ProxyUnreachable(f"no internet connection through proxy {server}") from e


class ProxyUnreachable(RuntimeError):
    pass


def stable_locale(profile_dir: str, ip: str) -> str:
    """The locale for `ip`, kept per profile while the IP stays in one country.

    Left to geoip, Camoufox would pick a new locale for the country on every launch.
    """
    from camoufox.geolocation import get_geolocation

    from chrome_engine.geo import remembered_locale

    locale = get_geolocation(ip).locale
    return remembered_locale(profile_dir, locale.region, locale.as_string)


def pin_geoip_locale(locale: str) -> None:
    """Make Camoufox's geoip draw `locale` instead of a random one for the IP's country.

    Passing `locale=` is not enough: geoip still draws at random and fills the
    locale keys with setdefault, and `locale=` only overwrites the script when
    its own locale has one. Keeping zh-HK after a draw of en-Latn-HK would
    present zh-Latn-HK. This runner is one process per profile, so the patch
    only affects this launch.
    """
    from camoufox.locales import SELECTOR, normalize_locale

    pinned = normalize_locale(locale)
    SELECTOR.from_region = lambda region: pinned


async def run_browser(
    profile_name: str,
    proxy_str: str,
    os_type: str,
    timezone: str = "",
    locale: str = "",
) -> int:
    profile_dir = os.path.join(os.getcwd(), DATA_DIR, profile_name)

    launch_config = {
        "os": os_type,
        "fingerprint": load_or_create(profile_dir, os_type),
        "block_images": False,
        "user_data_dir": profile_dir,
        "persistent_context": True,
        **geo_overrides(timezone, locale),
    }

    proxy_config = parse_proxy(proxy_str)
    if proxy_config:
        launch_config["proxy"] = proxy_config

    _safe_print(f"Starting browser for {profile_name}...")

    # Camoufox's own headless="virtual" starts Xvfb before it builds the launch
    # options and stops it only when the browser closes, so every failed launch
    # left an Xvfb behind. Own the display here and stop it however we exit; the
    # record lets the launcher stop it if this process is killed instead.
    display = None
    try:
        launch_config.update(camoufox_runtime_options(HEADLESS))
        from foxprofile_ext.config import extension_enabled

        if extension_enabled():
            from foxprofile_ext.runtime.cache import require_geoip_cache

            require_geoip_cache()
        # Blocking (up to ~30 s over a slow proxy), so off the event loop; inside
        # the try so a dead proxy reports LAUNCH_FAILED like any launch error.
        ip = await asyncio.to_thread(exit_ip, proxy_str)
        launch_config["geoip"] = ip
        if not locale:
            launch_config["locale"] = await asyncio.to_thread(stable_locale, profile_dir, ip)
        pin_geoip_locale(launch_config["locale"])
        display = await asyncio.to_thread(start_virtual_display, launch_config)
        _emit_runtime_metadata(launch_config.get("virtual_display"))
        async with AsyncCamoufox(**launch_config) as context:
            _emit_runtime_metadata(
                launch_config.get("virtual_display"),
                browser_process=browser_process_metadata("camoufox"),
            )
            if not context.pages:
                await context.new_page()
            if RESTORE_TABS:
                restored = await restore_tabs(context, session.load(profile_dir))
                if restored:
                    _safe_print(f"Restored {restored} tab(s)")

            close_event = asyncio.Event()

            def on_page_close(_page: object) -> None:
                # The session ends when the last tab closes, not the first one.
                if not [p for p in context.pages if not p.is_closed()]:
                    close_event.set()

            def watch(page: object) -> None:
                page.on("close", on_page_close)

            for page in context.pages:
                watch(page)
            context.on("page", watch)
            context.on("close", lambda: close_event.set())

            control_runner, port, token = await control.serve(context)
            # CONTROL must precede BROWSER_STARTED: the launcher reports the
            # profile ready on BROWSER_STARTED and callers may control it at once.
            _safe_print(f"CONTROL:{port}:{token}")
            _safe_print("BROWSER_STARTED")

            snapshot_task = (
                asyncio.create_task(snapshot_tabs(context, profile_dir, close_event))
                if RESTORE_TABS
                else None
            )
            close_task = asyncio.create_task(close_event.wait())
            shutdown_task = asyncio.create_task(_shutdown.wait())

            done, pending = await asyncio.wait(
                [close_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if close_task in done:
                _safe_print("BROWSER_CLOSED")
            elif RESTORE_TABS:
                # Graceful shutdown (signal): the tabs are still open, save them.
                session.save(profile_dir, _open_urls(context))
            if snapshot_task:
                snapshot_task.cancel()
            await control_runner.cleanup()

            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            return 0

    except asyncio.CancelledError:
        _safe_print("LAUNCH_CANCELLED")
        return 1
    except Exception as e:
        _safe_print(f"LAUNCH_FAILED: {type(e).__name__}: {_compact_error(e, 220)}")
        return 1
    finally:
        if display:
            display.kill()
            xvfb.clear()


async def _async_main() -> int:
    loop = asyncio.get_running_loop()
    _setup_signals(loop)
    return await run_browser(*sys.argv[1:6])


if __name__ == "__main__":
    _configure_stdio()

    if len(sys.argv) < 4:
        _safe_print("Usage: python runner.py <name> <proxy> <os> [timezone] [locale]")
        sys.exit(1)

    exit_code = 1
    try:
        exit_code = asyncio.run(_async_main())
    except KeyboardInterrupt:
        _safe_print("Interrupted by user")
        exit_code = 130

    sys.exit(exit_code)
