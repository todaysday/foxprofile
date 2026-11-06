"""Private seams for attaching temporary input to an existing confined run."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..dto import ProcessIdentity, RuntimeDescriptor
from ..storage.manual import HelperLiveness


@dataclass(frozen=True, slots=True)
class ManualRuntime:
    """Trusted resolver result: current generation and verified confinement."""

    descriptor: RuntimeDescriptor = field(repr=False)
    engine: str
    confined: bool


@dataclass(frozen=True, slots=True)
class BackendSession:
    """Server-only result; neither field belongs in a public response."""

    helper: ProcessIdentity = field(repr=False)
    channel: object = field(repr=False)


class ManualBackend(Protocol):
    """Owned helper and private connected channel, never a backend listener.

    start must gate its child BEFORE X attachment until claim_helper succeeds.
    Gate EOF/parent death cancels the child. stop must also cancel pending starts;
    probe may return DEAD only when no pending or late child can attach. All
    process operations match host plus PID/start ticks, never numeric PID alone.

    An independent watchdog is armed before attachment, including before the
    first heartbeat. It records heartbeat/first-loss deadlines privately and
    releases held input, revokes streams and confirms helper exit within the
    supplied absolute deadline. Heartbeats may slide the liveness deadline only
    before loss; stop freezes the earliest deadline and cannot extend it. These
    guarantees must survive gateway death; an in-process reaper is insufficient.
    Only helper/channel resources may be stopped, never browser/display/capacity.
    """

    def start(
        self,
        session_id: str,
        runtime: RuntimeDescriptor,
        claim_helper: Callable[[ProcessIdentity], bool],
        *,
        expires_at: float,
        cleanup_deadline: float,
        watchdog_deadline: float,
    ) -> BackendSession: ...

    def connect(self, session_id: str, helper: ProcessIdentity) -> object: ...

    def heartbeat(self, session_id: str, watchdog_deadline: float) -> None: ...

    def revoke(self, session_id: str) -> None: ...

    def release_input(self, session_id: str) -> None: ...

    def stop(
        self,
        session_id: str,
        host_id: str,
        helper: ProcessIdentity | None,
        cleanup_deadline: float,
    ) -> None: ...

    def probe(
        self, session_id: str, host_id: str, helper: ProcessIdentity | None
    ) -> HelperLiveness: ...
