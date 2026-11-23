"""Bounded, correlated commands on worker-initiated control connections."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import threading
from dataclasses import dataclass, field
from uuid import uuid4

from anyio import CancelScope

from ..errors import DomainError, ErrorCode

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_PENDING_COMMANDS = 128
HEARTBEAT_INTERVAL_SECONDS = 10
LOSS_TIMEOUT_SECONDS = 30


def _run_inventory(values):
    if (
        not isinstance(values, list)
        or len(values) > 10000
        or any(
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 128
                for key in ("profile_id", "run_id")
            )
            for item in values
        )
    ):
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    result = tuple((item["profile_id"], item["run_id"]) for item in values)
    if len({run_id for _, run_id in result}) != len(result):
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    return result


@dataclass
class WorkerConnection:
    worker_id: str
    boot_id: str
    generation: int
    websocket: object = field(repr=False)
    loop: asyncio.AbstractEventLoop = field(repr=False)
    pending: dict = field(default_factory=dict, repr=False)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    dropped: bool = False


class MasterWorkerHub:
    """Private connection owner; caller authorization stays in the application.

    API use cases run in worker threads. Only this object's event loop reads or
    writes the WebSocket, so slow transfers and browser operations cannot block
    authenticated heartbeats. Connection generation fences every response.
    """

    def __init__(self, registry, *, on_disconnect=None, on_event=None):
        self.registry = registry
        self.on_disconnect = on_disconnect or (lambda *_: None)
        self.on_event = on_event or (lambda *_: None)
        self._connections: dict[str, WorkerConnection] = {}
        self._lock = threading.RLock()

    def connection_for(self, run) -> WorkerConnection:
        return self._connection_for_owner(
            run.worker_id, run.worker_boot_id, run.connection_generation
        )

    def _connection_for_owner(self, worker_id, boot_id, generation) -> WorkerConnection:
        with self._lock:
            connection = self._connections.get(worker_id)
        if (
            connection is None
            or connection.dropped
            or (
                connection.boot_id,
                connection.generation,
            )
            != (boot_id, generation)
        ):
            raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
        with self.registry.connection_guard(worker_id, boot_id, generation):
            return connection

    def command(self, run, operation: str, payload: dict | None = None, *, timeout=30.0):
        connection = self.connection_for(run)
        return self._command(connection, run, operation, payload, timeout=timeout)

    def cleanup_closed_run(self, run, connection, proof):
        """Acknowledge terminal storage only; never resume an older generation."""
        if (connection.worker_id, connection.boot_id) != (run.worker_id, run.worker_boot_id):
            raise DomainError(ErrorCode.STALE_RUN)
        current = self._connection_for_owner(
            connection.worker_id, connection.boot_id, connection.generation
        )
        return self._command(
            current, run, "cleanup", {"disposition": "closed", "proof": proof}, timeout=10
        )

    def _command(self, connection, run, operation, payload, *, timeout):
        message = {
            "type": "command",
            "command_id": uuid4().hex,
            "worker_id": connection.worker_id,
            "boot_id": connection.boot_id,
            "connection_generation": connection.generation,
            "profile_id": run.ref.profile_id,
            "run_id": run.ref.run_id,
            "operation": operation,
            "payload": payload or {},
        }
        if len(json.dumps(message).encode()) > MAX_MESSAGE_BYTES:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        request = self._request(connection, message, timeout)
        try:
            future = asyncio.run_coroutine_threadsafe(request, connection.loop)
        except RuntimeError:
            request.close()
            raise DomainError(ErrorCode.WORKER_UNAVAILABLE) from None
        try:
            return future.result(timeout=timeout + 1)
        except (concurrent.futures.TimeoutError, TimeoutError):
            future.cancel()
            raise DomainError(ErrorCode.WORKER_UNAVAILABLE) from None
        except (OSError, RuntimeError):
            future.cancel()
            raise DomainError(ErrorCode.WORKER_UNAVAILABLE) from None

    async def _request(self, connection, message, timeout):
        if len(connection.pending) >= MAX_PENDING_COMMANDS:
            raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
        command_id = message["command_id"]
        future = concurrent.futures.Future()
        connection.pending[command_id] = (message, future)
        try:
            await self._send(connection, message)
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        finally:
            connection.pending.pop(command_id, None)
            future.cancel()

    async def _send(self, connection, message):
        async with connection.send_lock:
            await asyncio.wait_for(connection.websocket.send_json(message), 10)

    async def _result(self, connection, message):
        pending = connection.pending.get(message.get("command_id"))
        if pending is None:
            return
        request, future = pending
        if future.done() or any(
            message.get(key) != request[key]
            for key in ("worker_id", "boot_id", "connection_generation", "profile_id", "run_id")
        ):
            return
        # A timeout or disconnect can settle the request while its owner check waits.
        with contextlib.suppress(asyncio.InvalidStateError):
            await asyncio.to_thread(self._publish_result, connection, message, future)

    def _publish_result(self, connection, message, future):
        try:
            # Acquire the durable guard before the short pointer lock so an unrelated
            # SQLite writer cannot block the WebSocket event loop through either lock.
            with (
                self.registry.connection_guard(
                    connection.worker_id, connection.boot_id, connection.generation
                ),
                self._lock,
            ):
                if (
                    connection.dropped
                    or self._connections.get(connection.worker_id) is not connection
                ):
                    raise DomainError(ErrorCode.STALE_RUN)
                if message.get("ok") is True:
                    future.set_result(message.get("result"))
                else:
                    try:
                        code = ErrorCode(message.get("error_code"))
                    except (TypeError, ValueError):
                        code = ErrorCode.WORKER_UNAVAILABLE
                    future.set_exception(DomainError(code))
        except DomainError as error:
            future.set_exception(error)

    async def serve(self, websocket, credential: str):
        from .metrics import NodeMetrics

        connection = None
        await websocket.accept()
        try:
            hello = await self._receive(websocket, 10, max_bytes=65536)
            if hello.get("type") != "hello":
                raise DomainError(ErrorCode.AUTH_REQUIRED)
            worker = await asyncio.to_thread(
                self.registry.register,
                hello.get("worker_id"),
                credential,
                hello.get("boot_id"),
                hello.get("manifest_digest"),
                hello.get("preflight_ok") is True,
            )
            connection = WorkerConnection(
                worker.worker_id,
                worker.boot_id,
                worker.connection_generation,
                websocket,
                asyncio.get_running_loop(),
            )
            with self._lock:
                previous = self._connections.get(worker.worker_id)
                self._connections[worker.worker_id] = connection
            if previous is not None:
                await self._drop(previous)
            await self._send(
                connection,
                {
                    "type": "welcome",
                    "worker_id": worker.worker_id,
                    "connection_generation": worker.connection_generation,
                    "heartbeat_interval": HEARTBEAT_INTERVAL_SECONDS,
                    "loss_timeout": LOSS_TIMEOUT_SECONDS,
                },
            )
            heartbeat_deadline = connection.loop.time() + LOSS_TIMEOUT_SECONDS
            while True:
                message = await self._receive(
                    websocket, max(0, heartbeat_deadline - connection.loop.time())
                )
                if any(
                    message.get(key) != expected
                    for key, expected in (
                        ("worker_id", connection.worker_id),
                        ("boot_id", connection.boot_id),
                        ("connection_generation", connection.generation),
                    )
                ):
                    raise DomainError(ErrorCode.STALE_RUN)
                with self._lock:
                    if self._connections.get(connection.worker_id) is not connection:
                        raise DomainError(ErrorCode.STALE_RUN)
                kind = message.get("type")
                if kind == "heartbeat":
                    sample = message["metrics"]
                    metrics = NodeMetrics.from_report(**sample) if sample is not None else None
                    inventory = message["active_runs"]
                    active = _run_inventory(inventory) if inventory is not None else None
                    accounted = _run_inventory(message.get("memory_accounted_runs", []))
                    if (metrics is not None and active is None) or not set(accounted).issubset(
                        active or ()
                    ):
                        raise DomainError(ErrorCode.VALIDATION_ERROR)
                    await asyncio.to_thread(
                        self.registry.heartbeat,
                        connection.worker_id,
                        connection.boot_id,
                        connection.generation,
                        metrics,
                        tuple(run_id for _, run_id in active) if active is not None else None,
                        memory_accounted_run_ids=tuple(run_id for _, run_id in accounted),
                    )
                    heartbeat_deadline = connection.loop.time() + LOSS_TIMEOUT_SECONDS
                    await self._send(
                        connection,
                        {
                            "type": "heartbeat_ack",
                            "worker_id": connection.worker_id,
                            "boot_id": connection.boot_id,
                            "connection_generation": connection.generation,
                        },
                    )
                    await asyncio.to_thread(self.on_event, connection, message)
                elif kind == "result":
                    await self._result(connection, message)
                elif kind in {"stopped", "event"}:
                    await asyncio.to_thread(self.on_event, connection, message)
                else:
                    raise DomainError(ErrorCode.VALIDATION_ERROR)
        except (DomainError, ValueError, TypeError, KeyError, TimeoutError, OSError):
            pass
        finally:
            if connection is not None:
                with CancelScope(shield=True):
                    await self._drop(connection)
            with contextlib.suppress(Exception):
                await websocket.close(code=1008)

    @staticmethod
    async def _receive(websocket, timeout, *, max_bytes=MAX_MESSAGE_BYTES):
        from starlette.websockets import WebSocketDisconnect

        try:
            text = await asyncio.wait_for(websocket.receive_text(), timeout)
        except WebSocketDisconnect:
            raise OSError("Worker disconnected") from None
        if len(text.encode()) > max_bytes:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        value = json.loads(text)
        if not isinstance(value, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        return value

    async def _drop(self, connection):
        with self._lock:
            if connection.dropped:
                return
            connection.dropped = True
            if self._connections.get(connection.worker_id) is connection:
                self._connections.pop(connection.worker_id, None)
        for _, future in tuple(connection.pending.values()):
            with contextlib.suppress(concurrent.futures.InvalidStateError):
                future.set_exception(DomainError(ErrorCode.WORKER_UNAVAILABLE))
        await asyncio.to_thread(
            self.registry.disconnect,
            connection.worker_id,
            connection.boot_id,
            connection.generation,
        )
        await asyncio.to_thread(self.on_disconnect, connection)
        with contextlib.suppress(Exception):
            await connection.websocket.close(code=1012)

    def disconnect(self, worker_id):
        with self._lock:
            connection = self._connections.get(worker_id)
        if connection:
            asyncio.run_coroutine_threadsafe(self._drop(connection), connection.loop)

    def shutdown(self):
        with self._lock:
            ids = tuple(self._connections)
        for worker_id in ids:
            self.disconnect(worker_id)
