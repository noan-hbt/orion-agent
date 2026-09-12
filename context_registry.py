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
from typing import Any, Callable, Mapping


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

    @property
    def kind(self) -> str | None:
        """Expose the normalized conversation kind without leaking storage details."""
        value = self.data.get("kind")
        return None if value is None else str(value)


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
    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        scope_resolver: Callable[[], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.path = str(path)
        self.scope_resolver = scope_resolver
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

    def _write(
        self,
        table: str,
        key: tuple[str, str],
        fields: dict[str, Any],
        expected_revision: int | None,
    ):
        # key columns differ only for intent_states; this helper is used by the
        # principal/thread methods below, keeping SQL explicit and auditable.
        cols = list(fields)
        with self._lock, self._db:
            row = self._db.execute(
                f"SELECT revision FROM {table} WHERE id=? AND scope=?", key
            ).fetchone()
            current = None if row is None else int(row[0])
            if expected_revision is not None and current != int(expected_revision):
                raise RevisionConflict(
                    f"revision conflict (expected {expected_revision}, got {current})"
                )
            rev = 0 if current is None else current + 1
            vals = [fields[c] for c in cols]
            if row is None:
                self._db.execute(
                    f"INSERT INTO {table}(id,scope,{','.join(cols)},revision) VALUES(?,?,{','.join('?' for _ in cols)},?)",
                    (*key, *vals, rev),
                )
            else:
                self._db.execute(
                    f"UPDATE {table} SET {','.join(c + '=?' for c in cols)},revision=? WHERE id=? AND scope=?",
                    (*vals, rev, *key),
                )
            return rev

    def upsert_principal(
        self, principal: Principal, *, expected_revision: int | None = None
    ) -> Principal:
        rev = self._write(
            "principals",
            (principal.id, principal.scope),
            {"data": self._json(principal.data)},
            expected_revision,
        )
        return Principal(principal.id, principal.scope, dict(principal.data), rev)

    def get_principal(self, id: str, scope: str = "global") -> Principal | None:
        r = self._db.execute(
            "SELECT * FROM principals WHERE id=? AND scope=?", (id, scope)
        ).fetchone()
        return (
            None
            if r is None
            else Principal(r["id"], r["scope"], json.loads(r["data"]), r["revision"])
        )

    def upsert_thread(
        self, thread: ConversationThread, *, expected_revision: int | None = None
    ) -> ConversationThread:
        rev = self._write(
            "conversation_threads",
            (thread.id, thread.scope),
            {"principal_id": thread.principal_id, "data": self._json(thread.data)},
            expected_revision,
        )
        return ConversationThread(
            thread.id, thread.principal_id, thread.scope, dict(thread.data), rev
        )

    def get_thread(self, id: str, scope: str = "global") -> ConversationThread | None:
        r = self._db.execute(
            "SELECT * FROM conversation_threads WHERE id=? AND scope=?", (id, scope)
        ).fetchone()
        return (
            None
            if r is None
            else ConversationThread(
                r["id"],
                r["principal_id"],
                r["scope"],
                json.loads(r["data"]),
                r["revision"],
            )
        )

    def upsert_intent(
        self, state: IntentState, *, expected_revision: int | None = None
    ) -> IntentState:
        with self._lock, self._db:
            r = self._db.execute(
                "SELECT revision FROM intent_states WHERE thread_id=? AND scope=?",
                (state.thread_id, state.scope),
            ).fetchone()
            cur = None if r is None else int(r[0])
            if expected_revision is not None and cur != int(expected_revision):
                raise RevisionConflict("revision conflict")
            rev = 0 if cur is None else cur + 1
            if r is None:
                self._db.execute(
                    "INSERT INTO intent_states VALUES(?,?,?,?,?)",
                    (
                        state.thread_id,
                        state.scope,
                        state.intent,
                        self._json(state.data),
                        rev,
                    ),
                )
            else:
                self._db.execute(
                    "UPDATE intent_states SET intent=?,data=?,revision=? WHERE thread_id=? AND scope=?",
                    (
                        state.intent,
                        self._json(state.data),
                        rev,
                        state.thread_id,
                        state.scope,
                    ),
                )
            return IntentState(
                state.thread_id, state.intent, state.scope, dict(state.data), rev
            )

    def get_intent(self, thread_id: str, scope: str = "global") -> IntentState | None:
        r = self._db.execute(
            "SELECT * FROM intent_states WHERE thread_id=? AND scope=?",
            (thread_id, scope),
        ).fetchone()
        return (
            None
            if r is None
            else IntentState(
                r["thread_id"],
                r["intent"],
                r["scope"],
                json.loads(r["data"]),
                r["revision"],
            )
        )

    def bind_channel(
        self,
        channel: str,
        external_id: str,
        *,
        scope="global",
        principal_id=None,
        thread_id=None,
        data=None,
    ):
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO channel_bindings VALUES(?,?,?,?,?,?) ON CONFLICT(channel,external_id,scope) DO UPDATE SET principal_id=excluded.principal_id,thread_id=excluded.thread_id,data=excluded.data",
                (
                    channel,
                    str(external_id),
                    scope,
                    principal_id,
                    thread_id,
                    self._json(data),
                ),
            )

    def resolve_binding(self, channel: str, external_id: str, scope="global"):
        r = self._db.execute(
            "SELECT * FROM channel_bindings WHERE channel=? AND external_id=? AND scope=?",
            (channel, str(external_id), scope),
        ).fetchone()
        return None if r is None else dict(r)

    def snapshot(
        self,
        *,
        limit: int | None = None,
        scope: str | None = None,
        thread_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a JSON-serializable registry view scoped to one conversation.

        When explicit filters are omitted, an optional ``scope_resolver`` can
        provide the active runtime conversation/thread.  A truly unscoped call
        returns the complete registry by default instead of silently taking an
        arbitrary first 100 rows; administrative callers can still pass an
        explicit ``limit``.
        """
        resolved: Mapping[str, Any] = {}
        if self.scope_resolver is not None:
            try:
                candidate = self.scope_resolver()
                if isinstance(candidate, Mapping):
                    resolved = candidate
            except Exception:
                resolved = {}
        selected_scope = scope or resolved.get("scope")
        selected_thread = thread_id or resolved.get("thread_id")
        selected_conversation = conversation_id or resolved.get("conversation_id")
        target_ids = {
            str(value)
            for value in (selected_thread, selected_conversation)
            if value is not None and str(value).strip()
        }
        n: int | None
        if limit is None:
            n = None
        else:
            try:
                n = max(1, min(int(limit), 1000))
            except (TypeError, ValueError):
                n = 100

        def limited(sql: str) -> tuple[str, tuple[Any, ...]]:
            return (sql, ()) if n is None else (sql + " LIMIT ?", (n,))

        with self._lock:
            bindings: list[sqlite3.Row]
            thread_ids = set(target_ids)
            if target_ids:
                placeholders = ",".join("?" for _ in target_ids)
                where = (
                    f"(thread_id IN ({placeholders}) OR external_id IN ({placeholders}))"
                )
                params: list[Any] = [*target_ids, *target_ids]
                if selected_scope is not None:
                    where += " AND scope=?"
                    params.append(str(selected_scope))
                sql = (
                    "SELECT channel,external_id,scope,principal_id,thread_id,data "
                    f"FROM channel_bindings WHERE {where} ORDER BY channel,external_id"
                )
                if n is not None:
                    sql += " LIMIT ?"
                    params.append(n)
                bindings = self._db.execute(sql, tuple(params)).fetchall()
                thread_ids.update(
                    str(row["thread_id"])
                    for row in bindings
                    if row["thread_id"] is not None and str(row["thread_id"]).strip()
                )
            else:
                where = "" if selected_scope is None else " WHERE scope=?"
                sql, extra = limited(
                    "SELECT channel,external_id,scope,principal_id,thread_id,data "
                    f"FROM channel_bindings{where} ORDER BY channel,external_id"
                )
                params = (() if selected_scope is None else (str(selected_scope),)) + extra
                bindings = self._db.execute(sql, params).fetchall()

            if thread_ids:
                placeholders = ",".join("?" for _ in thread_ids)
                thread_params: list[Any] = list(thread_ids)
                thread_where = f"id IN ({placeholders})"
                intent_where = f"thread_id IN ({placeholders})"
                if selected_scope is not None:
                    thread_where += " AND scope=?"
                    intent_where += " AND scope=?"
                    thread_params.append(str(selected_scope))
                thread_sql = (
                    "SELECT id,scope,principal_id,data,revision FROM conversation_threads "
                    f"WHERE {thread_where} ORDER BY id"
                )
                intent_sql = (
                    "SELECT thread_id,scope,intent,data,revision FROM intent_states "
                    f"WHERE {intent_where} ORDER BY thread_id"
                )
                if n is not None:
                    thread_sql += " LIMIT ?"
                    intent_sql += " LIMIT ?"
                    query_params = (*thread_params, n)
                else:
                    query_params = tuple(thread_params)
                threads = self._db.execute(thread_sql, query_params).fetchall()
                intents = self._db.execute(intent_sql, query_params).fetchall()
            else:
                where = "" if selected_scope is None else " WHERE scope=?"
                scope_params = () if selected_scope is None else (str(selected_scope),)
                thread_sql, thread_extra = limited(
                    "SELECT id,scope,principal_id,data,revision FROM conversation_threads"
                    f"{where} ORDER BY id"
                )
                intent_sql, intent_extra = limited(
                    "SELECT thread_id,scope,intent,data,revision FROM intent_states"
                    f"{where} ORDER BY thread_id"
                )
                threads = self._db.execute(
                    thread_sql, scope_params + thread_extra
                ).fetchall()
                intents = self._db.execute(
                    intent_sql, scope_params + intent_extra
                ).fetchall()

            if target_ids:
                principal_ids = {
                    str(row["principal_id"])
                    for row in [*threads, *bindings]
                    if row["principal_id"] is not None
                    and str(row["principal_id"]).strip()
                }
                if principal_ids:
                    placeholders = ",".join("?" for _ in principal_ids)
                    principal_where = f"id IN ({placeholders})"
                    principal_params: list[Any] = list(principal_ids)
                    if selected_scope is not None:
                        principal_where += " AND scope=?"
                        principal_params.append(str(selected_scope))
                    principal_sql = (
                        "SELECT id,scope,data,revision FROM principals "
                        f"WHERE {principal_where} ORDER BY id"
                    )
                    if n is not None:
                        principal_sql += " LIMIT ?"
                        principal_params.append(n)
                    principals = self._db.execute(
                        principal_sql, tuple(principal_params)
                    ).fetchall()
                else:
                    principals = []
            else:
                where = "" if selected_scope is None else " WHERE scope=?"
                principal_sql, principal_extra = limited(
                    "SELECT id,scope,data,revision FROM principals"
                    f"{where} ORDER BY id"
                )
                params = (() if selected_scope is None else (str(selected_scope),)) + principal_extra
                principals = self._db.execute(principal_sql, params).fetchall()

        def payload(row: sqlite3.Row) -> dict[str, Any]:
            result = dict(row)
            raw = result.get("data")
            try:
                result["data"] = json.loads(raw) if isinstance(raw, str) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                result["data"] = {}
            return result

        return {
            "scope": None if selected_scope is None else str(selected_scope),
            "thread_id": None if selected_thread is None else str(selected_thread),
            "conversation_id": (
                None if selected_conversation is None else str(selected_conversation)
            ),
            "principals": [payload(r) for r in principals],
            "threads": [payload(r) for r in threads],
            "intents": [payload(r) for r in intents],
            "bindings": [payload(r) for r in bindings],
        }


__all__ = [
    "ContextRegistry",
    "Principal",
    "ConversationThread",
    "IntentState",
    "RevisionConflict",
]
