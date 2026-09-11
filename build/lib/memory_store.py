"""Small, local-first, namespace isolated memory store."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
import re
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class MemoryItem:
    id: str
    namespace: str
    content: str
    provenance: str | None = None
    confidence: float = 1.0
    consent: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float | None = None
    # Control-plane metadata.  These are appended to keep the original
    # positional constructor/API backwards compatible.
    kind: str = "fact"
    scope: str = "default"
    freshness: float = 1.0
    status: str = "active"
    supports: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()

class MemoryStore:
    def __init__(
        self,
        path: str = ":memory:",
        *,
        max_items: int = 10000,
        max_content_chars: int = 20000,
        purge_interval: float = 30.0,
        clock: Callable[[], float] | None = None,
    ):
        if max_items < 1 or max_content_chars < 1:
            raise ValueError("memory limits must be positive")
        if float(purge_interval) < 0:
            raise ValueError("purge_interval must be positive or zero")
        self.path, self.max_items, self.max_content_chars = path, int(max_items), int(max_content_chars)
        self._clock = clock or time.time
        self._purge_interval = float(purge_interval)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, namespace TEXT NOT NULL, content TEXT NOT NULL,
            provenance TEXT, confidence REAL NOT NULL, consent INTEGER NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
            kind TEXT NOT NULL DEFAULT 'fact', scope TEXT NOT NULL DEFAULT 'default',
            freshness REAL NOT NULL DEFAULT 1.0, status TEXT NOT NULL DEFAULT 'active',
            supports TEXT NOT NULL DEFAULT '[]', contradicts TEXT NOT NULL DEFAULT '[]')""")
        # Existing on-disk stores predate the control plane.  Migrate them in
        # place rather than requiring callers to recreate their database.
        columns = {r[1] for r in self._db.execute("PRAGMA table_info(memories)")}
        additions = {
            "kind": "TEXT NOT NULL DEFAULT 'fact'",
            "scope": "TEXT NOT NULL DEFAULT 'default'",
            "freshness": "REAL NOT NULL DEFAULT 1.0",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "supports": "TEXT NOT NULL DEFAULT '[]'",
            "contradicts": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
        self._db.execute("CREATE INDEX IF NOT EXISTS memories_ns_time ON memories(namespace, updated_at DESC)")
        self._db.commit()
        self._next_expiry_at = self._db.execute(
            "SELECT MIN(expires_at) FROM memories WHERE expires_at IS NOT NULL"
        ).fetchone()[0]
        self._next_purge_at = self._clock() + self._purge_interval

    def _maybe_purge_locked(self, now: float) -> None:
        """Amortize physical expiry cleanup without weakening read semantics.

        Reads always filter TTL in SQL, so expired rows can safely remain on
        disk until this bounded cleanup point.  ``_next_expiry_at`` avoids even
        probing SQLite while no expiry can be due, and ``_next_purge_at`` keeps
        a cluster of expirations from turning every read into DELETE+COMMIT.
        """
        next_expiry = self._next_expiry_at
        if next_expiry is None or now < float(next_expiry):
            return
        if now < self._next_purge_at:
            return

        cursor = self._db.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        if cursor.rowcount:
            self._db.commit()
        else:
            # DELETE opens a transaction even when it matches nothing. Avoid
            # leaving a read-triggered write transaction open.
            self._db.rollback()
        self._next_expiry_at = self._db.execute(
            "SELECT MIN(expires_at) FROM memories WHERE expires_at IS NOT NULL"
        ).fetchone()[0]
        self._next_purge_at = now + self._purge_interval

    @staticmethod
    def _item(row: sqlite3.Row) -> MemoryItem:
        values = dict(row)
        values["consent"] = bool(values["consent"])
        for key in ("supports", "contradicts"):
            try:
                values[key] = tuple(json.loads(values.get(key) or "[]"))
            except (TypeError, ValueError):
                values[key] = ()
        return MemoryItem(**values)

    def put(self, content: str, *, namespace: str = "default", provenance: str | None = None,
            confidence: float = 1.0, consent: bool = True, ttl: float | None = None,
            item_id: str | None = None, kind: str = "fact", scope: str | None = None,
            freshness: float = 1.0, status: str = "active",
            supports: list[str] | tuple[str, ...] = (),
            contradicts: list[str] | tuple[str, ...] = ()) -> MemoryItem:
        if not namespace or len(content) > self.max_content_chars:
            raise ValueError("invalid memory namespace or content size")
        if not 0 <= float(confidence) <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not 0 <= float(freshness) <= 1:
            raise ValueError("freshness must be between 0 and 1")
        if status not in {"active", "tombstone", "superseded", "retracted"}:
            raise ValueError("invalid memory status")
        now = self._clock()
        ident = item_id or uuid.uuid4().hex
        expires = now + float(ttl) if ttl is not None else None
        scope = scope or namespace
        with self._lock:
            self._maybe_purge_locked(now)
            self._db.execute("INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ident, namespace, str(content), provenance, float(confidence), int(bool(consent)), now, now, expires, str(kind), str(scope), float(freshness), status, json.dumps(list(supports)), json.dumps(list(contradicts))))
            # Expired rows do not consume the namespace capacity. Cleanup is
            # folded into this already-writing transaction instead of causing
            # an additional purge commit.
            self._db.execute(
                "DELETE FROM memories WHERE namespace=? AND id NOT IN ("
                "SELECT id FROM memories WHERE namespace=? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY updated_at DESC LIMIT ?)",
                (namespace, namespace, now, self.max_items),
            )
            self._db.commit()
            if expires is not None and expires > now and (
                self._next_expiry_at is None or expires < float(self._next_expiry_at)
            ):
                self._next_expiry_at = expires
            return self.get(ident, namespace=namespace)  # type: ignore[return-value]

    def get(self, item_id: str, *, namespace: str = "default") -> MemoryItem | None:
        now = self._clock()
        with self._lock:
            self._maybe_purge_locked(now)
            row = self._db.execute(
                "SELECT * FROM memories WHERE id=? AND namespace=? "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (item_id, namespace, now),
            ).fetchone()
            return self._item(row) if row else None

    def search(self, query: str = "", *, namespace: str = "default", limit: int = 20,
               include_without_consent: bool = False, scope: str | None = None,
               min_freshness: float = 0.0, now: float | None = None) -> list[MemoryItem]:
        """Return a bounded, deterministic set of eligible memories.

        Eligibility is deliberately enforced at the store boundary: namespace and
        optional scope, consent, active status, and TTL freshness.  Results are
        ranked by lexical term overlap, confidence/freshness, then recency and id.
        ``now`` is injectable to make callers and tests deterministic.
        """
        current_time = self._clock()
        eligibility_time = current_time if now is None else float(now)
        limit = max(1, min(int(limit), 100))
        args: list[Any] = [namespace]
        min_freshness = float(min_freshness)
        if not 0 <= min_freshness <= 1:
            raise ValueError("min_freshness must be between 0 and 1")
        sql = "SELECT * FROM memories WHERE namespace=? AND status='active' AND freshness>=?"
        args.append(min_freshness)
        sql += " AND (expires_at IS NULL OR expires_at > ?)"
        args.append(eligibility_time)
        if not include_without_consent:
            sql += " AND consent=1"
        if scope is not None:
            sql += " AND scope=?"
            args.append(str(scope))
        # Fetch a bounded candidate window, then rank in Python for portable,
        # stable relevance semantics (SQLite FTS is intentionally avoided).
        with self._lock:
            self._maybe_purge_locked(current_time)
            rows = self._db.execute(sql + " ORDER BY updated_at DESC, id ASC LIMIT ?", (*args, min(1000, max(limit * 10, limit)))).fetchall()
        terms = set(re.findall(r"[\w]+", query.casefold()))
        items = [self._item(row) for row in rows]
        if terms:
            # A non-empty query is a relevance request, not a recency fallback.
            # Returning zero-overlap memories makes unrelated facts look like
            # evidence and can bias the agent simply because the store is small.
            items = [
                item
                for item in items
                if terms & set(re.findall(r"[\w]+", item.content.casefold()))
            ]
        def key(item: MemoryItem):
            words = set(re.findall(r"[\w]+", item.content.casefold()))
            overlap = len(terms & words) if terms else 0
            phrase = 1 if query and query.casefold() in item.content.casefold() else 0
            return (-phrase, -overlap, -item.confidence, -item.freshness, -item.updated_at, item.id)
        return sorted(items, key=key)[:limit]

    def assert_item(self, item_id: str, *, namespace: str = "default") -> bool:
        """Return whether an item is currently an eligible active assertion."""
        item = self.get(item_id, namespace=namespace)
        return bool(item and item.status == "active" and item.consent)

    # Friendly alias used by policy/control-plane callers.
    assert_memory = assert_item

    def forget_query(self, query: str, *, namespace: str = "default", tombstone: bool = False) -> int:
        """Forget all matching memories in a namespace; returns affected count."""
        ids = [item.id for item in self.search(query, namespace=namespace, limit=100)]
        return sum(self.forget(i, namespace=namespace, tombstone=tombstone) for i in ids)

    def forget(self, item_id: str, *, namespace: str = "default", tombstone: bool = False) -> bool:
        with self._lock:
            if tombstone:
                cur = self._db.execute("UPDATE memories SET status='tombstone', updated_at=? WHERE id=? AND namespace=?", (self._clock(), item_id, namespace))
            else:
                cur = self._db.execute("DELETE FROM memories WHERE id=? AND namespace=?", (item_id, namespace))
            self._db.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()

__all__ = ["MemoryItem", "MemoryStore"]
