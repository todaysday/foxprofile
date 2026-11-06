from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from .config import LOG_DIR, LOG_LEVEL
from .events import EventBus
from .logging import setup_logging

if TYPE_CHECKING:
    from foxprofile_ext.application import ProtectedApplication
    from foxprofile_ext.auth import AuthService
    from foxprofile_ext.manual.factory import ManualGateway
    from foxprofile_ext.run_contracts import RunCoordinator


@dataclass(frozen=True, slots=True)
class _ExtensionServices:
    auth_service: AuthService
    protected_application: ProtectedApplication
    manual_gateway_factory: Callable[[], ManualGateway]


class _PrincipalScopedAdapter:
    """Internal upstream adapter whose operations require a live scoped actor."""

    __slots__ = ("__service", "__application", "__kind")

    def __init__(self, service: object, application: ProtectedApplication, kind: str) -> None:
        self.__service = service
        self.__application = application
        self.__kind = kind

    def __getattr__(self, name: str):
        from foxprofile_ext.errors import DomainError, ErrorCode
        from foxprofile_ext.request_context import current_principal

        principal = self.__application.current_principal(current_principal())
        if name.startswith("_"):
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        if self.__kind == "profile_manager" and name == "profiles":
            visible = self.__application.list_profiles(principal)
            allowed_names = {profile.name for profile in visible}
            return {
                name: replace(profile)
                for name, profile in self.__service.profiles.items()
                if name in allowed_names
            }
        allowed_methods = {
            "profile_manager": {"list_profiles"},
            "browser_launcher": {
                "exclusive",
                "is_running",
                "running_count",
                "running_profile_names",
                "wait_for_launch",
            },
            "proxy_service": {"check_proxy_sync"},
        }
        if name not in allowed_methods[self.__kind]:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)

        attribute = getattr(self.__service, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            actor = self.__application.current_principal(current_principal())
            if self.__kind == "profile_manager":
                if name == "list_profiles":
                    visible_ids = {
                        profile.profile_id for profile in self.__application.list_profiles(actor)
                    }
                    return [
                        replace(profile)
                        for profile in attribute(*args, **kwargs)
                        if profile.profile_id in visible_ids
                    ]
            elif self.__kind == "browser_launcher":
                if name in {"is_running", "wait_for_launch", "exclusive"}:
                    identifier = args[0] if args else kwargs.get("profile_name")
                    ref = self.__application.resolve_profile(actor, identifier)
                    if name == "is_running":
                        return any(
                            profile.profile_id == ref.profile_id
                            for profile in self.__application.running_profiles(actor)
                        )
                elif name == "running_profile_names":
                    visible_names = {
                        profile.name for profile in self.__application.running_profiles(actor)
                    }
                    return set(attribute(*args, **kwargs)) & visible_names
                elif name == "running_count":
                    return self.__application.running_count(actor)
            return attribute(*args, **kwargs)

        return call


class Container:
    def __init__(self, *, run_coordinator: RunCoordinator | None = None) -> None:
        setup_logging(LOG_DIR, getattr(logging, LOG_LEVEL, logging.INFO))
        self.__instances: dict = {}
        self.__run_coordinator = run_coordinator
        from foxprofile_ext.config import extension_enabled

        self.__extension_enabled = extension_enabled()
        # The first API requests arrive on several worker threads at once; each
        # service must be built exactly once or they would hold separate state.
        self.__lock = threading.RLock()
        if self.__extension_enabled:
            from foxprofile_ext.compatibility import require_compatible_hooks

            require_compatible_hooks()
            # Build identity, catalog, and recovery hooks before serving requests.
            self._get("ext", self._build_extension_services)

    def _get(self, key: str, build: Callable[[], object]):
        if self.__extension_enabled and key in {"pm", "bl", "ps"}:
            from foxprofile_ext.errors import DomainError, ErrorCode

            raise DomainError(ErrorCode.AUTH_REQUIRED)
        return self.__get_instance(key, build)

    def __get_instance(self, key: str, build: Callable[[], object]):
        instance = self.__instances.get(key)
        if instance is None:
            with self.__lock:
                instance = self.__instances.get(key)
                if instance is None:
                    instance = self.__instances[key] = build()
        return instance

    @property
    def event_bus(self) -> EventBus:
        return self._get("eb", EventBus)

    @property
    def profile_manager(self) -> IProfileManager:
        self._require_upstream_services_disabled()
        return self._get_profile_manager()

    def _get_profile_manager(self) -> IProfileManager:
        self._require_upstream_services_disabled()
        return self.__get_profile_manager_internal()

    def __get_profile_manager_internal(self):
        from ..services.profile.manager import ProfileManager

        return self.__get_instance("pm", ProfileManager)

    @property
    def browser_launcher(self) -> IBrowserLauncher:
        self._require_upstream_services_disabled()
        return self._get_browser_launcher()

    def _get_browser_launcher(self) -> IBrowserLauncher:
        self._require_upstream_services_disabled()
        return self.__get_browser_launcher_internal()

    def __get_browser_launcher_internal(self):
        from ..services.browser.launcher import BrowserLauncher

        return self.__get_instance("bl", BrowserLauncher)

    @property
    def proxy_service(self) -> IProxyService:
        self._require_upstream_services_disabled()
        return self._get_proxy_service()

    def _get_proxy_service(self) -> IProxyService:
        self._require_upstream_services_disabled()
        return self.__get_proxy_service_internal()

    def __get_proxy_service_internal(self):
        from ..services.proxy.service import ProxyService

        return self.__get_instance("ps", ProxyService)

    @property
    def auth_service(self) -> AuthService:
        """Personal auth use cases for the HTTP login/admin adapters."""
        return self._extension_services.auth_service

    @property
    def protected_application(self) -> ProtectedApplication:
        """Shared profile-policy facade for HTTP and direct application callers."""
        return self._extension_services.protected_application

    @property
    def manual_gateway(self) -> ManualGateway:
        """Build temporary manual access only when its first request arrives."""
        return self._get("manual", self._extension_services.manual_gateway_factory)

    def profile_manager_adapter(self):
        services = self._extension_services
        return _PrincipalScopedAdapter(
            self.__get_profile_manager_internal(), services.protected_application, "profile_manager"
        )

    def browser_launcher_adapter(self):
        services = self._extension_services
        return _PrincipalScopedAdapter(
            self.__get_browser_launcher_internal(),
            services.protected_application,
            "browser_launcher",
        )

    def proxy_service_adapter(self):
        services = self._extension_services
        return _PrincipalScopedAdapter(
            self.__get_proxy_service_internal(), services.protected_application, "proxy_service"
        )

    @property
    def _extension_services(self) -> _ExtensionServices:
        if not self.__extension_enabled:
            from foxprofile_ext.errors import DomainError, ErrorCode

            raise DomainError(ErrorCode.AUTH_REQUIRED)
        return self._get("ext", self._build_extension_services)

    def _require_upstream_services_disabled(self) -> None:
        if self.__extension_enabled:
            from foxprofile_ext.errors import DomainError, ErrorCode

            raise DomainError(ErrorCode.AUTH_REQUIRED)

    def shutdown(self, *, timeout: float = 30.0) -> None:
        """Stop owned browser children through their configured lifecycle owner."""
        if self.__extension_enabled:
            manual = self.__instances.get("manual")
            if manual is not None:
                manual.shutdown()
            shutdown = getattr(self.__run_coordinator, "shutdown", None)
            if callable(shutdown):
                shutdown(timeout=timeout)
            else:
                logging.getLogger(__name__).warning(
                    "Protected browser shutdown unavailable; run coordinator is not configured."
                )
            return
        launcher = self.__instances.get("bl")
        if launcher is not None:
            launcher.shutdown_all()

    def _build_extension_services(self) -> _ExtensionServices:
        from foxprofile_ext.application import ProtectedApplication
        from foxprofile_ext.auth import AuthService
        from foxprofile_ext.catalog import CatalogOperationError, ProfileCatalog
        from foxprofile_ext.config import extension_database_path
        from foxprofile_ext.storage import (
            AssignmentDAO,
            CapacityLedger,
            CatalogOperationDAO,
            ExtensionDatabase,
            SessionDAO,
            SettingsDAO,
            TransferHandleDAO,
            UserDAO,
        )

        manager = self.__get_profile_manager_internal()
        database = ExtensionDatabase(extension_database_path(manager.profiles_file).resolve())
        assignments = AssignmentDAO(database)
        catalog = ProfileCatalog(
            manager,
            CatalogOperationDAO(database),
            assignments,
            Path(manager.profiles_file).with_name("profiles.lock"),
            TransferHandleDAO(database),
        )
        catalog.recover()

        def profile_exists(profile_id: str) -> bool:
            try:
                catalog.get_profile(profile_id)
            except CatalogOperationError:
                return False
            return True

        auth_service = AuthService(
            UserDAO(database), SessionDAO(database), assignments, profile_exists
        )
        ledger = CapacityLedger(database)
        if self.__run_coordinator is None:
            from foxprofile_ext.runtime_factory import build_run_coordinator

            self.__run_coordinator = build_run_coordinator(
                database, catalog, auth_service, assignments, ledger
            )
            self.__instances["bl"] = self.__run_coordinator.launcher
        from foxprofile_ext.transfer import ManagedProfileTransfer

        transfers = ManagedProfileTransfer(
            catalog,
            TransferHandleDAO(database),
            Path(manager.profiles_file).with_name(".foxprofile-transfers"),
            lambda principal, profile_id: protected_application.perform_profile(
                principal, profile_id, replace
            ),
        )
        protected_application = ProtectedApplication(
            auth_service,
            catalog,
            assignments,
            ledger,
            self.__run_coordinator,
            settings=SettingsDAO(database),
            transfers=transfers,
        )

        def manual_gateway_factory() -> ManualGateway:
            from foxprofile_ext.manual.factory import build_manual_gateway

            return build_manual_gateway(
                database, catalog, auth_service, protected_application, self.__run_coordinator
            )

        return _ExtensionServices(auth_service, protected_application, manual_gateway_factory)
