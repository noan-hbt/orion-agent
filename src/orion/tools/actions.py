"""One tool per domain, with an `action`: fewer, clearer choices for the model.

Sixty small tools (`mission_start`, `mission_note`, `missions`…) make the model hesitate
between neighbours. A domain tool (`mission` with `action="start" | "note" | "list"…`) lets it
pick the domain first, which is easy, then the action, which its description lists with the
parameters each one takes.

Each action keeps its own input model, handler and effect: the arguments are validated against
the action's model, and the guardian judges each call by the effect of the action it asks for
(`Tool.effect_of`), so listing stays a read even in a context holding untrusted content.

Actions whose approval the guardian process checks by name (sending mail, adding to the
calendar, writing to a contact…) stay tools of their own: their name is what the owner's yes is
checked against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolHandler, ToolResult


@dataclass(frozen=True)
class Action:
    name: str
    help: str
    """One line: what it does, when to use it."""
    input_model: type[BaseModel]
    handler: ToolHandler
    effect: Effect = Effect.INTERNAL
    accepts_untrusted_context: bool = False
    """Like `Tool.accepts_untrusted_context`, for this action alone."""


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


NO_ARGS: type[BaseModel] = _NoArgs


_SHAPE_KEYS = ("type", "enum", "format")


def lean_shape(prop: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """What the model needs to fill a field right: its type and allowed values, not its
    lengths or bounds (the action's own model checks those, and says what is wrong). An
    optional field is shown as its type alone; a field that may be several types, as nothing."""
    if "$ref" in prop:
        name = str(prop["$ref"]).rsplit("/", 1)[-1]
        prop = {**schema.get("$defs", {}).get(name, {}), **{k: v for k, v in prop.items()}}
    options = [o for o in prop.get("anyOf", []) if o.get("type") != "null"]
    if len(options) == 1:
        prop = {**options[0], **{k: v for k, v in prop.items() if k != "anyOf"}}
    elif options:
        return {}
    shape = {k: prop[k] for k in _SHAPE_KEYS if k in prop}
    if "const" in prop:
        shape["enum"] = [prop["const"]]
    if shape.get("type") == "array" and isinstance(prop.get("items"), dict):
        items = lean_shape(cast(dict[str, Any], prop["items"]), schema)
        if items:
            shape["items"] = items
    return shape


def _params(model: type[BaseModel]) -> str:
    fields = model.model_fields
    if not fields:
        return ""
    parts = [f"{name}{'' if f.is_required() else '?'}" for name, f in fields.items()]
    return f" ({', '.join(parts)})"


def action_tool(
    name: str,
    description: str,
    actions: list[Action],
    *,
    reversibility: Reversibility = Reversibility.REVERSIBLE,
    accepts_untrusted_context: bool = False,
) -> Tool:
    """A tool whose `action` picks one of `actions`; the other fields are that action's
    parameters (all optional in the schema, with their type and allowed values, checked
    against the action's own model)."""
    by_name = {a.name: a for a in actions}
    if len(by_name) != len(actions):
        raise ValueError(f"{name}: duplicate actions")
    fields: dict[str, Any] = {}
    shapes: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        schema = action.input_model.model_json_schema()
        for field_name, info in action.input_model.model_fields.items():
            if field_name == "action":
                raise ValueError(f"{name}.{action.name}: 'action' is reserved")
            shapes.setdefault(field_name, []).append(
                lean_shape(schema["properties"][info.alias or field_name], schema)
            )
            if field_name not in fields:
                fields[field_name] = info.description
    for field_name, said in list(fields.items()):
        shape = shapes[field_name][0]
        same = all(s == shape for s in shapes[field_name])
        fields[field_name] = (
            Any,
            Field(
                default=None,
                description=said,
                json_schema_extra=shape if same and shape else None,
            ),
        )
    choice: Any = Literal[tuple(by_name)]  # pyright: ignore[reportInvalidTypeArguments]
    model: type[BaseModel] = create_model(  # pyright: ignore[reportCallIssue, reportUnknownVariableType]
        f"{name.title().replace('_', '')}Input",
        __config__=ConfigDict(extra="forbid"),
        action=(choice, Field(description="What to do")),
        **fields,
    )
    lines = [description, "Actions:"]
    lines += [f"- {a.name}{_params(a.input_model)}: {a.help}" for a in actions]

    def chosen(args: BaseModel) -> Action:
        picked: object = args.model_dump().get("action")
        return by_name[str(picked)]

    def own(args: BaseModel) -> BaseModel | str:
        """The action's own arguments, or what is wrong with them."""
        action = chosen(args)
        given = {
            k: v
            for k, v in args.model_dump().items()
            if k != "action" and v is not None and k in action.input_model.model_fields
        }
        others = [
            k
            for k, v in args.model_dump().items()
            if k != "action" and v is not None and k not in action.input_model.model_fields
        ]
        if others:
            takes = _params(action.input_model) or " (nothing else)"
            return f"{name}.{action.name} takes no {', '.join(others)}; it takes{takes}."
        try:
            return action.input_model.model_validate(given)
        except ValidationError as exc:
            return f"invalid arguments for {name}.{action.name}: {exc.errors(include_url=False)}"

    def check(args: BaseModel) -> str | None:
        found = own(args)
        return found if isinstance(found, str) else None

    async def handler(args: Any, ctx: ToolContext) -> ToolResult:
        parsed = own(args)
        if isinstance(parsed, str):
            return ToolResult(parsed, is_error=True)
        return await chosen(args).handler(parsed, ctx)

    def effect_of(args: BaseModel) -> Effect:
        return chosen(args).effect

    def untrusted_of(args: BaseModel) -> bool:
        return accepts_untrusted_context or chosen(args).accepts_untrusted_context

    strongest = max((a.effect for a in actions), key=_EFFECT_ORDER.index)
    return Tool(
        name=name,
        description="\n".join(lines),
        input_model=model,
        handler=handler,
        effect=strongest,
        reversibility=reversibility,
        concurrency_safe=all(a.effect is Effect.NONE for a in actions),
        accepts_untrusted_context=accepts_untrusted_context,
        effect_of=effect_of,
        untrusted_of=untrusted_of,
        check=check,
    )


_EFFECT_ORDER = [Effect.NONE, Effect.INTERNAL, Effect.EXTERNAL]


def grouped(name: str, description: str, tools: list[Tool], renames: dict[str, str]) -> Tool:
    """Tools of one domain as the actions of one tool: `renames` maps each tool's name to its
    action (every tool must be named). Each keeps its model, handler, effect and trust rule."""
    by_name = {t.name: t for t in tools}
    if set(by_name) != set(renames):
        raise ValueError(f"{name}: actions {sorted(renames)} for tools {sorted(by_name)}")
    return action_tool(
        name,
        description,
        [
            Action(
                renames[t.name],
                t.description,
                t.input_model,
                t.handler,
                t.effect,
                t.accepts_untrusted_context,
            )
            for t in tools
        ],
        reversibility=max(
            (t.reversibility for t in tools), key=lambda r: list(Reversibility).index(r)
        ),
    )
