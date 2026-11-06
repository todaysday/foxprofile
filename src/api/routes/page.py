"""Control the pages of a running profile: navigate, read, click, type, capture."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from foxprofile_ext.dto import PrincipalDTO
from foxprofile_ext.errors import DomainError

from ...services.browser.launcher import BrowserControlError
from ..dependencies import (
    domain_http_error,
    get_browser_launcher,
    get_optional_principal,
    get_profile_manager,
)
from ..helpers import require_profile
from ..schemas.page import (
    ClickAtRequest,
    ClickRequest,
    EvaluateRequest,
    KeyboardTypeRequest,
    NavigateRequest,
    PressRequest,
    TabNewRequest,
    TypeRequest,
    WaitRequest,
)

if TYPE_CHECKING:
    from ...interfaces import IBrowserLauncher, IProfileManager

router = APIRouter(prefix="/browser/{name}/page", tags=["page"])


def _run(
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    action: str,
    params: dict[str, Any] | None = None,
    request: Request | None = None,
    principal: PrincipalDTO | None = None,
) -> Any:
    try:
        if principal is not None and request is not None:
            return request.app.state.container.protected_application.control_run(
                principal, name, action, params or {}
            )
        require_profile(name, pm)
        return bl.control(name, action, params or {})
    except DomainError as error:
        raise domain_http_error(error) from error
    except BrowserControlError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


_PM = Depends(get_profile_manager)
_BL = Depends(get_browser_launcher)
_PRINCIPAL = Depends(get_optional_principal)


@router.post("/navigate")
def navigate(
    name: str,
    body: NavigateRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    """Open a URL (http, https or about:) in the active tab."""
    return _run(name, pm, bl, "navigate", body.model_dump(), request, principal)


@router.post("/back")
def back(
    name: str,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(name, pm, bl, "back", request=request, principal=principal)


@router.get("/snapshot")
def snapshot(
    name: str,
    request: Request,
    max_chars: int = Query(40_000, ge=500, le=200_000),
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    """Accessibility tree of the active tab, the best view for choosing selectors."""
    return _run(
        name,
        pm,
        bl,
        "snapshot",
        {"max_chars": max_chars},
        request,
        principal,
    )


@router.get("/text")
def text(
    name: str,
    request: Request,
    selector: str = "body",
    max_chars: int = Query(40_000, ge=500, le=200_000),
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(
        name,
        pm,
        bl,
        "text",
        {"selector": selector, "max_chars": max_chars},
        request,
        principal,
    )


@router.post("/click")
def click(
    name: str,
    body: ClickRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    """Click the first element matching a Playwright selector (CSS, text=, role=)."""
    return _run(name, pm, bl, "click", body.model_dump(), request, principal)


@router.post("/type")
def type_text(
    name: str,
    body: TypeRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(name, pm, bl, "type", body.model_dump(), request, principal)


@router.post("/press")
def press(
    name: str,
    body: PressRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(name, pm, bl, "press", body.model_dump(), request, principal)


@router.post("/click-at")
def click_at(
    name: str,
    body: ClickAtRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    """Click viewport coordinates (as on a non-full-page screenshot)."""
    return _run(name, pm, bl, "click_at", body.model_dump(), request, principal)


@router.post("/keyboard")
def keyboard_type(
    name: str,
    body: KeyboardTypeRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    """Type text into the focused element."""
    return _run(name, pm, bl, "keyboard_type", body.model_dump(), request, principal)


@router.post("/wait")
def wait_for(
    name: str,
    body: WaitRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(name, pm, bl, "wait_for", body.model_dump(), request, principal)


@router.get("/screenshot", responses={200: {"content": {"image/png": {}}}})
def screenshot(
    name: str,
    request: Request,
    full_page: bool = False,
    format: str = Query("png", pattern="^(png|json)$"),
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> Any:
    """PNG of the active tab; `format=json` returns it base64-encoded with url and title."""
    result = _run(
        name,
        pm,
        bl,
        "screenshot",
        {"full_page": full_page},
        request,
        principal,
    )
    if format == "json":
        return result
    return Response(
        base64.b64decode(result["png_base64"]),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/evaluate")
def evaluate(
    name: str,
    body: EvaluateRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    """Run JavaScript in the active tab and return its JSON-serialisable result."""
    return _run(name, pm, bl, "evaluate", body.model_dump(), request, principal)


@router.get("/tabs")
def tabs(
    name: str,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(name, pm, bl, "tabs", request=request, principal=principal)


@router.post("/tabs")
def tab_new(
    name: str,
    body: TabNewRequest,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(name, pm, bl, "tab_new", body.model_dump(), request, principal)


@router.post("/tabs/{index}/select")
def tab_select(
    name: str,
    index: int,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(name, pm, bl, "tab_select", {"index": index}, request, principal)


@router.delete("/tabs/{index}")
def tab_close(
    name: str,
    index: int,
    request: Request,
    pm=_PM,
    bl=_BL,
    principal: PrincipalDTO | None = _PRINCIPAL,
) -> dict:
    return _run(name, pm, bl, "tab_close", {"index": index}, request, principal)
