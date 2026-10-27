"""Tool contract.

Defaults are fail-closed: a tool that does not say otherwise is assumed to have external,
irreversible effects, to be unsafe to run concurrently, and to refuse untrusted context.
Tools with external effects never run inside the agent loop: they become action proposals that
the guardian decides on.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orion.core.events import Trust
from orion.providers.base import ToolCall, ToolSpec


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
    doing: str = ""
    """What a call does, in the owner's words (« je cherche sur le web »): said when a turn
    runs long and the model said nothing (`LoopSettings.progress_after`)."""
    doing_of: Callable[[BaseModel, bool], str] | None = None
    """What this call does, from its arguments (« je lis lemonde.fr »), shown live while it
    runs; "" falls back to `doing`. The flag says whether the context is trusted: when it is
    not, only what an argument's shape guarantees (a domain) may show, never its free text."""
    route: Callable[[BaseModel], tuple[str, dict[str, Any]]] | None = None
    """A tool that stands for others (`later`, D157): the tool and arguments a call becomes,
    before it runs (`ToolRegistry.resolve`); ValueError when it cannot. Its handler runs only
    when the call could not be turned, and says why."""

    def effect_for(self, arguments: BaseModel) -> Effect:
        return self.effect_of(arguments) if self.effect_of is not None else self.effect

    def accepts_untrusted_for(self, arguments: BaseModel) -> bool:
        if self.untrusted_of is not None:
            return self.untrusted_of(arguments)
        return self.accepts_untrusted_context

    def spec(self) -> ToolSpec:
        schema = compact_schema(self.input_model.model_json_schema())
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


def quoted(text: str, limit: int = 40) -> str:
    """Words from a call's arguments as a label shows them: « on one line, cut short »."""
    words = " ".join(text.split())
    return f"« {words if len(words) <= limit else words[: limit - 1].rstrip() + '…'} »"


_SCHEMA_MAPS = ("properties", "$defs", "patternProperties")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_ONE = ("items", "additionalProperties", "not")


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema without what pydantic adds for nobody: a `title` repeating each field's
    name, and `default: null` on optional fields. Every request carries every tool's schema,
    so this is paid on every call; only schema keywords are walked, never a value."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key == "default" and value is None:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _sub(sub) for name, sub in cast(dict[str, Any], value).items()}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [_sub(sub) for sub in cast(list[Any], value)]
        elif key in _SCHEMA_ONE:
            out[key] = _sub(value)
        else:
            out[key] = value
    return out


def _sub(value: Any) -> Any:
    return compact_schema(cast(dict[str, Any], value)) if isinstance(value, dict) else value


class ToolInputError(ValueError):
    """The model sent arguments that do not match the tool's schema."""


DRAWER = "orion"
"""The tool through which the drawer's tools are called (D151)."""


class _DrawerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="One of the tools listed above")
    args: dict[str, Any] = Field(
        default_factory=dict[str, Any], description="Its arguments; {} to see what it takes"
    )


DRAWER_LINE = 70
"""Characters of each drawer tool's description shown in the listing."""


def _first_sentence(text: str) -> str:
    line = text.strip().split("\n", 1)[0]
    cut = line.find(". ")
    line = line if cut < 0 else line[: cut + 1]
    return line if len(line) <= DRAWER_LINE else line[: DRAWER_LINE - 1].rsplit(" ", 1)[0] + "…"


class ToolRegistry:
    """A fixed, ordered tool catalog. Order and content never change for a given registry, so
    the serialized tool list is part of the stable, cacheable prompt prefix.

    `drawer`: tools used rarely (once a month, or when he asks), kept out of the model's menu
    and called through one tool, `orion` (D151). Each keeps its name everywhere else: the
    guardian, the journal and the owner's approvals see the drawer's tool, not `orion`.

    `behind`: tools another stands for (`Tool.route`, D157), in the drawer but not listed in
    it: the model reaches them through the tool that stands for them."""

    def __init__(
        self, tools: list[Tool], drawer: list[Tool] | None = None, behind: Sequence[Tool] = ()
    ) -> None:
        drawer = [*(drawer or []), *behind]
        names = [t.name for t in [*tools, *drawer]]
        if len(set(names)) != len(names) or DRAWER in names:
            raise ValueError(f"duplicate tool names: {names}")
        self._tools = {t.name: t for t in sorted([*tools, *drawer], key=lambda t: t.name)}
        self._drawer = {t.name for t in drawer}
        self._behind = hidden = {t.name for t in behind}
        shown = [t for t in self._tools.values() if t.name not in self._drawer]
        listed = sorted((t for t in drawer if t.name not in hidden), key=lambda t: t.name)
        if listed:
            shown.append(self._drawer_tool(listed))
            shown.sort(key=lambda t: t.name)
        self._specs = [t.spec() for t in shown]

    @staticmethod
    def _drawer_tool(drawer: list[Tool]) -> Tool:
        async def unreachable(args: Any, ctx: ToolContext) -> ToolResult:
            del args, ctx
            return ToolResult("orion is resolved before it runs", is_error=True)

        listed = "\n".join(f"- {t.name}: {_first_sentence(t.description)}" for t in drawer)
        return Tool(
            name=DRAWER,
            description="Your less frequent tools, called by name with their arguments:\n" + listed,
            input_model=_DrawerInput,
            handler=unreachable,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
        )

    def resolve(self, call: ToolCall) -> ToolCall:
        """A call through the drawer, as the call of the drawer's tool itself; a call of a tool
        that stands for others (`Tool.route`), as the call it stands for; others as they are.
        A call that cannot be turned stays as it is (and fails, saying why)."""
        if call.name != DRAWER:
            return self._routed(call)
        try:
            asked: Any = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return call
        if not isinstance(asked, dict):
            return call
        name = str(cast(dict[str, Any], asked).get("tool") or "")
        if name not in self._drawer:
            return call
        args: object = cast(dict[str, Any], asked).get("args") or {}
        return self._routed(
            ToolCall(id=call.id, name=name, arguments=json.dumps(args, ensure_ascii=False))
        )

    def _routed(self, call: ToolCall) -> ToolCall:
        tool = self._tools.get(call.name)
        if tool is None or tool.route is None:
            return call
        try:
            name, args = tool.route(tool.parse(call.arguments))
        except (ToolInputError, ValueError):
            return call
        if name not in self._tools or name == call.name:
            return call
        return ToolCall(id=call.id, name=name, arguments=json.dumps(args, ensure_ascii=False))

    def in_drawer(self, name: str) -> bool:
        return name in self._drawer

    def drawer_names(self) -> list[str]:
        """The drawer's listed tools (not those reached through another)."""
        return sorted(self._drawer - self._behind)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def __len__(self) -> int:
        return len(self._tools)
