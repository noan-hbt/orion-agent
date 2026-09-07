"""Small, local and backwards-compatible context registry.

The registry deliberately stores JSON payloads: callers can add fields without
requiring a schema migration.  Revisions are checked in the same transaction
as writes, making the API safe for concurrent workers.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Principal:
    id: str
    scope: str = "global"
    data: dict[str, Any] = field(default_factory=dict)
    revision: int = 0


@dataclass(frozen=True)
class ConversationThread:
    id: str
    principal_id: str | None = None
    scope: str = "global"
    data: dict[str, Any] = field(default_factory=dict)
    revision: int = 0


@dataclass(frozen=True)
class IntentState:
    thread_id: str
    intent: str | None = None
    scope: str = "global"
    data: dict[str, Any] = field(default_factory=dict)
    revision: int = 0


class RevisionConflict(ValueError):
    """Raised when an optimistic-concurrency revision is stale."""


class ContextRegistry:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._db:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS principals (
              id TEXT NOT NULL, scope TEXT NOT NULL, data TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(id, scope));
            CREATE TABLE IF NOT EXISTS conversation_threads (
              id TEXT NOT NULL, scope TEXT NOT NULL, principal_id TEXT,
              data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(id, scope));
            CREATE TABLE IF NOT EXISTS intent_states (
              thread_id TEXT NOT NULL, scope TEXT NOT NULL, intent TEXT,
              data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(thread_id, scope));
            CREATE TABLE IF NOT EXISTS channel_bindings (
              channel TEXT NOT NULL, external_id TEXT NOT NULL, scope TEXT NOT NULL,
              principal_id TEXT, thread_id TEXT, data TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY(channel, external_id, scope));
            CREATE INDEX IF NOT EXISTS idx_bindings_lookup ON channel_bindings(channel, external_id);
            """)

    @staticmethod
    def _json(value: Mapping[str, Any] | None) -> str:
        return json.dumps(dict(value or {}), ensure_ascii=False, default=str)

    def _write(self, table: str, key: tuple[str, str], fields: dict[str, Any], expected_revision: int | None):
        where = " AND ".join(f"{k}=?" for k in ("id", "scope") if k in fields or k == "id")
        # key columns differ only for intent_states; this helper is used by the
        # principal/thread methods below, keeping SQL explicit and auditable.
        cols = list(fields)
        with self._lock, self._db:
            row = self._db.execute(f"SELECT revision FROM {table} WHERE id=? AND scope=?", key).fetchone()
            current = None if row is None else int(row[0])
            if expected_revision is not None and current != int(expected_revision):
                raise RevisionConflict(f"revision conflict (expected {expected_revision}, got {current})")
            rev = 0 if current is None else current + 1
            vals = [fields[c] for c in cols]
            if row is None:
                self._db.execute(f"INSERT INTO {table}(id,scope,{','.join(cols)},revision) VALUES(?,?,{','.join('?' for _ in cols)},?)", (*key, *vals, rev))
            else:
                self._db.execute(f"UPDATE {table} SET {','.join(c+'=?' for c in cols)},revision=? WHERE id=? AND scope=?", (*vals, rev, *key))
            return rev

    def upsert_principal(self, principal: Principal, *, expected_revision: int | None = None) -> Principal:
        rev = self._write("principals", (principal.id, principal.scope), {"data": self._json(principal.data)}, expected_revision)
        return Principal(principal.id, principal.scope, dict(principal.data), rev)

    def get_principal(self, id: str, scope: str = "global") -> Principal | None:
        r = self._db.execute("SELECT * FROM principals WHERE id=? AND scope=?", (id, scope)).fetchone()
        return None if r is None else Principal(r["id"], r["scope"], json.loads(r["data"]), r["revision"])

    def upsert_thread(self, thread: ConversationThread, *, expected_revision: int | None = None) -> ConversationThread:
        rev = self._write("conversation_threads", (thread.id, thread.scope), {"principal_id": thread.principal_id, "data": self._json(thread.data)}, expected_revision)
        return ConversationThread(thread.id, thread.principal_id, thread.scope, dict(thread.data), rev)

    def get_thread(self, id: str, scope: str = "global") -> ConversationThread | None:
        r = self._db.execute("SELECT * FROM conversation_threads WHERE id=? AND scope=?", (id, scope)).fetchone()
        return None if r is None else ConversationThread(r["id"], r["principal_id"], r["scope"], json.loads(r["data"]), r["revision"])

    def upsert_intent(self, state: IntentState, *, expected_revision: int | None = None) -> IntentState:
        with self._lock, self._db:
            r = self._db.execute("SELECT revision FROM intent_states WHERE thread_id=? AND scope=?", (state.thread_id, state.scope)).fetchone()
            cur = None if r is None else int(r[0])
            if expected_revision is not None and cur != int(expected_revision): raise RevisionConflict("revision conflict")
            rev = 0 if cur is None else cur + 1
            if r is None: self._db.execute("INSERT INTO intent_states VALUES(?,?,?,?,?)", (state.thread_id,state.scope,state.intent,self._json(state.data),rev))
            else: self._db.execute("UPDATE intent_states SET intent=?,data=?,revision=? WHERE thread_id=? AND scope=?", (state.intent,self._json(state.data),rev,state.thread_id,state.scope))
            return IntentState(state.thread_id,state.intent,state.scope,dict(state.data),rev)

    def get_intent(self, thread_id: str, scope: str = "global") -> IntentState | None:
        r=self._db.execute("SELECT * FROM intent_states WHERE thread_id=? AND scope=?",(thread_id,scope)).fetchone()
        return None if r is None else IntentState(r["thread_id"],r["intent"],r["scope"],json.loads(r["data"]),r["revision"])

    def bind_channel(self, channel: str, external_id: str, *, scope="global", principal_id=None, thread_id=None, data=None):
        with self._lock, self._db: self._db.execute("INSERT INTO channel_bindings VALUES(?,?,?,?,?,?) ON CONFLICT(channel,external_id,scope) DO UPDATE SET principal_id=excluded.principal_id,thread_id=excluded.thread_id,data=excluded.data",(channel,str(external_id),scope,principal_id,thread_id,self._json(data)))

    def resolve_binding(self, channel: str, external_id: str, scope="global"):
        r=self._db.execute("SELECT * FROM channel_bindings WHERE channel=? AND external_id=? AND scope=?",(channel,str(external_id),scope)).fetchone()
        return None if r is None else dict(r)

    def snapshot(self, *, limit: int = 100, scope: str | None = None) -> dict[str, Any]:
        """Return a bounded, JSON-serializable view for prompt/runtime context.

        The registry connection and dataclass instances intentionally never
        cross this boundary.  ``limit`` applies independently to each table;
        invalid or negative values are normalized to a small safe default.
        """
        try:
            n = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            n = 100
        with self._lock:
            params: tuple[Any, ...] = () if scope is None else (scope,)
            clause = "" if scope is None else " WHERE scope=?"
            principals = self._db.execute(f"SELECT id,scope,data,revision FROM principals{clause} ORDER BY id LIMIT ?", (*params, n)).fetchall()
            threads = self._db.execute(f"SELECT id,scope,principal_id,data,revision FROM conversation_threads{clause} ORDER BY id LIMIT ?", (*params, n)).fetchall()
            intents = self._db.execute(f"SELECT thread_id,scope,intent,data,revision FROM intent_states{clause} ORDER BY thread_id LIMIT ?", (*params, n)).fetchall()
            bparams: tuple[Any, ...] = () if scope is None else (scope,)
            bclause = "" if scope is None else " WHERE scope=?"
            bindings = self._db.execute(f"SELECT channel,external_id,scope,principal_id,thread_id,data FROM channel_bindings{bclause} ORDER BY channel,external_id LIMIT ?", (*bparams, n)).fetchall()

        def payload(row: sqlite3.Row) -> dict[str, Any]:
            result = dict(row)
            raw = result.get("data")
            try:
                result["data"] = json.loads(raw) if isinstance(raw, str) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                result["data"] = {}
            return result

        return {
            "principals": [payload(r) for r in principals],
            "threads": [payload(r) for r in threads],
            "intents": [payload(r) for r in intents],
            "bindings": [payload(r) for r in bindings],
        }


__all__ = ["ContextRegistry","Principal","ConversationThread","IntentState","RevisionConflict"]
