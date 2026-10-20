"""Tool contract.

Defaults are fail-closed: a tool that does not say otherwise is assumed to have external,
irreversible effects, to be unsafe to run concurrently, and to refuse untrusted context.
Tools with external effects never run inside the agent loop: they become action proposals that
the guardian decides on.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from orion.core.events import Trust
from orion.providers.base import ToolSpec


class Effect(StrEnum):
    NONE = "none"  # pure computation or read-only lookup
    INTERNAL = "internal"  # changes Orion's own state (memory, tasks)
    EXTERNAL = "external"  # visible outside Orion (messages, purchases, files elsewhere)


class Reversibility(StrEnum):
    REVERSIBLE = "reversible"
    UNDO_WINDOW = "undo_window"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class ToolContext:
    thread_id: str
    correlation_id: str
    origin_event_id: str | None = None
    """The event that started this turn (e.g. the owner's message); memory writes derive from it."""
    context_trust: Trust = Trust.OWNER
    timezone: str = "Europe/Paris"
    grant: str | None = None
    """The owner's approval of this very call, as recorded by the guardian process: an action
    the guardian checks (calling a stranger, writing to another Orion) presents it."""
    alone: bool = False
    """Done without asking, within an autonomy the owner granted: he is told afterwards."""


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    trust: Trust = Trust.SYSTEM
    """Trust of the returned content. Tools that return outside content (web, mail) lower it."""


ToolHandler = Callable[[Any, ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    effect: Effect = Effect.EXTERNAL
    reversibility: Reversibility = Reversibility.IRREVERSIBLE
    concurrency_safe: bool = False
    accepts_untrusted_context: bool = False
    max_output_chars: int = 20_000
    effect_of: Callable[[BaseModel], Effect] | None = None
    """For a tool with several actions (`orion.tools.actions`): the effect of this call."""
    untrusted_of: Callable[[BaseModel], bool] | None = None
    """For a tool with several actions: whether this call may run in an untrusted context."""
    check: Callable[[BaseModel], str | None] | None = None
    """For a tool with several actions: what is wrong with this call's arguments, if anything.
    Run by `parse`, so a bad call is refused before the owner is asked to approve it."""

    def effect_for(self, arguments: BaseModel) -> Effect:
        return self.effect_of(arguments) if self.effect_of is not None else self.effect

    def accepts_untrusted_for(self, arguments: BaseModel) -> bool:
        if self.untrusted_of is not None:
            return self.untrusted_of(arguments)
        return self.accepts_untrusted_context

    def spec(self) -> ToolSpec:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, parameters=schema)

    def parse(self, raw_arguments: str) -> BaseModel:
        try:
            data = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ToolInputError(f"arguments are not valid JSON: {exc.msg}") from exc
        try:
            parsed = self.input_model.model_validate(data)
        except ValidationError as exc:
            raise ToolInputError(f"invalid arguments: {exc.errors(include_url=False)}") from exc
        wrong = self.check(parsed) if self.check is not None else None
        if wrong is not None:
            raise ToolInputError(wrong)
        return parsed


class ToolInputError(ValueError):
    """The model sent arguments that do not match the tool's schema."""


class ToolRegistry:
    """A fixed, ordered tool catalog. Order and content never change for a given registry, so
    the serialized tool list is part of the stable, cacheable prompt prefix."""

    def __init__(self, tools: list[Tool]) -> None:
        names = [t.name for t in tools]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate tool names: {names}")
        self._tools = {t.name: t for t in sorted(tools, key=lambda t: t.name)}
        self._specs = [t.spec() for t in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def __len__(self) -> int:
        return len(self._tools)
