"""One tool per domain, with an `action`: same handlers, and each call judged by its action."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Trust
from orion.guardian.policy import ActionProposal, Approval, Guardian, Verdict
from orion.tools.actions import NO_ARGS, Action, action_tool, grouped
from orion.tools.base import (
    Effect,
    Reversibility,
    Tool,
    ToolContext,
    ToolInputError,
    ToolResult,
)


class AddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(min_length=2, description="What to add")


async def add(args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult(f"added {args.what}")


async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult("the list")


NOTES = action_tool(
    "notes",
    "The owner's notes.",
    [
        Action("add", "Add a note.", AddInput, add),
        Action("list", "The notes.", NO_ARGS, listing, Effect.NONE, accepts_untrusted_context=True),
    ],
)
CTX = ToolContext("owner", "c")


async def run(tool: Tool, **args: object) -> ToolResult:
    try:
        parsed = tool.parse(json.dumps(args))
    except ToolInputError as exc:
        return ToolResult(str(exc), is_error=True)
    return await tool.handler(parsed, CTX)


async def test_a_bad_call_is_refused_before_anyone_is_asked() -> None:
    """20/10: configure was approved, then failed on its arguments, then asked again."""
    with pytest.raises(ToolInputError, match=r"notes\.add"):
        NOTES.parse(json.dumps({"action": "add"}))
    with pytest.raises(ToolInputError, match="takes no what"):
        NOTES.parse(json.dumps({"action": "list", "what": "pain"}))


async def test_each_action_takes_its_own_arguments() -> None:
    assert (await run(NOTES, action="add", what="pain")).content == "added pain"
    assert (await run(NOTES, action="list")).content == "the list"
    missing = await run(NOTES, action="add")
    assert missing.is_error and "notes.add" in missing.content
    stray = await run(NOTES, action="list", what="pain")
    assert stray.is_error and "takes no what" in stray.content
    description = NOTES.spec().description
    assert "- add (what): Add a note." in description and "- list: The notes." in description
    assert NOTES.spec().parameters["properties"]["action"]["enum"] == ["add", "list"]


class No:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def approve(self, proposal: ActionProposal) -> Approval:
        self.asked.append(proposal.describe())
        return Approval(False)


async def test_the_guardian_judges_the_action_not_the_tool() -> None:
    approver = No()
    guardian = Guardian(approver)

    def proposal(trust: Trust, **args: object) -> ActionProposal:
        return ActionProposal(NOTES, NOTES.parse(json.dumps(args)), trust, "owner")

    read = await guardian.decide(proposal(Trust.THIRD_PARTY, action="list"))
    assert read.verdict is Verdict.ALLOW  # reading stays a read, even beside a mail
    write = await guardian.decide(proposal(Trust.THIRD_PARTY, action="add", what="pain"))
    assert write.verdict is Verdict.DENY and len(approver.asked) == 1  # put to the owner
    trusted = await guardian.decide(proposal(Trust.OWNER, action="add", what="pain"))
    assert trusted.verdict is Verdict.ALLOW


def test_grouping_existing_tools_keeps_their_effects() -> None:
    def tool(name: str, effect: Effect, rev: Reversibility) -> Tool:
        return Tool(name, f"{name} it.", NO_ARGS, listing, effect, rev)

    group = grouped(
        "box",
        "A box.",
        [
            tool("box_open", Effect.INTERNAL, Reversibility.REVERSIBLE),
            tool("boxes", Effect.NONE, Reversibility.UNDO_WINDOW),
        ],
        {"box_open": "open", "boxes": "list"},
    )
    assert group.effect is Effect.INTERNAL and group.reversibility is Reversibility.UNDO_WINDOW
    assert group.effect_for(group.parse('{"action": "list"}')) is Effect.NONE
    assert not group.concurrency_safe


def test_a_schema_carries_no_generated_titles_or_null_defaults() -> None:
    """Every request carries every schema: what pydantic adds for nobody is left out, and a
    field named `title`, or a default that holds one, is kept."""
    from orion.tools.base import compact_schema

    class Doc(BaseModel):
        title: str = Field(description="Its title")
        note: str | None = None
        shape: dict[str, str] = Field(default={"title": "kept"})

    schema = compact_schema(Doc.model_json_schema())
    text = json.dumps(schema)
    assert '"title": "Doc"' not in text and '"title": "Note"' not in text
    assert "default" not in schema["properties"]["note"]
    assert schema["properties"]["title"] == {"description": "Its title", "type": "string"}
    assert schema["properties"]["note"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert schema["properties"]["shape"]["default"] == {"title": "kept"}


def test_the_model_sees_each_fields_type_and_allowed_values() -> None:
    """26/10: `playbook.learn` was called with scope « contacts », and `papers.find` with a kind
    off the list: the grouped schema showed neither. Types and allowed values are shown; lengths
    are not (the action's model checks them and says what is wrong); a field two actions type
    differently is shown without a type."""
    from enum import StrEnum
    from typing import Literal

    from orion.tools.base import compact_schema

    class Scope(StrEnum):
        STYLE = "style"
        WORK = "work"

    class Learn(BaseModel):
        model_config = ConfigDict(extra="forbid")

        rule: str = Field(min_length=3, max_length=300)
        scope: Scope = Scope.STYLE
        times: int | None = None
        ids: list[str] = Field(default_factory=list[str])
        day: int = 1

    class Close(BaseModel):
        model_config = ConfigDict(extra="forbid")

        status: Literal["done", "cancelled"] = "done"
        day: str = ""

    tool = action_tool(
        "rules",
        "Rules.",
        [Action("learn", "Learn.", Learn, add), Action("close", "Close.", Close, add)],
    )
    shown = compact_schema(tool.input_model.model_json_schema())["properties"]
    assert shown["rule"] == {"type": "string"}
    assert shown["scope"] == {"enum": ["style", "work"], "type": "string"}
    assert shown["times"] == {"type": "integer"}
    assert shown["ids"] == {"items": {"type": "string"}, "type": "array"}
    assert shown["status"] == {"enum": ["done", "cancelled"], "type": "string"}
    assert shown["day"] == {}
    with pytest.raises(ToolInputError, match=r"rules\.learn"):
        tool.parse(json.dumps({"action": "learn", "rule": "ok", "scope": "contacts"}))
