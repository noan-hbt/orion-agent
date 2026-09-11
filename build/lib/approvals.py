"""Persistent human-in-the-loop approvals.

This module deliberately has no runtime imports: callers can use it from
channels, workers, or the CLI without introducing dependency cycles.
"""
from __future__ import annotations
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class ApprovalStore:
    """SQLite-backed approval lifecycle (pending, approved, rejected, expired)."""
    def __init__(self, path: str = "data/approvals.sqlite3"):
        self.path = path
        self._lock = threading.RLock()
        self._memory_db: sqlite3.Connection | None = None
        self._subscribers: list[Callable[[dict[str, Any]], Any]] = []
        if path != ":memory:":
            import os
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, requester TEXT NOT NULL, scope TEXT NOT NULL,
                correlation_id TEXT, status TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT, decided_at TEXT,
                decided_by TEXT, decision_reason TEXT)""")
    def _connect(self):
        if self.path == ":memory:":
            if self._memory_db is None:
                self._memory_db = sqlite3.connect(
                    self.path, timeout=30, check_same_thread=False
                )
                self._memory_db.row_factory = sqlite3.Row
            return self._memory_db
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _db(self):
        """Open a transaction, keeping the in-memory connection alive."""
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            if self.path != ":memory:":
                db.close()

    @staticmethod
    def _expire_due(db, *, now: str, approval_id: str | None = None) -> list[str]:
        select = (
            "SELECT id FROM approvals WHERE status='pending' "
            "AND expires_at IS NOT NULL AND expires_at <= ?"
        )
        select_args: list[Any] = [now]
        if approval_id is not None:
            select += " AND id=?"
            select_args.append(approval_id)
        expired_ids = [str(row[0]) for row in db.execute(select, select_args).fetchall()]
        if not expired_ids:
            return []
        query = (
            "UPDATE approvals SET status='expired', decided_at=? "
            "WHERE status='pending' AND expires_at IS NOT NULL AND expires_at <= ?"
        )
        args: list[Any] = [now, now]
        if approval_id is not None:
            query += " AND id=?"
            args.append(approval_id)
        db.execute(query, args)
        return expired_ids

    def subscribe(self, callback: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        """Subscribe to approved/rejected/expired decisions without runtime coupling."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def _notify(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with self._lock:
            callbacks = list(self._subscribers)
        for item in items:
            for callback in callbacks:
                try:
                    callback(dict(item))
                except Exception:
                    # Approval persistence must not fail because an observer is
                    # unavailable; delivery consumers remain best-effort.
                    pass
    @staticmethod
    def _row(row):
        if row is None:
            return None
        import json
        return {**dict(row), "payload": json.loads(row["payload"])}
    def create(self, requester: str, scope: str, payload: dict[str, Any] | None = None,
               correlation_id: str | None = None, expires_at: str | None = None, approval_id: str | None = None):
        import json
        item = (approval_id or str(uuid.uuid4()), str(requester), str(scope), correlation_id,
                "pending", json.dumps(payload or {}, ensure_ascii=False), _now(), expires_at, None, None, None)
        with self._lock, self._db() as db:
            db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?)", item)
        return self.get(item[0])
    def get(self, approval_id: str):
        expired: list[dict[str, Any]] = []
        with self._lock, self._db() as db:
            now = _now()
            expired_ids = self._expire_due(db, now=now, approval_id=approval_id)
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            result = self._row(row)
            if result is not None and result["id"] in expired_ids:
                expired.append(result)
        self._notify(expired)
        return result
    def _decide(self, approval_id, status, decided_by=None, reason=None):
        decided = False
        expired: list[dict[str, Any]] = []
        with self._lock, self._db() as db:
            now = _now()
            expired_ids = self._expire_due(db, now=now, approval_id=approval_id)
            if expired_ids:
                expired_row = db.execute(
                    "SELECT * FROM approvals WHERE id=?", (approval_id,)
                ).fetchone()
                expired_item = self._row(expired_row)
                if expired_item is not None:
                    expired.append(expired_item)
            cur = db.execute(
                "UPDATE approvals SET status=?,decided_at=?,decided_by=?,decision_reason=? "
                "WHERE id=? AND status='pending' AND (expires_at IS NULL OR expires_at > ?)",
                (status, now, decided_by, reason, approval_id, now),
            )
            decided = bool(cur.rowcount)
        self._notify(expired)
        if not decided:
            raise ValueError("approval inexistante, expiree ou deja decidee")
        result = self.get(approval_id)
        if result is not None:
            self._notify([result])
        return result
    def approve(self, approval_id, decided_by=None, reason=None): return self._decide(approval_id,"approved",decided_by,reason)
    def reject(self, approval_id, decided_by=None, reason=None): return self._decide(approval_id,"rejected",decided_by,reason)
    def pending(self):
        expired: list[dict[str, Any]] = []
        with self._lock, self._db() as db:
            now = _now()
            expired_ids = self._expire_due(db, now=now)
            if expired_ids:
                placeholders = ",".join("?" for _ in expired_ids)
                expired = [
                    self._row(row)
                    for row in db.execute(
                        f"SELECT * FROM approvals WHERE id IN ({placeholders})", expired_ids
                    ).fetchall()
                ]
            rows = db.execute("SELECT * FROM approvals WHERE status='pending'").fetchall()
        self._notify([item for item in expired if item is not None])
        return [self._row(r) for r in rows]
