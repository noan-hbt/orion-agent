"""Bounded, typed context carried by delegated work.

The handoff object is deliberately independent from the runtime.  It is safe
to persist and pass through queues, and its allowlisted memory is the only
parent context copied to a child.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


MAX_ENVELOPE_BYTES = 48_000
MAX_OBJECTIVE_CHARS = 4_000
MAX_CONTRACT_CHARS = 2_000
MAX_WAITING_CHARS = 2_000
MAX_MEMORY_ENTRY_CHARS = 1_000
MAX_MEMORY_ENTRIES = 8
MAX_MEMORY_CHARS = 8_000
MAX_RESULT_CHARS = 24_000
MAX_ERROR_CHARS = 2_000
MAX_IDENTIFIER_CHARS = 128
MAX_DEPTH = 3

_TOP_LEVEL = {
    "version", "handoff_id", "kind", "correlation_id", "parent", "source",
    "target", "task", "state", "memory", "output", "routing", "created_at",
    "updated_at",
}
_PARENT_KEYS = {"event_id", "task_id", "run_id", "handoff_id"}
_PARTY_KEYS = {"scope", "instance_id", "agent_id"}
_TASK_KEYS = {"objective", "phase"}
_STATE_KEYS = {"status", "attempt", "waiting_for"}
_MEMORY_KEYS = {"facts", "decisions", "open_questions", "artifacts"}
_OUTPUT_KEYS = {"contract", "result", "error"}
_ROUTING_KEYS = {"channel", "reply_to", "conversation_id", "message_thread_id", "thread_id", "user_id", "parent_message_id", "principal_id", "canonical_conversation_id", "intent"}
_STATUSES = {"queued", "running", "waiting", "completed", "failed", "cancelled"}

# Values are redacted even when their key is innocuous.  This is intentionally
# conservative because handoffs can cross process and tenant boundaries.
_SECRET = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?key|password|passwd|secret|token|private[_-]?key)\s*[:=]\s*[^,;\s]+)"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<!\w)(?:\+\d[\d .()/-]{7,}\d|\d{3}[ .-]\d{3}[ .-]\d{3,4})(?!\w)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_text(value: str) -> str:
    value = _SECRET.sub("[REDACTED]", value)
    value = _EMAIL.sub("[REDACTED_EMAIL]", value)
    value = _PHONE.sub("[REDACTED_PHONE]", value)
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return _redact_text(value)
    try:
        return _redact_text(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))
    except (TypeError, ValueError):
        return _redact_text(str(value))


def _clip(value: Any, limit: int) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    marker = "… [truncated]"
    keep = max(0, limit - len(marker))
    return text[:keep].rstrip() + marker


def _identifier(value: Any, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    result = _clip(value, MAX_IDENTIFIER_CHARS).strip()
    return result or (None if allow_none else "unknown")


def derive_correlation_id(event: Any) -> str:
    """Derive the root correlation in the contract's precedence order."""
    if isinstance(event, Mapping):
        metadata = event.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        for value in (event.get("correlation_id"), metadata.get("correlation_id"), event.get("id")):
            if value is not None and str(value).strip():
                return _identifier(value, allow_none=False) or "unknown"
    else:
        # Event objects carry transport metadata on an attribute rather than
        # in the mapping itself.  Treat it exactly like mapping metadata so a
        # delegated handoff keeps the originating trace instead of generating
        # a fresh correlation id.
        metadata = getattr(event, "metadata", None)
        if isinstance(metadata, Mapping):
            for value in (getattr(event, "correlation_id", None), metadata.get("correlation_id"), getattr(event, "id", None)):
                if value is not None and str(value).strip():
                    return _identifier(value, allow_none=False) or "unknown"
    for attr in ("correlation_id", "id"):
        value = getattr(event, attr, None)
        if value is not None and str(value).strip():
            return _identifier(value, allow_none=False) or "unknown"
    return "corr_" + uuid.uuid4().hex


def _bounded_value(value: Any, depth: int = 0) -> Any:
    """Redact nested values and cap their depth without producing invalid JSON."""
    if depth >= MAX_DEPTH:
        return _clip(value, MAX_MEMORY_ENTRY_CHARS)
    if isinstance(value, Mapping):
        return {
            _clip(key, MAX_IDENTIFIER_CHARS): _bounded_value(item, depth + 1)
            for key, item in list(value.items())[:MAX_MEMORY_ENTRIES]
        }
    if isinstance(value, (list, tuple, set)):
        return [_bounded_value(item, depth + 1) for item in list(value)[:MAX_MEMORY_ENTRIES]]
    return _redact_text(str(value)) if not isinstance(value, (str, int, float, bool)) else (_redact_text(value) if isinstance(value, str) else value)


