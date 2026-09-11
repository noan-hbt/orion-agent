"""Durable SQLite ledger for channel independent communication delivery.

The ledger stores acceptance before a channel acknowledges an event. Workers
claim rows for a bounded lease and explicitly acknowledge delivery. A worker
crash therefore causes a later duplicate attempt (at-least-once delivery),
never an implicit exactly-once guarantee.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
import random
from pathlib import Path
from typing import Any, Mapping


class CommunicationLedgerError(RuntimeError):
    """Base error raised by the ledger."""


class IdempotencyConflict(CommunicationLedgerError, ValueError):
    """An event or idempotency key was reused for different content."""


class CommunicationLedger:
    """Thread-safe durable queue for inbound messages and outbound outputs."""

    def __init__(self, path: str | Path = "data/communication_ledger.sqlite3") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            # Create tables first, then run additive column migrations, and
            # only then create indexes that reference migrated columns.  Older
            # ledgers can legitimately lack lease/retry columns; creating the
            # ready index before ALTER TABLE would make those databases
            # impossible to open and therefore impossible to migrate.
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS communication_events (
                    id TEXT PRIMARY KEY,
                    event_id TEXT UNIQUE,
                    kind TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    idempotency_key TEXT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    correlation_id TEXT,
                    reply_to TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_owner TEXT,
                    lease_until REAL,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS communication_metrics (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS communication_cursors (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS communication_nonces (
                    namespace TEXT NOT NULL,
                    nonce_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(namespace, nonce_hash)
                );
                """
            )
            # Additive compatibility migration for ledgers created by older
            # releases. SQLite cannot add a column inside CREATE IF NOT EXISTS.
            columns = {row["name"] for row in self._connection.execute(
                "PRAGMA table_info(communication_events)"
            ).fetchall()}
            migrations = {
                "max_attempts": "ALTER TABLE communication_events ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3",
                "lease_owner": "ALTER TABLE communication_events ADD COLUMN lease_owner TEXT",
                "lease_until": "ALTER TABLE communication_events ADD COLUMN lease_until REAL",
                "next_attempt_at": "ALTER TABLE communication_events ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0",
                "last_error": "ALTER TABLE communication_events ADD COLUMN last_error TEXT",
            }
            for name, statement in migrations.items():
                if name not in columns:
                    self._connection.execute(statement)
            self._connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS communication_events_idempotency
                    ON communication_events(idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS communication_events_ready
                    ON communication_events(kind, status, next_attempt_at, lease_until);
                CREATE INDEX IF NOT EXISTS communication_nonces_expiry
                    ON communication_nonces(expires_at);
                """
            )
            # Versioned, additive migrations.  ``user_version`` is SQLite's
            # durable migration marker and keeps old databases compatible.
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version < 2:
                self._connection.execute("PRAGMA user_version = 2")

    def _write(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute a write with a short SQLITE_BUSY retry window."""
        for attempt in range(6):
            try:
                return self._connection.execute(sql, args)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt == 5:
                    raise
                time.sleep(min(0.5, 0.02 * (2 ** attempt) + random.random() * 0.02))
        raise AssertionError("unreachable")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )

    @classmethod
    def _fingerprint(
        cls, *, kind: str, channel: str, payload: Mapping[str, Any],
        correlation_id: str | None, reply_to: str | None,
    ) -> str:
        content = cls._json({
            "kind": kind, "channel": channel, "payload": payload,
            "correlation_id": correlation_id, "reply_to": reply_to,
        })
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _scoped_external_key(prefix: str, *, kind: str, channel: str, value: str) -> str:
        """Namespace externally supplied ids without relying on delimiter escaping."""
        kind = str(kind)
        channel = str(channel)
        value = str(value)
        return f"{prefix}:{len(kind)}:{kind}:{len(channel)}:{channel}:{value}"

    @classmethod
    def _dedupe_key(
        cls, *, event_id: str | None, idempotency_key: str | None,
        message_id: str | None, payload: Mapping[str, Any], kind: str, channel: str,
    ) -> str:
        if idempotency_key:
            return "key:" + str(idempotency_key)
        if event_id:
            return cls._scoped_external_key(
                "event", kind=kind, channel=channel, value=str(event_id)
            )
        if message_id:
            return cls._scoped_external_key(
                "message", kind=kind, channel=channel, value=str(message_id)
            )
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
        digest = hashlib.sha256(raw).hexdigest()
        return cls._scoped_external_key(
            "hash", kind=kind, channel=channel, value=digest
        )

    def _metric(self, name: str, amount: int = 1) -> None:
        self._connection.execute(
            "INSERT INTO communication_metrics(name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
            (name, amount),
        )

    def _existing_or_conflict(
        self, *, dedupe_key: str, stored_event_id: str, external_event_id: str,
        idempotency_key: str | None, fingerprint: str, kind: str, channel: str,
    ) -> sqlite3.Row | None:
        # New rows use channel/kind-scoped external ids.  The raw event-id
        # branch is retained only inside the same channel/kind so databases
        # written by older releases still dedupe instead of redelivering.
        row = self._connection.execute(
            "SELECT * FROM communication_events WHERE dedupe_key=? OR "
            "(channel=? AND kind=? AND event_id IN (?,?)) OR "
            "(? IS NOT NULL AND idempotency_key=?) LIMIT 1",
            (
                dedupe_key,
                channel,
                kind,
                stored_event_id,
                external_event_id,
                idempotency_key,
                idempotency_key,
            ),
        ).fetchone()
        if row is not None and row["fingerprint"] and row["fingerprint"] != fingerprint:
            raise IdempotencyConflict("event or idempotency key reused with different content")
        return row

    def _row_id_for_insert(self, preferred: str, *, kind: str, channel: str) -> str:
        """Keep historical raw ids when free; scope only when another row owns one."""
        if self._connection.execute(
            "SELECT 1 FROM communication_events WHERE id=?", (preferred,)
        ).fetchone() is None:
            return preferred
        scoped = self._scoped_external_key(
            "row", kind=kind, channel=channel, value=preferred
        )
        if self._connection.execute(
            "SELECT 1 FROM communication_events WHERE id=?", (scoped,)
        ).fetchone() is None:
            return scoped
        return f"{scoped}:{uuid.uuid4().hex}"

    def record(
        self, *, channel: str, payload: Mapping[str, Any], message_id: str | None = None,
        event_id: str | None = None, idempotency_key: str | None = None,
        correlation_id: str | None = None, reply_to: str | None = None,
        kind: str = "message", max_attempts: int = 3,
        fingerprint_payload: Mapping[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Durably enqueue one event and return ``(row_id, is_new)``."""
        if not channel:
            raise ValueError("channel is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        event_id = str(event_id or message_id or uuid.uuid4().hex)
        preferred_row_id = str(message_id or event_id)
        kind, channel, payload = str(kind), str(channel), dict(payload)
        stored_event_id = self._scoped_external_key(
            "event", kind=kind, channel=channel, value=event_id
        )
        fingerprint = self._fingerprint(
            kind=kind,
            channel=channel,
            payload=dict(fingerprint_payload) if fingerprint_payload is not None else payload,
            correlation_id=correlation_id, reply_to=reply_to,
        )
        dedupe_key = self._dedupe_key(
            event_id=event_id, idempotency_key=idempotency_key,
            message_id=message_id, payload=payload, kind=kind, channel=channel,
        )
        encoded, now = self._json(payload), time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._existing_or_conflict(
                    dedupe_key=dedupe_key,
                    stored_event_id=stored_event_id,
                    external_event_id=event_id,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    kind=kind,
                    channel=channel,
                )
                if existing is not None:
                    self._metric("duplicates")
                    self._connection.execute("COMMIT")
                    return str(existing["id"]), False
                row_id = self._row_id_for_insert(
                    preferred_row_id, kind=kind, channel=channel
                )
                self._connection.execute(
                    """INSERT INTO communication_events
                    (id,event_id,kind,channel,idempotency_key,dedupe_key,fingerprint,
                    payload,correlation_id,reply_to,status,max_attempts,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?, 'queued',?,?,?)""",
                    (row_id, stored_event_id, kind, channel, idempotency_key, dedupe_key,
                     fingerprint, encoded, correlation_id, reply_to, max_attempts, now, now),
                )
                self._metric("accepted")
                self._metric("queued")
                self._connection.execute("COMMIT")
                return row_id, True
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    enqueue = record

    def record_inbound(
        self, *, channel: str, payload: Mapping[str, Any] | None = None,
        event_id: str | None = None, message_id: str | None = None,
        idempotency_key: str | None = None, correlation_id: str | None = None,
        sender: str | None = None, text: str | None = None,
        received_at: str | None = None, reply_to: str | None = None,
        max_attempts: int = 3,
    ) -> tuple[str, bool]:
        data = dict(payload or {})
        for key, value in {"sender": sender, "text": text, "received_at": received_at}.items():
            if value is not None:
                data.setdefault(key, value)
        return self.record(
            channel=channel, payload=data, event_id=event_id, message_id=message_id,
            idempotency_key=idempotency_key, correlation_id=correlation_id,
            reply_to=reply_to, kind="inbound", max_attempts=max_attempts,
        )

    enqueue_inbound = record_inbound

    def record_outbound(
        self, *, channel: str, payload: Mapping[str, Any] | None = None,
        output_id: str | None = None, event_id: str | None = None,
        idempotency_key: str | None = None, correlation_id: str | None = None,
        reply_to: str | None = None, text: str | None = None,
        content: str | None = None, max_attempts: int = 3,
        fingerprint_payload: Mapping[str, Any] | None = None,
    ) -> tuple[str, bool]:
        data = dict(payload or {})
        if text is not None:
            data.setdefault("text", text)
        if content is not None:
            data.setdefault("content", content)
        return self.record(
            channel=channel, payload=data, event_id=event_id or output_id,
            idempotency_key=idempotency_key, correlation_id=correlation_id,
            reply_to=reply_to, kind="outbound", max_attempts=max_attempts,
            fingerprint_payload=fingerprint_payload,
        )

    enqueue_outbound = record_outbound
    record_output = record_outbound
    enqueue_output = record_outbound

    def claim(
        self, *, worker_id: str, kind: str | None = None, channel: str | None = None,
        lease_seconds: float = 30.0,
    ) -> dict[str, Any] | None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                query = (
                    "SELECT * FROM communication_events WHERE next_attempt_at<=? "
                    "AND (status IN ('queued','failed') OR "
                    "(status IN ('claimed','processing') AND lease_until<=?)) "
                    "AND attempts < max_attempts"
                )
                args: list[Any] = [now, now]
                if kind is not None:
                    query += " AND kind=?"
                    args.append(kind)
                if channel is not None:
                    query += " AND channel=?"
                    args.append(channel)
                query += " ORDER BY created_at, id LIMIT 1"
                row = self._connection.execute(query, args).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                until = now + lease_seconds
                self._connection.execute(
                    "UPDATE communication_events SET status='claimed', attempts=attempts+1, "
                    "lease_owner=?, lease_until=?, updated_at=? WHERE id=?",
                    (worker_id, until, now, row["id"]),
                )
                self._metric("claimed")
                self._connection.execute("COMMIT")
                result = dict(row)
                result.update({
                    "status": "claimed", "attempts": int(row["attempts"]) + 1,
                    "lease_owner": worker_id, "lease_until": until,
                    "payload": json.loads(row["payload"]),
                })
                return result
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def claim_by_id(
        self, row_id: str, *, worker_id: str, lease_seconds: float = 30.0
    ) -> dict[str, Any] | None:
        """Claim one known row, primarily for channel workers.

        This is intentionally additive to :meth:`claim`: adapters that first
        persist an inbound event can bind its in-memory queue item to the
        exact ledger row without racing another worker.
        """
        if not row_id or not worker_id:
            raise ValueError("row_id and worker_id are required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM communication_events WHERE id=? AND "
                    "(status IN ('queued','failed') OR "
                    "(status IN ('claimed','processing') AND lease_until<=?)) "
                    "AND attempts < max_attempts",
                    (row_id, now),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                until = now + lease_seconds
                self._connection.execute(
                    "UPDATE communication_events SET status='claimed', attempts=attempts+1, "
                    "lease_owner=?, lease_until=?, updated_at=? WHERE id=?",
                    (worker_id, until, now, row_id),
                )
                self._metric("claimed")
                self._connection.execute("COMMIT")
                result = dict(row)
                result.update({
                    "status": "claimed", "attempts": int(row["attempts"]) + 1,
                    "lease_owner": worker_id, "lease_until": until,
                    "payload": json.loads(row["payload"]),
                })
                return result
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def mark_processing(self, row_id: str, *, worker_id: str) -> bool:
        with self._lock:
            changed = self._connection.execute(
                "UPDATE communication_events SET status='processing',updated_at=? "
                "WHERE id=? AND status='claimed' AND lease_owner=? AND lease_until>?",
                (time.time(), row_id, worker_id, time.time()),
            ).rowcount
            return bool(changed)

    def renew_lease(self, row_id: str, *, worker_id: str, lease_seconds: float = 30.0) -> bool:
        """Extend a lease only while its owner still holds it (fencing)."""
        if not worker_id or lease_seconds <= 0:
            raise ValueError("worker_id and positive lease_seconds are required")
        now = time.time()
        with self._lock:
            changed = self._write(
                "UPDATE communication_events SET lease_until=?,updated_at=? "
                "WHERE id=? AND lease_owner=? AND status IN ('claimed','processing') "
                "AND lease_until>?", (now + lease_seconds, now, row_id, worker_id, now)
            ).rowcount
            return bool(changed)

    def ack(self, row_id: str, *, worker_id: str | None = None) -> bool:
        with self._lock:
            now = time.time()
            query = "UPDATE communication_events SET status='delivered',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND status IN ('claimed','processing') AND lease_until>?"
            args: list[Any] = [now, row_id, now]
            # Legacy callers did not pass a worker id.  Keep that API usable
            # while still fencing explicitly identified workers.  The lease
            # must remain live in both cases; an expired claim is never acked.
            if worker_id is not None:
                query += " AND lease_owner=?"
                args.append(worker_id)
            changed = self._connection.execute(query, args).rowcount
            if changed:
                self._metric("delivered")
            return bool(changed)

    acknowledge = ack
    deliver = ack

    def fail(
        self, row_id: str, error: str | BaseException, *, worker_id: str | None = None,
        max_attempts: int | None = None, backoff: float = 1.0,
    ) -> str | None:
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT attempts,max_attempts,lease_owner,lease_until,status FROM communication_events WHERE id=?",
                (row_id,),
            ).fetchone()
            if row is None or row["status"] not in {"claimed", "processing"}:
                return None
            if row["lease_until"] is None or row["lease_until"] <= now:
                return None
            if worker_id is not None and row["lease_owner"] != worker_id:
                return None
            limit = max(1, int(max_attempts if max_attempts is not None else row["max_attempts"]))
            attempts = int(row["attempts"])
            dead = attempts >= limit
            status = "dead_letter" if dead else "failed"
            delay = 0.0 if dead else max(0.0, float(backoff)) * (2 ** max(0, attempts - 1))
            # Fence the state transition in the UPDATE itself.  The SELECT is
            # only used to compute retry policy; another connection may reclaim
            # an expired lease before this write.  Matching the exact owner,
            # status, attempt generation and lease value turns the write into a
            # CAS so a stale worker can never clear a newer worker's claim.
            changed = self._connection.execute(
                "UPDATE communication_events SET status=?,last_error=?,lease_owner=NULL,"
                "lease_until=NULL,next_attempt_at=?,updated_at=? WHERE id=? "
                "AND status=? AND attempts=? AND lease_owner IS ? "
                "AND lease_until=? "
                "AND lease_until>((julianday('now') - 2440587.5) * 86400.0)",
                (
                    status,
                    str(error),
                    now + delay,
                    now,
                    row_id,
                    row["status"],
                    attempts,
                    row["lease_owner"],
                    row["lease_until"],
                ),
            ).rowcount
            if not changed:
                return None
            self._metric("dead_letter" if dead else "retries")
            return status

    def recover_expired_leases(self) -> int:
        now = time.time()
        with self._lock:
            changed = self._connection.execute(
                "UPDATE communication_events SET status=CASE WHEN attempts >= max_attempts THEN 'dead_letter' ELSE 'queued' END,lease_owner=NULL,lease_until=NULL,updated_at=? "
                "WHERE status IN ('claimed','processing') AND lease_until<=?",
                (now, now),
            ).rowcount
            if changed:
                self._metric("lease_recovered", int(changed))
            return int(changed)

    recover_leases = recover_expired_leases

    def replay(self, row_id: str) -> bool:
        with self._lock:
            changed = self._connection.execute(
                "UPDATE communication_events SET status='queued',attempts=0,next_attempt_at=0,"
                "lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=? "
                "WHERE id=? AND status='dead_letter'",
                (time.time(), row_id),
            ).rowcount
            if changed:
                self._metric("replayed")
            return bool(changed)

    replay_dead_letter = replay

    def get(self, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM communication_events WHERE id=?", (row_id,)
            ).fetchone()
        return self._decode(row)

    def get_by_event_id(
        self,
        event_id: str,
        *,
        channel: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            # Old databases stored the raw external event id. Prefer that
            # exact lookup first so their behavior is unchanged.
            row = self._connection.execute(
                "SELECT * FROM communication_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None and channel is not None and kind is not None:
                scoped = self._scoped_external_key(
                    "event", kind=str(kind), channel=str(channel), value=str(event_id)
                )
                row = self._connection.execute(
                    "SELECT * FROM communication_events WHERE event_id=?", (scoped,)
                ).fetchone()
            elif row is None:
                # Preserve the historical unscoped helper for callers that do
                # not yet pass channel/kind. Return a row only when the raw id
                # identifies exactly one new scoped event; ambiguity across
                # channels intentionally yields no result.
                candidates = self._connection.execute(
                    "SELECT * FROM communication_events WHERE event_id LIKE 'event:%'"
                ).fetchall()
                matches = [
                    candidate
                    for candidate in candidates
                    if candidate["event_id"]
                    == self._scoped_external_key(
                        "event",
                        kind=str(candidate["kind"]),
                        channel=str(candidate["channel"]),
                        value=str(event_id),
                    )
                ]
                row = matches[0] if len(matches) == 1 else None
        return self._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def dead_letters(
        self, *, kind: str | None = None, channel: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM communication_events WHERE status='dead_letter'"
            args: list[Any] = []
            if kind is not None:
                query += " AND kind=?"
                args.append(kind)
            if channel is not None:
                query += " AND channel=?"
                args.append(channel)
            query += " ORDER BY updated_at LIMIT ?"
            args.append(limit)
            rows = self._connection.execute(query, args).fetchall()
        return [self._decode(row) for row in rows if row is not None]

    def pending(
        self, *, kind: str | None = None, channel: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM communication_events WHERE status NOT IN ('delivered','dead_letter')"
            args: list[Any] = []
            if kind is not None:
                query += " AND kind=?"
                args.append(kind)
            if channel is not None:
                query += " AND channel=?"
                args.append(channel)
            query += " ORDER BY created_at LIMIT ?"
            args.append(limit)
            rows = self._connection.execute(query, args).fetchall()
        return [self._decode(row) for row in rows if row is not None]

    def get_cursor(self, name: str, *, default: int = 0) -> int:
        """Read a monotonic durable cursor used by polling channels."""
        if not name:
            raise ValueError("cursor name is required")
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM communication_cursors WHERE name=?", (name,)
            ).fetchone()
        return int(row["value"]) if row is not None else int(default)

    def set_cursor(self, name: str, value: int) -> int:
        """Persist a cursor monotonically and return the stored value."""
        if not name:
            raise ValueError("cursor name is required")
        value = int(value)
        if value < 0:
            raise ValueError("cursor value must be non-negative")
        now = time.time()
        with self._lock:
            self._connection.execute(
                "INSERT INTO communication_cursors(name,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=MAX(value,excluded.value), "
                "updated_at=excluded.updated_at",
                (name, value, now),
            )
            row = self._connection.execute(
                "SELECT value FROM communication_cursors WHERE name=?", (name,)
            ).fetchone()
        return int(row["value"])

    def remember_nonce(
        self,
        namespace: str,
        nonce: str,
        *,
        expires_at: float,
    ) -> bool:
        """Atomically remember one replay nonce until ``expires_at``.

        Only a SHA-256 digest is stored.  The unique primary key provides
        cross-thread and cross-process replay exclusion, unlike an in-memory
        cache which is reset on application restart.
        """
        namespace = str(namespace).strip()
        nonce = str(nonce)
        expires_at = float(expires_at)
        if not namespace or not nonce:
            raise ValueError("namespace and nonce are required")
        now = time.time()
        if expires_at <= now:
            return False
        nonce_hash = hashlib.sha256(nonce.encode("utf-8", "replace")).hexdigest()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "DELETE FROM communication_nonces WHERE expires_at<=?",
                    (now,),
                )
                changed = self._connection.execute(
                    "INSERT OR IGNORE INTO communication_nonces"
                    "(namespace,nonce_hash,expires_at,created_at) VALUES (?,?,?,?)",
                    (namespace, nonce_hash, expires_at, now),
                ).rowcount
                self._connection.execute("COMMIT")
                return bool(changed)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def metrics(self) -> dict[str, int | float]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT name,value FROM communication_metrics ORDER BY name"
            ).fetchall()
            result: dict[str, int | float] = {
                str(row["name"]): int(row["value"]) for row in rows
            }
            depth, oldest = self._connection.execute(
                "SELECT COUNT(*), MIN(created_at) FROM communication_events WHERE status NOT IN ('delivered','dead_letter')"
            ).fetchone()
            result["queue_depth"] = int(depth)
            result["oldest_age_seconds"] = max(0.0, time.time() - oldest) if oldest else 0.0
            return result

    stats = metrics

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> CommunicationLedger:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = ["CommunicationLedger", "CommunicationLedgerError", "IdempotencyConflict"]
