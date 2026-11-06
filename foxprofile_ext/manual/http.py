"""Authenticated HTTP lifecycle and binary WebSocket/private-RFB bridge."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
from urllib.parse import urlsplit

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from src.api.auth import is_loopback
from src.api.dependencies import (
    PERSONAL_SESSION_COOKIE,
    domain_http_error,
    get_request_principal,
    require_cookie_csrf,
)

from ..dto import PrincipalDTO
from ..errors import DomainError, ErrorCode
from ..policy import require_personal
from ..transport import DETECTION_BUDGET_SECONDS
from .backend import VNCBackendUnavailable
from .factory import ManualGateway
from .private_channel import PrivateChannel

router = APIRouter(prefix="/ext", tags=["manual browser access"])


class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser_run_id: StrictStr


def _gateway(connection: HTTPConnection) -> ManualGateway:
    if connection.query_params:
        raise HTTPException(status_code=422, detail="Query parameters are not allowed")
    _secure_transport(connection)
    try:
        return connection.app.state.container.manual_gateway
    except VNCBackendUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail={"code": ErrorCode.RUNTIME_RECOVERING.value, "message": str(error)},
        ) from None


def _secure_transport(connection: HTTPConnection) -> None:
    peer = connection.client.host if connection.client else ""
    host = connection.url.hostname or ""
    if connection.url.scheme not in {"https", "wss"} and not (
        peer and host and is_loopback(peer) and is_loopback(host)
    ):
        raise HTTPException(status_code=403, detail="Manual access requires HTTPS/WSS")


@router.post("/profiles/{profile_id}/manual-sessions", dependencies=[Depends(require_cookie_csrf)])
def open_session(
    profile_id: str,
    request: Request,
    body: OpenRequest = OpenRequest(),
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict:
    try:
        require_personal(principal)
        # Authorize before native-configuration errors or any factory effects.
        profile = request.app.state.container.protected_application.resolve_profile(
            principal, profile_id
        )
        if profile.profile_id != profile_id:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        return asdict(_gateway(request).service.create(principal, profile_id))
    except DomainError as error:
        raise domain_http_error(error) from error


@router.get("/manual-sessions/{session_id}")
def session_status(
    session_id: str, request: Request, principal: PrincipalDTO = Depends(get_request_principal)
) -> dict:
    try:
        require_personal(principal)
        return asdict(_gateway(request).service.status(principal, session_id))
    except DomainError as error:
        raise domain_http_error(error) from error


@router.delete("/manual-sessions/{session_id}", dependencies=[Depends(require_cookie_csrf)])
def close_session(
    session_id: str,
    body: CloseRequest,
    request: Request,
    principal: PrincipalDTO = Depends(get_request_principal),
) -> dict:
    try:
        require_personal(principal)
        return asdict(_gateway(request).service.close(principal, session_id, body.browser_run_id))
    except DomainError as error:
        raise domain_http_error(error) from error


def _websocket_principal(connection: HTTPConnection) -> PrincipalDTO:
    """Re-resolve this cookie, including while a passive socket remains open."""
    if "authorization" in connection.headers or "x-api-key" in connection.headers:
        raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
    container = connection.app.state.container
    session = container.auth_service.resolve_session(
        connection.cookies.get(PERSONAL_SESSION_COOKIE, "")
    )
    return require_personal(container.protected_application.current_principal(session.principal))


def _websocket_origin(connection: HTTPConnection) -> None:
    origin = connection.headers.get("origin", "")
    try:
        parsed = urlsplit(origin)
    except ValueError:
        raise HTTPException(status_code=403, detail="Origin is not allowed") from None
    scheme = {"ws": "http", "wss": "https"}.get(connection.url.scheme, connection.url.scheme)
    expected = f"{scheme}://{connection.url.netloc}"
    if (
        not origin
        or origin == "null"
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin != expected
    ):
        raise HTTPException(status_code=403, detail="Origin is not allowed")
    if connection.query_params:
        raise HTTPException(status_code=422, detail="WebSocket query parameters are not allowed")
    _secure_transport(connection)


async def _bridge(
    websocket: WebSocket,
    gateway: ManualGateway,
    session_id: str,
    run_id: str,
    channel: PrivateChannel,
) -> None:
    loop = asyncio.get_running_loop()
    revoked = asyncio.Event()

    def on_revoke() -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(revoked.set)

    channel.on_revoke(on_revoke)
    channel.socket.setblocking(False)

    def validate() -> None:
        principal = _websocket_principal(websocket)
        gateway.service.before_forward(principal, session_id, run_id)

    async def input_chunks() -> None:
        while True:
            data = await websocket.receive_bytes()
            await run_in_threadpool(validate)
            if channel.revoked:
                return
            await loop.sock_sendall(channel.socket, data)

    async def output_chunks() -> None:
        while True:
            data = await loop.sock_recv(channel.socket, 65536)
            if not data:
                return
            await run_in_threadpool(validate)
            if channel.revoked:
                return
            await websocket.send_bytes(data)

    async def idle_validation() -> None:
        while True:
            await asyncio.sleep(0.25)
            await run_in_threadpool(validate)

    tasks = [
        asyncio.create_task(fn())
        for fn in (
            input_chunks,
            output_chunks,
            idle_validation,
            revoked.wait,
        )
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        with CancelScope(shield=True):
            channel.revoke()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/manual-sessions/{session_id}/ws")
async def session_websocket(websocket: WebSocket, session_id: str) -> None:
    accepted = False
    try:
        # Never clean an owner's lease for an unapproved Origin/identity/locator.
        _websocket_origin(websocket)
        principal = await run_in_threadpool(_websocket_principal, websocket)
        gateway = await run_in_threadpool(_gateway, websocket)
        session = await run_in_threadpool(gateway.service.status, principal, session_id)
        run_id = session.browser_run_id
        with gateway.claim_stream(session_id):
            channel = await run_in_threadpool(
                gateway.service.connect, principal, session_id, run_id
            )
            try:
                if not isinstance(channel, PrivateChannel):
                    raise DomainError(ErrorCode.RUNTIME_RECOVERING)
                protocol = "binary" if "binary" in websocket.scope.get("subprotocols", []) else None
                await websocket.accept(subprotocol=protocol)
                accepted = True
                await _bridge(websocket, gateway, session_id, run_id, channel)
            finally:
                # Keep stream ownership until cancellation-safe cleanup completes.
                with CancelScope(shield=True):
                    with contextlib.suppress(DomainError):
                        await run_in_threadpool(
                            gateway.service.disconnect,
                            principal,
                            session_id,
                            run_id,
                            detection_budget_seconds=DETECTION_BUDGET_SECONDS,
                        )
    except (DomainError, HTTPException) as error:
        if not accepted:
            failure = domain_http_error(error) if isinstance(error, DomainError) else error
            await websocket.send_denial_response(
                JSONResponse(
                    status_code=failure.status_code,
                    content={"detail": failure.detail},
                )
            )
    except (WebSocketDisconnect, OSError, RuntimeError, KeyError):
        pass
    finally:
        with CancelScope(shield=True):
            if accepted:
                with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
                    await websocket.close(code=1000)
