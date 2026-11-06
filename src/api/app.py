from __future__ import annotations

import contextlib
import pathlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..core.config import API_PORT, API_TOKEN, HEADLESS
from ..core.logging import get_logger
from . import mcp_http
from .auth import require_token
from .dependencies import extension_enabled, get_request_principal, require_cookie_csrf
from .guard import LocalOnlyGuard
from .routes import (
    browser_router,
    mcp_setup_router,
    page_router,
    profiles_router,
    proxy_router,
)
from .routes.ext_auth import router as ext_auth_router
from .routes.ext_browser import router as ext_browser_router
from .routes.ext_transfer import router as ext_transfer_router
from .schemas.common import SuccessResponse

if TYPE_CHECKING:
    from ..core.container import Container

logger = get_logger("api")

API_PREFIX = "/api/v1"
VERSION = "2.4.0"
_PANEL = pathlib.Path(__file__).resolve().parents[1] / "web" / "index.html"
_FONTS = pathlib.Path(__file__).resolve().parents[1] / "assets" / "fonts"
# Served to the panel without a token: it needs them before login.
_FONT_FILES = {f.name: f for f in _FONTS.glob("*.ttf")}


def create_app(container: Container, self_url: str | None = None) -> FastAPI:
    """Build the FastAPI application.

    `self_url` is where this process's own API is reachable over loopback; the
    MCP endpoint's tools call back into it.
    """
    ext_enabled = extension_enabled()
    mcp_route, mcp_lifespan = mcp_http.build(
        self_url or f"http://127.0.0.1:{API_PORT}",
        API_TOKEN,
        require_service=ext_enabled,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp_lifespan:
            yield

    app = FastAPI(
        lifespan=lifespan,
        title="FoxProfile API",
        description=(
            "REST API for FoxProfile (Camoufox profile manager). When "
            "FOXPROFILE_API_TOKEN is set, send it as `Authorization: Bearer <token>`."
        ),
        version=VERSION,
    )
    app.state.container = container
    app.mount(
        "/assets/ext",
        StaticFiles(
            directory=pathlib.Path(__file__).resolve().parents[2] / "foxprofile_ext" / "assets"
        ),
        name="extension-assets",
    )
    if not API_TOKEN and not ext_enabled:
        # Token-less means local-only: block CSRF and DNS rebinding from websites.
        app.add_middleware(LocalOnlyGuard)

    if ext_enabled:
        protected = [Depends(get_request_principal), Depends(require_cookie_csrf)]
        app.include_router(ext_auth_router, prefix=API_PREFIX)
        app.include_router(ext_browser_router, prefix=API_PREFIX)
        app.include_router(ext_transfer_router, prefix=API_PREFIX)
    else:
        protected = [Depends(require_token)]
    app.include_router(profiles_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(browser_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(page_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(proxy_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(mcp_setup_router, prefix=API_PREFIX, dependencies=protected)
    app.router.routes.append(mcp_route)

    @app.get(f"{API_PREFIX}/health", response_model=SuccessResponse, tags=["health"])
    def health_check() -> SuccessResponse:
        return SuccessResponse(message="FoxProfile API is running")

    @app.get(f"{API_PREFIX}/info", tags=["health"])
    def info() -> dict:
        """Public, secret-free facts the web panel needs before logging in."""
        return {
            "name": "FoxProfile",
            "version": VERSION,
            "auth_required": bool(API_TOKEN) or ext_enabled,
            "auth_mode": "personal" if ext_enabled else "legacy",
            "headless": HEADLESS,
            "mcp_path": mcp_http.MCP_PATH,
        }

    @app.get("/", include_in_schema=False)
    def panel() -> FileResponse:
        return FileResponse(_PANEL, headers={"Cache-Control": "no-store"})

    @app.get("/assets/fonts/{name}", include_in_schema=False)
    def font(name: str) -> FileResponse:
        path = _FONT_FILES.get(name)
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(
            path, media_type="font/ttf", headers={"Cache-Control": "public, max-age=604800"}
        )

    logger.info("FastAPI application created")
    return app
