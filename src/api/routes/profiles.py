from __future__ import annotations

import os
import pathlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from foxprofile_ext.catalog import CatalogOperationError
from foxprofile_ext.dto import PrincipalDTO, PrincipalKind
from foxprofile_ext.errors import DomainError

from ...core.config import DATA_DIR
from ...core.logging import get_logger
from ...models.profile import ENGINES
from ...services.browser import chrome_prefetch, cookies, fingerprint
from ...services.browser.launcher import ProfileBusyError
from ...services.proxy.geo_check import check_geo
from ...utils.validation import (
    validate_locale,
    validate_profile_name,
    validate_proxy_format,
    validate_timezone,
)
from ..dependencies import (
    domain_http_error,
    get_browser_launcher,
    get_event_bus,
    get_optional_principal,
    get_profile_manager,
    perform_profile,
)
from ..helpers import build_profile_response, profile_response, require_profile
from ..schemas.common import ErrorResponse, SuccessResponse
from ..schemas.profiles import (
    CookieImportRequest,
    CookieImportResponse,
    DataDirResponse,
    ExportRequest,
    ExportResponse,
    FingerprintResponse,
    ImportRequest,
    ImportResponse,
    ProfileCreate,
    ProfileListResponse,
    ProfileResponse,
    ProfileUpdate,
)
from ..schemas.proxy import GeoCheckResponse

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager

logger = get_logger("api.profiles")

router = APIRouter(prefix="/profiles", tags=["profiles"])

_OS_TYPES = ("windows", "macos", "linux")


def _require_os(os_type: str) -> None:
    if os_type not in _OS_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"os_type must be one of: {', '.join(_OS_TYPES)}",
        )


def _require_engine(engine: str) -> None:
    if engine not in ENGINES:
        raise HTTPException(
            status_code=400,
            detail=f"engine must be one of: {', '.join(ENGINES)}",
        )
    if engine == "chrome" and (reason := chrome_prefetch.unsupported_reason()):
        raise HTTPException(status_code=400, detail=f"Chrome engine unavailable here: {reason}")


def _require_geo(timezone: str | None, locale: str | None) -> None:
    for validate, value in ((validate_timezone, timezone), (validate_locale, locale)):
        valid, msg = validate(value or "")
        if not valid:
            raise HTTPException(status_code=400, detail=msg)


def _protected_profile_response(profile, is_running: bool) -> dict[str, object]:
    """Return only an authorized profile's own settings and stable local ID."""
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "proxy": profile.proxy,
        "os_type": profile.os_type,
        "timezone": profile.timezone,
        "locale": profile.locale,
        "engine": profile.engine,
        "is_running": is_running,
    }


