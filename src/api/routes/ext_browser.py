from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from foxprofile_ext.dto import PrincipalDTO
from foxprofile_ext.errors import DomainError

from ...core.config import LAUNCH_WAIT_TIMEOUT
from ..dependencies import domain_http_error, get_request_principal, require_cookie_csrf

router = APIRouter(prefix="/ext/browser", tags=["browser"])


class BatchProfilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_ids: list[StrictStr]


class BatchStartRequest(BatchProfilesRequest):
    wait: StrictBool = False


@router.post("/start-batch", dependencies=[Depends(require_cookie_csrf)])
def start_batch(
    body: BatchStartRequest,
    request: Request,
    response: Response,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, object]:
    """Authorize and admit the complete batch before any coordinator effects."""
    try:
        results = request.app.state.container.protected_application.start_profiles(
            principal,
            body.profile_ids,
            wait=body.wait,
            timeout=LAUNCH_WAIT_TIMEOUT,
        )
    except DomainError as error:
        raise domain_http_error(error) from error

    if any(result.state == "starting" for result in results):
        response.status_code = 202
    return {
        "profiles": [
            {
                "profile_id": result.profile_id,
                "name": result.profile_name,
                "browser_run_id": result.browser_run_id,
                "state": result.state,
                "error_code": result.error_code,
            }
            for result in results
        ],
        "count": len(results),
    }


@router.post("/stop-batch", dependencies=[Depends(require_cookie_csrf)])
def stop_batch(
    body: BatchProfilesRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, object]:
    try:
        results = request.app.state.container.protected_application.stop_runs(
            principal, body.profile_ids, timeout=LAUNCH_WAIT_TIMEOUT
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    request.app.state.container.event_bus.emit()
    return {"profiles": [asdict(result) for result in results], "count": len(results)}
