"""Personal desktop session and narrow adapters for existing Flet responsibilities."""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from src.models.profile import Profile

from .dto import PrincipalDTO
from .errors import DomainError, ErrorCode

if TYPE_CHECKING:
    from .application import VisibleProfileDTO


class DesktopSession:
    """Keep the personal cookie in memory; each login gets new immutable bindings."""

    def __init__(self, container):
        self.auth = container.auth_service
        self.application = container.protected_application
        self._launcher_reader = container.browser_launcher_adapter()
        self._proxy_reader = container.proxy_service_adapter()
        self._token: str | None = None
        self._csrf_token: str | None = None
        self.binding: DesktopBinding | None = None

    def login(self, username: str, password: str) -> DesktopBinding:
        issued = self.auth.login(username, password)
        self.clear()
        self._token = issued.session_token
        self._csrf_token = issued.csrf_token
        self.binding = DesktopBinding(self, issued.principal)
        return self.binding

    def clear(self) -> None:
        binding, self.binding = self.binding, None
        self._token = None
        self._csrf_token = None
        if binding is not None:
            with contextlib.suppress(DomainError):
                self.auth.logout(binding.principal)


class DesktopBinding:
    """A callback retains its original actor and cannot migrate to a later login."""

    def __init__(self, session: DesktopSession, principal: PrincipalDTO):
        self.session = session
        self.principal = principal
        self.pm = DesktopProfileManager(self)
        self.bl = DesktopBrowserLauncher(self)
        self.ps = DesktopProxyService(self)

    def actor(self) -> PrincipalDTO:
        if self.session.binding is not self:
            raise DomainError(ErrorCode.AUTH_REQUIRED)
        return self.session.application.current_principal(self.principal)

    @property
    def application(self):
        return self.session.application

    def profile(self, identifier: str) -> Profile:
        return self.application.perform_profile(self.actor(), f"id:{identifier}", replace)

    def fingerprint(self, identifier: str, *, reset: bool = False):
        if not reset:
            ref = self.application.resolve_profile(self.actor(), f"id:{identifier}")
            for profile in self.application.list_profiles(self.actor()):
                if profile.profile_id == ref.profile_id:
                    summary = profile.fingerprint
                    return summary if summary and summary["exists"] else None
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)

        from src.core.config import DATA_DIR
        from src.services.browser import fingerprint

        def effect(profile):
            if self.bl.is_running(profile.profile_id):
                raise DomainError(ErrorCode.PROFILE_BUSY)
            with self.session._launcher_reader.exclusive(profile.name):
                return fingerprint.reset(str(Path(DATA_DIR) / profile.name))

        return self.application.perform_profile(self.actor(), f"id:{identifier}", effect)

    def cookies(self, identifier: str, *, content: str | None = None, format: str = "json"):
        from src.services.browser import cookies

        def effect(profile):
            if self.bl.is_running(profile.profile_id):
                raise DomainError(ErrorCode.PROFILE_BUSY)
            with self.session._launcher_reader.exclusive(profile.name):
                if content is not None:
                    return cookies.import_cookies(
                        profile.name, profile.os_type, content, profile.engine
                    )
                return cookies.export_cookies(profile.name, profile.os_type, format, profile.engine)

        return self.application.perform_profile(self.actor(), f"id:{identifier}", effect)


class DesktopProfileManager:
    def __init__(self, binding: DesktopBinding):
        self.binding = binding

    @property
    def profiles(self) -> dict[str, VisibleProfileDTO]:
        return {profile.name: profile for profile in self.list_profiles()}

    def list_profiles(self) -> list[VisibleProfileDTO]:
        b = self.binding
        return list(b.application.list_profiles(b.actor()))

    def add_profile(self, name, proxy, os_type, timezone=None, locale=None, engine="camoufox"):
        b = self.binding
        b.application.create_profile(
            b.actor(),
            name=name,
            proxy=proxy,
            os_type=os_type,
            timezone=timezone or None,
            locale=locale or None,
            engine=engine,
        )
        return True

    def update_profile(
        self, identifier, new_name, new_proxy, new_os, new_timezone=None, new_locale=None
    ):
        b = self.binding
        b.application.update_profile(
            b.actor(),
            f"id:{identifier}",
            {
                "name": new_name,
                "proxy": new_proxy,
                "os_type": new_os,
                "timezone": new_timezone or None,
                "locale": new_locale or None,
            },
        )
        return True

    def delete_profile(self, identifier):
        b = self.binding
        b.application.delete_profile(b.actor(), f"id:{identifier}")
        return True

    def import_profile(self, zip_path, overwrite=False):
        """Read native client input; application receives a stream, never a server path."""
        if overwrite:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        b = self.binding
        actor = b.actor()
        with Path(zip_path).open("rb") as source:
            profile = b.application.import_profile_archive(actor, source)
        return True, profile.name

    def export_profile(self, identifier, export_path, include_data=True):
        b = self.binding
        profile = b.profile(identifier)
        handle = b.application.export_profile_archive(
            b.actor(), profile.profile_id, include_data=include_data
        )
        destination = Path(export_path) / f"{profile.name}.zip"
        with (
            b.application.download_profile_archive(b.actor(), handle, profile.profile_id) as source,
            destination.open("wb") as target,
        ):
            shutil.copyfileobj(source, target)
        return True, str(destination)


class DesktopBrowserLauncher:
    def __init__(self, binding: DesktopBinding):
        self.binding = binding

    def running_profiles(self):
        b = self.binding
        return b.application.running_profiles(b.actor())

    def running_profile_names(self):
        return {profile.name for profile in self.running_profiles()}

    def running_count(self):
        return len(self.running_profiles())

    def is_running(self, identifier):
        b = self.binding
        profile = b.profile(identifier)
        return any(p.profile_id == profile.profile_id for p in self.running_profiles())

    def start_profiles(self, identifiers):
        b = self.binding
        return b.application.start_profiles(
            b.actor(), tuple(f"id:{value}" for value in identifiers)
        )

    def stop_profile(self, identifier, timeout=30):
        b = self.binding
        return b.application.stop_run(b.actor(), f"id:{identifier}", timeout=timeout)

    def stop_profiles(self, identifiers):
        b = self.binding
        return b.application.stop_runs(b.actor(), tuple(f"id:{value}" for value in identifiers))


class DesktopProxyService:
    def __init__(self, binding: DesktopBinding):
        self.binding = binding

    def check_proxy_sync(self, proxy_str, timeout=10):
        b = self.binding
        return b.application.perform_authenticated(
            b.actor(), lambda: b.session._proxy_reader.check_proxy_sync(proxy_str, timeout)
        )
