"""Bounded advisory reflection for Orion.

Reflection is an optional data-producing step. It never emits a transcript or
free-form chain of thought: only a validated orion.reflection.v1 object is
returned to the caller.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from context_assembler import ContextAssembler, redact_value
from openrouter_client import OpenRouterClient

REFLECTION_SCHEMA = "orion.reflection.v1"
_SOURCES = {"event", "task", "history", "unknown"}

class ReflectionContext(Protocol):
    event: Any
    task: Any
    loaded_state: Mapping[str, Any]

class ReflectionEngine:
    """Produce a short, schema-validated advisory object."""

    def __init__(self, client: OpenRouterClient, *, prompt_path: str | Path = "REFLECTION_CORE.md", model: str | None = None, max_input_chars: int = 12000, max_output_chars: int = 2000, temperature: float = 0.0, reflection_format: str = "advisory_json") -> None:
        if max_input_chars < 1000 or max_output_chars < 100:
            raise ValueError("reflection limits are invalid")
        if reflection_format not in {"advisory_json", "legacy_text"}:
            raise ValueError("reflection_format must be advisory_json or legacy_text")
        self.client = client
        self.prompt_path = Path(prompt_path)
        self.model = model
        self.max_input_chars = int(max_input_chars)
        self.max_output_chars = int(max_output_chars)
        self.temperature = float(temperature)
        self.reflection_format = reflection_format
        self.invalid_outputs = 0
        self._system_prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        if not self._system_prompt:
            raise ValueError(f"reflection prompt is empty: {self.prompt_path}")

    @staticmethod
    def _task_snapshot(task: Any) -> dict[str, Any] | None:
        if task is None:
            return None
        source = task.to_dict() if hasattr(task, "to_dict") else task
        if not isinstance(source, Mapping):
            return {"value": str(source)}
        return {key: source[key] for key in ("id", "objective", "status", "priority", "current_state", "current_plan_step", "waiting_for", "updated_at") if key in source}

    @staticmethod
    def _event_snapshot(event: Any) -> dict[str, Any]:
        def attr(name: str, default: Any = None) -> Any:
            value = getattr(event, name, default)
            return value.isoformat() if hasattr(value, "isoformat") else value
        return {"id": attr("id"), "type": attr("type"), "source": attr("source"), "priority": attr("priority"), "created_at": attr("created_at"), "local_time": attr("created_at"), "payload": attr("payload", {}), "metadata": attr("metadata", {})}

    def _context_text(self, context: ReflectionContext, history: Sequence[Mapping[str, Any]] = ()) -> str:
        payload = {"event": self._event_snapshot(context.event), "task": self._task_snapshot(context.task), "loaded_state": dict(context.loaded_state or {}), "recent_conversation": list(history)[-6:]}
        reduced = ContextAssembler.compact_value(redact_value(payload), max_chars=self.max_input_chars)
        return json.dumps(reduced, ensure_ascii=False, default=str, separators=(",", ":"))

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    def _validate(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or value.get("schema") != REFLECTION_SCHEMA:
            return None
        summary = self._bounded_text(value.get("summary"), 500)
        if not summary:
            return None
        result: dict[str, Any] = {"schema": REFLECTION_SCHEMA, "summary": summary}
        raw = value.get("facts", [])
        if not isinstance(raw, list):
            return None
        facts: list[dict[str, str]] = []
        for item in raw[:8]:
            if not isinstance(item, Mapping):
                continue
            text = self._bounded_text(item.get("text"), 300)
            if text:
                source = str(item.get("source", "unknown"))
                facts.append({"text": text, "source": source if source in _SOURCES else "unknown"})
        result["facts"] = facts
        for field in ("uncertainties", "risks", "checks"):
            raw = value.get(field, [])
            if not isinstance(raw, list):
                return None
            result[field] = [self._bounded_text(item, 240) for item in raw[:8] if self._bounded_text(item, 240)]
        confidence = value.get("confidence")
        if confidence is None:
            result["confidence"] = None
        else:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                return None
            if not 0 <= confidence <= 1:
                return None
            result["confidence"] = confidence
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > self.max_output_chars:
            result["summary"] = self._bounded_text(result["summary"], max(40, self.max_output_chars // 5))
            while len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > self.max_output_chars:
                removed = False
                for field in ("checks", "risks", "uncertainties", "facts"):
                    if result[field]:
                        result[field].pop()
                        removed = True
                        break
                if not removed:
                    break
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > self.max_output_chars:
            return None
        return result

    def reflect(self, context: ReflectionContext, *, history: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any] | str | None:
        """Call the model and return an advisory object, or None on failure."""
        prompt = "Return JSON only using schema orion.reflection.v1. Do not provide chain-of-thought, hidden reasoning, plans, refusal prose, user-facing text, or invented facts. Use only the supplied evidence." + chr(10) + self._context_text(context, history)
        try:
            response = self.client.complete([{"role": "system", "content": self._system_prompt}, {"role": "user", "content": prompt}], model=self.model, tools=None, parallel_tool_calls=False, temperature=self.temperature)
            text = OpenRouterClient.text_from_response(response).strip()
            if self.reflection_format == "legacy_text":
                return text[: self.max_output_chars] or None
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                self.invalid_outputs += 1
                return None
            result = self._validate(candidate)
            if result is None:
                self.invalid_outputs += 1
            return result
        except Exception:
            self.invalid_outputs += 1
            return None

__all__ = ["REFLECTION_SCHEMA", "ReflectionEngine"]