@router.get("", response_model=ProfileListResponse)
def list_profiles(
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> ProfileListResponse | JSONResponse:
    if principal is not None:
        application = request.app.state.container.protected_application
        try:
            profiles = application.list_profiles(principal)
            running = {item.profile_id for item in application.running_profiles(principal)}
        except DomainError as error:
            raise domain_http_error(error) from error
        return JSONResponse(
            {
                "profiles": [
                    {
                        "profile_id": profile.profile_id,
                        "name": profile.name,
                        "is_running": profile.profile_id in running,
                    }
                    for profile in profiles
                ],
                "total": len(profiles),
            }
        )
    profiles = [profile_response(p, bl) for p in pm.list_profiles()]
    return ProfileListResponse(profiles=profiles, total=len(profiles))


@router.post(
    "",
    response_model=ProfileResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def create_profile(
    body: ProfileCreate,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> ProfileResponse:
    valid, msg = validate_profile_name(body.name)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)
    supplied = body.model_dump(exclude_unset=True)
    os_type = supplied.get("os_type")
    if os_type is None:
        # Protected-mode new Camoufox profiles use the agreed Linux persona;
        # Chrome retains the upstream Windows default. Legacy mode also stays
        # Windows when the field is omitted.
        os_type = "windows" if body.engine == "chrome" or principal is None else "linux"
    _require_os(os_type)
    _require_engine(body.engine)
    _require_geo(body.timezone, body.locale)

    if body.proxy:
        valid, msg = validate_proxy_format(body.proxy)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

    if principal is not None:
        try:
            profile = request.app.state.container.protected_application.create_profile(
                principal,
                name=body.name,
                proxy=body.proxy or "",
                os_type=os_type,
                timezone=body.timezone or None,
                locale=body.locale or None,
                engine=body.engine,
            )
        except DomainError as error:
            raise domain_http_error(error) from error
        if body.engine == "chrome":
            chrome_prefetch.start()
        bus.emit()
        logger.info("API created profile: %s", body.name)
        return JSONResponse(
            _protected_profile_response(profile, False),
            status_code=201,
        )

    if not pm.add_profile(
        body.name,
        body.proxy or "",
        os_type,
        body.timezone or None,
        body.locale or None,
        body.engine,
    ):
        raise HTTPException(status_code=409, detail="Profile already exists")

    logger.info("API created profile: %s", body.name)
    if body.engine == "chrome":
        chrome_prefetch.start()
    bus.emit()
    return build_profile_response(body.name, pm, bl)


@router.get(
    "/{name}",
    response_model=ProfileResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_profile(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> ProfileResponse | JSONResponse:
    if principal is not None:
        payload = perform_profile(
            request,
            principal,
            name,
            lambda profile: _protected_profile_response(profile, bl.is_running(profile.name)),
        )
        return JSONResponse(payload)
    require_profile(name, pm)
    return build_profile_response(name, pm, bl)


@router.patch(
    "/{name}",
    response_model=ProfileResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_profile(
    name: str,
    body: ProfileUpdate,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> ProfileResponse | JSONResponse:
    if principal is not None:
        supplied = body.model_dump(exclude_unset=True)
        try:
            current = request.app.state.container.protected_application.perform_profile(
                principal, name, lambda profile: profile.engine
            )
        except DomainError as error:
            raise domain_http_error(error) from error
        if "name" in supplied:
            valid, msg = validate_profile_name(supplied["name"])
            if not valid:
                raise HTTPException(status_code=400, detail=msg)
        if supplied.get("engine") not in (None, current):
            raise HTTPException(
                status_code=400,
                detail="A profile's engine cannot be changed; create a new profile instead",
            )
        if "os_type" in supplied:
            _require_os(supplied["os_type"])
        for field_name in ("timezone", "locale"):
            if field_name in supplied:
                _require_geo(
                    supplied[field_name] if field_name == "timezone" else None,
                    supplied[field_name] if field_name == "locale" else None,
                )
        if "proxy" in supplied and supplied["proxy"]:
            valid, msg = validate_proxy_format(supplied["proxy"])
            if not valid:
                raise HTTPException(status_code=400, detail=msg)
        try:
            updated = request.app.state.container.protected_application.update_profile(
                principal, name, {key: value for key, value in supplied.items() if key != "engine"}
            )
        except DomainError as error:
            raise domain_http_error(error) from error
        try:
            running_ids = {
                item.profile_id
                for item in request.app.state.container.protected_application.running_profiles(
                    principal
                )
            }
        except DomainError as error:
            raise domain_http_error(error) from error
        payload = _protected_profile_response(updated, updated.profile_id in running_ids)
        logger.info("API updated profile: %s", name)
        bus.emit()
        return JSONResponse(payload)
    profile = require_profile(name, pm)
    supplied = body.model_dump(exclude_unset=True)

    new_name = supplied.get("name", name)
    new_proxy = supplied.get("proxy", profile.proxy)
    new_os = supplied.get("os_type", profile.os_type)
    new_timezone = supplied.get("timezone", profile.timezone)
    new_locale = supplied.get("locale", profile.locale)

    if "name" in supplied:
        valid, msg = validate_profile_name(new_name)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)
        if new_name != name and bl.is_running(name):
            raise HTTPException(
                status_code=409,
                detail="Stop the browser before renaming",
            )

    if supplied.get("engine") not in (None, profile.engine):
        raise HTTPException(
            status_code=400,
            detail="A profile's engine cannot be changed; create a new profile instead",
        )
    if "os_type" in supplied:
        _require_os(new_os)
    _require_geo(supplied.get("timezone"), supplied.get("locale"))

    if "proxy" in supplied and new_proxy:
        valid, msg = validate_proxy_format(new_proxy)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

    if not pm.update_profile(
        name, new_name, new_proxy or "", new_os, new_timezone or None, new_locale or None
    ):
        raise HTTPException(status_code=409, detail="Update failed (name conflict?)")

    logger.info("API updated profile: %s -> %s", name, new_name)
    bus.emit()
    return build_profile_response(new_name, pm, bl)


@router.delete(
    "/{name}",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def delete_profile(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> SuccessResponse:
    if principal is not None:
        try:
            request.app.state.container.protected_application.delete_profile(principal, name)
        except DomainError as error:
            raise domain_http_error(error) from error
        bus.emit()
        logger.info("API deleted profile: %s", name)
        return SuccessResponse(message=f"Profile '{name}' deleted")
    require_profile(name, pm)
    if bl.is_running(name):
        raise HTTPException(
            status_code=409,
            detail="Stop the browser before deleting",
        )
    pm.delete_profile(name)
    logger.info("API deleted profile: %s", name)
    bus.emit()
    return SuccessResponse(message=f"Profile '{name}' deleted")


@router.get(
    "/{name}/data-dir",
    response_model=DataDirResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_data_dir(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> DataDirResponse:
    if principal is not None and principal.kind is not PrincipalKind.SERVICE:
        raise HTTPException(status_code=404, detail="Resource unavailable")
    if principal is not None:
        return perform_profile(
            request,
            principal,
            name,
            lambda profile: DataDirResponse(
                name=profile.name,
                data_dir=os.path.join(os.getcwd(), DATA_DIR, profile.name),
                exists=pathlib.Path(os.path.join(os.getcwd(), DATA_DIR, profile.name)).exists(),
            ),
        )
    require_profile(name, pm)
    data_dir = os.path.join(os.getcwd(), DATA_DIR, name)
    return DataDirResponse(
        name=name,
        data_dir=data_dir,
        exists=pathlib.Path(data_dir).exists(),
    )


@router.post(
    "/{name}/export",
    response_model=ExportResponse,
    responses={404: {"model": ErrorResponse}},
)
def export_profile(
    name: str,
    body: ExportRequest,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> ExportResponse:
    if principal is not None and principal.kind is not PrincipalKind.SERVICE:
        raise HTTPException(status_code=404, detail="Resource unavailable")
    if principal is not None:
        try:
            path = request.app.state.container.protected_application.export_service_archive(
                principal, name, body.export_dir, include_data=body.include_data
            )
        except DomainError as error:
            raise domain_http_error(error) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return ExportResponse(success=True, zip_path=path)
    require_profile(name, pm)
    if body.export_dir is None or not pathlib.Path(body.export_dir).is_dir():
        raise HTTPException(status_code=400, detail="export_dir is not a directory")

    success, result = pm.export_profile(name, body.export_dir, body.include_data)
    if success:
        logger.info("API exported profile: %s -> %s", name, result)
        return ExportResponse(success=True, zip_path=result)
    return ExportResponse(success=False, error=result)


@router.post(
    "/import",
    response_model=ImportResponse,
    responses={400: {"model": ErrorResponse}},
)
def import_profile(
    body: ImportRequest,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> ImportResponse:
    if principal is not None and principal.kind is not PrincipalKind.SERVICE:
        raise HTTPException(status_code=404, detail="Resource unavailable")
    if principal is not None:
        try:
            profile = request.app.state.container.protected_application.import_service_archive(
                principal, body.zip_path, overwrite=body.overwrite
            )
        except DomainError as error:
            raise domain_http_error(error) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except CatalogOperationError:
            raise HTTPException(
                status_code=409, detail="Profile archive could not be imported"
            ) from None
        bus.emit()
        return ImportResponse(success=True, profile_name=profile.name)
    if not pathlib.Path(body.zip_path).is_file():
        raise HTTPException(status_code=400, detail="zip_path is not a file")

    success, result = pm.import_profile(body.zip_path, body.overwrite)
    if success:
        logger.info("API imported profile: %s", result)
        bus.emit()
        return ImportResponse(success=True, profile_name=result)
    return ImportResponse(success=False, error=result)


def _require_stopped(name: str, bl: IBrowserLauncher, action: str) -> None:
    if bl.is_running(name):
        raise HTTPException(status_code=409, detail=f"Stop the browser before {action}")


_BUSY_DETAIL = "Profile is running or busy with another operation"


@router.get(
    "/{name}/cookies",
    response_class=PlainTextResponse,
    responses={
        200: {"content": {"text/plain": {}, "application/json": {}}},
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
def export_cookies(
    name: str,
    request: Request,
    format: str = Query("json", pattern="^(json|netscape)$"),
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> PlainTextResponse:
    """Export cookies as Cookie-Editor JSON or Netscape cookies.txt."""
    try:

        def export_authorized(profile):
            _require_stopped(profile.name, bl, "exporting cookies")
            with bl.exclusive(profile.name):
                return cookies.export_cookies(profile.name, profile.os_type, format, profile.engine)

        if principal is not None:
            text, count = perform_profile(request, principal, name, export_authorized)
        else:
            profile = require_profile(name, pm)
            text, count = export_authorized(profile)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=_BUSY_DETAIL) from e
    except cookies.CookieError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    media = "application/json" if format == "json" else "text/plain"
    return PlainTextResponse(text, media_type=media, headers={"X-Cookie-Count": str(count)})


@router.post(
    "/{name}/cookies",
    response_model=CookieImportResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def import_cookies(
    name: str,
    body: CookieImportRequest,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> CookieImportResponse:
    """Import cookies from JSON or Netscape text into a stopped profile."""
    try:

        def import_authorized(profile):
            _require_stopped(profile.name, bl, "importing cookies")
            with bl.exclusive(profile.name):
                return cookies.import_cookies(
                    profile.name, profile.os_type, body.content, profile.engine
                )

        if principal is not None:
            count = perform_profile(request, principal, name, import_authorized)
        else:
            profile = require_profile(name, pm)
            count = import_authorized(profile)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=_BUSY_DETAIL) from e
    except cookies.CookieError as e:
        return CookieImportResponse(success=False, error=str(e))
    logger.info("API imported %d cookies into %s", count, name)
    return CookieImportResponse(success=True, imported=count)


@router.get(
    "/{name}/fingerprint",
    response_model=FingerprintResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_fingerprint(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> FingerprintResponse:
    """Show the fingerprint the profile presents on every launch."""
    if principal is not None:

        def fingerprint_authorized(profile):
            info = fingerprint.summary(os.path.join(DATA_DIR, profile.name))
            if info is None:
                return FingerprintResponse(name=profile.name, exists=False)
            return FingerprintResponse(name=profile.name, exists=True, **info)

        return perform_profile(request, principal, name, fingerprint_authorized)
    require_profile(name, pm)
    info = fingerprint.summary(os.path.join(DATA_DIR, name))
    if info is None:
        return FingerprintResponse(name=name, exists=False)
    return FingerprintResponse(name=name, exists=True, **info)


@router.delete(
    "/{name}/fingerprint",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def reset_fingerprint(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> SuccessResponse:
    """Discard the saved fingerprint; the next launch generates a new device."""
    if principal is not None:

        def reset_authorized(profile):
            _require_stopped(profile.name, bl, "resetting the fingerprint")
            removed = fingerprint.reset(os.path.join(DATA_DIR, profile.name))
            logger.info("API reset fingerprint for %s (existed=%s)", profile.name, removed)
            return profile.name

        profile_name = perform_profile(request, principal, name, reset_authorized)
        return SuccessResponse(message=f"Fingerprint reset for '{profile_name}'")
    require_profile(name, pm)
    _require_stopped(name, bl, "resetting the fingerprint")
    removed = fingerprint.reset(os.path.join(DATA_DIR, name))
    logger.info("API reset fingerprint for %s (existed=%s)", name, removed)
    return SuccessResponse(message=f"Fingerprint reset for '{name}'")


@router.get(
    "/{name}/ip-check",
    response_model=GeoCheckResponse,
    responses={404: {"model": ErrorResponse}},
)
def check_profile_ip(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    principal: PrincipalDTO | None = Depends(get_optional_principal),
) -> GeoCheckResponse:
    """Where GeoIP sources place the profile's exit IP vs. the timezone/locale it presents.

    Goes out through the profile's proxy to Cloudflare, ipinfo and ip-api (~10s).
    """

    def check_authorized(profile):
        data_dir = os.path.join(os.getcwd(), DATA_DIR, profile.name)
        return GeoCheckResponse.from_result(
            check_geo(profile.proxy, profile.timezone, profile.locale, data_dir)
        )

    if principal is not None:
        return perform_profile(request, principal, name, check_authorized)
    profile = require_profile(name, pm)
    return check_authorized(profile)
