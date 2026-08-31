"""Assemblage et compaction indépendante des composants du contexte Orion."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openrouter_client import OpenRouterClient


@dataclass(frozen=True)
class ContextComponent:
    """Élément indépendant du contexte envoyé au modèle principal."""

    name: str
    value: Any
    max_chars: int
    priority: int = 50


class ContextAssembler:
    """Réduit chaque composant avant l'assemblage du prompt final."""

    def __init__(
        self,
        *,
        compactor: OpenRouterClient | None = None,
        compactor_model: str | None = "openai/gpt-4o-mini",
        total_max_chars: int = 60000,
        compactor_input_chars: int = 30000,
        cache_size: int = 64,
    ) -> None:
        if total_max_chars < 1 or compactor_input_chars < 1 or cache_size < 1:
            raise ValueError("Les limites du ContextAssembler doivent être positives.")
        self.compactor = compactor
        self.compactor_model = compactor_model
        self.total_max_chars = int(total_max_chars)
        self.compactor_input_chars = int(compactor_input_chars)
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_size = int(cache_size)

    @staticmethod
    def _serialize(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    @classmethod
    def _shrink_value(cls, value: Any, *, max_chars: int) -> Any:
        encoded = cls._serialize(value)
        if len(encoded) <= max_chars:
            return value
        if isinstance(value, str):
            return {"truncated": True, "preview": value[:max_chars]}
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            child_limit = max(500, max_chars // 4)
            for key, item in value.items():
                result[str(key)] = cls._shrink_value(item, max_chars=child_limit)
                if len(cls._serialize(result)) > max_chars:
                    result.pop(str(key), None)
                    break
            result["truncated"] = True
            return result
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            items = list(value)[-8:]
            return {
                "truncated": True,
                "items": [cls._shrink_value(item, max_chars=max(500, max_chars // 8)) for item in items],
            }
        return {"truncated": True, "preview": encoded[:max_chars]}

    @classmethod
    def compact_value(cls, value: Any, *, max_chars: int = 4000) -> Any:
        """Expose une réduction déterministe pour les résultats de tools."""
        if max_chars < 1:
            raise ValueError("max_chars doit être positif.")
        return cls._shrink_value(value, max_chars=max_chars)

    @classmethod
    def _projection(cls, name: str, value: Any, *, max_chars: int) -> Any:
        if not isinstance(value, Mapping):
            return cls._shrink_value(value, max_chars=max_chars)

        preferred: dict[str, tuple[str, ...]] = {
            "task": (
                "id", "objective", "status", "priority", "current_state",
                "current_plan_step", "waiting_for", "recent_runs",
                "recent_actions", "updated_at",
            ),
            "event": (
                "id", "type", "source", "priority", "created_at",
                "local_time", "payload", "metadata",
            ),
        }
        keys = preferred.get(name, tuple(str(key) for key in value))
        if not any(key in value for key in keys):
            keys = tuple(str(key) for key in value)
        projected: dict[str, Any] = {}
        child_limit = max(500, max_chars // max(len(keys), 1))
        for key in keys:
            if key in value:
                projected[key] = cls._shrink_value(value[key], max_chars=child_limit)
        return cls._shrink_value(projected, max_chars=max_chars)

    def _llm_compact(self, name: str, source: str, *, max_chars: int, cache_key: str) -> str:
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        compacted = ""
        if self.compactor is not None:
            try:
                response = self.compactor.complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Tu compactes un composant du contexte d'un agent. "
                                "Conserve uniquement les faits utiles, identifiants, statuts, "
                                "dates, heures et sources. N'invente rien. Réponds en texte "
                                "concis, sans préambule ni raisonnement."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Composant: {name}\nDonnées:\n{source[:self.compactor_input_chars]}",
                        },
                    ],
                    model=self.compactor_model,
                    temperature=0,
                )
                from openrouter_client import OpenRouterClient

                compacted = OpenRouterClient.text_from_response(response).strip()
            except Exception:
                compacted = ""
        if not compacted:
            compacted = source
        compacted = compacted[:max_chars]
        self._cache[cache_key] = compacted
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return compacted

    def render(self, component: ContextComponent) -> str:
        if component.max_chars < 1:
            raise ValueError("La limite d'un composant doit être positive.")
        raw = self._serialize(component.value)
        if len(raw) <= component.max_chars:
            return raw

        projection = self._projection(
            component.name,
            component.value,
            max_chars=max(component.max_chars, 2000),
        )
        projected = self._serialize(projection)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cache_key = f"{component.name}:{component.max_chars}:{digest}"
        return self._llm_compact(
            component.name,
            projected,
            max_chars=component.max_chars,
            cache_key=cache_key,
        )

    def assemble(self, components: Sequence[ContextComponent]) -> dict[str, str]:
        """Rend les composants, puis applique une garde totale sans les mélanger."""
        rendered = {component.name: self.render(component) for component in components}
        total = sum(len(value) for value in rendered.values())
        if total <= self.total_max_chars:
            return rendered

        remaining = self.total_max_chars
        for component in sorted(components, key=lambda item: item.priority, reverse=True):
            value = rendered[component.name]
            allowed = min(len(value), remaining)
            rendered[component.name] = value[:allowed]
            remaining = max(0, remaining - allowed)
        return rendered


__all__ = ["ContextAssembler", "ContextComponent"]
