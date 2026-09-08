"""Deterministic, redacted and bounded context primitives.

The assembler accepts the old ContextComponent shape while exposing the v1
contract: values are redacted before serialization, structured values are
reduced as Python objects, and final JSON is never produced by slicing text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openrouter_client import OpenRouterClient


CONTEXT_CONTRACT_VERSION = "v1"
EVIDENCE_SCHEMA = "orion.evidence.v1"
_TRUNCATION_MARKER = "…[truncated]"
_SENSITIVE_KEY = re.compile(
    r"(?:pass(?:word|wd)?|secret|token|credential|authorization|cookie|"
    r"api[_. -]?key|private[_. -]?key|access[_. -]?key|bearer)", re.I,
)
_SENSITIVE_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|(?:sk|rk|pk)[-_][A-Za-z0-9_-]{12,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"-----BEGIN [A-Z ]+PRIVATE KEY-----)", re.I,
)
_URL_SECRET = re.compile(
    r"([?&](?:token|key|secret|password|passwd|authorization|credential)=)"
    r"[^&#\s]+", re.I,
)


def _token_count(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4) if text else 0


def redact_value(value: Any, *, _key: str | None = None) -> Any:
    """Recursively redact sensitive keys and credential-like values."""
    if _key is not None and _SENSITIVE_KEY.search(_key):
        return f"[REDACTED:{_key.lower().replace(' ', '_')}]"
    if isinstance(value, str):
        if _SENSITIVE_VALUE.search(value):
            return "[REDACTED:credential]"
        return _URL_SECRET.sub(r"\1[REDACTED:url-secret]", value)
    if isinstance(value, Mapping):
        return {str(key): redact_value(item, _key=str(key)) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return "[REDACTED:binary]"
    return value


@dataclass(frozen=True)
class ContextPolicy:
    """Versioned budget policy. Values are compatibility knobs."""

    version: str = CONTEXT_CONTRACT_VERSION
    context_mode: str = "contract"
    policy_max_chars: int = 12_000
    policy_max_tokens: int = 3_000
    request_max_chars: int = 8_000
    request_max_tokens: int = 2_000
    profile_max_chars: int = 4_000
    profile_max_tokens: int = 1_000
    event_max_chars: int = 12_000
    event_max_tokens: int = 3_000
    history_max_chars: int = 10_000
    history_max_tokens: int = 2_500
    observations_max_chars: int = 8_000
    observations_max_tokens: int = 2_000
    reflection_max_chars: int = 2_000
    reflection_max_tokens: int = 500
    total_max_chars: int = 48_000
    total_max_tokens: int = 12_000
    output_reserve_tokens: int = 3_000
    history_turn_limit: int = 20
    redaction_enabled: bool = True
    llm_compaction_enabled: bool = False

    def __post_init__(self) -> None:
        if self.version != CONTEXT_CONTRACT_VERSION:
            raise ValueError(f"Unsupported context contract: {self.version}")
        if self.context_mode not in {"contract", "legacy"}:
            raise ValueError("context_mode must be contract or legacy")
        for name, value in self.__dict__.items():
            if name.endswith(("_chars", "_tokens", "_reserve_tokens", "_limit")) and value < 1:
                raise ValueError(f"{name} must be positive")
        if self.total_max_chars < 1 or self.total_max_tokens <= self.output_reserve_tokens:
            raise ValueError("total context budget must leave room for output")


@dataclass(frozen=True)
class ContextComponent:
    name: str
    value: Any
    max_chars: int
    priority: int = 50
    max_tokens: int | None = None


class ContextAssembler:
    """Reduce context components while preserving valid structured values."""

    def __init__(
        self,
        *,
        compactor: OpenRouterClient | None = None,
        compactor_model: str | None = "openai/gpt-4o-mini",
        total_max_chars: int = 48_000,
        total_max_tokens: int = 12_000,
        output_reserve_tokens: int = 3_000,
        compactor_input_chars: int = 30_000,
        cache_size: int = 64,
        policy: ContextPolicy | None = None,
        redaction_enabled: bool = True,
        token_counter: Any | None = None,
        memory_store: Any | None = None,
        memory_namespace: str = "default",
        # Optional context registry/retrieval wiring.  Kept deliberately
        # duck-typed so older MemoryStore implementations remain supported.
        context_registry: Any | None = None,
    ) -> None:
        if policy is None:
            policy = ContextPolicy(
                total_max_chars=int(total_max_chars),
                total_max_tokens=int(total_max_tokens),
                output_reserve_tokens=int(output_reserve_tokens),
                redaction_enabled=bool(redaction_enabled),
                llm_compaction_enabled=compactor is not None,
            )
        if compactor_input_chars < 1 or cache_size < 1:
            raise ValueError("ContextAssembler limits must be positive")
        self.policy = policy
        self.compactor = compactor
        self.compactor_model = compactor_model
        self.total_max_chars = int(policy.total_max_chars)
        self.total_max_tokens = int(policy.total_max_tokens - policy.output_reserve_tokens)
        self.compactor_input_chars = int(compactor_input_chars)
        self.token_counter = token_counter
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_size = int(cache_size)
        self.memory_store = memory_store
        self.memory_namespace = str(memory_namespace)
        self.context_registry = context_registry

    def count_tokens(self, text: str) -> int:
        if self.token_counter is not None:
            try:
                return max(0, int(self.token_counter(text)))
            except Exception:
                pass
        return _token_count(text)

    @staticmethod
    def _serialize(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))

    @staticmethod
    def _clip_text(value: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        if len(value) <= max_chars:
            return value
        marker = _TRUNCATION_MARKER
        if max_chars <= len(marker):
            return marker[:max_chars]
        return value[: max_chars - len(marker)] + marker

    @classmethod
    def _shrink_value(cls, value: Any, *, max_chars: int) -> Any:
        """Produce a bounded Python value, never by slicing JSON text."""
        if max_chars < 1:
            return {"truncated": True, "omitted": 1}
        if isinstance(value, str):
            return cls._clip_text(value, max_chars)
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            omitted = 0
            for key, item in value.items():
                key = str(key)
                candidate = cls._shrink_value(item, max_chars=max(1, max_chars // 3))
                trial = dict(result)
                trial[key] = candidate
                if len(cls._serialize(trial)) <= max_chars:
                    result[key] = candidate
                else:
                    omitted += 1
            if omitted or len(cls._serialize(result)) > max_chars:
                result["truncated"] = True
                result["omitted"] = max(1, omitted)
            while len(cls._serialize(result)) > max_chars and result:
                removable = next((key for key in result if key not in {"truncated", "omitted"}), None)
                if removable is None:
                    break
                result.pop(removable)
            if len(cls._serialize(result)) > max_chars:
                return {}
            return result
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values = list(value)
            result: list[Any] = []
            omitted = 0
            for item in values[-8:]:
                candidate = cls._shrink_value(item, max_chars=max(1, max_chars // 3))
                if len(cls._serialize(result + [candidate])) <= max_chars:
                    result.append(candidate)
                else:
                    omitted += 1
            omitted += max(0, len(values) - min(len(values), 8))
            if omitted:
                marker = {"truncated": True, "omitted": omitted}
                while result and len(cls._serialize([marker] + result)) > max_chars:
                    result.pop(0)
                result.insert(0, marker)
            if len(cls._serialize(result)) > max_chars:
                return []
            return result
        encoded = cls._serialize(value)
        return value if len(encoded) <= max_chars else {"truncated": True, "omitted": 1}

    @classmethod
    def compact_value(cls, value: Any, *, max_chars: int = 4000) -> Any:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        return cls._shrink_value(redact_value(value), max_chars=max_chars)

    @classmethod
    def _projection(cls, name: str, value: Any, *, max_chars: int) -> Any:
        value = redact_value(value)
        if not isinstance(value, Mapping):
            return cls._shrink_value(value, max_chars=max_chars)
        preferred: dict[str, tuple[str, ...]] = {
            "task": ("id", "objective", "status", "priority", "current_state", "current_plan_step", "waiting_for", "updated_at"),
            "event": ("id", "type", "source", "priority", "created_at", "local_time", "payload", "metadata"),
            "loaded_state": ("status", "state", "phase", "updated_at", "cursor", "version"),
        }
        keys = preferred.get(name, tuple(str(key) for key in value))
        projected: dict[str, Any] = {}
        for key in keys:
            if key in value:
                projected[key] = value[key]
        return cls._shrink_value(projected, max_chars=max_chars)

    def _llm_compact(self, name: str, source: Any, *, max_chars: int, cache_key: str) -> str:
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached
        compacted = ""
        if self.compactor is not None and self.policy.llm_compaction_enabled:
            try:
                response = self.compactor.complete(
                    [{"role": "system", "content": "Compact redacted context. Return only bounded facts; never reason aloud."},
                     {"role": "user", "content": f"Component: {name}\n{self._clip_text(str(source), self.compactor_input_chars)}"}],
                    model=self.compactor_model, temperature=0,
                )
                from openrouter_client import OpenRouterClient
                compacted = OpenRouterClient.text_from_response(response).strip()
            except Exception:
                compacted = ""
        if not compacted:
            compacted = self._serialize(source)
        compacted = self._clip_text(compacted, max_chars)
        self._cache[cache_key] = compacted
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return compacted

    def render_value(self, component: ContextComponent) -> Any:
        if component.max_chars < 1:
            raise ValueError("component max_chars must be positive")
        value = redact_value(component.value) if self.policy.redaction_enabled else component.value
        raw = self._serialize(value)
        token_limit = component.max_tokens
        within = len(raw) <= component.max_chars and (token_limit is None or self.count_tokens(raw) <= token_limit)
        if within:
            return value
        projection = self._projection(component.name, value, max_chars=component.max_chars)
        projected = self._serialize(projection)
        preferred = {
            "task": ("id", "objective", "status", "priority", "current_state", "current_plan_step", "waiting_for", "updated_at"),
            "event": ("id", "type", "source", "priority", "created_at", "local_time", "payload", "metadata"),
            "loaded_state": ("status", "state", "phase", "updated_at", "cursor", "version"),
        }
        if isinstance(value, Mapping):
            source_keys = preferred.get(component.name, tuple(str(key) for key in value))
            projection_source = {key: value[key] for key in source_keys if key in value}
            source_size = len(self._serialize(projection_source))
        else:
            source_size = len(raw)
        if len(projected) <= component.max_chars and source_size <= component.max_chars and (token_limit is None or self.count_tokens(projected) <= token_limit):
            return projection
        digest = hashlib.sha256(projected.encode("utf-8")).hexdigest()
        compacted = self._llm_compact(component.name, projection, max_chars=component.max_chars, cache_key=f"{component.name}:{component.max_chars}:{digest}")
        if isinstance(value, (Mapping, list, tuple)):
            try:
                parsed = json.loads(compacted)
                return self._shrink_value(parsed, max_chars=component.max_chars)
            except (TypeError, json.JSONDecodeError):
                return self._shrink_value(projection, max_chars=component.max_chars)
        return self._clip_text(compacted, component.max_chars)

    def render(self, component: ContextComponent) -> str:
        rendered = self.render_value(component)
        encoded = self._serialize(rendered)
        if len(encoded) > component.max_chars:
            encoded = self._serialize(self._shrink_value(rendered, max_chars=component.max_chars))
        return encoded

    @classmethod
    def _history_blocks(cls, history: Sequence[Any]) -> list[list[Any]]:
        blocks: list[list[Any]] = []
        index = 0
        values = list(history)
        while index < len(values):
            message = values[index]
            block = [message]
            if isinstance(message, Mapping) and message.get("role") == "assistant" and message.get("tool_calls"):
                ids = {str(call.get("id")) for call in message.get("tool_calls", []) if isinstance(call, Mapping)}
                index += 1
                while index < len(values) and isinstance(values[index], Mapping) and values[index].get("role") == "tool" and str(values[index].get("tool_call_id")) in ids:
                    block.append(values[index]); index += 1
                blocks.append(block)
                continue
            if isinstance(message, Mapping) and message.get("role") == "user" and index + 1 < len(values):
                nxt = values[index + 1]
                if isinstance(nxt, Mapping) and nxt.get("role") == "assistant":
                    block.append(nxt); index += 1
            blocks.append(block)
            index += 1
        return blocks

    @classmethod
    def bound_history(cls, history: Sequence[Any], *, max_chars: int = 10_000, max_tokens: int = 2_500, turn_limit: int | None = None) -> list[Any]:
        blocks = cls._history_blocks(redact_value(history))
        if turn_limit is not None:
            blocks = blocks[-max(1, int(turn_limit)):]
        kept: list[list[Any]] = []
        total_chars = 2
        total_tokens = 1
        for block in reversed(blocks):
            encoded = cls._serialize(block)
            if kept and (total_chars + len(encoded) > max_chars or total_tokens + _token_count(encoded) > max_tokens):
                break
            if not kept and len(encoded) > max_chars:
                block = cls._shrink_value(block, max_chars=max_chars - 2)
                if not isinstance(block, list):
                    block = [{"truncated": True, "omitted": 1}]
            kept.insert(0, block)
            encoded = cls._serialize(block)
            total_chars += len(encoded)
            total_tokens += _token_count(encoded)
        omitted = max(0, len(blocks) - len(kept))
        result: list[Any] = ([{"truncated": True, "omitted": omitted}] if omitted else [])
        for block in kept:
            result.extend(block)
        return result

    def assemble(self, components: Sequence[ContextComponent]) -> dict[str, str]:
        # Memory is deliberately opt-in: callers provide a MemoryStore and a
        # component named ``memory_query``. Existing callers are unchanged.
        if self.memory_store is not None:
            resolved = []
            for c in components:
                if c.name != "memory_query":
                    resolved.append(c)
                    continue
                try:
                    found = self.memory_store.search(str(c.value), namespace=self.memory_namespace)
                except Exception:
                    found = []
                # Make provenance explicit and stable; dataclasses and legacy
                # mapping results are both accepted.
                items = []
                for item in found or []:
                    if isinstance(item, Mapping):
                        value = dict(item)
                    else:
                        value = {k: getattr(item, k) for k in ("id", "content", "provenance", "confidence", "namespace") if hasattr(item, k)}
                    items.append(value)
                resolved.append(ContextComponent("memories", items, c.max_chars, c.priority, c.max_tokens))
            components = resolved
        rendered = {component.name: self.render(component) for component in components}
        remaining_chars = self.total_max_chars
        remaining_tokens = self.total_max_tokens
        for component in sorted(components, key=lambda item: item.priority, reverse=True):
            value = rendered[component.name]
            allowed_chars = min(len(value), max(0, remaining_chars))
            target_chars = min(allowed_chars, max(0, remaining_tokens * 4))
            if len(value) > allowed_chars or self.count_tokens(value) > remaining_tokens:
                if target_chars < 2:
                    rendered[component.name] = ""
                else:
                    bounded = self._shrink_value(component.value, max_chars=target_chars)
                    if isinstance(component.value, (Mapping, Sequence)) and not isinstance(component.value, str):
                        rendered[component.name] = self._serialize(bounded)
                        while rendered[component.name] and self.count_tokens(rendered[component.name]) > remaining_tokens and target_chars > 2:
                            target_chars -= 1
                            rendered[component.name] = self._serialize(self._shrink_value(component.value, max_chars=target_chars))
                        if self.count_tokens(rendered[component.name]) > remaining_tokens:
                            rendered[component.name] = ""
                    else:
                        rendered[component.name] = self._clip_text(value, target_chars)
                    while (not isinstance(component.value, (Mapping, Sequence)) or isinstance(component.value, str)) and rendered[component.name] and self.count_tokens(rendered[component.name]) > remaining_tokens:
                        rendered[component.name] = self._clip_text(rendered[component.name], len(rendered[component.name]) - 1)
            remaining_chars = max(0, remaining_chars - len(rendered[component.name]))
            remaining_tokens = max(0, remaining_tokens - self.count_tokens(rendered[component.name]))
        return rendered

    def evidence_envelope(self, data: Mapping[str, Any], *, source: str = "runtime", max_chars: int | None = None) -> str:
        payload = {"schema": EVIDENCE_SCHEMA, "source": str(source), "trust": "untrusted", "redacted": True, "data": redact_value(dict(data))}
        encoded = self._serialize(self._shrink_value(payload, max_chars=max_chars or self.total_max_chars))
        encoded = encoded.replace("BEGIN_ORION_EVIDENCE", "BEGIN_ORION_EVIDENCE_")
        encoded = encoded.replace("END_ORION_EVIDENCE", "END_ORION_EVIDENCE_")
        return encoded


__all__ = ["CONTEXT_CONTRACT_VERSION", "ContextAssembler", "ContextComponent", "ContextPolicy", "EVIDENCE_SCHEMA", "redact_value"]
