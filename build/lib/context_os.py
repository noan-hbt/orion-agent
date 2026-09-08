"""Etat minimal et versionné d'un thread de conversation.

Le format est volontairement petit et tolère les anciens fichiers JSON.
Chaque écriture incrémente ``version``; les mises à jour conditionnelles
permettent aux appelants de détecter une course sans imposer de dépendance.
"""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from context_registry import (
    ContextRegistry,
    ConversationThread,
    IntentState,
    Principal,
    RevisionConflict,
)


@dataclass(frozen=True)
class ThreadState:
    version: int = 0
    thread_id: str = "default"
    values: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "thread_id": self.thread_id, **copy.deepcopy(self.values)}


class ThreadStateStore:
    """Stockage atomique d'un état court, avec lecture tolérante."""
    def __init__(self, path: str | Path = ":memory:", *, thread_id: str = "default") -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        self.thread_id = str(thread_id or "default")
        self._lock = threading.RLock()
        self._state = ThreadState(thread_id=self.thread_id)
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping):
                version = int(raw.get("version", 0) or 0)
                tid = str(raw.get("thread_id") or self.thread_id)
                values = {str(k): copy.deepcopy(v) for k, v in raw.items() if k not in {"version", "thread_id"}}
                self._state = ThreadState(max(0, version), tid, values)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def get(self) -> ThreadState:
        with self._lock:
            return ThreadState(self._state.version, self._state.thread_id, copy.deepcopy(self._state.values))

    @property
    def state(self) -> ThreadState:
        return self.get()

    def update(self, values: Mapping[str, Any] | None = None, *, expected_version: int | None = None, **kwargs: Any) -> ThreadState:
        with self._lock:
            if expected_version is not None and int(expected_version) != self._state.version:
                raise ValueError("thread state version conflict")
            merged = dict(values or {})
            merged.update(kwargs)
            data = copy.deepcopy(self._state.values)
            data.update(merged)
            self._state = ThreadState(self._state.version + 1, self._state.thread_id, data)
            self._save()
            return self.get()

    set = update

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


__all__ = [
    "ThreadState", "ThreadStateStore", "ContextRegistry", "Principal",
    "ConversationThread", "IntentState", "RevisionConflict",
]
