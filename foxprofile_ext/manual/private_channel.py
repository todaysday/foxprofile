"""Gateway-owned connected RFB channel; never a listening socket."""

import contextlib
import socket
import threading
from collections.abc import Callable


class PrivateChannel:
    """Binary RFB socket plus synchronous stream-revocation hooks.

    The gateway registers its WebSocket shutdown hook before forwarding bytes.
    A hook registered after revocation runs immediately. No backend credential
    or connection address exists to serialize into a public response.
    """

    def __init__(self, connection: socket.socket):
        self.socket = connection
        self._lock = threading.Lock()
        self._revoked = False
        self._hooks: list[Callable[[], None]] = []

    @property
    def revoked(self) -> bool:
        with self._lock:
            return self._revoked

    def on_revoke(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if not self._revoked:
                self._hooks.append(callback)
                return
        callback()

    def revoke(self) -> None:
        with self._lock:
            if self._revoked:
                return
            self._revoked = True
            hooks, self._hooks = self._hooks, []
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        for callback in hooks:
            try:
                callback()
            except Exception:
                # One disconnected gateway must not prevent other revocations.
                continue
