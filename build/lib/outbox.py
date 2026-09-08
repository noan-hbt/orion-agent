"""Durable outbound message queue.

The runtime/channel adapter contract is deliberately small: enqueue an item,
claim ready items, deliver them, then ack or fail the claim.  Channel adapters
should treat ``payload`` as opaque JSON and use ``idempotency_key`` for their
own delivery de-duplication.
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class OutboxItem:
    id: int
    channel: str
    payload: Any
    idempotency_key: str
    state: str
    attempts: int
    available_at: float
    lease_until: float | None
    last_error: str | None
    lease_owner: str | None = None


class Outbox:
    """SQLite-backed outbox, safe for multiple workers on one local host."""

    def __init__(self, path: str | Path = "data/outbox.sqlite3", *, clock=time.time,
                 max_backoff: float = 3600.0, base_backoff: float = 1.0,
                 jitter: float = 0.2) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.max_backoff, self.base_backoff, self.jitter = clock, max_backoff, base_backoff, jitter
        self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
          payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          available_at REAL NOT NULL, lease_until REAL, last_error TEXT,
          created_at REAL NOT NULL, sent_at REAL
        );
        CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(state, available_at);
        """)
        try: self._db.execute("ALTER TABLE outbox ADD COLUMN lease_owner TEXT")
        except sqlite3.OperationalError: pass

    def close(self) -> None:
        self._db.close()

    def enqueue(self, channel: str, payload: Any, idempotency_key: str | None = None,
                *, available_at: float | None = None) -> int:
        key = idempotency_key or str(uuid.uuid4())
        now = self.clock()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        cur = self._db.execute(
            "INSERT OR IGNORE INTO outbox(channel,payload,idempotency_key,available_at,created_at) VALUES(?,?,?,?,?)",
            (channel, encoded, key, now if available_at is None else available_at, now))
        if cur.rowcount:
            return int(cur.lastrowid)
        row = self._db.execute("SELECT id,channel,payload FROM outbox WHERE idempotency_key=?", (key,)).fetchone()
        if row and (row["channel"] != channel or row["payload"] != encoded):
            raise ValueError("idempotency_key reused with different content")
        return int(row["id"])

    def claim(self, *, limit: int = 10, lease_seconds: float = 60.0,
              channel: str | None = None, worker_id: str | None = None) -> list[OutboxItem]:
        now = self.clock()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            where = "(state='pending' OR (state='sending' AND lease_until <= ?)) AND available_at <= ?"
            args: list[Any] = [now, now]
            if channel is not None:
                where += " AND channel=?"; args.append(channel)
            rows = self._db.execute(f"SELECT * FROM outbox WHERE {where} ORDER BY id LIMIT ?", (*args, max(0, limit))).fetchall()
            until = now + max(0.1, lease_seconds)
            for row in rows:
                self._db.execute("UPDATE outbox SET state='sending', lease_until=?, lease_owner=?, attempts=attempts+1 WHERE id=?", (until, worker_id, row["id"]))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK"); raise
        return [self._item(r, state="sending", attempts=r["attempts"] + 1, lease_until=until, lease_owner=worker_id) for r in rows]

    def ack(self, item_id: int, worker_id: str | None = None) -> bool:
        now=self.clock(); cur = self._db.execute("UPDATE outbox SET state='sent', lease_until=NULL, lease_owner=NULL, sent_at=? WHERE id=? AND state='sending' AND lease_until>? AND ((? IS NULL AND lease_owner IS NULL) OR (? IS NOT NULL AND lease_owner=?))", (now, item_id, now, worker_id, worker_id, worker_id))
        return bool(cur.rowcount)

    def fail(self, item_id: int, error: str, *, retry: bool = True, retry_at: float | None = None, worker_id: str | None = None) -> bool:
        now = self.clock()
        row = self._db.execute("SELECT attempts,lease_owner,lease_until FROM outbox WHERE id=? AND state='sending'", (item_id,)).fetchone()
        if not row: return False
        if row["lease_until"] is None or row["lease_until"] <= now: return False
        # An omitted worker id denotes an unowned lease; it must not be
        # treated as a wildcard or another worker could ack/fail a live claim.
        if (worker_id is None and row["lease_owner"] is not None) or (
            worker_id is not None and row["lease_owner"] != worker_id
        ): return False
        if retry:
            delay = min(self.max_backoff, self.base_backoff * (2 ** max(0, row[0] - 1)))
            delay *= 1 + random.uniform(-self.jitter, self.jitter)
            when = retry_at if retry_at is not None else now + max(0, delay)
            state = "pending"
        else:
            when, state = self.clock(), "failed"
        cur = self._db.execute("UPDATE outbox SET state=?,available_at=?,lease_until=NULL,lease_owner=NULL,last_error=? WHERE id=? AND state='sending' AND lease_until>? AND ((? IS NULL AND lease_owner IS NULL) OR (? IS NOT NULL AND lease_owner=?))", (state, when, str(error)[:4000], item_id, now, worker_id, worker_id, worker_id))
        return bool(cur.rowcount)

    def renew(self, item_id: int, lease_seconds: float = 60.0, worker_id: str | None = None) -> bool:
        now=self.clock(); cur = self._db.execute("UPDATE outbox SET lease_until=? WHERE id=? AND state='sending' AND lease_until>? AND ((? IS NULL AND lease_owner IS NULL) OR (? IS NOT NULL AND lease_owner=?))", (now + max(.1, lease_seconds), item_id, now, worker_id, worker_id, worker_id))
        return bool(cur.rowcount)

    def release(self, item_id: int, worker_id: str | None = None) -> bool:
        now=self.clock(); cur = self._db.execute("UPDATE outbox SET state='pending', lease_until=NULL, lease_owner=NULL, available_at=? WHERE id=? AND state='sending' AND lease_until>? AND ((? IS NULL AND lease_owner IS NULL) OR (? IS NOT NULL AND lease_owner=?))", (now, item_id, now, worker_id, worker_id, worker_id))
        return bool(cur.rowcount)

    def replay(self, *, include_failed: bool = True, include_sent: bool = False) -> int:
        """Return terminal items to the delivery queue.

        Replaying an item must also invalidate any lease metadata.  This is
        mostly defensive for databases migrated from older versions, but it
        prevents a replayed row from retaining an owner that no longer has a
        claim on it.  ``sent_at`` is cleared because the row is no longer a
        successfully delivered item once it is made pending again.
        """
        states = (("sent",) if include_sent else ()) + (("failed",) if include_failed else ())
        if not states: return 0
        marks = ",".join("?" for _ in states)
        cur = self._db.execute(
            f"UPDATE outbox SET state='pending', available_at=?, lease_until=NULL, "
            f"lease_owner=NULL, sent_at=NULL WHERE state IN ({marks})",
            (self.clock(), *states),
        )
        return cur.rowcount

    def metrics(self) -> dict[str, int]:
        rows = self._db.execute("SELECT state,COUNT(*) n FROM outbox GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def _item(self, r: sqlite3.Row, **overrides: Any) -> OutboxItem:
        return OutboxItem(
            r["id"], r["channel"], json.loads(r["payload"]), r["idempotency_key"],
            overrides.get("state", r["state"]), overrides.get("attempts", r["attempts"]),
            r["available_at"], overrides.get("lease_until", r["lease_until"]),
            r["last_error"], overrides.get("lease_owner", r["lease_owner"]),
        )
