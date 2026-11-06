"""Personal profile archives through streamed uploads and scoped downloads."""

from __future__ import annotations

import tempfile

from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from foxprofile_ext.catalog import CatalogOperationError
from foxprofile_ext.dto import PrincipalDTO
from foxprofile_ext.errors import DomainError
from foxprofile_ext.policy import require_personal

from ..dependencies import (
    domain_http_error,
    get_request_principal,
    require_cookie_csrf,
)

router = APIRouter(prefix="/ext", tags=["profile archives"])


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_data: StrictBool = True


@router.post("/profiles/import", status_code=201, dependencies=[Depends(require_cookie_csrf)])
async def import_profile(
    request: Request,
    target_profile_id: str | None = None,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, str]:
    """Receive a ZIP body; optional target ID selects an authorized overwrite."""
    if set(request.query_params) - {"target_profile_id"}:
        raise HTTPException(status_code=422, detail="Unknown import fields")
    if request.headers.get("content-type", "").split(";", 1)[0] not in {
        "application/zip",
        "application/octet-stream",
    }:
        raise HTTPException(status_code=415, detail="An application/zip body is required")
    application = request.app.state.container.protected_application
    try:
        require_personal(application.current_principal(principal))
        if target_profile_id is not None:
            application.resolve_profile(principal, target_profile_id)
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as upload:
            async for chunk in request.stream():
                upload.write(chunk)
            upload.seek(0)
            profile = await run_in_threadpool(
                application.import_profile_archive,
                principal,
                upload,
                target_profile_id=target_profile_id,
            )
    except DomainError as error:
        raise domain_http_error(error) from error
    except CatalogOperationError:
        raise HTTPException(
            status_code=422, detail="Profile archive could not be imported"
        ) from None
    request.app.state.container.event_bus.emit()
    return {"profile_id": profile.profile_id, "name": profile.name}


@router.post("/profiles/{profile_id}/export", dependencies=[Depends(require_cookie_csrf)])
def export_profile(
    profile_id: str,
    body: ExportRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict[str, str]:
    try:
        download_id = request.app.state.container.protected_application.export_profile_archive(
            principal, profile_id, include_data=body.include_data
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    return {
        "download_id": download_id,
        "profile_id": profile_id,
        "download_url": f"/api/v1/ext/downloads/{download_id}?profile_id={profile_id}",
    }


class _ArchiveResponse(Response):
    """Keep authorization, source file, and lock on one worker through delivery."""

    media_type = "application/zip"

    def __init__(self, application, principal, download_id, profile_id):
        super().__init__(headers={"Cache-Control": "no-store"})
        self.application = application
        self.principal = principal
        self.download_id = download_id
        self.profile_id = profile_id

    async def __call__(self, scope, receive, send) -> None:
        try:
            await run_in_threadpool(self._send_archive, send)
        except DomainError as error:
            response = domain_http_error(error)
            await JSONResponse({"detail": response.detail}, status_code=response.status_code)(
                scope, receive, send
            )

    def _send_archive(self, send) -> None:
        with self.application.download_profile_archive(
            self.principal, self.download_id, self.profile_id
        ) as source:
            headers = [(b"content-type", b"application/zip"), (b"cache-control", b"no-store")]
            from_thread.run(
                send, {"type": "http.response.start", "status": 200, "headers": headers}
            )
            while chunk := source.read(64 * 1024):
                from_thread.run(
                    send, {"type": "http.response.body", "body": chunk, "more_body": True}
                )
            from_thread.run(send, {"type": "http.response.body", "body": b"", "more_body": False})


@router.get("/downloads/{download_id}")
def download_profile(
    download_id: str,
    profile_id: str,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> Response:
    return _ArchiveResponse(
        request.app.state.container.protected_application, principal, download_id, profile_id
    )
