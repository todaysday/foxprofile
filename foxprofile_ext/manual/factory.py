"""Compose temporary manual access from trusted catalog/runtime state."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from ..application import ProtectedApplication
from ..auth import AuthService
from ..catalog import CatalogOperationError, ProfileCatalog
from ..dto import RuntimeDescriptor
from ..errors import DomainError, ErrorCode
from ..runtime.confinement import ConfinementPurpose, ConfinementSpec
from ..storage import CapacityLedger, ExtensionDatabase, ManualLeaseDAO, SessionDAO, UserDAO
from .backend import PrivateVNCBackend, VNCBackendUnavailable
from .contracts import ManualBackend, ManualRuntime
from .service import ManualSessionService

logger = logging.getLogger(__name__)


class ManualGateway:
    """One service, periodic reconciliation, and exclusive stream ownership.

    The independent native helper watchdog owns the gateway-death deadline.
    This job only reconciles leases; it never heartbeats an unattended stream.
    """

    def __init__(self, service: ManualSessionService, state_root: Path):
        self.service = service
        self.state_root = state_root
        self._stopped = threading.Event()
        service.reap_expired()
        self._reaper = threading.Thread(target=self._reap, name="manual-reaper", daemon=True)
        self._reaper.start()

    def _reap(self) -> None:
        while not self._stopped.wait(1):
            try:
                self.service.reap_expired()
            except Exception:
                logger.warning("Manual resource reconciliation is incomplete")

    @contextmanager
    def claim_stream(self, session_id: str) -> Iterator[None]:
        """One reader per private socket, including across local worker processes."""
        import fcntl

        name = hashlib.sha256(session_id.encode()).hexdigest() + ".stream.lock"
        descriptor = os.open(self.state_root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DomainError(ErrorCode.MANUAL_SESSION_BUSY) from None
            if self._stopped.is_set():
                raise DomainError(ErrorCode.MANUAL_SESSION_CLOSED)
            yield
        finally:
            # Keep the inode: unlinking permits concurrent locks on different inodes.
            os.close(descriptor)

    def shutdown(self) -> None:
        self._stopped.set()
        self._reaper.join(timeout=3)
        self.service.reconcile()


def build_manual_gateway(
    database: ExtensionDatabase,
    catalog: ProfileCatalog,
    auth: AuthService,
    application: ProtectedApplication,
    coordinator: object,
    *,
    backend_factory: Callable[..., ManualBackend] = PrivateVNCBackend,
    library_paths: Sequence[Path] = (),
) -> ManualGateway:
    """Lazy composition: missing native dependencies affect only manual access.

    Operators must supply an installed executable and its independently pinned
    SHA-256. No binary download or automatically trusted digest is performed.
    """
    executable = os.getenv("FOXPROFILE_X11VNC_EXECUTABLE", "").strip()
    digest = os.getenv("FOXPROFILE_X11VNC_SHA256", "").strip()
    if not executable or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise VNCBackendUnavailable(
            "Configure FOXPROFILE_X11VNC_EXECUTABLE and FOXPROFILE_X11VNC_SHA256 "
            "for an installed, pinned x11vnc binary."
        )
    binary = Path(executable)
    if (
        not binary.is_absolute()
        or not binary.is_file()
        or not os.access(binary, os.X_OK)
        or hashlib.sha256(binary.read_bytes()).hexdigest() != digest
    ):
        raise VNCBackendUnavailable(
            "Install x11vnc and verify FOXPROFILE_X11VNC_EXECUTABLE against "
            "FOXPROFILE_X11VNC_SHA256."
        )
    if shutil.which("bwrap") is None:
        raise VNCBackendUnavailable("Install bubblewrap to enable confined manual access.")
    state_root = database.path.with_name(".foxprofile-manual")
    state_root.mkdir(mode=0o700, exist_ok=True)
    info = state_root.lstat()
    if (
        state_root.is_symlink()
        or not state_root.is_dir()
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise VNCBackendUnavailable("Manual state directory must be service-owned with mode 0700.")
    runtime = getattr(coordinator, "runtime", None)
    verified = getattr(runtime, "verified_descriptor", None)
    ledger = CapacityLedger(database)

    def current_runtime(profile_id: str) -> ManualRuntime | None:
        try:
            profile = catalog.get_profile(profile_id)
        except CatalogOperationError:
            return None
        if profile.engine != "camoufox":
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        if not callable(verified):
            raise DomainError(ErrorCode.MANUAL_UNSUPPORTED_RUNTIME)
        run = next(
            (item for item in ledger.active_runs() if item.ref.profile_id == profile_id), None
        )
        descriptor = verified(run.ref) if run is not None else None
        return ManualRuntime(descriptor, profile.engine, True) if descriptor is not None else None

    # Fixed system/Python dependencies plus exact explicitly supplied native assets.
    paths = tuple(
        dict.fromkeys(
            path
            for path in (
                Path("/usr"),
                Path("/bin"),
                Path("/lib"),
                Path("/lib64"),
                Path(sys.prefix),
                Path(sys.base_prefix),
                binary.resolve(),
                *library_paths,
            )
            if path.exists()
        )
    )

    def confinement_for(descriptor: RuntimeDescriptor) -> ConfinementSpec:
        current = current_runtime(descriptor.run.profile_id)
        if current is None or current.descriptor != descriptor:
            raise DomainError(ErrorCode.STALE_BROWSER_RUN)
        profile = catalog.get_profile(descriptor.run.profile_id)
        return ConfinementSpec(
            Path(catalog.manager.data_dir).resolve() / profile.name,
            Path(descriptor.tmp_path),
            Path(__file__).resolve().parents[2],
            paths,
            ConfinementPurpose.VNC_HELPER,
        )

    backend = backend_factory(
        state_root=state_root,
        confinement_for=confinement_for,
        executable=binary.resolve(),
        executable_sha256=digest,
        library_paths=library_paths,
    )
    service = ManualSessionService(
        auth,
        UserDAO(database),
        SessionDAO(database),
        ManualLeaseDAO(database),
        application.resolve_profile,
        current_runtime,
        backend,
    )
    return ManualGateway(service, state_root)
