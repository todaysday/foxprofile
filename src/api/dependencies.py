from __future__ import annotations

import hmac
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from foxprofile_ext.config import extension_enabled
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind
from foxprofile_ext.errors import DomainError, ErrorCode, ProfileCapacityExceeded

from . import auth

PERSONAL_SESSION_COOKIE = "foxprofile_session"
CSRF_HEADER = "X-CSRF-Token"


if TYPE_CHECKING:
    from ..core.container import Container
    from ..core.events import EventBus
    from ..interfaces import IBrowserLauncher, IProfileManager, IProxyService


def _container(request: Request) -> Container:
    return request.app.state.container


def domain_http_error(error: DomainError) -> HTTPException:
    status = {
        ErrorCode.AUTH_REQUIRED: 401,
        ErrorCode.FORBIDDEN_OR_NOT_FOUND: 404,
        ErrorCode.ADMIN_REQUIRED: 403,
        ErrorCode.VALIDATION_ERROR: 422,
        ErrorCode.PROFILE_CAPACITY_EXCEEDED: 409,
        ErrorCode.PROFILE_BUSY: 409,
        ErrorCode.MANUAL_SESSION_BUSY: 409,
        ErrorCode.MANUAL_UNSUPPORTED_RUNTIME: 409,
        ErrorCode.STALE_BROWSER_RUN: 409,
        ErrorCode.MANUAL_SESSION_CLOSED: 410,
        ErrorCode.RUNTIME_RECOVERING: 503,
    }[error.code]
    detail = {"code": error.code.value, "message": error.message}
    if isinstance(error, ProfileCapacityExceeded):
        detail["max_concurrent_profiles"] = error.max_concurrent_profiles
    return HTTPException(status_code=status, detail=detail)


def _has_service_credential(request: Request) -> bool:
    authorization = request.headers.get("Authorization", "")
    return authorization.lower().startswith("bearer ") or "X-API-Key" in request.headers


def _service_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def _request_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def require_exact_origin(request: Request) -> None:
    origin = request.headers.get("Origin", "")
    try:
        parsed = urlsplit(origin)
    except ValueError:
        raise HTTPException(status_code=403, detail="Origin is not allowed") from None
    if (
        not origin
        or origin == "null"
        or not parsed.scheme
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin != _request_origin(request)
    ):
        raise HTTPException(status_code=403, detail="Origin is not allowed")


def get_request_principal(request: Request) -> PrincipalDTO:
    """Authenticate a service bearer or live personal cookie without mixing them."""
    if not extension_enabled():
        raise HTTPException(status_code=401, detail="Authentication required")

    container = _container(request)
    protected = container.protected_application
    try:
        if _has_service_credential(request):
            token = _service_token(request)
            if not auth.API_TOKEN or not hmac.compare_digest(token, auth.API_TOKEN):
                raise HTTPException(
                    status_code=401,
                    detail="Missing or invalid API token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            principal = PrincipalDTO("trusted-service", PrincipalKind.SERVICE)
            return protected.current_principal(principal)

        session_token = request.cookies.get(PERSONAL_SESSION_COOKIE, "")
        authenticated = container.auth_service.resolve_session(session_token)
        request.state.personal_session = authenticated
        return protected.current_principal(authenticated.principal)
    except DomainError as error:
        raise domain_http_error(error) from error


def get_optional_principal(request: Request) -> PrincipalDTO | None:
    """Keep the legacy API dependency-free while loading principal in extension mode."""
    if not extension_enabled():
        return None
    return get_request_principal(request)


def perform_profile(request: Request, principal: PrincipalDTO, identifier: str, effect):
    """Run a trusted route adapter only after current identity and grant checks."""
    try:
        return request.app.state.container.protected_application.perform_profile(
            principal, identifier, effect
        )
    except DomainError as error:
        raise domain_http_error(error) from error


def require_cookie_csrf(
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> None:
    """Require same-origin and a live session-bound token for cookie mutations."""
    if request.method.upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return
    if principal.kind is PrincipalKind.SERVICE:
        return
    require_exact_origin(request)
    session_token = request.cookies.get(PERSONAL_SESSION_COOKIE, "")
    try:
        valid = _container(request).auth_service.verify_csrf(
            session_token, request.headers.get(CSRF_HEADER)
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    if not valid:
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def get_profile_manager(
    container: Container = Depends(_container),
) -> IProfileManager:
    if extension_enabled():
        return container.profile_manager_adapter()
    return container.profile_manager


def get_browser_launcher(
    container: Container = Depends(_container),
) -> IBrowserLauncher:
    if extension_enabled():
        return container.browser_launcher_adapter()
    return container.browser_launcher


def get_proxy_service(
    container: Container = Depends(_container),
) -> IProxyService:
    if extension_enabled():
        return container.proxy_service_adapter()
    return container.proxy_service


def get_event_bus(
    container: Container = Depends(_container),
) -> EventBus:
    return container.event_bus
