"""One tool per domain, with an `action`: same handlers, and each call judged by its action."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Trust
from orion.guardian.policy import ActionProposal, Approval, Guardian, Verdict
from orion.tools.actions import NO_ARGS, Action, action_tool, grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


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
    return await tool.handler(tool.parse(json.dumps(args)), CTX)


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