def _unknown(value: Mapping[str, Any], allowed: set[str], section: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {section} fields: {', '.join(sorted(map(str, unknown)))}")


@dataclass(frozen=True)
class HandoffContext:
    version: int
    handoff_id: str
    kind: str
    correlation_id: str
    parent: dict[str, str | None]
    source: dict[str, str | None]
    target: dict[str, str | None]
    task: dict[str, str]
    state: dict[str, Any]
    memory: dict[str, list[str]]
    output: dict[str, Any]
    routing: dict[str, str | None]
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        objective: str,
        correlation_id: str,
        source_scope: str = "default",
        source_instance_id: str = "orion",
        target_scope: str | None = None,
        target_instance_id: str = "orion",
        source_agent_id: str | None = None,
        target_agent_id: str | None = None,
        parent_event_id: str | None = None,
        parent_task_id: str | None = None,
        parent_run_id: str | None = None,
        parent_handoff_id: str | None = None,
        phase: str = "delegation",
        memory: Mapping[str, Any] | None = None,
        contract: str = "Return a bounded result to the delegating parent.",
        routing: Mapping[str, Any] | None = None,
        handoff_id: str | None = None,
    ) -> "HandoffContext":
        if kind not in {"subagent", "team_job"}:
            raise ValueError("Handoff kind must be subagent or team_job")
        if not str(correlation_id or "").strip():
            raise ValueError("correlation_id doit être renseigné")
        now = _now()
        context = cls(
            version=1,
            handoff_id=_identifier(handoff_id or "hf_" + uuid.uuid4().hex, allow_none=False) or "hf_unknown",
            kind=str(kind),
            correlation_id=_identifier(correlation_id, allow_none=False) or "unknown",
            parent={"event_id": _identifier(parent_event_id), "task_id": _identifier(parent_task_id), "run_id": _identifier(parent_run_id), "handoff_id": _identifier(parent_handoff_id)},
            source={"scope": _identifier(source_scope, allow_none=False), "instance_id": _identifier(source_instance_id, allow_none=False), "agent_id": _identifier(source_agent_id)},
            target={"scope": _identifier(target_scope or source_scope, allow_none=False), "instance_id": _identifier(target_instance_id, allow_none=False), "agent_id": _identifier(target_agent_id)},
            task={"objective": _clip(objective, MAX_OBJECTIVE_CHARS), "phase": _clip(phase, MAX_IDENTIFIER_CHARS)},
            state={"status": "queued", "attempt": 0, "waiting_for": None},
            memory={key: [] for key in _MEMORY_KEYS},
            output={"contract": _clip(contract, MAX_CONTRACT_CHARS), "result": None, "error": None},
            routing={key: _identifier((routing or {}).get(key)) for key in _ROUTING_KEYS},
            created_at=now,
            updated_at=now,
        )
        if memory:
            values = {key: value for key, value in memory.items() if key in _MEMORY_KEYS}
            context = context.with_memory(values)
        return context

    @classmethod
    def from_event(
        cls,
        event: Any,
        *,
        kind: str,
        objective: str,
        source_scope: str = "default",
        source_instance_id: str = "orion",
        target_scope: str | None = None,
        target_instance_id: str = "orion",
        target_agent_id: str | None = None,
        phase: str = "delegation",
        memory: Mapping[str, Any] | None = None,
        contract: str = "Return a bounded result to the delegating parent.",
    ) -> "HandoffContext":
        """Create a child envelope from an event-like object or mapping."""
        metadata = event.get("metadata", {}) if isinstance(event, Mapping) else getattr(event, "metadata", {})
        task_id = getattr(getattr(event, "task", None), "id", None)
        if isinstance(event, Mapping):
            task_id = event.get("task_id", task_id)
        route = {
            key: (metadata or {}).get(key) if isinstance(metadata, Mapping) else None
            for key in ("channel", "reply_to", "conversation_id", "message_thread_id", "thread_id", "user_id", "parent_message_id")
        }
        # Typed channel context is deliberately consumed structurally so this
        # module remains independent of channels.py (and older event objects).
        principal = getattr(event, "principal", None)
        conversation = getattr(event, "conversation", None)
        intent = getattr(event, "intent", None)
        if isinstance(event, Mapping):
            principal = event.get("principal", principal)
            conversation = event.get("conversation", conversation)
            intent = event.get("intent", intent)
        if isinstance(principal, Mapping):
            route["principal_id"] = principal.get("id") or principal.get("canonical_id")
        elif principal is not None:
            route["principal_id"] = getattr(principal, "id", None)
        if isinstance(conversation, Mapping):
            route["canonical_conversation_id"] = conversation.get("id") or conversation.get("canonical_id")
        elif conversation is not None:
            route["canonical_conversation_id"] = getattr(conversation, "id", None)
        if intent is not None:
            route["intent"] = intent if isinstance(intent, str) else getattr(intent, "name", None)
        if isinstance(metadata, Mapping):
            route["message_thread_id"] = route["message_thread_id"] or metadata.get("message_thread_id")
            route["thread_id"] = route["thread_id"] or metadata.get("thread_id")
            route["user_id"] = route["user_id"] or metadata.get("user_id")
            route["parent_message_id"] = route["parent_message_id"] or metadata.get("message_id")
        return cls.create(
            kind=kind, objective=objective, correlation_id=derive_correlation_id(event),
            source_scope=source_scope, source_instance_id=source_instance_id,
            target_scope=target_scope, target_instance_id=target_instance_id,
            target_agent_id=target_agent_id,
            parent_event_id=(event.get("id") if isinstance(event, Mapping) else getattr(event, "id", None)),
            parent_task_id=task_id, phase=phase, memory=memory,
            contract=contract, routing=route,
        )

    @classmethod
    def deserialize(cls, value: Mapping[str, Any] | str) -> "HandoffContext":
        if isinstance(value, str):
            value = json.loads(value)
        return cls.from_dict(value)

    def serialize(self) -> str:
        return self.to_json()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoffContext":
        if not isinstance(value, Mapping):
            raise TypeError("HandoffContext must be a mapping")
        try:
            if len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")) > MAX_ENVELOPE_BYTES:
                raise ValueError("HandoffContext envelope exceeds 48,000 bytes")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and "exceeds" in str(exc):
                raise
        _unknown(value, _TOP_LEVEL, "handoff")
        required = _TOP_LEVEL - {"created_at", "updated_at"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"Missing handoff fields: {', '.join(sorted(missing))}")
        for key, allowed in (("parent", _PARENT_KEYS), ("source", _PARTY_KEYS), ("target", _PARTY_KEYS), ("task", _TASK_KEYS), ("state", _STATE_KEYS), ("memory", _MEMORY_KEYS), ("output", _OUTPUT_KEYS), ("routing", _ROUTING_KEYS)):
            section = value.get(key)
            if not isinstance(section, Mapping):
                raise ValueError(f"Handoff {key} must be an object")
            _unknown(section, allowed, key)
        if int(value.get("version", 0)) != 1:
            raise ValueError("Unsupported HandoffContext version")
        kind = str(value.get("kind", ""))
        if kind not in {"subagent", "team_job"}:
            raise ValueError("Handoff kind must be subagent or team_job")
        status = str(value["state"].get("status", ""))
        if status not in _STATUSES:
            raise ValueError("Invalid handoff state")
        result = cls(
            version=1,
            handoff_id=_identifier(value.get("handoff_id"), allow_none=False) or "unknown",
            kind=kind,
            correlation_id=_identifier(value.get("correlation_id"), allow_none=False) or "unknown",
            parent={key: _identifier(value["parent"].get(key)) for key in _PARENT_KEYS},
            source={key: _identifier(value["source"].get(key)) for key in _PARTY_KEYS},
            target={key: _identifier(value["target"].get(key)) for key in _PARTY_KEYS},
            task={"objective": _clip(value["task"].get("objective", ""), MAX_OBJECTIVE_CHARS), "phase": _clip(value["task"].get("phase", ""), MAX_IDENTIFIER_CHARS)},
            state={"status": status, "attempt": max(0, int(value["state"].get("attempt", 0))), "waiting_for": _clip(value["state"].get("waiting_for"), MAX_WAITING_CHARS) if value["state"].get("waiting_for") is not None else None},
            memory={key: [] for key in _MEMORY_KEYS},
            output={"contract": _clip(value["output"].get("contract", ""), MAX_CONTRACT_CHARS), "result": _clip(value["output"].get("result"), MAX_RESULT_CHARS) if value["output"].get("result") is not None else None, "error": _clip(value["output"].get("error"), MAX_ERROR_CHARS) if value["output"].get("error") is not None else None},
            routing={key: _identifier(value["routing"].get(key)) for key in _ROUTING_KEYS},
            created_at=_clip(value.get("created_at", _now()), MAX_IDENTIFIER_CHARS),
            updated_at=_clip(value.get("updated_at", _now()), MAX_IDENTIFIER_CHARS),
        )
        bounded = result.with_memory(value["memory"])
        return HandoffContext(**{**bounded.__dict__, "updated_at": result.updated_at})

    def with_memory(self, memory: Mapping[str, Any]) -> "HandoffContext":
        bounded: dict[str, list[str]] = {key: [] for key in _MEMORY_KEYS}
        total = 0
        for category in _MEMORY_KEYS:
            raw = memory.get(category, [])
            if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple, set)):
                raw = [raw] if raw else []
            for item in list(raw)[:MAX_MEMORY_ENTRIES]:
                entry = _clip(_bounded_value(item), MAX_MEMORY_ENTRY_CHARS)
                if total + len(entry) > MAX_MEMORY_CHARS:
                    entry = _clip(entry, max(1, MAX_MEMORY_CHARS - total))
                if not entry:
                    continue
                bounded[category].append(entry)
                total += len(entry)
                if total >= MAX_MEMORY_CHARS:
                    break
            if total >= MAX_MEMORY_CHARS:
                break
        return HandoffContext(**{**self.__dict__, "memory": bounded, "updated_at": _now()})

    def with_state(self, status: str, *, waiting_for: Any = None, attempt: int | None = None, result: Any = None, error: Any = None) -> "HandoffContext":
        if status not in _STATUSES:
            raise ValueError(f"Invalid handoff state: {status}")
        old = str(self.state.get("status"))
        if old in {"completed", "failed", "cancelled"} and status != old:
            raise ValueError("Terminal handoff state is immutable")
        state = {"status": status, "attempt": max(0, int(self.state.get("attempt", 0) if attempt is None else attempt)), "waiting_for": _clip(waiting_for, MAX_WAITING_CHARS) if waiting_for is not None else None}
        output = dict(self.output)
        if result is not None:
            output["result"] = _clip(result, MAX_RESULT_CHARS)
        if error is not None:
            output["error"] = _clip(error, MAX_ERROR_CHARS)
        return HandoffContext(**{**self.__dict__, "state": state, "output": output, "updated_at": _now()})

    def child(self, *, kind: str, objective: str, target_scope: str | None = None, target_instance_id: str = "orion", target_agent_id: str | None = None, phase: str = "delegation", contract: str | None = None) -> "HandoffContext":
        return HandoffContext.create(kind=kind, objective=objective, correlation_id=self.correlation_id, source_scope=self.target.get("scope") or "default", source_instance_id=self.target.get("instance_id") or "orion", target_scope=target_scope or self.target.get("scope") or "default", target_instance_id=target_instance_id, target_agent_id=target_agent_id, parent_event_id=self.parent.get("event_id"), parent_task_id=self.parent.get("task_id"), parent_run_id=self.parent.get("run_id"), parent_handoff_id=self.handoff_id, phase=phase, memory=self.memory, contract=contract or self.output.get("contract") or "Return a bounded result to the delegating parent.", routing=self.routing)

    def validate_scope(self, *, scope: str, instance_id: str | None = None, recipient: str | None = None) -> None:
        if not scope or scope == "*" or self.source.get("scope") != scope and self.target.get("scope") != scope:
            raise PermissionError("Handoff scope mismatch")
        if instance_id is not None and instance_id != "*" and self.target.get("instance_id") != instance_id:
            raise PermissionError("Handoff recipient mismatch")
        if recipient is not None and recipient != "*" and self.target.get("agent_id") not in {None, recipient}:
            raise PermissionError("Handoff recipient mismatch")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "version": 1, "handoff_id": self.handoff_id, "kind": self.kind, "correlation_id": self.correlation_id,
            "parent": dict(self.parent), "source": dict(self.source), "target": dict(self.target), "task": dict(self.task),
            "state": dict(self.state), "memory": {key: list(values) for key, values in self.memory.items()}, "output": dict(self.output),
            "routing": dict(self.routing), "created_at": self.created_at, "updated_at": self.updated_at,
        }
        # Round-trip through the parser normalizes legacy/foreign mappings and
        # ensures clipping happened before persistence.
        value = HandoffContext.from_dict(value).__dict__
        value = {key: (dict(item) if isinstance(item, dict) else item) for key, item in value.items()}
        return value

    def to_json(self) -> str:
        value = self.to_dict()
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= MAX_ENVELOPE_BYTES:
            return encoded
        # All free text is already bounded.  A deterministic digest is the
        # final fallback for unusual unicode/object values.
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        value["output"]["result"] = _clip(value["output"].get("result"), 8_000) if value["output"].get("result") else None
        value["output"]["error"] = _clip(value["output"].get("error"), 1_000) if value["output"].get("error") else None
        value["memory"] = {key: entries[:2] for key, entries in value["memory"].items()}
        value["memory"]["artifacts"].append(f"envelope_digest:{digest}")
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ValueError("HandoffContext envelope exceeds 48,000 bytes")
        return encoded


__all__ = [
    "HandoffContext", "MAX_ENVELOPE_BYTES", "MAX_MEMORY_CHARS", "MAX_RESULT_CHARS",
    "derive_correlation_id",
]
