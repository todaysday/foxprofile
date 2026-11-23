"""Master-only worker administration, control and bounded profile streams."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr
from starlette.concurrency import run_in_threadpool

from src.api.dependencies import domain_http_error, get_request_principal, require_cookie_csrf

from ..dto import BrowserRunRefDTO, PrincipalDTO
from ..errors import DomainError, ErrorCode
from ..manual.http import _secure_transport
from ..policy import require_admin

router = APIRouter(prefix="/workers", tags=["distributed workers"])


class DrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draining: StrictBool


class FenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: StrictStr
    evidence: StrictStr
    host_fenced: StrictBool


def _coordinator(connection):
    try:
        return connection.app.state.container.worker_coordinator
    except DomainError as error:
        raise domain_http_error(error) from error


def _admin(request, principal):
    application = request.app.state.container.protected_application
    try:
        require_admin(application.current_principal(principal))
    except DomainError as error:
        raise domain_http_error(error) from error
    return _coordinator(request)


def _credential(connection):
    _secure_transport(connection)
    if connection.query_params:
        raise HTTPException(422, detail="Query parameters are not allowed")
    supplied = connection.headers.get("authorization", "")
    if not supplied.startswith("Bearer ") or not 16 <= len(supplied[7:]) <= 512:
        raise HTTPException(401, detail="Worker credential required")
    return supplied[7:]


def _transfer_binding(request, run_id, operation):
    coordinator = _coordinator(request)
    token = _credential(request)
    with coordinator.ledger.database.read() as db:
        row = db.execute(
            "SELECT profile_id FROM browser_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, detail="Transfer unavailable")
    run = coordinator.ledger.get(BrowserRunRefDTO(row["profile_id"], run_id))
    try:
        generation = int(request.headers.get("x-connection-generation", ""))
        if (
            request.headers.get("x-worker-id"),
            request.headers.get("x-worker-boot-id"),
            generation,
        ) != (
            run.worker_id,
            run.worker_boot_id,
            run.connection_generation,
        ):
            raise DomainError(ErrorCode.STALE_RUN)
        # A live transport generation is required even if a capability survives
        # a process restart. Revocation/rotation disconnects this connection.
        coordinator.hub.connection_for(run)
        grant = coordinator.profiles.validate_grant(
            token,
            operation=operation,
            profile_id=run.ref.profile_id,
            run_id=run_id,
            worker_id=run.worker_id,
            worker_boot_id=run.worker_boot_id,
            connection_generation=run.connection_generation,
            consume=False,
        )
    except (ValueError, TypeError):
        raise HTTPException(422, detail="Invalid transfer binding") from None
    except DomainError as error:
        raise domain_http_error(error) from error
    except Exception as error:
        raise _transfer_error(error) from None
    return coordinator, run, grant


def _transfer_error(error):
    code = getattr(error, "code", ErrorCode.TRANSFER_INVALID)
    try:
        failure = DomainError(ErrorCode(code))
    except ValueError:
        failure = DomainError(ErrorCode.TRANSFER_INVALID)
    return domain_http_error(failure)


@router.websocket("/connect")
async def connect_worker(websocket: WebSocket):
    try:
        credential = _credential(websocket)
        coordinator = _coordinator(websocket)
    except HTTPException:
        await websocket.close(code=1008)
        return
    await coordinator.hub.serve(websocket, credential)


@router.get("")
def list_workers(request: Request, principal: PrincipalDTO = Depends(get_request_principal)):
    try:
        result = request.app.state.container.protected_application.admin_workers(principal)
        if result is None:
            raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
        return result
    except DomainError as error:
        raise domain_http_error(error) from error


@router.post("/{worker_id}/enroll", dependencies=[Depends(require_cookie_csrf)])
def enroll_worker(
    worker_id: str, request: Request, principal: PrincipalDTO = Depends(get_request_principal)
):
    coordinator = _admin(request, principal)
    try:
        return {"worker_id": worker_id, "credential": coordinator.workers.enroll(worker_id)}
    except (ValueError, DomainError) as error:
        raise _transfer_error(error) from None


@router.post("/{worker_id}/rotate", dependencies=[Depends(require_cookie_csrf)])
def rotate_worker(
    worker_id: str, request: Request, principal: PrincipalDTO = Depends(get_request_principal)
):
    coordinator = _admin(request, principal)
    try:
        credential = coordinator.workers.rotate(worker_id)
        coordinator.hub.disconnect(worker_id)
        return {"worker_id": worker_id, "credential": credential}
    except (ValueError, DomainError) as error:
        raise _transfer_error(error) from None


@router.post("/{worker_id}/revoke", dependencies=[Depends(require_cookie_csrf)])
def revoke_worker(
    worker_id: str, request: Request, principal: PrincipalDTO = Depends(get_request_principal)
):
    coordinator = _admin(request, principal)
    coordinator.workers.revoke(worker_id)
    coordinator.hub.disconnect(worker_id)
    return {"revoked": True}


@router.post("/{worker_id}/drain", dependencies=[Depends(require_cookie_csrf)])
def drain_worker(
    worker_id: str,
    body: DrainRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
):
    try:
        request.app.state.container.protected_application.set_worker_draining(
            principal, worker_id, body.draining
        )
    except DomainError as error:
        raise domain_http_error(error) from error
    return {"draining": body.draining}


@router.post("/runs/{run_id}/fence", dependencies=[Depends(require_cookie_csrf)])
def fence_run(
    run_id: str,
    body: FenceRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
):
    coordinator = _admin(request, principal)
    if body.host_fenced is not True:
        raise HTTPException(422, detail="Verified host shutdown evidence is required")
    run = coordinator.ledger.get(BrowserRunRefDTO(body.profile_id, run_id))
    if run is None:
        raise HTTPException(404, detail="Run unavailable")
    try:
        return {"released": coordinator.fence(run, body.evidence)}
    except DomainError as error:
        raise domain_http_error(error) from error


@router.get("/runs/{run_id}/snapshot")
def download_snapshot(run_id: str, request: Request):
    coordinator, run, grant = _transfer_binding(request, run_id, "download")
    try:
        snapshot = coordinator.profiles.get_snapshot(run_id)
        if snapshot is None:
            raise DomainError(ErrorCode.TRANSFER_INVALID)
        return FileResponse(
            snapshot.path,
            media_type="application/zip",
            headers={"Cache-Control": "no-store", "X-Archive-Digest": snapshot.archive_digest},
        )
    except Exception as error:
        raise _transfer_error(error) from None


@router.post("/runs/{run_id}/upload")
async def upload_snapshot(run_id: str, request: Request):
    coordinator, run, grant = await run_in_threadpool(_transfer_binding, request, run_id, "upload")
    expected = request.headers.get("x-archive-digest", "")
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise HTTPException(422, detail="Archive checksum required")
    if expected != grant.archive_digest:
        raise HTTPException(409, detail="Archive differs from the granted upload")
    limit = coordinator.profiles.max_archive_bytes
    try:
        if int(request.headers.get("content-length", "0")) > limit:
            raise HTTPException(413, detail="Archive exceeds limit")
    except ValueError:
        raise HTTPException(422, detail="Invalid content length") from None
    fd, pathname = tempfile.mkstemp(
        prefix="upload-", suffix=".zip", dir=coordinator.profiles.staging_dir
    )
    path = Path(pathname)
    digest = hashlib.sha256()
    count = 0
    try:
        with os.fdopen(fd, "wb") as stream:
            async for chunk in request.stream():
                count += len(chunk)
                if count > limit:
                    raise HTTPException(413, detail="Archive exceeds limit")
                digest.update(chunk)
                await run_in_threadpool(stream.write, chunk)
            await run_in_threadpool(stream.flush)
            await run_in_threadpool(os.fsync, stream.fileno())
        if digest.hexdigest() != expected:
            raise HTTPException(422, detail="Archive checksum mismatch")
        # Recheck generation/revocation after the last byte, before promotion.
        await run_in_threadpool(_transfer_binding, request, run_id, "upload")
        try:
            return await run_in_threadpool(coordinator.commit_upload, run, path, expected)
        except Exception as error:
            raise _transfer_error(error) from None
    finally:
        path.unlink(missing_ok=True)


@router.websocket("/manual/{relay_id}")
async def manual_relay(websocket: WebSocket, relay_id: str):
    try:
        token = _credential(websocket)
        coordinator = _coordinator(websocket)
        relay = coordinator.manual_relay
        await relay.serve(websocket, relay_id, token)
    except (HTTPException, DomainError, AttributeError):
        await websocket.close(code=1008)
