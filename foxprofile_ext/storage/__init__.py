"""Durable, local extension state backed by SQLite."""

from .capacity import (
    AdmissionResult,
    CapacityLedger,
    ProcessLiveness,
    RecoveryReport,
    RunRecord,
    RunStatus,
)
from .dao import (
    AccountRecord,
    AccountSessionRecord,
    AssignmentDAO,
    BootstrapAlreadyConfigured,
    CatalogOperationDAO,
    CatalogOperationRecord,
    LastAdministratorError,
    SessionDAO,
    SettingsDAO,
    StorageConflict,
    TransferHandleDAO,
    TransferHandleRecord,
    UserDAO,
)
from .database import ExtensionDatabase
from .manual import (
    HelperLiveness,
    LeaseAcquireResult,
    ManualLeaseBusy,
    ManualLeaseDAO,
    ManualLeaseRecord,
)

__all__ = [
    "AdmissionResult",
    "AccountRecord",
    "AccountSessionRecord",
    "AssignmentDAO",
    "BootstrapAlreadyConfigured",
    "CatalogOperationDAO",
    "CatalogOperationRecord",
    "CapacityLedger",
    "ExtensionDatabase",
    "HelperLiveness",
    "LeaseAcquireResult",
    "LastAdministratorError",
    "ManualLeaseBusy",
    "ManualLeaseDAO",
    "ManualLeaseRecord",
    "SessionDAO",
    "SettingsDAO",
    "ProcessLiveness",
    "RecoveryReport",
    "RunRecord",
    "RunStatus",
    "StorageConflict",
    "TransferHandleDAO",
    "TransferHandleRecord",
    "UserDAO",
]
