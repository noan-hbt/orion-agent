"""Pre-reflexion interne d'un run Orion.

Cette etape est volontairement separee de la boucle principale : elle ne
dispose d'aucun tool et son texte n'est jamais envoye directement a
l'utilisateur. Elle fournit seulement une piste de lecture au modele qui
prendra ensuite la decision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from context_assembler import ContextAssembler
from openrouter_client import OpenRouterClient


class ReflectionContext(Protocol):
    """Partie minimale de RunContext necessaire a la reflexion."""

    event: Any
    task: Any
    loaded_state: Mapping[str, Any]


class ReflectionEngine:
    """Produit une reflexion courte avec un prompt systeme dedie."""

    def __init__(
        self,
        client: OpenRouterClient,
        *,
        prompt_path: str | Path = "REFLECTION_CORE.md",
        model: str | None = None,
        max_input_chars: int = 12000,
        max_output_chars: int = 5000,
        temperature: float = 0.7,
    ) -> None:
        if max_input_chars < 1000 or max_output_chars < 100:
            raise ValueError("Les limites de reflexion sont invalides.")
        self.client = client
        self.prompt_path = Path(prompt_path)
        self.model = model
        self.max_input_chars = int(max_input_chars)
        self.max_output_chars = int(max_output_chars)
        self.temperature = float(temperature)
        self._system_prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        if not self._system_prompt:
            raise ValueError(f"Le prompt de reflexion est vide : {self.prompt_path}")

    @staticmethod
    def _task_snapshot(task: Any) -> dict[str, Any] | None:
        if task is None:
            return None
        source = task.to_dict() if hasattr(task, "to_dict") else task
        if not isinstance(source, Mapping):
            return {"value": str(source)}
        # Les runs/actions complets sont exclus : ils sont a la fois couteux
        # et rarement necessaires pour le premier jugement.
        return {
            key: source[key]
            for key in (
                "id", "objective", "status", "priority", "current_state",
                "plan", "waiting_for", "updated_at",
            )
            if key in source
        }

    @staticmethod
    def _event_snapshot(event: Any) -> dict[str, Any]:
        return {
            "id": event.id,
            "type": event.type,
            "source": event.source,
            "priority": event.priority,
            "created_at": event.created_at.isoformat(),
            "local_time": event.created_at.astimezone().isoformat(),
            "payload": event.payload,
            "metadata": event.metadata,
        }

    def _context_text(
        self,
        context: ReflectionContext,
        history: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        payload = {
            "event": self._event_snapshot(context.event),
            "task": self._task_snapshot(context.task),
            "loaded_state": dict(context.loaded_state),
            "recent_conversation": list(history)[-6:],
        }
        reduced = ContextAssembler.compact_value(
            payload,
            max_chars=self.max_input_chars,
        )
        return json.dumps(reduced, ensure_ascii=False, default=str)[: self.max_input_chars]

    def reflect(
        self,
        context: ReflectionContext,
        *,
        history: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        """Execute la pre-etape sans tools et renvoie son texte interne."""
        response = self.client.complete(
            [
                {"role": "system", "content": self._system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Voici le contexte disponible. Reflechis a ce qui se passe "
                        "reellement, sans produire de reponse utilisateur :\n"
                        + self._context_text(context, history)
                    ),
                },
            ],
            model=self.model,
            tools=None,
            parallel_tool_calls=False,
            temperature=self.temperature,
        )
        return OpenRouterClient.text_from_response(response).strip()[: self.max_output_chars]


__all__ = ["ReflectionEngine"]
