from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from foxprofile_ext.dto import PrincipalDTO, PrincipalKind, Role
from foxprofile_ext.errors import DomainError

from ..auth import is_loopback
from ..dependencies import (
    PERSONAL_SESSION_COOKIE,
    _container,
    domain_http_error,
    get_request_principal,
    require_cookie_csrf,
    require_exact_origin,
)

router = APIRouter(prefix="/ext", tags=["personal authentication"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: StrictStr
    password: StrictStr


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: StrictStr
    password: StrictStr
    role: Role = Role.USER


class UserStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    active: StrictBool | None = None


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: StrictStr


class ProfileAssignmentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_ids: list[StrictStr]


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent_profiles: StrictInt


def _public_user(user) -> dict[str, object]:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "role": user.role.value,
        "active": user.active,
    }


def _personal(principal: PrincipalDTO) -> None:
    if principal.kind is not PrincipalKind.PERSONAL:
        raise HTTPException(status_code=404, detail="Resource unavailable")


def _check_login_transport(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    url_host = request.url.hostname or ""
    local_request = (
        bool(client_host) and bool(url_host) and is_loopback(client_host) and is_loopback(url_host)
    )
    if request.url.scheme != "https" and not local_request:
        raise HTTPException(status_code=403, detail="Personal login requires HTTPS")
    require_exact_origin(request)


@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    """Issue an opaque personal session cookie; credentials never become a token response."""
    _check_login_transport(request)
    try:
        issued = _container(request).auth_service.login(body.username, body.password)
    except DomainError as error:
        raise domain_http_error(error) from error

    response.set_cookie(
        PERSONAL_SESSION_COOKIE,
        issued.session_token,
        max_age=12 * 60 * 60,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return {"user": _public_user(issued.user), "csrf_token": issued.csrf_token}


@router.get("/auth/me")
def current_user(
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, object]:
    _personal(principal)
    try:
        user = _container(request).auth_service.current_user(principal)
    except DomainError as error:
        raise domain_http_error(error) from error
    session = request.state.personal_session
    return {"user": _public_user(user), "csrf_token": session.csrf_token}


@router.post("/auth/logout", dependencies=[Depends(require_cookie_csrf)])
def logout(
    request: Request,
    response: Response,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, bool]:
    _personal(principal)
    try:
        _container(request).auth_service.logout(principal)
    except DomainError as error:
        raise domain_http_error(error) from error
    response.delete_cookie(
        PERSONAL_SESSION_COOKIE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return {"success": True}


@router.get("/admin/users")
def list_users(
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, list[dict[str, object]]]:
    try:
        users = _container(request).auth_service.list_users(principal)
    except DomainError as error:
        raise domain_http_error(error) from error
    return {"users": [_public_user(user) for user in users]}


@router.post("/admin/users", status_code=201, dependencies=[Depends(require_cookie_csrf)])
def create_user(
    body: UserCreateRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, object]:
    try:
        user = _container(request).auth_service.create_user(
            principal, body.username, body.password, body.role
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    return {"user": _public_user(user)}


@router.patch("/admin/users/{user_id}", dependencies=[Depends(require_cookie_csrf)])
def update_user(
    user_id: str,
    body: UserStateRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, object]:
    try:
        user = _container(request).auth_service.update_user_state(
            principal, user_id, role=body.role, active=body.active
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    return {"user": _public_user(user)}


@router.post(
    "/admin/users/{user_id}/reset-password",
    dependencies=[Depends(require_cookie_csrf)],
)
def reset_password(
    user_id: str,
    body: PasswordResetRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, object]:
    try:
        user = _container(request).auth_service.reset_password(principal, user_id, body.password)
    except DomainError as error:
        raise domain_http_error(error) from error
    return {"user": _public_user(user)}


@router.get("/admin/users/{user_id}/profile-assignments")
def get_profile_assignments(
    user_id: str,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, list[str]]:
    try:
        profile_ids = _container(request).protected_application.assignments_for_user(
            principal, user_id
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    return {"profile_ids": list(profile_ids)}


@router.post("/profiles/delete-batch", dependencies=[Depends(require_cookie_csrf)])
def delete_profiles_batch(
    body: ProfileAssignmentsRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, object]:
    try:
        results = _container(request).protected_application.delete_profiles(
            principal,
            [
                f"id:{profile_id}" if profile_id.strip() else profile_id
                for profile_id in body.profile_ids
            ],
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    _container(request).event_bus.emit()
    return {"profiles": [asdict(result) for result in results], "count": len(results)}


@router.put(
    "/admin/users/{user_id}/profile-assignments",
    dependencies=[Depends(require_cookie_csrf)],
)
def replace_profile_assignments(
    user_id: str,
    body: ProfileAssignmentsRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, list[str]]:
    try:
        profile_ids = _container(request).protected_application.replace_assignments(
            principal, user_id, tuple(body.profile_ids)
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    return {"profile_ids": list(profile_ids)}


@router.get("/admin/settings")
def settings(
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, int]:
    try:
        return asdict(_container(request).protected_application.admin_settings(principal))
    except DomainError as error:
        raise domain_http_error(error) from error


@router.patch("/admin/settings", dependencies=[Depends(require_cookie_csrf)])
def update_settings(
    body: SettingsUpdateRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, int]:
    try:
        return asdict(
            _container(request).protected_application.update_admin_settings(
                principal, body.max_concurrent_profiles
            )
        )
    except DomainError as error:
        raise domain_http_error(error) from error
