"""Read or write a stopped profile's cookies through a headless Camoufox.

Run as a subprocess (like runner.py) so the async browser gets its own event
loop. Usage:
    python cookie_tool.py export <profile> <os> [engine]
    python cookie_tool.py import <profile> <os> [engine]   (Playwright cookies JSON on stdin)

engine is "camoufox" (default) or "chrome".

The last stdout line is "RESULT:<json>".
"""

import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from camoufox.async_api import AsyncCamoufox

from foxprofile_ext.config import extension_enabled
from src.core.config import DATA_DIR, HEADLESS
from src.models.profile import ENGINES
from src.services.browser import display as xvfb
from src.services.browser.fingerprint import load_or_create
from src.services.browser.runner import (
    _emit_runtime_metadata,
    browser_process_metadata,
    camoufox_runtime_options,
    runtime_executable,
    start_virtual_display,
)
from src.utils.validation import validate_profile_name


def _open(profile_dir: str, os_type: str, engine: str, options: dict | None = None) -> Any:
    if engine == "chrome":
        from chrome_engine import ChromeEngine

        # No proxy: cookies need no network, and no exit-IP lookup is made.
        return ChromeEngine(
            profile_dir,
            platform=os_type,
            headless=True,
            executable=runtime_executable("chrome"),
        )
    return AsyncCamoufox(
        **(options if options is not None else _camoufox_options(profile_dir, os_type))
    )


def _camoufox_options(profile_dir: str, os_type: str) -> dict:
    return {
        **(camoufox_runtime_options(HEADLESS) if extension_enabled() else {"headless": True}),
        "os": os_type,
        "fingerprint": load_or_create(profile_dir, os_type),
        "user_data_dir": profile_dir,
        "persistent_context": True,
    }


async def _run(
    action: str, profile_name: str, os_type: str, payload: list, engine: str = "camoufox"
) -> dict:
    profile_dir = os.path.join(os.getcwd(), DATA_DIR, profile_name)
    display = None
    try:
        options = _camoufox_options(profile_dir, os_type) if engine == "camoufox" else None
        if options is not None:
            display = await asyncio.to_thread(start_virtual_display, options)
        _emit_runtime_metadata(options.get("virtual_display") if options else None)
        async with _open(profile_dir, os_type, engine, options) as context:
            _emit_runtime_metadata(
                options.get("virtual_display") if options else None,
                browser_process=browser_process_metadata(engine),
            )
            if action == "export":
                return {"cookies": await context.cookies()}
            await context.add_cookies(payload)
            return {"imported": len(payload)}
    finally:
        if display:
            display.kill()
            xvfb.clear()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) not in (4, 5) or sys.argv[1] not in ("export", "import"):
        print('RESULT:{"error": "usage: cookie_tool.py export|import <profile> <os> [engine]"}')
        return 2

    action, profile_name, os_type = sys.argv[1:4]
    engine = sys.argv[4] if len(sys.argv) == 5 else "camoufox"
    valid, msg = validate_profile_name(profile_name)
    if not valid or os_type not in ("windows", "macos", "linux") or engine not in ENGINES:
        print("RESULT:" + json.dumps({"error": msg or f"invalid os/engine {os_type!r}/{engine!r}"}))
        return 2
    payload = json.loads(sys.stdin.read() or "[]") if action == "import" else []
    try:
        result = asyncio.run(_run(action, profile_name, os_type, payload, engine))
    except Exception as e:
        print("RESULT:" + json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    print("RESULT:" + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
