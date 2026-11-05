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
    "LastAdministratorError",
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
