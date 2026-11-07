"""Offline real Chromium panel contracts; fake accounts, no browser profiles launched."""

from __future__ import annotations

import asyncio
import base64
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII="
)


@pytest.fixture
def panel_server():
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response
    from starlette.routing import Route, WebSocketRoute
    from starlette.staticfiles import StaticFiles

    state = SimpleNamespace(
        actor=None,
        mode="personal",
        requests=[],
        assignments=["profile-a"],
        limit=2,
        occupied=2,
        manual=None,
        native=None,
        block_detail=None,
        block_manual=None,
        block_close=None,
        block_screenshot=None,
        manual_error=None,
        close_manual=None,
        close_error=None,
        batch_failure=None,
    )
    profiles = [
        dict(
            profile_id="profile-a",
            name="Fixture A",
            engine="camoufox",
            os_type="linux",
            proxy="http://192.0.2.1:8080",
            timezone="",
            locale="",
            state="running",
            is_running=True,
            fingerprint={"exists": False},
        ),
        dict(
            profile_id="profile-b",
            name="Fixture B",
            engine="chrome",
            os_type="windows",
            proxy=None,
            timezone="",
            locale="",
            state="stopped",
            is_running=False,
            fingerprint={"exists": False},
        ),
    ]

    state.profiles = profiles

    async def index(request):
        return FileResponse(ROOT / "src/web/index.html")

    async def api(request: Request):
        path = request.path_params["path"]
        body = (
            await request.json()
            if request.method not in {"GET", "HEAD"} and await request.body()
            else None
        )
        state.requests.append((request.method, path, body, dict(request.headers)))
        if path == "info":
            return JSONResponse(dict(auth_required=True, auth_mode=state.mode))
        if path == "ext/auth/login":
            state.actor = "admin" if body["username"] == "admin" else "user"
            response = JSONResponse(
                dict(
                    user=dict(
                        user_id=state.actor, username=state.actor, role=state.actor, active=True
                    ),
                    csrf_token="fixture-csrf",
                )
            )
            response.set_cookie(
                "foxprofile_session", "fixture-session", httponly=True, samesite="strict"
            )
            return response
        if state.actor is None:
            return JSONResponse({"detail": {"code": "AUTH_REQUIRED"}}, status_code=401)
        if request.method != "GET" and state.mode == "personal":
            assert request.headers.get("x-csrf-token") == "fixture-csrf"
            assert "authorization" not in request.headers
        if path == "ext/auth/me":
            return JSONResponse(
                dict(
                    user=dict(
                        user_id=state.actor, username=state.actor, role=state.actor, active=True
                    ),
                    csrf_token="fixture-csrf",
                )
            )
        if path == "ext/auth/logout":
            state.actor = None
            return JSONResponse({"success": True})
        if path == "profiles":
            if request.method == "POST":
                return JSONResponse(body)
            return JSONResponse({"profiles": profiles if state.actor == "admin" else profiles[:1]})
        if path == "browser":
            return JSONResponse({"running": ["Fixture A"]})
        if path.startswith("profiles/") and path.endswith("/fingerprint"):
            return JSONResponse({"exists": False})
        if path.startswith("profiles/") and path.count("/") == 1:
            if state.block_detail:
                await asyncio.to_thread(state.block_detail.wait)
            profile = next(
                p for p in profiles if path.split("/")[1] in {p["name"], "id:" + p["profile_id"]}
            )
            return JSONResponse({**profile, "proxy": "http://user:pass@192.0.2.1:8080"})
        if path.endswith("/screenshot"):
            if state.block_screenshot:
                blocker, state.block_screenshot = state.block_screenshot, None
                await asyncio.to_thread(blocker.wait, 5)
            return Response(PNG, media_type="image/png")
        if path.endswith("/tabs"):
            return JSONResponse({"tabs": []})
        if path == "mcp/setup":
            return JSONResponse(
                dict(
                    mcp_url="http://127.0.0.1/mcp",
                    auth_required=True,
                    token_placeholder="YOUR_SERVICE_TOKEN",
                    configs=[
                        dict(
                            id="claude-code",
                            name="Claude Code",
                            where="config",
                            code="YOUR_SERVICE_TOKEN",
                        )
                    ],
                )
            )
        if path == "ext/admin/users":
            return JSONResponse(
                {"users": [dict(user_id="user", username="Fixture User", role="user", active=True)]}
            )
        if path == "ext/admin/settings":
            if body:
                state.limit = body["max_concurrent_profiles"]
            return JSONResponse(dict(max_concurrent_profiles=state.limit, occupied=state.occupied))
        if path.endswith("/profile-assignments"):
            if body:
                state.assignments = body["profile_ids"]
            return JSONResponse({"profile_ids": state.assignments})
        if path in {"ext/browser/stop-batch", "ext/profiles/delete-batch"}:
            outcome = "deleted" if path.endswith("delete-batch") else "stopped"
            results = [
                {
                    "profile_id": profile["profile_id"],
                    "name": profile["name"],
                    outcome: profile["profile_id"] != state.batch_failure,
                    "error_code": "PROFILE_BUSY"
                    if profile["profile_id"] == state.batch_failure
                    else None,
                }
                for profile in profiles
                if profile["profile_id"] in body["profile_ids"]
            ]
            return JSONResponse({"profiles": results, "count": len(results)})
        if path == "ext/browser/start-batch":
            return JSONResponse(
                {
                    "detail": {
                        "code": "PROFILE_CAPACITY_EXCEEDED",
                        "message": "REDACTED FOREIGN COUNT MUST NOT RENDER",
                        "max_concurrent_profiles": 2,
                    }
                },
                status_code=409,
            )
        if path.endswith("/manual-sessions"):
            if state.block_manual:
                await asyncio.to_thread(state.block_manual.wait, 5)
            if state.manual_error:
                return JSONResponse({"detail": state.manual_error}, status_code=409)
            state.manual = dict(
                session_id="session-1",
                profile_id="profile-a",
                browser_run_id="run-1",
                state="active",
                websocket_path="/api/v1/ext/manual-sessions/session-1/ws",
            )
            return JSONResponse(state.manual)
        if path == "ext/manual-sessions/session-1":
            if request.method == "DELETE":
                if state.block_close:
                    await asyncio.to_thread(state.block_close.wait, 5)
                if state.close_error:
                    return JSONResponse(
                        {"detail": {"code": "MANUAL_SESSION_BUSY", "message": state.close_error}},
                        status_code=409,
                    )
                state.manual["state"] = "closed"
                if state.close_manual:
                    await asyncio.to_thread(state.close_manual)
            return JSONResponse(state.manual)
        return JSONResponse({"success": True})

    async def websocket(ws):
        await ws.accept(
            subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None
        )
        if not state.native:
            await ws.close()
            return
        channel = state.native
        channel.setblocking(False)
        loop = asyncio.get_running_loop()

        async def incoming():
            while True:
                await loop.sock_sendall(channel, await ws.receive_bytes())

        async def outgoing():
            while True:
                data = await loop.sock_recv(channel, 65536)
                if not data:
                    return
                await ws.send_bytes(data)

        tasks = [asyncio.create_task(fn()) for fn in (incoming, outgoing)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/v1/{path:path}", api, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
            WebSocketRoute("/api/v1/ext/manual-sessions/session-1/ws", websocket),
        ]
    )
    app.mount("/assets/ext", StaticFiles(directory=ROOT / "foxprofile_ext/assets"))
    app.mount("/assets/fonts", StaticFiles(directory=ROOT / "src/assets/fonts"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        thread.join(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive()


@pytest.fixture
def panel_page(panel_server):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch()
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Install isolated Playwright Chromium for real DOM checks")
            raise
        page = browser.new_page()
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        origin, state = panel_server
        page.on(
            "request",
            lambda req: (
                external.append(req.url)
                if not req.url.startswith((origin, "blob:" + origin))
                else None
            ),
        )
        page.add_init_script(
            "localStorage.setItem('foxprofile.token', 'fixture-old-service-key'); localStorage.setItem('foxprofile.lang', 'en')"
        )
        page.goto(origin)
        page.locator("#username").wait_for(state="visible")
        try:
            yield page, state
            assert not errors
            assert not external
        finally:
            browser.close()


def login(page, username="admin"):
    page.locator("#username").fill(username)
    page.locator("#password").fill("fixture-password")
    page.locator("#login-form button").click()
    page.locator("#app").wait_for(state="visible")
    page.locator(".row.body").first.wait_for()


# Reuse the real protected facade, catalog and personal auth from the API contracts.
from tests.test_ext_api_access import real_api as real_api  # noqa: E402, F401


@pytest.mark.parametrize("original_deleted", [False, True])
@pytest.mark.parametrize("replacement_owner", ["alice", "bob"])
@pytest.mark.parametrize("replacement_name", ["original-name", "original-id"])
def test_delete_confirmation_keeps_captured_id_after_name_reuse(
    panel_page, real_api, original_deleted, replacement_owner, replacement_name
):
    from foxprofile_ext.errors import DomainError
    from tests.test_ext_api_access import ORIGIN, PASSWORD

    page, _ = panel_page
    client, container, _, original, _ = real_api
    application = container.protected_application
    alice = container.auth_service.login("alice", PASSWORD).principal
    owner = container.auth_service.login(replacement_owner, PASSWORD).principal
    requests = []

    def protected_request(route):
        request = route.request
        path = request.url.split("/api/v1/", 1)[1]
        requests.append((request.method, unquote(path)))
        headers = {"Origin": ORIGIN}
        for key in ("content-type", "x-csrf-token"):
            if key in request.headers:
                headers[key] = request.headers[key]
        response = client.request(
            request.method, "/api/v1/" + path, content=request.post_data, headers=headers
        )
        route.fulfill(
            status=response.status_code,
            body=response.content,
            headers={"content-type": response.headers.get("content-type", "")},
        )

    page.route("**/api/v1/**", protected_request)
    page.locator("#username").fill("alice")
    page.locator("#password").fill(PASSWORD)
    page.locator("#login-form button").click()
    page.get_by_role("button", name="Delete: Own", exact=True).click()
    if original_deleted:
        application.delete_profile(alice, original.profile_id)
    else:
        application.update_profile(alice, original.profile_id, {"name": "Renamed"})
    name = original.profile_id if replacement_name == "original-id" else "Own"
    replacement = application.create_profile(owner, name=name)

    page.locator(".modal").get_by_role("button", name="Delete", exact=True).click()
    page.locator("#toast").wait_for(state="visible")
    assert ("DELETE", "profiles/id:" + original.profile_id) in requests
    assert not any(method == "DELETE" and path == "profiles/Own" for method, path in requests)
    assert application.resolve_profile(owner, replacement.profile_id).name == name
    with pytest.raises(DomainError):
        application.resolve_profile(alice, "id:" + original.profile_id)


@pytest.mark.parametrize("action", ["edit", "export", "import", "fingerprint"])
def test_private_profile_actions_keep_id_when_modal_name_is_reused(panel_page, action):
    page, state = panel_page
    login(page)
    if action == "edit":
        page.get_by_role("button", name="Edit: Fixture B", exact=True).click()
        page.locator("#f-name").wait_for()
        assert any(req[:2] == ("GET", "profiles/id:profile-b") for req in state.requests)
    else:
        page.get_by_role("button", name="Cookies & fingerprint: Fixture B", exact=True).click()
        if action == "fingerprint":
            page.get_by_role("button", name="New fingerprint", exact=False).click()
    original = state.profiles[1]
    state.profiles.append({**original, "profile_id": "replacement-b"})
    original["name"] = "Renamed"
    page.evaluate("refresh()")

    method = {"edit": "PATCH", "export": "GET", "import": "POST", "fingerprint": "DELETE"}[action]
    suffix = {
        "edit": "",
        "export": "/cookies?format=json",
        "import": "/cookies",
        "fingerprint": "/fingerprint",
    }[action]
    with page.expect_response(
        lambda response: (
            response.request.method == method
            and unquote(response.url).endswith("/profiles/id:profile-b" + suffix)
        )
    ):
        if action == "edit":
            page.locator("#f-name").fill("Renamed")
            page.get_by_role("button", name="Save changes", exact=True).click()
        elif action == "export":
            with page.expect_download():
                page.get_by_role("button", name="Export cookies as JSON", exact=False).click()
        elif action == "import":
            page.locator('.modal input[type="file"]').set_input_files(
                {"name": "fixture.json", "mimeType": "application/json", "buffer": b"[]"}
            )
        else:
            page.locator(".modal").last.get_by_role(
                "button", name="New fingerprint", exact=True
            ).click()
    assert not any("Fixture B" in req[1] for req in state.requests)


def test_selection_uses_ids_across_rename_and_same_name_replacement(panel_page):
    page, state = panel_page
    login(page)
    page.get_by_role("checkbox", name="Select all").check()
    original = state.profiles[1]
    state.profiles.append({**original, "profile_id": "replacement-b"})
    original["name"] = "Renamed"
    page.evaluate("refresh()")
    assert page.get_by_role("checkbox", name="Select Renamed", exact=True).is_checked()
    assert not page.get_by_role("checkbox", name="Select Fixture B", exact=True).is_checked()
    page.locator(".bulk").get_by_role("button", name="Delete", exact=True).click()
    with page.expect_response("**/api/v1/ext/profiles/delete-batch"):
        page.locator(".modal").get_by_role("button", name="Delete", exact=True).click()
    batches = [req for req in state.requests if req[1] == "ext/profiles/delete-batch"]
    assert batches[-1][2] == {"profile_ids": ["profile-b"]}
    state.profiles.remove(original)
    page.evaluate("refresh()")
    assert not page.get_by_role("checkbox", name="Select Fixture B", exact=True).is_checked()


def test_ordinary_viewer_and_stop_keep_captured_id_after_name_reuse(panel_page):
    page, state = panel_page
    login(page, "user")
    page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
    page.locator("#v-screen").wait_for(state="visible")
    original = state.profiles[0]
    state.profiles.append({**original, "profile_id": "replacement-a"})
    original["name"] = "Renamed"
    page.evaluate("refresh()")
    with page.expect_response("**/api/v1/browser/id%3Aprofile-a/page/keyboard"):
        page.locator("#v-text").fill("fixture text")
        page.locator('#v-type button[type="submit"]').click()
    page.locator("#manual-open").click()
    page.locator("#manual-open").wait_for(state="visible")
    assert any(
        req[:2] == ("POST", "ext/profiles/profile-a/manual-sessions") for req in state.requests
    )
    page.locator("#v-close").click()
    with page.expect_response("**/api/v1/browser/id%3Aprofile-a/stop"):
        page.locator(".row.body .main").click()
    resource_paths = [req[1] for req in state.requests if req[1].startswith("browser/")]
    assert resource_paths
    assert all(path.startswith("browser/id:profile-a/") for path in resource_paths)


def test_personal_login_admin_capacity_assignments_and_single_batch(panel_page):
    page, state = panel_page
    login(page)
    assert page.evaluate("localStorage.getItem('foxprofile.token')") is None
    assert page.locator("#password").input_value() == ""
    page.locator("#admin-btn").click()
    page.locator("#admin-occupancy").wait_for()
    assert "Occupied: 2" in page.locator("#admin-occupancy").inner_text()
    page.locator("#admin-limit").fill("1")
    page.locator("#admin-limit").locator("xpath=ancestor::form").get_by_role(
        "button", name="Save changes", exact=True
    ).click()
    page.wait_for_function(
        'document.querySelector("#admin-occupancy").textContent.includes("Limit: 1")'
    )
    assert state.limit == 1
    page.get_by_role("button", name="Assigned profiles", exact=True).click()
    page.locator(".admin-grants").wait_for()
    assert page.locator(".admin-grants input:checked").count() == 1
    page.locator(".admin-grants input").nth(1).check()
    page.locator(".modal").last.get_by_role("button", name="Save changes", exact=True).click()
    page.locator(".modal").last.get_by_role("button", name="Save changes", exact=True).click()
    page.wait_for_function('document.querySelectorAll(".admin-grants").length === 0')
    assert state.assignments == ["profile-a", "profile-b"]
    page.locator(".modal").last.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("checkbox", name="Select all").check()
    page.locator(".bulk").get_by_role("button", name="Launch", exact=True).click()
    page.locator("#toast").filter(has_text="Shared profile limit (2) reached").wait_for()
    batches = [request for request in state.requests if request[1] == "ext/browser/start-batch"]
    assert len(batches) == 1
    assert batches[0][2] == {"profile_ids": ["profile-b"]}
    assert not any("/launch" in request[1] for request in state.requests)


def test_ordinary_detail_before_edit_and_revocation_clears_viewer(panel_page):
    page, state = panel_page
    login(page, "user")
    assert page.locator("#admin-btn").is_hidden()
    assert page.locator(".row.body").count() == 1
    page.get_by_role("button", name="Edit: Fixture A", exact=True).click()
    assert page.locator("#f-proxy").input_value() == "http://user:pass@192.0.2.1:8080"
    page.locator(".modal").get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
    page.locator("#v-screen").wait_for(state="visible")
    state.actor = None
    page.wait_for_function('!document.querySelector("#login").classList.contains("hidden")')
    assert page.locator("#viewer").is_hidden()
    assert page.locator("#sheet").inner_text() == ""
    assert page.locator("#v-screen").get_attribute("src") in {"", None}
    assert page.locator("#modal-root").inner_text() == ""


def test_manual_failure_restores_ordinary_viewer_and_local_rfb_module(panel_page):
    page, state = panel_page
    login(page, "user")
    page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
    page.locator("#manual-open").click()
    page.locator("#manual-open").wait_for(state="visible")
    page.locator("#v-screen").wait_for(state="visible")
    assert any(
        request[0] == "DELETE"
        and request[1] == "ext/manual-sessions/session-1"
        and request[2] == {"browser_run_id": "run-1"}
        for request in state.requests
    )
    assert (
        page.evaluate("import('/assets/ext/novnc/core/rfb.js').then(m => typeof m.default)")
        == "function"
    )
    page.locator("#v-text").fill("fixture text")
    page.locator('#v-type button[type="submit"]').click()
    page.wait_for_function("document.querySelector('#v-text').value === ''")
    assert sum(request[1].endswith("/keyboard") for request in state.requests) == 1


def test_manual_back_updates_status_only_after_successful_close(panel_page):
    page, state = panel_page
    login(page, "user")
    page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
    page.locator("#manual-open").click()
    page.locator("#manual-back").wait_for(state="visible")
    page.locator("#manual-status").evaluate(
        "element => element.textContent = 'Manual control connected'"
    )
    page.locator("#manual-back").click()
    page.locator("#manual-status").filter(has_text="Manual control ended").wait_for()
    assert any(
        request[0] == "DELETE" and request[1] == "ext/manual-sessions/session-1"
        for request in state.requests
    )


def test_manual_back_preserves_failed_close_error(panel_page):
    page, state = panel_page
    login(page, "user")
    page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
    state.close_error = "Close failed"
    page.locator("#manual-open").click()
    page.locator("#manual-back").wait_for(state="visible")
    page.locator("#manual-status").evaluate(
        "element => element.textContent = 'Manual control connected'"
    )
    page.locator("#manual-back").click()
    page.locator("#manual-status").filter(has_text="Manual access is busy").wait_for()
    assert page.locator("#manual-status").inner_text() != "Manual control ended"


def test_native_novnc_input_and_back_preserve_display(panel_page, native, monkeypatch):
    """Real noVNC -> binary WebSocket -> isolated x11vnc/Xvfb, without a browser profile."""
    from tests.test_ext_vnc_backend import XInputProbe, eventually, start

    page, state = panel_page
    monkeypatch.setenv("XAUTHORITY", native.runtime.xauthority_path)
    probe = XInputProbe(native.runtime.display)
    started = start(native, "web-session")
    state.native = started.channel.socket
    state.close_manual = lambda: native.backend.stop(
        "web-session", native.runtime.host_id, started.helper, time.time() + 2
    )
    login(page, "user")
    try:
        page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
        page.locator("#manual-open").click()
        page.locator("#manual-status").filter(has_text="Manual control connected").wait_for()
        assert native.process.poll() is None
        before = len([request for request in state.requests if request[1].endswith("/screenshot")])
        canvas = page.locator("#manual-screen canvas")
        canvas.wait_for(state="visible")
        canvas.click(position={"x": 60, "y": 60})
        page.keyboard.down("Control")
        box = canvas.bounding_box()
        page.mouse.move(box["x"] + 70, box["y"] + 70)
        page.mouse.down()
        page.mouse.move(box["x"] + 90, box["y"] + 90)
        eventually(lambda: probe.state()[0] and bool(probe.state()[1] & (1 << 8)))
        page.mouse.up()
        page.keyboard.up("Control")
        page.mouse.wheel(0, 120)
        eventually(lambda: probe.state() == (False, 0))
        page.set_viewport_size({"width": 640, "height": 900})
        page.wait_for_timeout(2100)
        assert (
            len([request for request in state.requests if request[1].endswith("/screenshot")])
            == before
        )
        assert not any(
            request[1].endswith(("/click-at", "/keyboard", "/press")) for request in state.requests
        )
        page.locator("#manual-back").click()
        page.locator("#v-screen").wait_for(state="visible")
        page.locator("#manual-status").filter(has_text="Manual control ended").wait_for()
        page.wait_for_function("!document.querySelector('#v-screen').src.endsWith('/')")
        assert native.process.poll() is None
        probe.ordinary_click()
        assert (
            len([request for request in state.requests if request[1].endswith("/screenshot")])
            > before
        )
        page.locator("#v-text").fill("after manual")
        page.locator('#v-type button[type="submit"]').click()
        page.wait_for_timeout(100)
        assert sum(request[1].endswith("/keyboard") for request in state.requests) == 1
    finally:
        native.backend.stop("web-session", native.runtime.host_id, started.helper, time.time() + 2)
        probe.close()


# Shared native fixture already owns isolated process setup, pin verification and cleanup.
from tests.test_ext_vnc_backend import native as native  # noqa: E402, F401


def test_local_modules_parse_and_translation_keys_match(panel_server):
    playwright = pytest.importorskip("playwright.sync_api")
    origin, _ = panel_server
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch()
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Install isolated Playwright Chromium for real module checks")
            raise
        try:
            page = browser.new_page()
            page.goto(origin + "/assets/ext/panel.css")
            result = page.evaluate("""async () => {
                const { createPanel } = await import('/assets/ext/panel.js');
                const strings = {en: {}, vi: {}};
                const panel = createPanel({strings}, {auth_mode: 'legacy'});
                const { default: RFB } = await import('/assets/ext/novnc/core/rfb.js');
                return {en: Object.keys(strings.en).sort(), vi: Object.keys(strings.vi).sort(), personal: panel.personal, rfb: typeof RFB};
            }""")
            assert result["en"] == result["vi"]
            assert result["personal"] is False
            assert result["rfb"] == "function"
        finally:
            browser.close()


def test_create_defaults_explicit_os_and_admin_account_controls(panel_page):
    page, state = panel_page
    login(page)
    page.locator("#new-btn").click()
    assert page.locator("#f-os").input_value() == "linux"
    page.locator("#f-engine").select_option("chrome")
    assert page.locator("#f-os").input_value() == "windows"
    page.locator("#f-os").select_option("macos")
    page.locator("#f-engine").select_option("camoufox")
    assert page.locator("#f-os").input_value() == "macos"
    page.locator(".modal").get_by_role("button", name="Close", exact=True).click()
    page.locator("#admin-btn").click()
    previous_admin = page.locator("#admin-username").element_handle()
    page.locator("#role-user").select_option("admin")
    page.locator(".admin-user input[type=checkbox]").uncheck()
    page.locator(".admin-user").get_by_role("button", name="Save changes", exact=True).click()
    page.locator(".modal").last.get_by_role("button", name="Save changes", exact=True).click()
    page.wait_for_function("old => !old.isConnected", arg=previous_admin)
    page.locator("#admin-username").wait_for()
    assert any(
        request[0] == "PATCH"
        and request[1] == "ext/admin/users/user"
        and request[2] == {"role": "admin", "active": False}
        for request in state.requests
    )
    previous_admin = page.locator("#admin-username").element_handle()
    page.get_by_role("button", name="Reset password", exact=True).click()
    page.locator("#reset-password").fill("fixture-new-password")
    page.locator(".modal").last.get_by_role("button", name="Save changes", exact=True).click()
    page.locator(".modal").last.get_by_role("button", name="Save changes", exact=True).click()
    page.wait_for_function("old => !old.isConnected", arg=previous_admin)
    page.locator("#admin-username").wait_for()
    assert any(
        request[0] == "POST"
        and request[1] == "ext/admin/users/user/reset-password"
        and request[2] == {"password": "fixture-new-password"}
        for request in state.requests
    )
    page.locator("#admin-username").fill("fixture-new-user")
    page.locator("#admin-password").fill("fixture-password")
    previous_admin = page.locator("#admin-username").element_handle()
    page.get_by_role("button", name="Create user", exact=True).click()
    page.wait_for_function("old => !old.isConnected", arg=previous_admin)
    page.wait_for_function('document.querySelector("#admin-username")?.value === ""')
    assert any(
        request[0] == "POST"
        and request[1] == "ext/admin/users"
        and request[2]["username"] == "fixture-new-user"
        for request in state.requests
    )
    assert "fixture-password" not in page.evaluate("JSON.stringify(localStorage)")
    page.locator(".modal").get_by_role("button", name="Close", exact=True).click()
    page.locator("[data-lang=vi]").click()
    assert page.locator("#admin-btn").inner_text() == "Quản trị"
    page.locator("#admin-btn").click()
    assert "Đang chiếm: 2" in page.locator("#admin-occupancy").inner_text()


def test_late_profile_detail_cannot_open_editor_for_next_actor(panel_page):
    page, state = panel_page
    login(page)
    state.block_detail = threading.Event()
    page.get_by_role("button", name="Edit: Fixture B", exact=True).click()
    page.wait_for_timeout(100)
    page.locator("#logout-btn").click()
    page.locator("#username").wait_for(state="visible")
    login(page, "user")
    state.block_detail.set()
    page.wait_for_timeout(200)
    assert page.locator("#f-name").count() == 0
    assert page.locator(".row.body").count() == 1
    assert "Fixture B" not in page.locator("#sheet").inner_text()


def test_revoked_confirmation_cannot_submit_grants_for_next_actor(panel_page):
    page, state = panel_page
    login(page)
    page.locator("#admin-btn").click()
    page.get_by_role("button", name="Assigned profiles", exact=True).click()
    page.locator(".admin-grants input").nth(1).check()
    page.locator(".modal").last.get_by_role("button", name="Save changes", exact=True).click()
    assert page.locator(".modal").count() == 3
    state.actor = None
    page.evaluate("api('GET', '/profiles').catch(() => {})")
    page.locator("#username").wait_for(state="visible")
    login(page, "user")
    assert page.locator(".modal").count() == 0
    assert not any(request[0] == "PUT" for request in state.requests)


def test_production_app_serves_offline_panel_and_transitive_novnc_assets(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app

    monkeypatch.setenv("FOXPROFILE_EXT_ENABLED", "false")
    client = TestClient(create_app(SimpleNamespace()), base_url="http://127.0.0.1")
    for asset in (
        "panel.js",
        "panel-manual.js",
        "panel.css",
        "novnc/core/rfb.js",
        "novnc/core/display.js",
        "novnc/vendor/pako/lib/zlib/inflate.js",
    ):
        response = client.get("/assets/ext/" + asset)
        assert response.status_code == 200, asset
        assert response.content
    assert client.get("/api/v1/info").json()["auth_mode"] == "legacy"


def test_protected_bulk_stop_and_delete_use_one_authorized_route(panel_page):
    page, state = panel_page
    login(page)
    page.get_by_role("checkbox", name="Select all").check()
    page.locator(".bulk").get_by_role("button", name="Stop", exact=True).click()
    page.wait_for_timeout(100)
    assert any(
        request[0] == "POST"
        and request[1] == "ext/browser/stop-batch"
        and request[2] == {"profile_ids": ["profile-a"]}
        for request in state.requests
    )
    assert not any(request[1].endswith("/stop") for request in state.requests)
    page.locator(".bulk").get_by_role("button", name="Delete", exact=True).click()
    page.locator(".modal").last.get_by_role("button", name="Delete", exact=True).click()
    page.wait_for_timeout(100)
    assert any(
        request[0] == "POST"
        and request[1] == "ext/profiles/delete-batch"
        and request[2] == {"profile_ids": ["profile-b"]}
        for request in state.requests
    )
    assert not any(
        request[0] == "DELETE" and request[1].startswith("profiles/") for request in state.requests
    )


def test_legacy_mode_retains_token_entry_and_windows_default(panel_server):
    playwright = pytest.importorskip("playwright.sync_api")
    origin, state = panel_server
    state.mode, state.actor = "legacy", "admin"
    with playwright.sync_playwright() as runner:
        try:
            browser = runner.chromium.launch()
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Install isolated Playwright Chromium for real DOM checks")
            raise
        try:
            page = browser.new_page()
            page.add_init_script(
                "localStorage.setItem('foxprofile.lang', 'en'); localStorage.setItem('foxprofile.token', 'fixture-legacy-key')"
            )
            page.goto(origin)
            page.locator(".row.body").first.wait_for()
            assert page.locator("#username").count() == 0
            assert page.locator("#admin-btn").count() == 0
            assert page.locator("#manual-open").count() == 0
            page.locator("#new-btn").click()
            assert page.locator("#f-os").input_value() == "windows"
            assert page.evaluate("localStorage.getItem('foxprofile.token')") == "fixture-legacy-key"
            assert any(
                request[1] == "profiles"
                and request[3].get("authorization") == "Bearer fixture-legacy-key"
                for request in state.requests
            )
            page.locator(".modal").get_by_role("button", name="Close", exact=True).click()
            page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
            page.locator("#v-screen").wait_for(state="visible")
            with page.expect_response("**/api/v1/browser/Fixture%20A/page/keyboard"):
                page.locator("#v-text").fill("legacy text")
                page.locator('#v-type button[type="submit"]').click()
            page.locator("#v-close").click()
            with page.expect_response("**/api/v1/browser/Fixture%20A/stop"):
                page.locator(".row.body .main").first.click()
            page.get_by_role("button", name="Edit: Fixture B", exact=True).click()
            with page.expect_response("**/api/v1/profiles/Fixture%20B"):
                page.get_by_role("button", name="Save changes", exact=True).click()
            assert not any("id:" in request[1] for request in state.requests)
        finally:
            browser.close()


def test_detached_editor_and_row_callbacks_cannot_act_as_next_user(panel_page):
    page, state = panel_page
    login(page)
    page.evaluate(
        "window.fixtureOldLaunch = document.querySelector('.row.body:nth-last-child(1) .main')"
    )
    page.get_by_role("button", name="Edit: Fixture A", exact=True).click()
    page.locator("#f-name").fill("Fixture stale edit")
    page.evaluate("window.fixtureOldForm = document.querySelector('.modal')")
    state.actor = None
    page.evaluate("api('GET', '/profiles').catch(() => {})")
    page.locator("#username").wait_for(state="visible")
    login(page, "user")
    page.evaluate(
        "window.fixtureOldForm.dispatchEvent(new Event('submit', {cancelable: true})); window.fixtureOldLaunch.click()"
    )
    page.wait_for_timeout(100)
    assert not any(
        request[0] == "PATCH" and request[1].startswith("profiles/") for request in state.requests
    )
    assert not any(request[1] == "ext/browser/start-batch" for request in state.requests)


def test_authoritative_starting_stopping_stamps_and_embedded_fingerprint(panel_page):
    page, state = panel_page
    login(page, "user")
    assert not any(request[1].endswith("/fingerprint") for request in state.requests)
    for runtime_state, label in [
        ("reserved", "Starting"),
        ("starting", "Starting"),
        ("stopping", "Stopping"),
        ("recovering", "Recovering"),
    ]:
        state.profiles[0]["state"] = runtime_state
        page.evaluate("refresh()")
        assert page.locator(".stamp").text_content() == label
        assert page.get_by_role("button", name="View screen: Fixture A", exact=True).is_disabled()
        assert page.locator(".row.body.live").count() == 0
        stop = page.locator(".row.body .main")
        assert stop.text_content().strip() == "Stop"
        assert stop.is_disabled() is (runtime_state == "stopping")


def test_bulk_delete_retains_failed_selection_and_localized_outcome(panel_page):
    page, state = panel_page
    login(page)
    state.batch_failure = "profile-b"
    page.get_by_role("checkbox", name="Select Fixture B", exact=True).check()
    page.locator(".bulk").get_by_role("button", name="Delete", exact=True).click()
    page.locator(".modal").last.get_by_role("button", name="Delete", exact=True).click()
    page.locator("#toast").filter(has_text="Fixture B: Profile is busy.").wait_for()
    assert page.get_by_role("checkbox", name="Select Fixture B", exact=True).is_checked()


def test_capacity_validation_and_admin_mcp_guide(panel_page):
    page, state = panel_page
    login(page)
    page.locator("#mcp-btn").click()
    page.locator(".modal pre").wait_for()
    assert page.locator(".modal pre").inner_text() == "YOUR_SERVICE_TOKEN"
    assert page.locator(".modal input[type=checkbox]").count() == 0
    page.locator(".modal").get_by_role("button", name="Close", exact=True).first.click()
    page.locator("#admin-btn").click()
    assert "blocks new starts" in page.locator(".modal").inner_text()
    form = page.locator("#admin-limit").locator("xpath=ancestor::form")
    for invalid in ("0", "1.5", ""):
        page.locator("#admin-limit").fill(invalid)
        form.get_by_role("button", name="Save changes", exact=True).click()
    assert not any(req[0] == "PATCH" for req in state.requests)
    assert state.limit == 2


def test_user_global_callbacks_and_capacity_error_are_redacted(panel_page):
    page, state = panel_page
    state.profiles[0].update(state="idle", is_running=False)
    login(page, "user")
    assert "user:pass" not in page.locator("#sheet").inner_text()
    assert page.locator('a[href="/docs"]').is_hidden()
    page.evaluate(
        "document.querySelector('#admin-btn').click(); document.querySelector('#mcp-btn').click()"
    )
    assert not any(req[1].startswith(("ext/admin", "mcp/setup")) for req in state.requests)
    page.locator(".row.body .main").click()
    page.locator("#toast").filter(has_text="Shared profile limit (2) reached").wait_for()
    assert "retry" in page.locator("#toast").inner_text()
    assert "REDACTED" not in page.locator("#toast").inner_text()
    assert not any("authorization" in req[3] for req in state.requests)
    assert "fixture-csrf" not in page.evaluate("JSON.stringify(localStorage)")
    state.profiles[0].update(state="running", is_running=True)
    page.evaluate("refresh()")
    page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
    page.locator("#v-screen").wait_for(state="visible")


def test_manual_pending_back_and_repeated_close_wait_for_exact_cleanup(panel_page):
    page, state = panel_page
    login(page, "user")
    page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
    page.locator("#v-screen").wait_for(state="visible")
    state.block_manual, state.block_close = threading.Event(), threading.Event()
    try:
        page.locator("#manual-open").click()
        page.locator("#manual-back").click()
        page.evaluate("void panel.manual.close(); void panel.manual.close()")
        assert page.locator("#manual-back").is_disabled()
        assert page.locator("#v-screen").is_hidden()
        state.block_manual.set()
        page.wait_for_timeout(100)
        page.evaluate("void panel.manual.close()")
        assert page.locator("#v-screen").is_hidden()
        before = sum(req[1].endswith("/screenshot") for req in state.requests)
        state.block_close.set()
        page.locator("#v-screen").wait_for(state="visible")
        page.wait_for_function("document.querySelector('#v-screen').src.startsWith('blob:')")
        assert sum(req[1].endswith("/screenshot") for req in state.requests) == before + 1
        assert sum(req[0] == "DELETE" for req in state.requests) == 1
        page.evaluate("void panel.manual.close(); void panel.manual.close()")
        assert sum(req[0] == "DELETE" for req in state.requests) == 1
    finally:
        state.block_manual.set()
        state.block_close.set()


def test_manual_error_ignores_foreign_identity_and_restores_fresh_shot(panel_page):
    page, state = panel_page
    login(page, "user")
    page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
    page.locator("#v-screen").wait_for(state="visible")
    old_image = page.locator("#v-screen").get_attribute("src")
    state.manual_error = dict(code="MANUAL_SESSION_BUSY", message="FOREIGN PROFILE SECRET")
    page.locator("#manual-open").click()
    page.locator("#manual-status").filter(has_text="Manual access is busy").wait_for()
    page.locator("#v-screen").wait_for(state="visible")
    page.wait_for_function(
        "old => document.querySelector('#v-screen').src !== old && document.querySelector('#v-screen').src.startsWith('blob:')",
        arg=old_image,
    )
    assert "FOREIGN" not in page.locator("#viewer").inner_text()
    assert not any(req[0] == "DELETE" for req in state.requests)


def test_late_editor_detail_cannot_replace_new_private_modal(panel_page):
    page, state = panel_page
    login(page)
    state.block_detail = threading.Event()
    try:
        page.get_by_role("button", name="Edit: Fixture B", exact=True).click()
        page.locator("#new-btn").click()
        assert page.locator("#f-name").input_value() == ""
        state.block_detail.set()
        page.wait_for_timeout(150)
        assert page.locator(".modal").count() == 1
        assert page.locator("#f-name").input_value() == ""
    finally:
        state.block_detail.set()


def test_closed_private_editor_clears_credentials_and_denies_detached_submit(panel_page):
    page, state = panel_page
    login(page, "user")
    page.get_by_role("button", name="Edit: Fixture A", exact=True).click()
    page.locator("#f-proxy").wait_for()
    page.evaluate("window.fixtureClosedEditor = document.querySelector('.modal')")
    assert page.locator("#f-proxy").input_value() == "http://user:pass@192.0.2.1:8080"
    page.locator(".modal").get_by_role("button", name="Close", exact=True).click()
    assert page.evaluate("window.fixtureClosedEditor.querySelector('#f-proxy').value") == ""
    page.evaluate(
        "window.fixtureClosedEditor.dispatchEvent(new Event('submit', {cancelable: true}))"
    )
    page.wait_for_timeout(100)
    assert not any(req[0] == "PATCH" for req in state.requests)


def test_closed_admin_forms_cannot_mutate_or_reopen(panel_page):
    page, state = panel_page
    login(page)
    page.locator("#admin-btn").click()
    page.locator("#admin-occupancy").wait_for()
    page.evaluate("window.fixtureClosedAdmin = document.querySelector('.modal')")
    page.locator(".modal").get_by_role("button", name="Close", exact=True).click()
    page.evaluate(
        "for (const form of window.fixtureClosedAdmin.querySelectorAll('form')) form.dispatchEvent(new Event('submit', {cancelable:true})); for (const button of window.fixtureClosedAdmin.querySelectorAll('.admin-user button')) button.click()"
    )
    page.wait_for_timeout(100)
    assert page.locator(".modal").count() == 0
    assert not any(
        req[0] in {"PATCH", "PUT"} or req[1].endswith("reset-password") for req in state.requests
    )


def test_manual_return_refreshes_while_previous_screenshot_is_pending(panel_page):
    page, state = panel_page
    login(page, "user")
    blocked = state.block_screenshot = threading.Event()
    try:
        page.get_by_role("button", name="View screen: Fixture A", exact=True).click()
        page.locator("#viewer").wait_for(state="visible")
        state.manual_error = dict(code="MANUAL_SESSION_BUSY", message="Busy")
        page.locator("#manual-open").click()
        page.locator("#manual-status").filter(has_text="Manual access is busy").wait_for()
        page.wait_for_function("document.querySelector('#v-screen').src.startsWith('blob:')")
        assert sum(req[1].endswith("/screenshot") for req in state.requests) == 2
        fresh = page.locator("#v-screen").get_attribute("src")
        blocked.set()
        page.wait_for_timeout(150)
        assert page.locator("#v-screen").get_attribute("src") == fresh
    finally:
        blocked.set()


def test_late_session_probe_cannot_clear_successful_personal_login(panel_page):
    page, state = panel_page
    probes = []
    page.route("**/api/v1/ext/auth/me", lambda route: probes.append(route))
    page.evaluate("void panel.enter()")
    login(page)
    assert probes
    probes[0].fulfill(status=401, json={"detail": {"code": "AUTH_REQUIRED"}})
    page.wait_for_timeout(100)
    assert page.locator("#app").is_visible()
    assert page.locator("#personal-user").inner_text() == "admin"
    assert page.locator(".row.body").count() == 2
    assert state.actor == "admin"


def test_invalid_personal_login_clears_password_and_allows_retry(panel_page):
    page, state = panel_page
    page.route(
        "**/api/v1/ext/auth/login",
        lambda route: route.fulfill(status=401, json={"detail": {"code": "AUTH_REQUIRED"}}),
        times=1,
    )
    page.locator("#username").fill("admin")
    page.locator("#password").fill("fixture-wrong-password")
    page.locator("#login-form button").click()
    page.locator("#login-err").filter(has_text="Invalid username or password.").wait_for()
    assert page.locator("#password").input_value() == ""
    assert state.actor is None
    login(page)
    assert page.locator("#personal-user").inner_text() == "admin"
