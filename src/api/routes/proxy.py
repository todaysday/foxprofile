from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request

from foxprofile_ext.dto import PrincipalDTO
from foxprofile_ext.errors import DomainError

from ...services.proxy.geo_check import check_geo
from ...utils.validation import validate_locale, validate_proxy_format, validate_timezone
from ..dependencies import (
    domain_http_error,
    get_optional_principal,
    get_proxy_service,
)
from ..schemas.proxy import GeoCheckRequest, GeoCheckResponse, ProxyCheckRequest, ProxyCheckResponse

if TYPE_CHECKING:
    from ...interfaces import IProxyService

router = APIRouter(prefix="/proxy", tags=["proxy"])


@router.post("/check", response_model=ProxyCheckResponse)
def check_proxy(
    body: ProxyCheckRequest,
    request: Request,
    ps: IProxyService = Depends(get_proxy_service),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> ProxyCheckResponse:
    if principal is not None:
        try:
            ok, message = request.app.state.container.protected_application.perform_authenticated(
                principal, lambda: ps.check_proxy_sync(body.proxy, body.timeout or 10)
            )
        except DomainError as error:
            raise domain_http_error(error) from error
    else:
        ok, message = ps.check_proxy_sync(body.proxy, body.timeout or 10)
    return ProxyCheckResponse(success=ok, message=message)


@router.post("/geo-check", response_model=GeoCheckResponse)
def geo_check(
    body: GeoCheckRequest,
    request: Request,
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> GeoCheckResponse:
    """Check an unsaved proxy / timezone / locale combination (see GET /profiles/{name}/ip-check)."""
    for validate, value in (
        (validate_proxy_format, body.proxy),
        (validate_timezone, body.timezone),
        (validate_locale, body.locale),
    ):
        valid, msg = validate(value or "")
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

    def check() -> GeoCheckResponse:
        return GeoCheckResponse.from_result(
            check_geo(body.proxy or None, body.timezone or None, body.locale or None)
        )

    if principal is not None:
        try:
            return request.app.state.container.protected_application.perform_authenticated(
                principal, check
            )
        except DomainError as error:
            raise domain_http_error(error) from error
    return check()
