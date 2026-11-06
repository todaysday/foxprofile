"""Explicit seam to the upstream browser launcher, without importing it here."""

from __future__ import annotations

from typing import Protocol

from src.models.profile import Profile


class BrowserRuntimeAdapter(Protocol):
    """Protected process and helper operations supplied by the upstream hook."""

    def spawn_browser(self, profile: Profile, profile_id: str, run_id: str) -> object: ...

    def request_stop(self, profile_id: str, run_id: str, timeout: float) -> bool: ...

    def control(
        self,
        profile_id: str,
        run_id: str,
        action: str,
        params: dict[str, object] | None,
        timeout: float,
    ) -> object: ...

    def shutdown(self, timeout: float) -> None: ...

    def run_helper(
        self,
        action: str,
        profile_name: str,
        os_type: str,
        engine: str,
        stdin: str,
        timeout: float,
    ) -> object: ...


class LaunchProgress(Protocol):
    """Optional launcher readiness surface used by wait=True calls."""

    def wait_for_launch(self, profile_name: str, timeout: float) -> tuple[bool, str | None]: ...
