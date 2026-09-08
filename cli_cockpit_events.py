"""Lossless, idempotent projection of runtime ``AgentOutput`` objects.

The runtime is allowed to deliver the same output after an outbox replay and
sub-agent notifications may arrive on different worker threads.  This module
keeps that transport concern out of the terminal UI: outputs are correlated,
deduplicated, and projected into a stable transcript.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Iterable


@dataclass(frozen=True)
class CockpitEvent:
    text: str
    correlation_id: str | None = None
    event_id: str | None = None
    kind: str = "message"
    state: str | None = None
    progress: bool = False
    delta: bool = False
    final: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Transcript:
    """Thread-safe transcript projection, grouped by correlation id."""

    messages: list[CockpitEvent] = field(default_factory=list)
    progress: list[CockpitEvent] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set, repr=False)
    _text: dict[str | None, str] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def add(self, output: Any) -> CockpitEvent | None:
        """Project one AgentOutput; return ``None`` for a replay."""
        meta = dict(getattr(output, "metadata", {}) or {})
        oid = getattr(output, "output_id", None) or getattr(output, "event_id", None)
        explicit_key = getattr(output, "idempotency_key", None)
        # ``event_id`` identifies the runtime turn, not every emitted chunk:
        # progress and final outputs may legitimately reuse it.  Only an
        # explicit idempotency key is globally unique; otherwise include the
        # observable event shape so an exact replay is suppressed while a
        # later chunk with the same event id is retained.
        if explicit_key:
            key = f"idempotency:{explicit_key}"
        else:
            key = repr((oid, getattr(output, "correlation_id", None),
                        meta.get("event_type", meta.get("type", meta.get("kind", "message"))),
                        getattr(output, "text", None) or getattr(output, "content", ""),
                        meta.get("state_version"), meta.get("sequence")))
        with self._lock:
            if key in self._seen:
                return None
            self._seen.add(key)
            raw_kind = meta.get("event_type", meta.get("type", meta.get("kind", "message")))
            kind = str(raw_kind)
            state = meta.get("state", meta.get("status"))
            text = str(getattr(output, "text", None) or getattr(output, "content", ""))
            progress = bool(meta.get("progress")) or kind.endswith(".progress") or kind == "progress"
            delta = bool(meta.get("delta")) or kind in {"delta", "message.delta", "output.delta"}
            final = bool(meta.get("final")) or kind.endswith((".completed", ".failed", ".cancelled", ".final"))
            event = CockpitEvent(text, getattr(output, "correlation_id", None), str(oid) if oid else None,
                                 kind, str(state) if state is not None else None, progress, delta, final, meta)
            if progress:
                self.progress.append(event)
            else:
                corr = event.correlation_id
                self._text[corr] = self._text.get(corr, "") + text if delta else text
                self.messages.append(event)
            return event

    ingest = add

    def final_text(self, correlation_id: str | None = None) -> str:
        with self._lock:
            return self._text.get(correlation_id, "")

    def all_events(self) -> list[CockpitEvent]:
        with self._lock:
            return [*self.messages, *self.progress]

    def __len__(self) -> int:
        with self._lock:
            return len(self.messages) + len(self.progress)


CockpitTranscript = Transcript
TranscriptModel = Transcript

__all__ = ["CockpitEvent", "Transcript", "CockpitTranscript", "TranscriptModel"]
