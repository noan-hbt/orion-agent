"""Registre persistant d'actions et protection contre les répétitions.

Le ledger est consulté par le runtime avant un effet de bord. Il ne dépend pas
de la mémoire du LLM et fonctionne avec SQLite, inclus dans Python.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


def normalize_action_value(value: Any) -> Any:
    """Normalise récursivement les valeurs utilisées pour une empreinte."""
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"\s+", " ", normalized).strip()
    if isinstance(value, Mapping):
        return {str(key): normalize_action_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [normalize_action_value(item) for item in value]
    return value


def action_key(
    operation: str,
    arguments: Mapping[str, Any],
    *,
    target: str | None = None,
) -> str:
    """Construit une clé stable pour une opération et ses paramètres."""
    canonical = json.dumps(
        {
            "arguments": normalize_action_value(arguments),
            "target": normalize_action_value(target),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(f"{operation}:{canonical}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionRecord:
    action_key: str
    operation: str
    target: str | None
    arguments: dict[str, Any]
    status: str
    result: Any
    error: str | None
    created_at: float
    updated_at: float
    attempts: int
    owner_id: str | None = None
    lease_until: float | None = None
    fence_token: int = 0

    @property
    def created_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.created_at, tz=timezone.utc)

    @property
    def needs_reconciliation(self) -> bool:
        return self.status == "uncertain"


@dataclass(frozen=True)
class ActionDecision:
    allowed: bool
    action_key: str
    reason: str
    existing: ActionRecord | None = None
    owner_id: str | None = None
    lease_until: float | None = None
    fence_token: int | None = None


class ActionLedger:
    # Terminal rows older than this are eligible for automatic pruning.  Kept
    # far above the default 24h dedupe window so idempotency is unaffected.
    _PRUNE_RETENTION_SECONDS = 604800.0
    """Ledger SQLite thread-safe pour réserver et dédupliquer des actions."""

    def __init__(
        self,
        path: str | Path = "data/action_ledger.sqlite3",
        *,
        owner_id: str | None = None,
        default_lease_seconds: float = 300.0,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if owner_id is not None and not str(owner_id).strip():
            raise ValueError("owner_id must be non-empty when provided")
        if (
            isinstance(default_lease_seconds, bool)
            or not isinstance(default_lease_seconds, (int, float))
            or default_lease_seconds <= 0
        ):
            raise ValueError("default_lease_seconds must be > 0")
        self.owner_id = str(owner_id).strip() if owner_id is not None else uuid.uuid4().hex
        self.default_lease_seconds = float(default_lease_seconds)
        self._lock = threading.RLock()
        self._owned_reservations: dict[str, tuple[str, int]] = {}
        self._closed = False
        # Retention housekeeping is throttled: the DELETE is indexed, but it
        # should not run on every reservation.
        self._prune_interval = 3600.0
        self._last_prune_at = time.time()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS actions (
                action_key TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                target TEXT,
                arguments_json TEXT NOT NULL,
                normalized_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                owner_id TEXT,
                lease_until REAL,
                fence_token INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._migrate_schema()
        self._mark_stale_running_locked(time.time())
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_lookup "
            "ON actions(operation, target, status, created_at)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_status_updated "
            "ON actions(status, updated_at)"
        )
        if int(self._connection.execute("PRAGMA user_version").fetchone()[0]) < 2:
            self._connection.execute("PRAGMA user_version = 2")
        self._connection.commit()

    def _migrate_schema(self) -> None:
        """Upgrade legacy ledgers without making old RUNNING rows replayable."""
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(actions)").fetchall()
        }
        if "updated_at" not in columns:
            self._connection.execute("ALTER TABLE actions ADD COLUMN updated_at REAL")
            columns.add("updated_at")
        if "owner_id" not in columns:
            self._connection.execute("ALTER TABLE actions ADD COLUMN owner_id TEXT")
            columns.add("owner_id")
        if "lease_until" not in columns:
            self._connection.execute("ALTER TABLE actions ADD COLUMN lease_until REAL")
            columns.add("lease_until")
        if "fence_token" not in columns:
            self._connection.execute(
                "ALTER TABLE actions ADD COLUMN fence_token INTEGER NOT NULL DEFAULT 0"
            )

        self._connection.execute(
            "UPDATE actions SET updated_at=COALESCE(updated_at, created_at), "
            "fence_token=COALESCE(fence_token, 0)"
        )
        # A pre-lease RUNNING row may have produced its external effect before
        # the process died. It is therefore unsafe to infer either success or
        # failure, and especially unsafe to reserve it again automatically.
        self._connection.execute(
            "UPDATE actions SET status='uncertain', updated_at=?, "
            "error=COALESCE(error, 'legacy running reservation requires reconciliation') "
            "WHERE status='running' AND (owner_id IS NULL OR lease_until IS NULL)",
            (time.time(),),
        )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @classmethod
    def _bounded_result(cls, value: Any, *, max_chars: int = 4000) -> Any:
        """Empêche le ledger de conserver des snapshots récursifs complets."""
        if isinstance(value, str):
            if len(value) <= max_chars:
                return value
            return {"truncated": True, "preview": value[:max_chars]}
        encoded = cls._json(value)
        if len(encoded) <= max_chars:
            return value
        return {"truncated": True, "preview": encoded[:max_chars]}

    @staticmethod
    def _record(row: sqlite3.Row | None) -> ActionRecord | None:
        if row is None:
            return None
        return ActionRecord(
            action_key=row["action_key"],
            operation=row["operation"],
            target=row["target"],
            arguments=json.loads(row["arguments_json"]),
            status=row["status"],
            result=ActionLedger._bounded_result(
                json.loads(row["result_json"]) if row["result_json"] else None
            ),
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            attempts=row["attempts"],
            owner_id=row["owner_id"],
            lease_until=row["lease_until"],
            fence_token=int(row["fence_token"] or 0),
        )

    def _mark_stale_running_locked(self, now: float) -> int:
        """Turn expired reservations into an explicit reconciliation state."""
        cursor = self._connection.execute(
            "UPDATE actions SET status='uncertain', updated_at=?, "
            "error=COALESCE(error, 'reservation lease expired; reconciliation required') "
            "WHERE status='running' AND (lease_until IS NULL OR lease_until <= ?)",
            (now, now),
        )
        return int(cursor.rowcount)

    def reserve(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        target: str | None = None,
        dedupe_window: float = 86400.0,
        allow_repeat: bool = False,
        owner_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> ActionDecision:
        """Réserve une action ou retourne la raison de son blocage.

        Les doublons exacts sont toujours détectés par clé. Les actions proches
        sont bloquées lorsqu'elles ciblent la même cible dans la fenêtre
        configurée, sauf si ``allow_repeat=True`` est demandé par du code de
        confiance.
        """
        key = action_key(operation, arguments, target=target)
        now = time.time()
        normalized = self._json(normalize_action_value(arguments))
        owner = self.owner_id if owner_id is None else str(owner_id).strip()
        if not owner:
            raise ValueError("owner_id must be non-empty")
        lease = self.default_lease_seconds if lease_seconds is None else lease_seconds
        if (
            isinstance(lease, bool)
            or not isinstance(lease, (int, float))
            or lease <= 0
        ):
            raise ValueError("lease_seconds must be > 0")
        lease_until = now + float(lease)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._mark_stale_running_locked(now)
                if now - self._last_prune_at >= self._prune_interval:
                    self._last_prune_at = now
                    try:
                        self._connection.execute(
                            "DELETE FROM actions WHERE status IN "
                            "('succeeded', 'failed') AND updated_at < ?",
                            (now - self._PRUNE_RETENTION_SECONDS,),
                        )
                    except sqlite3.Error:
                        # Housekeeping must never fail a reservation.
                        pass
                row = self._connection.execute(
                    "SELECT * FROM actions WHERE action_key = ?", (key,)
                ).fetchone()
                existing = self._record(row)
                if existing is not None:
                    if existing.status == "running":
                        decision = ActionDecision(
                            False, key, "already_running", existing,
                            existing.owner_id, existing.lease_until, existing.fence_token,
                        )
                        self._connection.commit()
                        return decision
                    if existing.status == "succeeded":
                        decision = ActionDecision(False, key, "already_succeeded", existing)
                        self._connection.commit()
                        return decision
                    if existing.status == "uncertain":
                        decision = ActionDecision(
                            False, key, "needs_reconciliation", existing,
                            existing.owner_id, existing.lease_until, existing.fence_token,
                        )
                        self._connection.commit()
                        return decision

                if not allow_repeat and dedupe_window > 0:
                    since = now - dedupe_window
                    rows = self._connection.execute(
                        "SELECT * FROM actions WHERE operation = ? AND status IN "
                        "('running', 'succeeded', 'uncertain') AND created_at >= ?",
                        (operation, since),
                    ).fetchall()
                    for candidate_row in rows:
                        candidate = self._record(candidate_row)
                        if candidate is None or (
                            target is not None and candidate.target != target
                        ):
                            continue
                        candidate_normalized = self._json(
                            normalize_action_value(candidate.arguments)
                        )
                        if SequenceMatcher(None, normalized, candidate_normalized).ratio() >= 0.92:
                            decision = ActionDecision(
                                False,
                                key,
                                (
                                    "needs_reconciliation"
                                    if candidate.status == "uncertain"
                                    else "potential_duplicate"
                                ),
                                candidate,
                            )
                            self._connection.commit()
                            return decision

                if existing is None:
                    fence_token = 1
                    self._connection.execute(
                        "INSERT INTO actions(action_key, operation, target, arguments_json, "
                        "normalized_json, status, created_at, updated_at, attempts, owner_id, "
                        "lease_until, fence_token) "
                        "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, 1, ?, ?, ?)",
                        (
                            key,
                            operation,
                            target,
                            self._json(arguments),
                            normalized,
                            now,
                            now,
                            owner,
                            lease_until,
                            fence_token,
                        ),
                    )
                else:
                    fence_token = existing.fence_token + 1
                    self._connection.execute(
                        "UPDATE actions SET status='running', error=NULL, result_json=NULL, "
                        "updated_at=?, attempts=attempts + 1, owner_id=?, lease_until=?, "
                        "fence_token=? WHERE action_key=? AND status='failed'",
                        (now, owner, lease_until, fence_token, key),
                    )
                self._connection.commit()
                self._owned_reservations[key] = (owner, fence_token)
                return ActionDecision(
                    True,
                    key,
                    "reserved",
                    owner_id=owner,
                    lease_until=lease_until,
                    fence_token=fence_token,
                )
            except Exception:
                self._connection.rollback()
                raise

    def _reservation_identity(
        self,
        key: str,
        owner_id: str | None,
        fence_token: int | None,
    ) -> tuple[str, int | None]:
        cached = self._owned_reservations.get(key)
        owner = (
            str(owner_id)
            if owner_id is not None
            else cached[0]
            if cached is not None
            else self.owner_id
        )
        fence = (
            int(fence_token)
            if fence_token is not None
            else cached[1]
            if cached is not None
            else None
        )
        return owner, fence

    def complete(
        self,
        key: str,
        result: Any = None,
        *,
        owner_id: str | None = None,
        fence_token: int | None = None,
    ) -> ActionRecord | None:
        """Marque une action comme réussie et conserve son résultat."""
        with self._lock:
            now = time.time()
            owner, fence = self._reservation_identity(key, owner_id, fence_token)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._mark_stale_running_locked(now)
                if fence is not None:
                    cursor = self._connection.execute(
                        "UPDATE actions SET status='succeeded', result_json=?, error=NULL, "
                        "updated_at=?, lease_until=NULL WHERE action_key=? AND status='running' "
                        "AND owner_id=? AND fence_token=? AND lease_until>?",
                        (
                            self._json(self._bounded_result(result)),
                            now,
                            key,
                            owner,
                            fence,
                            now,
                        ),
                    )
                    if cursor.rowcount:
                        self._owned_reservations.pop(key, None)
                row = self._connection.execute(
                    "SELECT * FROM actions WHERE action_key=?", (key,)
                ).fetchone()
                self._connection.commit()
                return self._record(row)
            except Exception:
                self._connection.rollback()
                raise

    def fail(
        self,
        key: str,
        error: str,
        *,
        owner_id: str | None = None,
        fence_token: int | None = None,
    ) -> ActionRecord | None:
        """Marque une action échouée ; elle pourra être retentée plus tard."""
        with self._lock:
            now = time.time()
            owner, fence = self._reservation_identity(key, owner_id, fence_token)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._mark_stale_running_locked(now)
                if fence is not None:
                    cursor = self._connection.execute(
                        "UPDATE actions SET status='failed', error=?, updated_at=?, "
                        "lease_until=NULL WHERE action_key=? AND status='running' "
                        "AND owner_id=? AND fence_token=? AND lease_until>?",
                        (error, now, key, owner, fence, now),
                    )
                    if cursor.rowcount:
                        self._owned_reservations.pop(key, None)
                row = self._connection.execute(
                    "SELECT * FROM actions WHERE action_key=?", (key,)
                ).fetchone()
                self._connection.commit()
                return self._record(row)
            except Exception:
                self._connection.rollback()
                raise

    def mark_uncertain(
        self,
        key: str,
        error: str,
        *,
        owner_id: str | None = None,
        fence_token: int | None = None,
    ) -> ActionRecord | None:
        """Fence a dispatched action into reconciliation-required state.

        This transition is for failures observed *after dispatch*, where the
        external system may already have applied the effect.  Unlike ``fail``,
        UNCERTAIN is never automatically reservable/retryable.  An expired
        reservation is first promoted to the same safe state, while a stale
        owner/fence cannot alter a newer reservation.
        """
        with self._lock:
            now = time.time()
            owner, fence = self._reservation_identity(key, owner_id, fence_token)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._mark_stale_running_locked(now)
                if fence is not None:
                    cursor = self._connection.execute(
                        "UPDATE actions SET status='uncertain', result_json=NULL, error=?, "
                        "updated_at=?, lease_until=NULL WHERE action_key=? AND status='running' "
                        "AND owner_id=? AND fence_token=? AND lease_until>?",
                        (str(error)[:4000], now, key, owner, fence, now),
                    )
                    if cursor.rowcount:
                        self._owned_reservations.pop(key, None)
                row = self._connection.execute(
                    "SELECT * FROM actions WHERE action_key=?", (key,)
                ).fetchone()
                self._connection.commit()
                return self._record(row)
            except Exception:
                self._connection.rollback()
                raise

    def prune(self, *, retention_seconds: float = 604800.0) -> int:
        """Delete terminal action rows older than ``retention_seconds``.

        Nothing ever removed rows from ``actions``, so the table (and the
        near-duplicate scan in ``reserve``, which reads every row of an
        operation inside the dedupe window) grew for the life of the
        installation.  Only ``succeeded``/``failed`` rows are eligible and only
        once they are older than the retention window, which is far longer than
        the default 24h dedupe window, so idempotency semantics are unchanged.
        ``running`` and ``uncertain`` rows are never touched: the first is live
        and the second still needs reconciliation.
        """
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, (int, float))
            or retention_seconds <= 0
        ):
            raise ValueError("retention_seconds must be > 0")
        cutoff = time.time() - float(retention_seconds)
        with self._lock:
            if self._closed:
                return 0
            cursor = self._connection.execute(
                "DELETE FROM actions WHERE status IN ('succeeded', 'failed') "
                "AND updated_at < ?",
                (cutoff,),
            )
            self._connection.commit()
            return int(cursor.rowcount or 0)

    def get(self, key: str) -> ActionRecord | None:
        with self._lock:
            now = time.time()
            self._mark_stale_running_locked(now)
            row = self._connection.execute(
                "SELECT * FROM actions WHERE action_key = ?", (key,)
            ).fetchone()
            self._connection.commit()
            return self._record(row)

    def recent(self, *, operation: str | None = None, limit: int = 50) -> list[ActionRecord]:
        with self._lock:
            self._mark_stale_running_locked(time.time())
            if operation:
                rows = self._connection.execute(
                    "SELECT * FROM actions WHERE operation=? ORDER BY created_at DESC LIMIT ?",
                    (operation, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            self._connection.commit()
            return [record for row in rows if (record := self._record(row)) is not None]

    def needs_reconciliation(self, *, limit: int = 50) -> list[ActionRecord]:
        """Return actions whose external effect can no longer be inferred safely."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        with self._lock:
            self._mark_stale_running_locked(time.time())
            rows = self._connection.execute(
                "SELECT * FROM actions WHERE status='uncertain' "
                "ORDER BY updated_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            self._connection.commit()
            return [record for row in rows if (record := self._record(row)) is not None]

    def reconcile(
        self,
        key: str,
        *,
        outcome: str,
        result: Any = None,
        error: str | None = None,
    ) -> ActionRecord:
        """Resolve an UNCERTAIN action after an external source of truth is checked."""
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("outcome must be 'succeeded' or 'failed'")
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._mark_stale_running_locked(now)
                row = self._connection.execute(
                    "SELECT * FROM actions WHERE action_key=?", (key,)
                ).fetchone()
                record = self._record(row)
                if record is None:
                    raise KeyError(key)
                if record.status != "uncertain":
                    raise ValueError("action does not need reconciliation")
                if outcome == "succeeded":
                    self._connection.execute(
                        "UPDATE actions SET status='succeeded', result_json=?, error=NULL, "
                        "updated_at=?, lease_until=NULL WHERE action_key=? AND status='uncertain'",
                        (self._json(self._bounded_result(result)), now, key),
                    )
                else:
                    self._connection.execute(
                        "UPDATE actions SET status='failed', result_json=NULL, error=?, "
                        "updated_at=?, lease_until=NULL WHERE action_key=? AND status='uncertain'",
                        (error or "reconciled as not completed", now, key),
                    )
                row = self._connection.execute(
                    "SELECT * FROM actions WHERE action_key=?", (key,)
                ).fetchone()
                self._connection.commit()
                self._owned_reservations.pop(key, None)
                reconciled = self._record(row)
                assert reconciled is not None
                return reconciled
            except Exception:
                self._connection.rollback()
                raise

    @staticmethod
    def _unavailable_snapshot() -> dict[str, Any]:
        """Return the stable shape used when the SQLite handle is closed."""
        return {
            "component": "action_ledger",
            "available": False,
            "closed": True,
            "total": None,
            "status_counts": {
                "running": None,
                "uncertain": None,
                "succeeded": None,
                "failed": None,
            },
            "needs_reconciliation": None,
            "uncertain_action_keys": [],
            "oldest_uncertain_age_seconds": None,
        }

    def snapshot(
        self,
        *,
        uncertain_limit: int = 20,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return compact, read-only operational state without action payloads.

        Expired RUNNING leases are reported as effectively ``uncertain`` even
        before another ledger operation performs the durable status migration.
        Snapshot collection itself never reconciles, retries, or mutates rows.
        Only stable action hashes are exposed for operator follow-up.
        """
        if uncertain_limit < 0:
            raise ValueError("uncertain_limit must be >= 0")
        limit = min(int(uncertain_limit), 100)
        current_time = time.time() if now is None else float(now)

        with self._lock:
            if self._closed:
                return self._unavailable_snapshot()

            raw_counts = {
                str(row["status"]): int(row["count"])
                for row in self._connection.execute(
                    "SELECT status, COUNT(*) AS count FROM actions GROUP BY status"
                ).fetchall()
            }
            expired_running = self._connection.execute(
                "SELECT COUNT(*) AS count, "
                "MIN(COALESCE(lease_until, updated_at)) AS oldest_at "
                "FROM actions WHERE status='running' "
                "AND (lease_until IS NULL OR lease_until <= ?)",
                (current_time,),
            ).fetchone()
            uncertain = self._connection.execute(
                "SELECT COUNT(*) AS count, MIN(updated_at) AS oldest_at "
                "FROM actions WHERE status='uncertain'"
            ).fetchone()

            expired_count = int(expired_running["count"] or 0)
            uncertain_count = int(uncertain["count"] or 0) + expired_count
            counts = {
                "running": max(0, int(raw_counts.get("running", 0)) - expired_count),
                "uncertain": uncertain_count,
                "succeeded": int(raw_counts.get("succeeded", 0)),
                "failed": int(raw_counts.get("failed", 0)),
            }

            oldest_candidates = [
                float(value)
                for value in (
                    uncertain["oldest_at"],
                    expired_running["oldest_at"],
                )
                if value is not None
            ]

            action_keys: list[str] = []
            if limit:
                rows = self._connection.execute(
                    "SELECT action_key FROM ("
                    "SELECT action_key, updated_at AS uncertain_since "
                    "FROM actions WHERE status='uncertain' "
                    "UNION ALL "
                    "SELECT action_key, COALESCE(lease_until, updated_at) AS uncertain_since "
                    "FROM actions WHERE status='running' "
                    "AND (lease_until IS NULL OR lease_until <= ?)"
                    ") ORDER BY uncertain_since ASC, action_key ASC LIMIT ?",
                    (current_time, limit),
                ).fetchall()
                action_keys = [str(row["action_key"]) for row in rows]

            total = sum(int(value) for value in raw_counts.values())
            return {
                "component": "action_ledger",
                "available": True,
                "closed": False,
                "total": total,
                "status_counts": counts,
                "needs_reconciliation": uncertain_count,
                "uncertain_action_keys": action_keys,
                "oldest_uncertain_age_seconds": (
                    max(0.0, current_time - min(oldest_candidates))
                    if oldest_candidates
                    else None
                ),
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> ActionLedger:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "ActionDecision",
    "ActionLedger",
    "ActionRecord",
    "action_key",
    "normalize_action_value",
]
