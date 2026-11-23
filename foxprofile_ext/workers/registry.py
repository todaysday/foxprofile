"""Durable worker enrollment, connection generations, and health telemetry."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from math import isfinite
from time import time

from ..errors import DomainError, ErrorCode
from ..storage.database import ExtensionDatabase
from .metrics import NodeMetrics

_FRESH_SECONDS = 30.0
_MAX_ACTIVE_RUNS = 10_000
_WORKER_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_DIGEST = re.compile(r"[A-Fa-f0-9]{64}\Z")


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    worker_id: str
    boot_id: str | None
    connection_generation: int
    state: str
    metrics: NodeMetrics | None
    last_seen: float | None
    manifest_digest: str
    draining: bool = False


class WorkerRegistry:
    def __init__(
        self,
        database: ExtensionDatabase,
        expected_manifest_digest: str,
        clock=time,
    ):
        if not isinstance(expected_manifest_digest, str) or not _DIGEST.fullmatch(
            expected_manifest_digest
        ):
            raise ValueError("expected_manifest_digest must be a 64-character hex digest")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.database = database
        self.expected_manifest_digest = expected_manifest_digest
        self.clock = clock

    def enroll(self, worker_id: str) -> str:
        _worker_id(worker_id)
        secret = secrets.token_urlsafe(32)
        with self.database.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO workers (worker_id, credential_hash, manifest_digest, preflight_ok) "
                    "VALUES (?, ?, ?, 1)",
                    (worker_id, _credential_hash(secret), self.expected_manifest_digest),
                )
            except Exception as exc:
                if _is_integrity_error(exc):
                    raise DomainError(ErrorCode.WORKER_UNAVAILABLE) from None
                raise
        return secret

    def rotate(self, worker_id: str) -> str:
        _worker_id(worker_id)
        secret = secrets.token_urlsafe(32)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT revoked FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if row is None or row["revoked"]:
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
            connection.execute(
                "UPDATE workers SET credential_hash = ?, connection_generation = connection_generation + 1, "
                "connected = 0, metrics_json = NULL, active_run_ids_json = '[]' WHERE worker_id = ?",
                (_credential_hash(secret), worker_id),
            )
        return secret

    def revoke(self, worker_id: str) -> None:
        _worker_id(worker_id)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE workers SET revoked = 1, connection_generation = connection_generation + 1, "
                "connected = 0, metrics_json = NULL, active_run_ids_json = '[]' WHERE worker_id = ?",
                (worker_id,),
            )
            if cursor.rowcount != 1:
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)

    def set_draining(self, worker_id: str, draining: bool) -> None:
        _worker_id(worker_id)
        if type(draining) is not bool:
            raise ValueError("draining must be bool")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE workers SET draining = ? WHERE worker_id = ? AND revoked = 0",
                (int(draining), worker_id),
            )
            if cursor.rowcount != 1:
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)

    def register(
        self,
        worker_id: str,
        credential: str,
        boot_id: str,
        manifest_digest: str,
        preflight_ok: bool,
    ) -> WorkerRecord:
        _worker_id(worker_id)
        _text(credential, "worker credential")
        _boot_id(boot_id)
        _text(manifest_digest, "manifest digest")
        if type(preflight_ok) is not bool:
            raise ValueError("preflight_ok must be bool")
        now = self._now()
        supplied_hash = _credential_hash(credential)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                row is None
                or row["revoked"]
                or not hmac.compare_digest(row["credential_hash"], supplied_hash)
            ):
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
            generation = row["connection_generation"] + 1
            connection.execute(
                "UPDATE workers SET boot_id = ?, connection_generation = ?, manifest_digest = ?, "
                "preflight_ok = ?, connected = 1, metrics_json = NULL, active_run_ids_json = '[]', last_seen = NULL "
                "WHERE worker_id = ? AND revoked = 0",
                (
                    boot_id,
                    generation,
                    manifest_digest,
                    int(preflight_ok),
                    worker_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        return self._record(updated, now)

    def heartbeat(
        self,
        worker_id: str,
        boot_id: str,
        connection_generation: int,
        metrics: NodeMetrics | None,
        active_run_ids: Sequence[str] | None = (),
        *,
        memory_accounted_run_ids: Sequence[str] = (),
    ) -> WorkerRecord:
        _worker_id(worker_id)
        _boot_id(boot_id)
        if type(connection_generation) is not int or connection_generation < 1:
            raise ValueError("connection_generation must be a positive integer")
        active_ids = _active_ids(active_run_ids) if active_run_ids is not None else None
        accounted_ids = _active_ids(memory_accounted_run_ids)
        if not set(accounted_ids).issubset(active_ids or ()):
            raise ValueError("memory-accounted runs must be active in the same sample")
        measured = None
        if metrics is not None:
            if active_ids is None or not isinstance(metrics, NodeMetrics):
                raise ValueError("metrics require an active run inventory and NodeMetrics")
            measured = asdict(replace(metrics))
            measured["memory_accounted_run_ids"] = accounted_ids
        elif accounted_ids:
            raise ValueError("memory-accounted runs require a memory sample")
        now = self._now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                row is None
                or row["revoked"]
                or not row["connected"]
                or row["boot_id"] != boot_id
                or row["connection_generation"] != connection_generation
            ):
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
            connection.execute(
                "UPDATE workers SET metrics_json = ?, active_run_ids_json = COALESCE(?, active_run_ids_json), last_seen = ? "
                "WHERE worker_id = ? AND boot_id = ? AND connection_generation = ? AND connected = 1 AND revoked = 0",
                (
                    json.dumps(measured, separators=(",", ":")) if measured is not None else None,
                    json.dumps(active_ids, separators=(",", ":"))
                    if active_ids is not None
                    else None,
                    now,
                    worker_id,
                    boot_id,
                    connection_generation,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        return self._record(updated, now)

    def disconnect(self, worker_id: str, boot_id: str, generation: int) -> bool:
        _worker_id(worker_id)
        _boot_id(boot_id)
        if type(generation) is not int or generation < 1:
            return False
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE workers SET connected = 0 WHERE worker_id = ? AND boot_id = ? "
                "AND connection_generation = ? AND revoked = 0",
                (worker_id, boot_id, generation),
            )
            return cursor.rowcount == 1

    def get(self, worker_id: str) -> WorkerRecord | None:
        _worker_id(worker_id)
        now = self._now()
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        return self._record(row, now) if row is not None else None

    @contextmanager
    def connection_guard(self, worker_id: str, boot_id: str, generation: int):
        """Serialize a short result publication with durable owner invalidation.

        The caller must not await or perform I/O while this guard is held.
        """
        _worker_id(worker_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if row is None or (row["boot_id"], row["connection_generation"]) != (
                boot_id,
                generation,
            ):
                raise DomainError(ErrorCode.STALE_RUN)
            if self._control_state(row, self._now()) not in {"ready", "draining"}:
                raise DomainError(ErrorCode.WORKER_UNAVAILABLE)
            yield

    def list(self) -> tuple[WorkerRecord, ...]:
        now = self._now()
        with self.database.read() as connection:
            rows = tuple(connection.execute("SELECT * FROM workers ORDER BY worker_id"))
        return tuple(self._record(row, now) for row in rows)

    def _records_in_transaction(self, connection, now: float) -> tuple[WorkerRecord, ...]:
        """Read current generations and telemetry under the scheduler's write lock."""
        rows = tuple(connection.execute("SELECT * FROM workers ORDER BY worker_id"))
        return tuple(self._record(row, now) for row in rows)

    def _memory_accounted_inventory_in_transaction(self, connection, now: float):
        """Return only running IDs already reflected in the same memory sample."""
        rows = connection.execute(
            "SELECT worker_id, boot_id, connection_generation, active_run_ids_json, last_seen, "
            "connected, revoked, draining, manifest_digest, preflight_ok, metrics_json FROM workers"
        )
        inventory = {}
        for row in rows:
            if (
                self._control_state(row, now) not in {"ready", "draining"}
                or _metrics_from_json(row["metrics_json"]) is None
                or row["boot_id"] is None
            ):
                continue
            try:
                active_ids = _active_ids(json.loads(row["active_run_ids_json"]))
                metrics = json.loads(row["metrics_json"])
                run_ids = _active_ids(metrics.get("memory_accounted_run_ids", ()))
                if not set(run_ids).issubset(active_ids):
                    run_ids = ()
            except (TypeError, ValueError, json.JSONDecodeError):
                run_ids = ()
            inventory[(row["worker_id"], row["boot_id"], row["connection_generation"])] = frozenset(
                run_ids
            )
        return inventory

    def _control_state(self, row, now: float) -> str:
        if row["revoked"]:
            return "revoked"
        if row["manifest_digest"] != self.expected_manifest_digest or not row["preflight_ok"]:
            return "incompatible"
        if (
            not row["connected"]
            or row["last_seen"] is None
            or now - row["last_seen"] >= _FRESH_SECONDS
        ):
            return "offline"
        return "draining" if row["draining"] else "ready"

    def _record(self, row, now: float) -> WorkerRecord:
        state = self._control_state(row, now)
        metrics = _metrics_from_json(row["metrics_json"])
        if metrics is None and state in {"ready", "draining"}:
            state = "offline"
        return WorkerRecord(
            row["worker_id"],
            row["boot_id"],
            row["connection_generation"],
            state,
            metrics,
            row["last_seen"],
            row["manifest_digest"],
            bool(row["draining"]),
        )

    def _now(self) -> float:
        now = self.clock()
        if type(now) not in (int, float) or not isfinite(now):
            raise ValueError("clock must return a finite Unix timestamp")
        return float(now)


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{label} must be a non-empty string")


def _worker_id(value: object) -> None:
    if not isinstance(value, str) or not _WORKER_ID.fullmatch(value):
        raise ValueError("worker_id must use letters, digits, dot, underscore, or dash")


def _boot_id(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("boot_id must be a non-empty string of at most 128 characters")


def _credential_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_integrity_error(error: Exception) -> bool:
    return isinstance(error, sqlite3.IntegrityError)


def _metrics_from_json(raw: str | None) -> NodeMetrics | None:
    if raw is None:
        return None
    try:
        values = json.loads(raw)
        if not isinstance(values, dict):
            return None
        values.pop("memory_accounted_run_ids", None)
        return NodeMetrics.from_report(**values)
    except (TypeError, ValueError):
        return None


def _active_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("active_run_ids must be a sequence of run IDs")
    if len(values) > _MAX_ACTIVE_RUNS:
        raise ValueError("active_run_ids exceeds the supported limit")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in values):
        raise ValueError("active_run_ids contains an invalid run ID")
    if len(set(values)) != len(values):
        raise ValueError("active_run_ids cannot contain duplicates")
    return tuple(values)
