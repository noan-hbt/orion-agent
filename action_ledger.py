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
import unicodedata
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

    @property
    def created_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.created_at, tz=timezone.utc)


@dataclass(frozen=True)
class ActionDecision:
    allowed: bool
    action_key: str
    reason: str
    existing: ActionRecord | None = None


class ActionLedger:
    """Ledger SQLite thread-safe pour réserver et dédupliquer des actions."""

    def __init__(self, path: str | Path = "data/action_ledger.sqlite3") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
                attempts INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_lookup "
            "ON actions(operation, target, status, created_at)"
        )
        self._connection.commit()

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
        )

    def reserve(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        target: str | None = None,
        dedupe_window: float = 86400.0,
        allow_repeat: bool = False,
    ) -> ActionDecision:
        """Réserve une action ou retourne la raison de son blocage.

        Les doublons exacts sont toujours détectés par clé. Les actions proches
        sont bloquées lorsqu'elles ciblent la même cible dans la fenêtre
        configurée, sauf si ``allow_repeat=True`` est demandé par du code de
        confiance.
        """
        key = action_key(operation, arguments, target=target)
        now = datetime.now(timezone.utc).timestamp()
        normalized = self._json(normalize_action_value(arguments))
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM actions WHERE action_key = ?", (key,)
            ).fetchone()
            existing = self._record(row)
            if existing is not None and existing.status in {"running", "succeeded"}:
                return ActionDecision(False, key, "already_" + existing.status, existing)

            if not allow_repeat and dedupe_window > 0:
                since = now - dedupe_window
                rows = self._connection.execute(
                    "SELECT * FROM actions WHERE operation = ? AND status IN "
                    "('running', 'succeeded') AND created_at >= ?",
                    (operation, since),
                ).fetchall()
                for candidate_row in rows:
                    candidate = self._record(candidate_row)
                    if candidate is None or (target is not None and candidate.target != target):
                        continue
                    candidate_normalized = self._json(
                        normalize_action_value(candidate.arguments)
                    )
                    if SequenceMatcher(None, normalized, candidate_normalized).ratio() >= 0.92:
                        return ActionDecision(False, key, "potential_duplicate", candidate)

            if existing is None:
                self._connection.execute(
                    "INSERT INTO actions(action_key, operation, target, arguments_json, "
                    "normalized_json, status, created_at, updated_at, attempts) "
                    "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, 1)",
                    (key, operation, target, self._json(arguments), normalized, now, now),
                )
            else:
                self._connection.execute(
                    "UPDATE actions SET status='running', error=NULL, result_json=NULL, "
                    "updated_at=?, attempts=attempts + 1 WHERE action_key=?",
                    (now, key),
                )
            self._connection.commit()
            return ActionDecision(True, key, "reserved")

    def complete(self, key: str, result: Any = None) -> ActionRecord | None:
        """Marque une action comme réussie et conserve son résultat."""
        with self._lock:
            now = datetime.now(timezone.utc).timestamp()
            self._connection.execute(
                "UPDATE actions SET status='succeeded', result_json=?, error=NULL, "
                "updated_at=? WHERE action_key=?",
                (self._json(self._bounded_result(result)), now, key),
            )
            self._connection.commit()
            return self.get(key)

    def fail(self, key: str, error: str) -> ActionRecord | None:
        """Marque une action échouée ; elle pourra être retentée plus tard."""
        with self._lock:
            now = datetime.now(timezone.utc).timestamp()
            self._connection.execute(
                "UPDATE actions SET status='failed', error=?, updated_at=? WHERE action_key=?",
                (error, now, key),
            )
            self._connection.commit()
            return self.get(key)

    def get(self, key: str) -> ActionRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM actions WHERE action_key = ?", (key,)
            ).fetchone()
            return self._record(row)

    def recent(self, *, operation: str | None = None, limit: int = 50) -> list[ActionRecord]:
        with self._lock:
            if operation:
                rows = self._connection.execute(
                    "SELECT * FROM actions WHERE operation=? ORDER BY created_at DESC LIMIT ?",
                    (operation, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [record for row in rows if (record := self._record(row)) is not None]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

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
