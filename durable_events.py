"""Reusable durable SQLite inbox for Orion event boundaries.

The store is deliberately independent from ``EventHandler`` and ``AgentRuntime``.
Callers choose a namespace (for example ``event_handler`` or ``runtime``),
durably accept a JSON event, claim queued receipts with a fenced lease, then
acknowledge or fail that exact claim. Expired processing leases are *not*
silently replayed: they are exposed by :meth:`list_recoverable` and must be
returned to the queue explicitly with :meth:`recover_stale`.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class DurableEventError(RuntimeError):
    """Base error for durable inbox operations."""


class DurableEventConflict(DurableEventError, ValueError):
    """An event identity was reused with a different fingerprint."""


class DurableEventValidationError(DurableEventError, ValueError):
    """The supplied event cannot be stored safely as bounded JSON."""


@dataclass(frozen=True, slots=True)
class DurableEventReceipt:
    receipt_id: str
    namespace: str
    event_id: str
    idempotency_key: str | None
    message_id: str | None
    fingerprint: str
    payload: dict[str, Any]
    status: str
    attempts: int
    owner_id: str | None
    lease_until: float | None
    fence_token: int
    last_error: str | None
    created_at: float
    updated_at: float

    def is_stale(self, now: float) -> bool:
        return (
            self.status == "processing"
            and self.lease_until is not None
            and self.lease_until <= now
        )


class DurableEventStore:
    """Namespace-scoped durable inbox safe for multiple SQLite connections."""

    _SCHEMA_VERSION = 1
    _STATUSES = frozenset({"queued", "processing", "acked", "failed"})

    def __init__(
        self,
        path: str | Path = "data/durable_events.sqlite3",
        *,
        namespace: str,
        max_payload_bytes: int = 256 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = str(path)
        self.namespace = str(namespace).strip()
        if not self.namespace:
            raise ValueError("namespace must be non-empty")
        if (
            isinstance(max_payload_bytes, bool)
            or not isinstance(max_payload_bytes, int)
            or max_payload_bytes < 1
        ):
            raise ValueError("max_payload_bytes must be a positive integer")
        self.max_payload_bytes = max_payload_bytes
        self.clock = clock
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        self._initialize()
        if self.path != ":memory:":
            self._enable_wal()

    def _retry_locked(self, operation: Callable[[], Any]) -> Any:
        """Retry SQLite startup operations that can transiently race peers."""
        deadline = time.monotonic() + 30.0
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _enable_wal(self) -> None:
        self._retry_locked(lambda: self._db.execute("PRAGMA journal_mode=WAL").fetchone())

    def _initialize(self) -> None:
        with self._lock:
            self._retry_locked(lambda: self._db.execute("BEGIN IMMEDIATE"))
            try:
                self._db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS durable_events (
                        receipt_id TEXT PRIMARY KEY,
                        namespace TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        idempotency_key TEXT,
                        message_id TEXT,
                        fingerprint TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'queued',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        owner_id TEXT,
                        lease_until REAL,
                        fence_token INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                self._migrate_schema()
                statements = (
                    "CREATE UNIQUE INDEX IF NOT EXISTS durable_events_event_identity "
                    "ON durable_events(namespace, event_id)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS durable_events_idempotency_identity "
                    "ON durable_events(namespace, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL",
                    "CREATE UNIQUE INDEX IF NOT EXISTS durable_events_message_identity "
                    "ON durable_events(namespace, message_id) "
                    "WHERE message_id IS NOT NULL",
                    "CREATE INDEX IF NOT EXISTS durable_events_ready "
                    "ON durable_events(namespace, status, created_at)",
                    "CREATE INDEX IF NOT EXISTS durable_events_lease "
                    "ON durable_events(namespace, status, lease_until)",
                    "CREATE TABLE IF NOT EXISTS durable_events_meta ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                )
                for statement in statements:
                    self._db.execute(statement)
                self._db.execute(
                    "INSERT INTO durable_events_meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(self._SCHEMA_VERSION),),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _migrate_schema(self) -> None:
        """Apply additive migration for early/pre-release durable inbox tables."""
        columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(durable_events)").fetchall()
        }
        migrations = {
            "idempotency_key": "ALTER TABLE durable_events ADD COLUMN idempotency_key TEXT",
            "message_id": "ALTER TABLE durable_events ADD COLUMN message_id TEXT",
            "status": "ALTER TABLE durable_events ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'",
            "attempts": "ALTER TABLE durable_events ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "owner_id": "ALTER TABLE durable_events ADD COLUMN owner_id TEXT",
            "lease_until": "ALTER TABLE durable_events ADD COLUMN lease_until REAL",
            "fence_token": "ALTER TABLE durable_events ADD COLUMN fence_token INTEGER NOT NULL DEFAULT 0",
            "last_error": "ALTER TABLE durable_events ADD COLUMN last_error TEXT",
            "updated_at": "ALTER TABLE durable_events ADD COLUMN updated_at REAL",
        }
        for name, statement in migrations.items():
            if name not in columns:
                self._db.execute(statement)
        self._db.execute(
            "UPDATE durable_events SET updated_at=COALESCE(updated_at, created_at), "
            "attempts=COALESCE(attempts,0), fence_token=COALESCE(fence_token,0), "
            "status=COALESCE(status,'queued')"
        )

    @staticmethod
    def _identity(value: Any, *, field: str, required: bool) -> str | None:
        if value is None:
            if required:
                raise DurableEventValidationError(f"{field} is required")
            return None
        text = str(value).strip()
        if not text:
            if required:
                raise DurableEventValidationError(f"{field} must be non-empty")
            return None
        return text

    def _encode_event(self, event: Mapping[str, Any]) -> str:
        if not isinstance(event, Mapping):
            raise DurableEventValidationError("event must be a mapping")
        try:
            encoded = json.dumps(
                dict(event),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise DurableEventValidationError("event must contain valid JSON values") from exc
        if len(encoded.encode("utf-8")) > self.max_payload_bytes:
            raise DurableEventValidationError(
                f"event payload exceeds {self.max_payload_bytes} bytes"
            )
        return encoded

    @staticmethod
    def _row(row: sqlite3.Row | None) -> DurableEventReceipt | None:
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise DurableEventValidationError("stored event payload is not a JSON object")
        return DurableEventReceipt(
            receipt_id=str(row["receipt_id"]),
            namespace=str(row["namespace"]),
            event_id=str(row["event_id"]),
            idempotency_key=row["idempotency_key"],
            message_id=row["message_id"],
            fingerprint=str(row["fingerprint"]),
            payload=payload,
            status=str(row["status"]),
            attempts=int(row["attempts"] or 0),
            owner_id=row["owner_id"],
            lease_until=row["lease_until"],
            fence_token=int(row["fence_token"] or 0),
            last_error=row["last_error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _matching_identity_rows(
        self,
        *,
        event_id: str,
        idempotency_key: str | None,
        message_id: str | None,
    ) -> list[sqlite3.Row]:
        clauses = ["event_id=?"]
        args: list[Any] = [event_id]
        if idempotency_key is not None:
            clauses.append("idempotency_key=?")
            args.append(idempotency_key)
        if message_id is not None:
            clauses.append("message_id=?")
            args.append(message_id)
        rows = self._db.execute(
            "SELECT * FROM durable_events WHERE namespace=? AND ("
            + " OR ".join(clauses)
            + ")",
            (self.namespace, *args),
        ).fetchall()
        return list(rows)

    def accept(self, event: Mapping[str, Any]) -> DurableEventReceipt:
        """Durably accept an event, returning the existing receipt on duplicate."""
        encoded = self._encode_event(event)
        event_id = self._identity(event.get("event_id"), field="event_id", required=True)
        fingerprint = self._identity(
            event.get("fingerprint"), field="fingerprint", required=True
        )
        idempotency_key = self._identity(
            event.get("idempotency_key"), field="idempotency_key", required=False
        )
        message_id = self._identity(
            event.get("message_id"), field="message_id", required=False
        )
        assert event_id is not None and fingerprint is not None
        now = float(self.clock())
        if not math.isfinite(now):
            raise DurableEventValidationError("clock returned a non-finite timestamp")

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                matches = self._matching_identity_rows(
                    event_id=event_id,
                    idempotency_key=idempotency_key,
                    message_id=message_id,
                )
                if matches:
                    receipt_ids = {str(row["receipt_id"]) for row in matches}
                    if len(receipt_ids) != 1:
                        raise DurableEventConflict(
                            "event identities refer to different durable receipts"
                        )
                    existing = matches[0]
                    if str(existing["fingerprint"]) != fingerprint:
                        raise DurableEventConflict(
                            "event identity reused with a different fingerprint"
                        )
                    receipt = self._row(existing)
                    assert receipt is not None
                    self._db.execute("COMMIT")
                    return receipt

                receipt_id = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO durable_events(receipt_id,namespace,event_id,idempotency_key,"
                    "message_id,fingerprint,payload_json,status,attempts,owner_id,lease_until,"
                    "fence_token,last_error,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'queued',0,NULL,NULL,0,NULL,?,?)",
                    (
                        receipt_id,
                        self.namespace,
                        event_id,
                        idempotency_key,
                        message_id,
                        fingerprint,
                        encoded,
                        now,
                        now,
                    ),
                )
                row = self._db.execute(
                    "SELECT * FROM durable_events WHERE receipt_id=?", (receipt_id,)
                ).fetchone()
                receipt = self._row(row)
                assert receipt is not None
                self._db.execute("COMMIT")
                return receipt
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def claim(
        self,
        *,
        owner_id: str,
        lease_seconds: float = 60.0,
        limit: int = 1,
    ) -> list[DurableEventReceipt]:
        """Claim queued events only; stale processing requires explicit recovery."""
        owner = self._identity(owner_id, field="owner_id", required=True)
        assert owner is not None
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be >= 1")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a finite number > 0")
        now = float(self.clock())
        lease_until = now + float(lease_seconds)

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT receipt_id FROM durable_events "
                    "WHERE namespace=? AND status='queued' "
                    "ORDER BY created_at, receipt_id LIMIT ?",
                    (self.namespace, limit),
                ).fetchall()
                receipt_ids = [str(row["receipt_id"]) for row in rows]
                for receipt_id in receipt_ids:
                    self._db.execute(
                        "UPDATE durable_events SET status='processing', attempts=attempts+1, "
                        "owner_id=?, lease_until=?, fence_token=fence_token+1, last_error=NULL, "
                        "updated_at=? WHERE receipt_id=? AND namespace=? AND status='queued'",
                        (owner, lease_until, now, receipt_id, self.namespace),
                    )
                claimed: list[DurableEventReceipt] = []
                for receipt_id in receipt_ids:
                    row = self._db.execute(
                        "SELECT * FROM durable_events WHERE receipt_id=? AND namespace=?",
                        (receipt_id, self.namespace),
                    ).fetchone()
                    receipt = self._row(row)
                    if receipt is not None and receipt.status == "processing":
                        claimed.append(receipt)
                self._db.execute("COMMIT")
                return claimed
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def claim_receipt(
        self,
        receipt_id: str,
        *,
        owner_id: str,
        lease_seconds: float = 60.0,
    ) -> DurableEventReceipt | None:
        """Claim one specific queued receipt while preserving its fence contract.

        This is useful for consumers that keep their own in-memory priority
        queue: the durable claim must follow the item selected by that queue,
        rather than claiming whichever durable row happened to be oldest.
        """
        owner = self._identity(owner_id, field="owner_id", required=True)
        assert owner is not None
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a finite number > 0")
        now = float(self.clock())
        lease_until = now + float(lease_seconds)
        receipt_id = str(receipt_id)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._db.execute(
                    "UPDATE durable_events SET status='processing', attempts=attempts+1, "
                    "owner_id=?, lease_until=?, fence_token=fence_token+1, last_error=NULL, "
                    "updated_at=? WHERE receipt_id=? AND namespace=? AND status='queued'",
                    (owner, lease_until, now, receipt_id, self.namespace),
                )
                if not cursor.rowcount:
                    self._db.execute("COMMIT")
                    return None
                row = self._db.execute(
                    "SELECT * FROM durable_events WHERE receipt_id=? AND namespace=?",
                    (receipt_id, self.namespace),
                ).fetchone()
                receipt = self._row(row)
                self._db.execute("COMMIT")
                return receipt
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def renew_claim(
        self,
        receipt_id: str,
        *,
        owner_id: str,
        fence_token: int,
        lease_seconds: float = 60.0,
    ) -> bool:
        """Extend one live processing lease without changing its fence.

        Renewal is deliberately fenced by the exact owner/token pair and by
        the *current* lease still being live.  A late heartbeat can therefore
        never resurrect an expired claim after another runtime recovered it.
        """
        owner = self._identity(owner_id, field="owner_id", required=True)
        assert owner is not None
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a finite number > 0")
        now = float(self.clock())
        if not math.isfinite(now):
            raise DurableEventValidationError("clock returned a non-finite timestamp")
        lease_until = now + float(lease_seconds)
        with self._lock:
            cursor = self._db.execute(
                "UPDATE durable_events SET lease_until=?, updated_at=? "
                "WHERE receipt_id=? AND namespace=? AND status='processing' "
                "AND owner_id=? AND fence_token=? AND lease_until>?",
                (
                    lease_until,
                    now,
                    str(receipt_id),
                    self.namespace,
                    owner,
                    int(fence_token),
                    now,
                ),
            )
            return bool(cursor.rowcount)

    def _finish_claim(
        self,
        receipt_id: str,
        *,
        owner_id: str,
        fence_token: int,
        status: str,
        error: str | None,
    ) -> bool:
        now = float(self.clock())
        owner = self._identity(owner_id, field="owner_id", required=True)
        assert owner is not None
        with self._lock:
            cursor = self._db.execute(
                "UPDATE durable_events SET status=?, owner_id=NULL, lease_until=NULL, "
                "last_error=?, updated_at=? WHERE receipt_id=? AND namespace=? "
                "AND status='processing' AND owner_id=? AND fence_token=? AND lease_until>?",
                (
                    status,
                    error,
                    now,
                    str(receipt_id),
                    self.namespace,
                    owner,
                    int(fence_token),
                    now,
                ),
            )
            return bool(cursor.rowcount)

    def ack(self, receipt_id: str, *, owner_id: str, fence_token: int) -> bool:
        """Acknowledge only the live owner/fence claim."""
        return self._finish_claim(
            receipt_id,
            owner_id=owner_id,
            fence_token=fence_token,
            status="acked",
            error=None,
        )

    def fail(
        self,
        receipt_id: str,
        error: str,
        *,
        owner_id: str,
        fence_token: int,
        retry: bool = False,
    ) -> bool:
        """Fail a live claim, optionally returning it to the queue explicitly."""
        return self._finish_claim(
            receipt_id,
            owner_id=owner_id,
            fence_token=fence_token,
            status="queued" if retry else "failed",
            error=str(error)[:4000],
        )

    def get(self, receipt_id: str) -> DurableEventReceipt | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM durable_events WHERE receipt_id=? AND namespace=?",
                (str(receipt_id), self.namespace),
            ).fetchone()
            return self._row(row)

    def list_events(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[DurableEventReceipt]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be >= 1")
        if status is not None and status not in self._STATUSES:
            raise ValueError(f"unsupported status: {status}")
        with self._lock:
            if status is None:
                rows = self._db.execute(
                    "SELECT * FROM durable_events WHERE namespace=? "
                    "ORDER BY created_at, receipt_id LIMIT ?",
                    (self.namespace, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM durable_events WHERE namespace=? AND status=? "
                    "ORDER BY created_at, receipt_id LIMIT ?",
                    (self.namespace, status, limit),
                ).fetchall()
            return [receipt for row in rows if (receipt := self._row(row)) is not None]

    def list_recoverable(self, *, limit: int = 100) -> list[DurableEventReceipt]:
        """List queued work and expired processing claims without mutating them."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be >= 1")
        now = float(self.clock())
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM durable_events WHERE namespace=? AND "
                "(status='queued' OR (status='processing' AND lease_until<=?)) "
                "ORDER BY created_at, receipt_id LIMIT ?",
                (self.namespace, now, limit),
            ).fetchall()
            return [receipt for row in rows if (receipt := self._row(row)) is not None]

    def recover_stale(self, *, limit: int = 100) -> list[DurableEventReceipt]:
        """Explicitly requeue expired processing claims after a crash/restart."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be >= 1")
        now = float(self.clock())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT receipt_id FROM durable_events WHERE namespace=? "
                    "AND status='processing' AND lease_until<=? "
                    "ORDER BY created_at, receipt_id LIMIT ?",
                    (self.namespace, now, limit),
                ).fetchall()
                receipt_ids = [str(row["receipt_id"]) for row in rows]
                for receipt_id in receipt_ids:
                    self._db.execute(
                        "UPDATE durable_events SET status='queued', owner_id=NULL, "
                        "lease_until=NULL, updated_at=? WHERE receipt_id=? AND namespace=? "
                        "AND status='processing' AND lease_until<=?",
                        (now, receipt_id, self.namespace, now),
                    )
                recovered: list[DurableEventReceipt] = []
                for receipt_id in receipt_ids:
                    row = self._db.execute(
                        "SELECT * FROM durable_events WHERE receipt_id=? AND namespace=?",
                        (receipt_id, self.namespace),
                    ).fetchone()
                    receipt = self._row(row)
                    if receipt is not None and receipt.status == "queued":
                        recovered.append(receipt)
                self._db.execute("COMMIT")
                return recovered
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> DurableEventStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "DurableEventConflict",
    "DurableEventError",
    "DurableEventReceipt",
    "DurableEventStore",
    "DurableEventValidationError",
]
