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
    redaction_enabled: bool = False
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
        redaction_enabled: bool = False,
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
            fragment_sizes: dict[str, int] = {}
            encoded_size = 2  # opening/closing braces
            omitted = 0

            def fragment_size(key: str, item: Any) -> int:
                # Serializing the one-entry object gives the exact JSON size of
                # ``key:value`` (including escaping/quotes) without repeatedly
                # re-encoding the growing result mapping.
                return len(
                    json.dumps(
                        {key: item},
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    )
                ) - 2

            def assign(key: str, item: Any, *, size: int | None = None) -> int:
                """Assign one entry and return the resulting encoded JSON size."""
                nonlocal encoded_size
                if size is None:
                    size = fragment_size(key, item)
                if key in result:
                    encoded_size += size - fragment_sizes[key]
                else:
                    if result:
                        encoded_size += 1  # comma separator
                    encoded_size += size
                result[key] = item
                fragment_sizes[key] = size
                return encoded_size

            def remove(key: str) -> None:
                nonlocal encoded_size
                size = fragment_sizes.pop(key)
                result.pop(key)
                encoded_size -= size
                if result:
                    encoded_size -= 1  # one comma disappears with the entry

            for key, item in value.items():
                key = str(key)
                candidate = cls._shrink_value(item, max_chars=max(1, max_chars // 3))
                candidate_size = fragment_size(key, candidate)
                if key in result:
                    trial_size = encoded_size + candidate_size - fragment_sizes[key]
                else:
                    trial_size = encoded_size + candidate_size + (1 if result else 0)
                if trial_size <= max_chars:
                    assign(key, candidate, size=candidate_size)
                else:
                    omitted += 1
            if omitted or encoded_size > max_chars:
                assign("truncated", True)
                assign("omitted", max(1, omitted))
            while encoded_size > max_chars and result:
                removable = next((key for key in result if key not in {"truncated", "omitted"}), None)
                if removable is None:
                    break
                remove(removable)
            if encoded_size > max_chars:
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
        return cls._shrink_value(value, max_chars=max_chars)

    @classmethod
    def _projection(cls, name: str, value: Any, *, max_chars: int) -> Any:
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
        if (
            component.name == "history"
            and isinstance(value, Sequence)
            and not isinstance(value, (str, bytes, bytearray))
        ):
            bounded = self.bound_history(
                value,
                max_chars=component.max_chars,
                max_tokens=token_limit or max(1, self.count_tokens(raw)),
                turn_limit=self.policy.history_turn_limit,
            )
            # A custom tokenizer may be stricter than the deterministic
            # fallback used by bound_history. Tighten the char allowance until
            # both contracts agree, still dropping whole history blocks only.
            if token_limit is not None:
                allowed_chars = component.max_chars
                while bounded and self.count_tokens(self._serialize(bounded)) > token_limit:
                    allowed_chars = max(1, allowed_chars - max(1, allowed_chars // 8))
                    bounded = self.bound_history(
                        value,
                        max_chars=allowed_chars,
                        max_tokens=token_limit,
                        turn_limit=self.policy.history_turn_limit,
                    )
            return bounded
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
        """Group history into protocol-safe atomic trimming blocks.

        Assistant tool calls and their tool results are never separated.  If
        the input already contains an orphan/malformed tool sequence, drop that
        protocol fragment rather than preserving a provider-invalid history.
        """
        blocks: list[list[Any]] = []
        index = 0
        values = list(history)

        def tool_block(start: int) -> tuple[list[Any] | None, int]:
            assistant = values[start]
            if not isinstance(assistant, Mapping):
                return None, start + 1
            calls = assistant.get("tool_calls")
            if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes, bytearray)):
                return None, start + 1
            ids = [
                str(call.get("id"))
                for call in calls
                if isinstance(call, Mapping) and call.get("id") is not None
            ]
            declared = set(ids)
            cursor = start + 1
            results: list[Any] = []
            seen: list[str] = []
            while cursor < len(values):
                item = values[cursor]
                if not isinstance(item, Mapping) or item.get("role") != "tool":
                    break
                results.append(item)
                seen.append(str(item.get("tool_call_id")))
                cursor += 1
            valid = bool(declared) and len(ids) == len(declared) and set(seen) == declared and len(seen) == len(declared)
            return ([assistant, *results] if valid else None), cursor

        while index < len(values):
            message = values[index]
            if isinstance(message, Mapping) and message.get("role") == "user" and index + 1 < len(values):
                nxt = values[index + 1]
                if isinstance(nxt, Mapping) and nxt.get("role") == "assistant":
                    if nxt.get("tool_calls"):
                        protocol, next_index = tool_block(index + 1)
                        if protocol is not None:
                            blocks.append([message, *protocol])
                        else:
                            blocks.append([message])
                        index = next_index
                        continue
                    blocks.append([message, nxt])
                    index += 2
                    continue
            if isinstance(message, Mapping) and message.get("role") == "assistant" and message.get("tool_calls"):
                protocol, next_index = tool_block(index)
                if protocol is not None:
                    blocks.append(protocol)
                index = next_index
                continue
            if isinstance(message, Mapping) and message.get("role") == "tool":
                # Never keep an orphaned tool result.
                index += 1
                continue
            blocks.append([message])
            index += 1
        return blocks

    @classmethod
    def bound_history(cls, history: Sequence[Any], *, max_chars: int = 10_000, max_tokens: int = 2_500, turn_limit: int | None = None) -> list[Any]:
        blocks = cls._history_blocks(history)
        omitted_by_turn_limit = 0
        if turn_limit is not None:
            limit = max(1, int(turn_limit))
            omitted_by_turn_limit = max(0, len(blocks) - limit)
            blocks = blocks[-limit:]

        def flatten(selected: Sequence[Sequence[Any]], omitted: int, *, marker: bool = True) -> list[Any]:
            result: list[Any] = []
            if omitted and marker:
                result.append({"truncated": True, "omitted": omitted})
            for block in selected:
                result.extend(block)
            return result

        def fits(value: list[Any]) -> bool:
            encoded = cls._serialize(value)
            return len(encoded) <= max_chars and _token_count(encoded) <= max_tokens

        kept: list[list[Any]] = []
        total_blocks = len(blocks)
        for block in reversed(blocks):
            candidate = [block, *kept]
            omitted = omitted_by_turn_limit + total_blocks - len(candidate)
            candidate_value = flatten(candidate, omitted)
            if not fits(candidate_value):
                # Prefer recent real history over an informational marker when
                # the marker alone is what makes an otherwise valid candidate
                # exceed a tight budget.
                candidate_without_marker = flatten(candidate, omitted, marker=False)
                if not kept and fits(candidate_without_marker):
                    kept = candidate
                break
            kept = candidate

        omitted = omitted_by_turn_limit + max(0, total_blocks - len(kept))
        result = flatten(kept, omitted)
        if fits(result):
            return result
        # A very tight budget may not have room for the truncation marker.
        result = flatten(kept, omitted, marker=False)
        if fits(result):
            return result
        return []

    def guard_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        stage: str = "provider",
        final: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a provider-safe message list within the full input budget.

        The budget includes the serialized message envelope and tool schemas.
        Leading system messages are treated as immutable policy prefix: they
        are retained preferentially, but still consume the same char/token
        budget as every other provider input. Remaining history is reduced by
        :meth:`bound_history`, which keeps tool-call/result blocks atomic.
        """
        del final  # Callers pass the actual tool set; no hidden final-mode discount.

        normalized = [dict(message) for message in messages]
        tool_list = [dict(tool) for tool in tools] if tools else []

        prefix: list[dict[str, Any]] = []
        remainder = list(normalized)
        while remainder and remainder[0].get("role") == "system":
            prefix.append(remainder.pop(0))

        def provider_payload(selected: Sequence[Mapping[str, Any]]) -> str:
            body: dict[str, Any] = {"messages": list(selected)}
            if tool_list:
                body["tools"] = tool_list
            return self._serialize(body)

        def fits(selected: Sequence[Mapping[str, Any]]) -> bool:
            encoded = provider_payload(selected)
            return (
                len(encoded) <= self.total_max_chars
                and self.count_tokens(encoded) <= self.total_max_tokens
            )

        # Policy + tools are not reducible here. If they already exceed the
        # provider input budget, sending any request would be impossible.
        if not fits(prefix):
            encoded = provider_payload(prefix)
            raise ValueError(
                "Context provider budget exceeded by policy/system messages and tool schemas "
                f"at stage={stage!r}: chars={len(encoded)}/{self.total_max_chars}, "
                f"tokens={self.count_tokens(encoded)}/{self.total_max_tokens}."
            )

        if not remainder or fits(normalized):
            return normalized

        # Derive a conservative allowance for the reducible suffix. The final
        # provider-envelope check below remains authoritative because JSON
        # separators/wrappers also consume budget.
        base = provider_payload(prefix)
        available_chars = max(1, self.total_max_chars - len(base))
        available_tokens = max(1, self.total_max_tokens - self.count_tokens(base))

        def bounded_suffix(chars: int, tokens: int) -> list[dict[str, Any]]:
            bounded = self.bound_history(
                remainder,
                max_chars=max(1, chars),
                max_tokens=max(1, tokens),
                turn_limit=self.policy.history_turn_limit,
            )
            # bound_history may emit an informational truncation marker. It is
            # useful in stored context but is not a valid chat message because
            # it has no role, so omit it from the provider payload.
            return [
                dict(item)
                for item in bounded
                if isinstance(item, Mapping) and item.get("role") is not None
            ]

        suffix = bounded_suffix(available_chars, available_tokens)
        guarded = [*prefix, *suffix]
        if fits(guarded):
            return guarded

        # A custom token counter or envelope overhead can make the first
        # conservative estimate slightly too large. Tighten the suffix budget
        # geometrically, always re-running the protocol-safe reducer.
        chars = available_chars
        tokens = available_tokens
        previous: tuple[int, int] | None = None
        while suffix:
            chars = max(1, chars - max(1, chars // 8))
            tokens = max(1, tokens - max(1, tokens // 8))
            current = (chars, tokens)
            if current == previous:
                break
            previous = current
            suffix = bounded_suffix(chars, tokens)
            guarded = [*prefix, *suffix]
            if fits(guarded):
                return guarded

        # Policy alone was proven to fit, so dropping all reducible messages is
        # always preferable to emitting an over-budget provider request.
        return prefix

    def assemble(self, components: Sequence[ContextComponent]) -> dict[str, str]:
        # Memory is deliberately opt-in: callers provide a MemoryStore and a
        # component named ``memory_query``. Existing callers are unchanged.
        if self.memory_store is not None:
            resolved: list[ContextComponent] = []
            retrieved: list[Any] = []
            retrieval_limits: list[ContextComponent] = []
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
                retrieved.extend(items)
                retrieval_limits.append(c)

            if retrieval_limits:
                existing_index = next(
                    (index for index, item in enumerate(resolved) if item.name == "memories"),
                    None,
                )
                if existing_index is None:
                    template = retrieval_limits[0]
                    resolved.append(
                        ContextComponent(
                            "memories",
                            retrieved,
                            template.max_chars,
                            template.priority,
                            template.max_tokens,
                        )
                    )
                elif retrieved:
                    # Runtime contract context may already contain memories
                    # extracted into PromptContextStore.  Retrieval augments
                    # that durable set; it must never overwrite it merely
                    # because both components share the public ``memories``
                    # name.
                    existing = resolved[existing_index]
                    base = existing.value if isinstance(existing.value, list) else [existing.value]
                    resolved[existing_index] = ContextComponent(
                        "memories",
                        [*base, *retrieved],
                        max(existing.max_chars, *(item.max_chars for item in retrieval_limits)),
                        max(existing.priority, *(item.priority for item in retrieval_limits)),
                        max(
                            existing.max_tokens or 0,
                            *(item.max_tokens or 0 for item in retrieval_limits),
                        ) or None,
                    )
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
                elif (
                    component.name == "history"
                    and isinstance(component.value, Sequence)
                    and not isinstance(component.value, (str, bytes, bytearray))
                ):
                    bounded_history = self.bound_history(
                        component.value,
                        max_chars=target_chars,
                        max_tokens=max(1, remaining_tokens),
                        turn_limit=self.policy.history_turn_limit,
                    )
                    rendered[component.name] = self._serialize(bounded_history)
                    while (
                        rendered[component.name]
                        and self.count_tokens(rendered[component.name]) > remaining_tokens
                        and target_chars > 2
                    ):
                        target_chars = max(2, target_chars - max(1, target_chars // 8))
                        bounded_history = self.bound_history(
                            component.value,
                            max_chars=target_chars,
                            max_tokens=max(1, remaining_tokens),
                            turn_limit=self.policy.history_turn_limit,
                        )
                        rendered[component.name] = self._serialize(bounded_history)
                    if self.count_tokens(rendered[component.name]) > remaining_tokens:
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
        payload_data = redact_value(dict(data)) if self.policy.redaction_enabled else dict(data)
        payload = {
            "schema": EVIDENCE_SCHEMA,
            "source": str(source),
            "trust": "untrusted",
            "redacted": bool(self.policy.redaction_enabled),
            "data": payload_data,
        }
        encoded = self._serialize(self._shrink_value(payload, max_chars=max_chars or self.total_max_chars))
        encoded = encoded.replace("BEGIN_ORION_EVIDENCE", "BEGIN_ORION_EVIDENCE_")
        encoded = encoded.replace("END_ORION_EVIDENCE", "END_ORION_EVIDENCE_")
        return encoded


__all__ = ["CONTEXT_CONTRACT_VERSION", "ContextAssembler", "ContextComponent", "ContextPolicy", "EVIDENCE_SCHEMA", "redact_value"]
