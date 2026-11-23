"""Private outbound RFB relay under the existing single manual-session lease."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field

from anyio import CancelScope

from ..dto import BrowserRunRefDTO, ProcessIdentity
from ..errors import DomainError, ErrorCode
from ..manual.contracts import BackendSession
from ..manual.private_channel import PrivateChannel
from ..storage.manual import HelperLiveness, ManualLeaseDAO


@dataclass
class _Relay:
    run: object
    token_hash: str = field(repr=False)
    channel: PrivateChannel = field(repr=False)
    peer: socket.socket = field(repr=False)
    expires_at: float
    consumed: bool = False


class ManualRelay:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._relays: dict[str, _Relay] = {}
        self._lock = threading.RLock()

    def create(self, run):
        relay_id, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        left, right = socket.socketpair()
        channel = PrivateChannel(left)
        relay = _Relay(
            run,
            hashlib.sha256(token.encode()).hexdigest(),
            channel,
            right,
            time.time() + 60,
        )
        with self._lock:
            self._relays[relay_id] = relay
        channel.on_revoke(lambda: self.revoke(relay_id))
        url = self.coordinator.public_url.replace("https://", "wss://", 1)
        return relay_id, channel, {"url": f"{url}/api/v1/workers/manual/{relay_id}", "token": token}

    def revoke(self, relay_id):
        with self._lock:
            relay = self._relays.pop(relay_id, None)
        if relay:
            relay.channel.revoke()
            with contextlib.suppress(OSError):
                relay.peer.shutdown(socket.SHUT_RDWR)
            relay.peer.close()

    async def serve(self, websocket, relay_id, token):
        with self._lock:
            relay = self._relays.get(relay_id)
            if (
                relay is None
                or relay.consumed
                or relay.expires_at <= time.time()
                or not hmac.compare_digest(
                    relay.token_hash, hashlib.sha256(token.encode()).hexdigest()
                )
            ):
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        await asyncio.to_thread(self.coordinator.hub.connection_for, relay.run)
        with self._lock:
            if (
                self._relays.get(relay_id) is not relay
                or relay.consumed
                or relay.expires_at <= time.time()
            ):
                raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
            relay.consumed = True
        loop = asyncio.get_running_loop()

        async def incoming():
            while True:
                data = await websocket.receive_bytes()
                if len(data) > 65536:
                    raise DomainError(ErrorCode.VALIDATION_ERROR)
                await asyncio.to_thread(self.coordinator.hub.connection_for, relay.run)
                await loop.sock_sendall(relay.peer, data)

        async def outgoing():
            while True:
                data = await loop.sock_recv(relay.peer, 65536)
                if not data:
                    return
                await asyncio.to_thread(self.coordinator.hub.connection_for, relay.run)
                await websocket.send_bytes(data)

        tasks = []
        try:
            await websocket.accept()
            relay.peer.setblocking(False)
            tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            with CancelScope(shield=True):
                self.revoke(relay_id)
                for task in tasks:
                    task.cancel()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(websocket.close(), 5)


@dataclass
class _RemoteSession:
    run: object
    helper: ProcessIdentity | None = None
    channel: PrivateChannel | None = None
    relay_id: str | None = None
    stopped: bool = False
    last_heartbeat: float = 0


class DistributedManualBackend:
    """Choose only the already-assigned host; never create a browser or slot."""

    def __init__(self, coordinator, local_factory):
        self.coordinator = coordinator
        self.local_factory = local_factory
        self._local_backend = None
        self.relay = coordinator.manual_relay
        self._sessions: dict[str, _RemoteSession] = {}
        self._lock = threading.RLock()
        self.leases = ManualLeaseDAO(coordinator.ledger.database)

    def prepare_local(self) -> None:
        """Validate and cache the local backend before a new local lease can be acquired."""
        with self._lock:
            if self._local_backend is None:
                self._local_backend = self.local_factory()

    @property
    def local(self):
        self.prepare_local()
        return self._local_backend

    def _session(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            return session
        lease = self.leases.get(session_id)
        if lease is None:
            return None
        run = self.coordinator.ledger.get(BrowserRunRefDTO(lease.profile_id, lease.browser_run_id))
        if run is None or run.worker_id is None:
            return None
        # A Master restart loses its socket, not proof of helper liveness.
        session = _RemoteSession(run, lease.helper)
        with self._lock:
            return self._sessions.setdefault(session_id, session)

    def start(self, session_id, runtime, claim_helper, **deadlines):
        run = self.coordinator.ledger.get(runtime.run)
        if run is None:
            raise DomainError(ErrorCode.STALE_RUN)
        if run.worker_id is None:
            return self.local.start(session_id, runtime, claim_helper, **deadlines)
        relay_id, channel, relay_grant = self.relay.create(run)
        session = _RemoteSession(run, channel=channel, relay_id=relay_id)
        with self._lock:
            self._sessions[session_id] = session
        try:
            result = self.coordinator.hub.command(
                run,
                "manual_open",
                {
                    "session_id": session_id,
                    "relay": relay_grant,
                    **deadlines,
                },
                timeout=15,
            )
            helper = ProcessIdentity(**result["helper"])
            session.helper = helper
            # Worker keeps its helper behind a gate until this durable claim.
            if not claim_helper(helper):
                raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
            self.coordinator.hub.command(
                run, "manual_activate", {"session_id": session_id}, timeout=15
            )
            return BackendSession(helper, channel)
        except Exception:
            self.relay.revoke(relay_id)
            with contextlib.suppress(DomainError):
                self.stop(
                    session_id, runtime.host_id, session.helper, deadlines["cleanup_deadline"]
                )
            raise

    def connect(self, session_id, helper):
        session = self._session(session_id)
        if session is None:
            return self.local.connect(session_id, helper)
        if session.helper != helper or session.channel is None or session.channel.revoked:
            raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
        self.coordinator.hub.connection_for(session.run)
        return session.channel

    def heartbeat(self, session_id, watchdog_deadline):
        session = self._session(session_id)
        if session is None:
            return self.local.heartbeat(session_id, watchdog_deadline)
        self.coordinator.hub.connection_for(session.run)
        if time.monotonic() - session.last_heartbeat >= 5:
            self.coordinator.hub.command(
                session.run,
                "manual_heartbeat",
                {
                    "session_id": session_id,
                    "watchdog_deadline": watchdog_deadline,
                },
                timeout=5,
            )
            session.last_heartbeat = time.monotonic()

    def revoke(self, session_id):
        session = self._session(session_id)
        if session is None:
            return self.local.revoke(session_id)
        if session.relay_id:
            self.relay.revoke(session.relay_id)

    def release_input(self, session_id):
        session = self._session(session_id)
        if session is None:
            return self.local.release_input(session_id)
        self.coordinator.hub.command(
            session.run, "manual_release", {"session_id": session_id}, timeout=5
        )

    def stop(self, session_id, host_id, helper, cleanup_deadline):
        session = self._session(session_id)
        if session is None:
            return self.local.stop(session_id, host_id, helper, cleanup_deadline)
        if host_id != session.run.host_id or (session.helper and helper != session.helper):
            raise DomainError(ErrorCode.STALE_RUN)
        self.revoke(session_id)
        if self.coordinator.remote_helpers_stopped(session.run):
            session.stopped = True
            return
        result = self.coordinator.hub.command(
            session.run,
            "manual_close",
            {
                "session_id": session_id,
                "cleanup_deadline": cleanup_deadline,
            },
            timeout=max(1, min(10, cleanup_deadline - time.time())),
        )
        session.stopped = isinstance(result, dict) and result.get("stopped") is True

    def probe(self, session_id, host_id, helper):
        session = self._session(session_id)
        if session is None:
            return self.local.probe(session_id, host_id, helper)
        if host_id != session.run.host_id or (session.helper and helper != session.helper):
            return HelperLiveness.UNKNOWN
        if session.stopped or self.coordinator.remote_helpers_stopped(session.run):
            return HelperLiveness.DEAD
        try:
            result = self.coordinator.hub.command(
                session.run, "manual_probe", {"session_id": session_id}, timeout=5
            )
            return HelperLiveness(result["liveness"])
        except (DomainError, KeyError, ValueError, TypeError):
            return HelperLiveness.UNKNOWN
