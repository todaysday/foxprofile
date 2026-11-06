from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from foxprofile_ext.dto import PrincipalDTO
from foxprofile_ext.errors import DomainError

from ...core.config import LAUNCH_WAIT_TIMEOUT
from ...core.logging import get_logger
from ...services.browser.launcher import ProfileBusyError
from ..dependencies import (
    domain_http_error,
    get_browser_launcher,
    get_event_bus,
    get_optional_principal,
    get_profile_manager,
)
from ..helpers import require_profile
from ..schemas.browser import (
    BrowserStatusResponse,
    LaunchResponse,
    RunningBrowsersResponse,
)
from ..schemas.common import ErrorResponse, SuccessResponse

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager

logger = get_logger("api.browser")

router = APIRouter(prefix="/browser", tags=["browser"])


def _api_log(msg: str) -> None:
    logger.info("[browser] %s", msg)


@router.get("", response_model=RunningBrowsersResponse)
def list_running(
    request: Request,
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> RunningBrowsersResponse:
    if principal is not None:
        try:
            running = request.app.state.container.protected_application.running_profiles(principal)
        except DomainError as error:
            raise domain_http_error(error) from error
        names = sorted(profile.name for profile in running)
        return RunningBrowsersResponse(running=names, count=len(names))
    names = sorted(bl.running_profile_names())
    return RunningBrowsersResponse(running=names, count=len(names))


@router.get(
    "/{name}/status",
    response_model=BrowserStatusResponse,
    responses={404: {"model": ErrorResponse}},
)
def browser_status(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> BrowserStatusResponse:
    if principal is not None:
        try:
            state = request.app.state.container.protected_application.run_status(principal, name)
        except DomainError as error:
            raise domain_http_error(error) from error
        return BrowserStatusResponse(
            name=state.profile_name if state else name,
            is_running=state is not None and state.state in {"running", "starting"},
        )
    require_profile(name, pm)
    return BrowserStatusResponse(name=name, is_running=bl.is_running(name))


@router.post(
    "/{name}/launch",
    response_model=LaunchResponse,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        502: {"model": LaunchResponse},
    },
)
def launch_browser(
    name: str,
    response: Response,
    request: Request = None,
    wait: bool = Query(
        True,
        description=(
            "If true (default), block until the browser is ready or has failed, "
            f"up to {LAUNCH_WAIT_TIMEOUT}s (FOXPROFILE_LAUNCH_TIMEOUT). "
            "Pass wait=false to return 202 immediately and poll GET /{name}/status."
        ),
    ),
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> LaunchResponse:
    if isinstance(principal, PrincipalDTO):
        if request is None:
            raise HTTPException(status_code=503, detail="Protected request context unavailable")
        try:
            result = request.app.state.container.protected_application.start_profile(
                principal, name, wait=wait, timeout=LAUNCH_WAIT_TIMEOUT
            )
        except DomainError as error:
            raise domain_http_error(error) from error
        if result.state == "starting":
            response.status_code = 202
            return LaunchResponse(
                success=True,
                message=f"Browser for '{result.profile_name}' is starting",
            )
        if result.state == "failed":
            response.status_code = 502
            return LaunchResponse(success=False, message="Browser launch failed")
        return LaunchResponse(success=True, message=f"Browser started for '{result.profile_name}'")
    profile = require_profile(name, pm)
    if bl.is_running(name):
        raise HTTPException(status_code=409, detail="Browser already running")

    def _on_ready() -> None:
        bus.emit()

    def _on_stop() -> None:
        bus.emit()

    try:
        started = bl.start_thread(profile, _api_log, on_ready=_on_ready, on_stop=_on_stop)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail="Profile is busy") from e
    if not started:
        raise HTTPException(status_code=409, detail="Browser already running")
    logger.info("API launched browser for: %s", name)
    bus.emit()

    if not wait:
        response.status_code = 202
        return LaunchResponse(success=True, message=f"Browser launching for '{name}'")

    ok, error = bl.wait_for_launch(name, LAUNCH_WAIT_TIMEOUT)
    if ok:
        return LaunchResponse(success=True, message=f"Browser started for '{name}'")
    if error == "timeout":
        response.status_code = 202
        return LaunchResponse(
            success=True,
            message=f"Browser for '{name}' is still starting",
        )
    response.status_code = 502
    return LaunchResponse(success=False, message=f"Launch failed: {error}")


@router.post(
    "/{name}/stop",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def stop_browser(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> SuccessResponse:
    if principal is not None:
        application = request.app.state.container.protected_application
        try:
            stopped = application.stop_run(principal, name)
        except DomainError as error:
            raise domain_http_error(error) from error
        if not stopped:
            raise HTTPException(status_code=409, detail="Browser is not running")
        bus.emit()
        logger.info("API stopped browser for: %s", name)
        return SuccessResponse(message=f"Browser stopped for '{name}'")
    require_profile(name, pm)
    if not bl.is_running(name):
        raise HTTPException(status_code=409, detail="Browser is not running")

    bl.stop_profile(name)
    logger.info("API stopped browser for: %s", name)
    bus.emit()
    return SuccessResponse(message=f"Browser stopped for '{name}'")
