"""One installation-wide manual-browser lease with durable cleanup state."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite

from ..dto import ManualSessionState, ProcessIdentity
from ..errors import DomainError, ErrorCode
from .database import ExtensionDatabase


class ManualLeaseBusy(DomainError):
    """The installation already has an unavailable manual lease."""

    def __init__(self):
        super().__init__(ErrorCode.MANUAL_SESSION_BUSY)


class HelperLiveness(str, Enum):
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ManualLeaseRecord:
    session_id: str
    owner_user_id: str
    personal_session_id: str
    profile_id: str
    browser_run_id: str
    host_id: str
    state: ManualSessionState
    created_at: float
    updated_at: float
    expires_at: float
    cleanup_deadline: float
    helper: ProcessIdentity | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class LeaseAcquireResult:
    record: ManualLeaseRecord
    created: bool


class ManualLeaseDAO:
    def __init__(self, database: ExtensionDatabase):
        self.database = database

    def acquire(
        self,
        session_id: str,
        owner_user_id: str,
        personal_session_id: str,
        profile_id: str,
        browser_run_id: str,
        host_id: str,
        created_at: float,
        expires_at: float,
        cleanup_deadline: float,
    ) -> LeaseAcquireResult:
        _required_text(
            session_id,
            owner_user_id,
            personal_session_id,
            profile_id,
            browser_run_id,
            host_id,
        )
        _epoch(created_at)
        _epoch(expires_at)
        _epoch(cleanup_deadline)
        if expires_at <= created_at or cleanup_deadline <= created_at:
            raise ValueError("lease expiry and cleanup deadline must follow creation")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM manual_leases WHERE state != 'closed' LIMIT 1"
            ).fetchone()
            if row is not None:
                same_owner_run = (
                    row["owner_user_id"] == owner_user_id
                    and row["personal_session_id"] == personal_session_id
                    and row["profile_id"] == profile_id
                    and row["browser_run_id"] == browser_run_id
                    and row["host_id"] == host_id
                )
                if same_owner_run:
                    return LeaseAcquireResult(self._record(row), False)
                raise ManualLeaseBusy
            connection.execute(
                "INSERT INTO manual_leases "
                "(session_id, owner_user_id, personal_session_id, profile_id, browser_run_id, host_id, state, created_at, updated_at, expires_at, cleanup_deadline) "
                "VALUES (?, ?, ?, ?, ?, ?, 'provisioning', ?, ?, ?, ?)",
                (
                    session_id,
                    owner_user_id,
                    personal_session_id,
                    profile_id,
                    browser_run_id,
                    host_id,
                    created_at,
                    created_at,
                    expires_at,
                    cleanup_deadline,
                ),
            )
            row = connection.execute(
                "SELECT * FROM manual_leases WHERE session_id = ?", (session_id,)
            ).fetchone()
        return LeaseAcquireResult(self._record(row), True)

    def current(self) -> ManualLeaseRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM manual_leases WHERE state != 'closed' LIMIT 1"
            ).fetchone()
        return self._record(row) if row is not None else None

    def get(self, session_id: str) -> ManualLeaseRecord | None:
        """Find retained lease, including closed locators for expiry responses."""
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM manual_leases WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def transition(
        self,
        session_id: str,
        owner_user_id: str,
        personal_session_id: str,
        profile_id: str,
        browser_run_id: str,
        host_id: str,
        expected: ManualSessionState | str,
        new_state: ManualSessionState | str,
        updated_at: float,
    ) -> bool:
        expected = ManualSessionState(expected)
        new_state = ManualSessionState(new_state)
        _epoch(updated_at)
        allowed = {
            ManualSessionState.PROVISIONING: {
                ManualSessionState.ACTIVE,
                ManualSessionState.CLOSING,
                ManualSessionState.RECOVERING,
            },
            ManualSessionState.ACTIVE: {
                ManualSessionState.CLOSING,
                ManualSessionState.RECOVERING,
            },
            ManualSessionState.CLOSING: {ManualSessionState.RECOVERING},
            ManualSessionState.RECOVERING: {
                ManualSessionState.ACTIVE,
                ManualSessionState.CLOSING,
            },
            ManualSessionState.CLOSED: set(),
        }
        if new_state not in allowed[expected]:
            raise ValueError("invalid manual lease state transition")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE manual_leases SET state = ?, updated_at = ? WHERE session_id = ? "
                "AND owner_user_id = ? AND personal_session_id = ? AND profile_id = ? "
                "AND browser_run_id = ? AND host_id = ? AND state = ?",
                (
                    new_state.value,
                    updated_at,
                    session_id,
                    owner_user_id,
                    personal_session_id,
                    profile_id,
                    browser_run_id,
                    host_id,
                    expected.value,
                ),
            )
            return cursor.rowcount == 1

    def register_helper(
        self,
        session_id: str,
        owner_user_id: str,
        personal_session_id: str,
        profile_id: str,
        browser_run_id: str,
        host_id: str,
        process: ProcessIdentity,
        updated_at: float,
    ) -> bool:
        _epoch(updated_at)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE manual_leases SET helper_pid = ?, helper_start_ticks = ?, updated_at = ? "
                "WHERE session_id = ? AND owner_user_id = ? AND personal_session_id = ? "
                "AND profile_id = ? AND browser_run_id = ? AND host_id = ? "
                "AND state IN ('provisioning', 'active', 'closing', 'recovering') "
                "AND helper_pid IS NULL AND helper_start_ticks IS NULL",
                (
                    process.pid,
                    process.start_ticks,
                    updated_at,
                    session_id,
                    owner_user_id,
                    personal_session_id,
                    profile_id,
                    browser_run_id,
                    host_id,
                ),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                "SELECT helper_pid, helper_start_ticks FROM manual_leases WHERE session_id = ? "
                "AND owner_user_id = ? AND personal_session_id = ? AND profile_id = ? "
                "AND browser_run_id = ? AND host_id = ? AND state != 'closed'",
                (
                    session_id,
                    owner_user_id,
                    personal_session_id,
                    profile_id,
                    browser_run_id,
                    host_id,
                ),
            ).fetchone()
            return row is not None and (row["helper_pid"], row["helper_start_ticks"]) == (
                process.pid,
                process.start_ticks,
            )

    def close_after_exit(
        self,
        session_id: str,
        owner_user_id: str,
        personal_session_id: str,
        profile_id: str,
        browser_run_id: str,
        host_id: str,
        updated_at: float,
        probe: Callable[[str, ProcessIdentity | None], str],
    ) -> bool:
        """Probe outside transaction; close only after trusted cleanup proof."""
        _epoch(updated_at)
        row = self._get_exact(
            session_id,
            owner_user_id,
            personal_session_id,
            profile_id,
            browser_run_id,
            host_id,
        )
        if row is None or row.state is ManualSessionState.CLOSED:
            return False
        try:
            if probe(host_id, row.helper) != HelperLiveness.DEAD:
                return False
        except Exception:
            return False
        with self.database.transaction() as connection:
            if row.helper is None:
                helper_predicate = "helper_pid IS NULL AND helper_start_ticks IS NULL"
                values: tuple[object, ...] = ()
            else:
                helper_predicate = "helper_pid = ? AND helper_start_ticks = ?"
                values = (row.helper.pid, row.helper.start_ticks)
            cursor = connection.execute(
                "UPDATE manual_leases SET state = 'closed', updated_at = ? "
                "WHERE session_id = ? AND owner_user_id = ? AND personal_session_id = ? "
                "AND profile_id = ? AND browser_run_id = ? AND host_id = ? "
                "AND state IN ('provisioning', 'active', 'closing', 'recovering') AND "
                + helper_predicate,
                (
                    updated_at,
                    session_id,
                    owner_user_id,
                    personal_session_id,
                    profile_id,
                    browser_run_id,
                    host_id,
                    *values,
                ),
            )
            return cursor.rowcount == 1

    def _get_exact(
        self,
        session_id: str,
        owner_user_id: str,
        personal_session_id: str,
        profile_id: str,
        browser_run_id: str,
        host_id: str,
    ) -> ManualLeaseRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM manual_leases WHERE session_id = ? AND owner_user_id = ? "
                "AND personal_session_id = ? AND profile_id = ? AND browser_run_id = ? AND host_id = ?",
                (
                    session_id,
                    owner_user_id,
                    personal_session_id,
                    profile_id,
                    browser_run_id,
                    host_id,
                ),
            ).fetchone()
        return self._record(row) if row is not None else None

    @staticmethod
    def _record(row: sqlite3.Row) -> ManualLeaseRecord:
        helper = (
            ProcessIdentity(row["helper_pid"], row["helper_start_ticks"])
            if row["helper_pid"] is not None
            else None
        )
        return ManualLeaseRecord(
            row["session_id"],
            row["owner_user_id"],
            row["personal_session_id"],
            row["profile_id"],
            row["browser_run_id"],
            row["host_id"],
            ManualSessionState(row["state"]),
            row["created_at"],
            row["updated_at"],
            row["expires_at"],
            row["cleanup_deadline"],
            helper,
        )


def _required_text(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("values must be non-empty strings")


def _epoch(value: float) -> None:
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError("time must be finite UTC Unix seconds")
