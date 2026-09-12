"""Etat minimal et versionné d'un thread de conversation.

Le format est volontairement petit et tolère les anciens fichiers JSON.
Chaque écriture incrémente ``version``; les mises à jour conditionnelles
permettent aux appelants de détecter une course sans imposer de dépendance.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from context_registry import (
    ContextRegistry,
    ConversationThread,
    IntentState,
    Principal,
    RevisionConflict,
)


class _InterprocessFileLock:
    """Cross-process lock with an additional per-process path lock."""

    _registry_guard = threading.Lock()
    _thread_locks: dict[str, threading.RLock] = {}

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = float(timeout)
        self._handle: Any | None = None
        self._thread_lock: threading.RLock | None = None

    @classmethod
    def _lock_for_path(cls, path: Path) -> threading.RLock:
        key = str(path.resolve())
        with cls._registry_guard:
            return cls._thread_locks.setdefault(key, threading.RLock())

    def __enter__(self) -> _InterprocessFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        thread_lock = self._lock_for_path(self.path)
        if not thread_lock.acquire(timeout=self.timeout):
            raise TimeoutError(f"timed out acquiring thread-state lock: {self.path}")
        self._thread_lock = thread_lock

        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._handle = handle
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out acquiring thread-state lock: {self.path}"
                        )
                    time.sleep(0.01)
        except Exception:
            handle.close()
            self._release_thread_lock()
            raise

    def _release_thread_lock(self) -> None:
        thread_lock = self._thread_lock
        self._thread_lock = None
        if thread_lock is not None:
            thread_lock.release()

    def __exit__(self, *_: Any) -> None:
        handle = self._handle
        self._handle = None
        try:
            if handle is not None:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        finally:
            self._release_thread_lock()


@dataclass(frozen=True)
class ThreadState:
    version: int = 0
    thread_id: str = "default"
    values: dict[str, Any] = field(default_factory=dict)
    conversation_id: str = "default"
    scope: str = "global"

    def to_dict(self) -> dict[str, Any]:
        return {
            **copy.deepcopy(self.values),
            "version": self.version,
            "thread_id": self.thread_id,
            "conversation_id": self.conversation_id,
            "scope": self.scope,
        }


class ThreadStateStore:
    """Stockage atomique d'états courts isolés par conversation/thread.

    ``scope_resolver`` est optionnel et permet au runtime de sélectionner le
    scope courant sans modifier son ancienne API ``get()``/``update()``. Il
    retourne un mapping contenant éventuellement ``scope``,
    ``conversation_id`` et ``thread_id``.
    """

    _SCHEMA = "orion.thread_state.v2"

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        thread_id: str = "default",
        conversation_id: str | None = None,
        scope: str = "global",
        scope_resolver: Callable[[], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        self.thread_id = str(thread_id or "default")
        self.conversation_id = str(conversation_id or self.thread_id or "default")
        self.scope = str(scope or "global")
        self.scope_resolver = scope_resolver
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str, str], ThreadState] = {}
        self._process_lock_path = (
            None
            if self.path is None
            else self.path.with_name(f".{self.path.name}.lock")
        )
        self._load()

    @staticmethod
    def _key(scope: str, conversation_id: str, thread_id: str) -> tuple[str, str, str]:
        return (str(scope or "global"), str(conversation_id or "default"), str(thread_id or "default"))

    def _identity(
        self,
        *,
        thread_id: str | None = None,
        conversation_id: str | None = None,
        scope: str | None = None,
    ) -> tuple[str, str, str]:
        resolved: Mapping[str, Any] = {}
        if self.scope_resolver is not None:
            try:
                candidate = self.scope_resolver()
                if isinstance(candidate, Mapping):
                    resolved = candidate
            except Exception:
                resolved = {}
        selected_scope = str(scope or resolved.get("scope") or self.scope or "global")
        selected_conversation = str(
            conversation_id
            or resolved.get("conversation_id")
            or resolved.get("thread_id")
            or self.conversation_id
            or "default"
        )
        selected_thread = str(
            thread_id
            or resolved.get("thread_id")
            or resolved.get("conversation_id")
            or self.thread_id
            or selected_conversation
        )
        return self._key(selected_scope, selected_conversation, selected_thread)

    def _read_states(self) -> dict[tuple[str, str, str], ThreadState]:
        if self.path is None or not self.path.exists():
            return {}
        states: dict[tuple[str, str, str], ThreadState] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping):
                if raw.get("schema") == self._SCHEMA and isinstance(raw.get("states"), list):
                    for item in raw["states"]:
                        if not isinstance(item, Mapping):
                            continue
                        version = max(0, int(item.get("version", 0) or 0))
                        tid = str(item.get("thread_id") or "default")
                        conversation_id = str(item.get("conversation_id") or tid or "default")
                        scope = str(item.get("scope") or "global")
                        values = {
                            str(k): copy.deepcopy(v)
                            for k, v in item.items()
                            if k not in {"version", "thread_id", "conversation_id", "scope"}
                        }
                        states[self._key(scope, conversation_id, tid)] = ThreadState(
                            version,
                            tid,
                            values,
                            conversation_id,
                            scope,
                        )
                else:
                    # Legacy single-thread JSON becomes one scoped entry.  Keep
                    # its persisted thread id authoritative for compatibility.
                    version = int(raw.get("version", 0) or 0)
                    tid = str(raw.get("thread_id") or self.thread_id)
                    conversation_id = str(raw.get("conversation_id") or tid or "default")
                    scope = str(raw.get("scope") or "global")
                    values = {
                        str(k): copy.deepcopy(v)
                        for k, v in raw.items()
                        if k not in {"version", "thread_id", "conversation_id", "scope"}
                    }
                    states[self._key(scope, conversation_id, tid)] = ThreadState(
                        max(0, version),
                        tid,
                        values,
                        conversation_id,
                        scope,
                    )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return states

    def _refresh_locked(self) -> None:
        if self.path is not None:
            self._states = self._read_states()

    def _load(self) -> None:
        if self.path is None:
            return
        assert self._process_lock_path is not None
        with _InterprocessFileLock(self._process_lock_path):
            self._refresh_locked()

    def get(
        self,
        *,
        thread_id: str | None = None,
        conversation_id: str | None = None,
        scope: str | None = None,
    ) -> ThreadState:
        with self._lock:
            if self._process_lock_path is not None:
                with _InterprocessFileLock(self._process_lock_path):
                    self._refresh_locked()
                    return self._get_locked(
                        thread_id=thread_id,
                        conversation_id=conversation_id,
                        scope=scope,
                    )
            return self._get_locked(
                thread_id=thread_id,
                conversation_id=conversation_id,
                scope=scope,
            )

    def _get_locked(
        self,
        *,
        thread_id: str | None = None,
        conversation_id: str | None = None,
        scope: str | None = None,
    ) -> ThreadState:
            key = self._identity(
                thread_id=thread_id,
                conversation_id=conversation_id,
                scope=scope,
            )
            state = self._states.get(key)
            if (
                state is None
                and thread_id is None
                and conversation_id is None
                and scope is None
                and self.scope_resolver is None
                and len(self._states) == 1
            ):
                # Historical callers reopened a single-state file with any
                # constructor thread id and expected the persisted id to win.
                state = next(iter(self._states.values()))
            if state is None:
                selected_scope, selected_conversation, selected_thread = key
                state = ThreadState(
                    thread_id=selected_thread,
                    conversation_id=selected_conversation,
                    scope=selected_scope,
                )
            return ThreadState(
                state.version,
                state.thread_id,
                copy.deepcopy(state.values),
                state.conversation_id,
                state.scope,
            )

    @property
    def state(self) -> ThreadState:
        return self.get()

    def update(
        self,
        values: Mapping[str, Any] | None = None,
        *,
        expected_version: int | None = None,
        thread_id: str | None = None,
        conversation_id: str | None = None,
        scope: str | None = None,
        **kwargs: Any,
    ) -> ThreadState:
        with self._lock:
            if self._process_lock_path is not None:
                with _InterprocessFileLock(self._process_lock_path):
                    self._refresh_locked()
                    return self._update_locked(
                        values,
                        expected_version=expected_version,
                        thread_id=thread_id,
                        conversation_id=conversation_id,
                        scope=scope,
                        **kwargs,
                    )
            return self._update_locked(
                values,
                expected_version=expected_version,
                thread_id=thread_id,
                conversation_id=conversation_id,
                scope=scope,
                **kwargs,
            )

    def _update_locked(
        self,
        values: Mapping[str, Any] | None = None,
        *,
        expected_version: int | None = None,
        thread_id: str | None = None,
        conversation_id: str | None = None,
        scope: str | None = None,
        **kwargs: Any,
    ) -> ThreadState:
            key = self._identity(
                thread_id=thread_id,
                conversation_id=conversation_id,
                scope=scope,
            )
            current = self._states.get(key)
            if (
                current is None
                and thread_id is None
                and conversation_id is None
                and scope is None
                and self.scope_resolver is None
                and len(self._states) == 1
            ):
                current = next(iter(self._states.values()))
                key = self._key(current.scope, current.conversation_id, current.thread_id)
            if current is None:
                selected_scope, selected_conversation, selected_thread = key
                current = ThreadState(
                    thread_id=selected_thread,
                    conversation_id=selected_conversation,
                    scope=selected_scope,
                )
            if expected_version is not None and int(expected_version) != current.version:
                raise ValueError("thread state version conflict")
            merged = dict(values or {})
            merged.update(kwargs)
            data = copy.deepcopy(current.values)
            data.update(merged)
            self._states[key] = ThreadState(
                current.version + 1,
                current.thread_id,
                data,
                current.conversation_id,
                current.scope,
            )
            self._save_locked()
            state = self._states[key]
            return ThreadState(
                state.version,
                state.thread_id,
                copy.deepcopy(state.values),
                state.conversation_id,
                state.scope,
            )

    set = update

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": self._SCHEMA,
            "states": [
                state.to_dict()
                for _, state in sorted(self._states.items(), key=lambda item: item[0])
            ],
        }
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(self.path)


__all__ = [
    "ThreadState", "ThreadStateStore", "ContextRegistry", "Principal",
    "ConversationThread", "IntentState", "RevisionConflict",
]
