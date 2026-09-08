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
from typing import Any

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class ApprovalStore:
    """SQLite-backed approval lifecycle (pending, approved, rejected, expired)."""
    def __init__(self, path: str = "data/approvals.sqlite3"):
        self.path = path; self._lock = threading.RLock()
        if path != ":memory:":
            import os; os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, requester TEXT NOT NULL, scope TEXT NOT NULL,
                correlation_id TEXT, status TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT, decided_at TEXT,
                decided_by TEXT, decision_reason TEXT)""")
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row; return db

    @contextmanager
    def _db(self):
        """Open, commit/rollback and close one short-lived SQLite session."""
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    @staticmethod
    def _row(row):
        if row is None: return None
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
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            result = self._row(row)
            if result and result["status"] == "pending" and result["expires_at"] and result["expires_at"] <= _now():
                db.execute("UPDATE approvals SET status='expired', decided_at=? WHERE id=?", (_now(), approval_id)); result["status"] = "expired"
            return result
    def _decide(self, approval_id, status, decided_by=None, reason=None):
        with self._lock, self._db() as db:
            cur = db.execute("UPDATE approvals SET status=?,decided_at=?,decided_by=?,decision_reason=? WHERE id=? AND status='pending'", (status,_now(),decided_by,reason,approval_id))
            if not cur.rowcount: raise ValueError("approval inexistante ou deja decidee")
        return self.get(approval_id)
    def approve(self, approval_id, decided_by=None, reason=None): return self._decide(approval_id,"approved",decided_by,reason)
    def reject(self, approval_id, decided_by=None, reason=None): return self._decide(approval_id,"rejected",decided_by,reason)
    def pending(self):
        with self._lock, self._db() as db: rows = db.execute("SELECT * FROM approvals WHERE status='pending'").fetchall()
        return [self._row(r) for r in rows]
