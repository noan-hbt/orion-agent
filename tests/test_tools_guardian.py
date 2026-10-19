from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from orion.core.events import Trust
from orion.guardian.policy import ActionProposal, Guardian
from orion.tools.base import Effect, Tool, ToolContext, ToolInputError, ToolRegistry, ToolResult
from orion.tools.builtin import builtin_tools

CTX = ToolContext(thread_id="t", correlation_id="c")


def tool(name: str) -> Tool:
    registry = ToolRegistry(builtin_tools())
    found = registry.get(name)
    assert found is not None
    return found


async def run(name: str, **args: Any) -> ToolResult:
    t = tool(name)
    return await t.handler(t.input_model.model_validate(args), CTX)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [("2 + 3 * 4", "14"), ("(12.5 * 3) / 4 ** 2", "2.34375"), ("-7 // 2", "-4")],
)
async def test_calculate(expression: str, expected: str) -> None:
    result = await run("calculate", expression=expression)
    assert not result.is_error
    assert result.content == expected


@pytest.mark.parametrize(
    "expression", ["__import__('os').system('id')", "a + 1", "2 ** 100000", "1 / 0", "[1, 2]"]
)
async def test_calculate_rejects_anything_but_arithmetic(expression: str) -> None:
    assert (await run("calculate", expression=expression)).is_error


async def test_date_math_gives_weekday_in_french() -> None:
    result = await run("date_math", start="2026-10-19", days=12)
    assert result.content.startswith("samedi 2026-10-31")


async def test_current_time_uses_requested_timezone() -> None:
    result = await run("current_time", timezone="America/New_York")
    assert "America/New_York" in result.content
    assert (await run("current_time", timezone="Mars/Olympus")).is_error


def test_registry_is_sorted_and_rejects_duplicates() -> None:
    registry = ToolRegistry(builtin_tools())
    assert [s.name for s in registry.specs()] == ["calculate", "current_time", "date_math"]
    with pytest.raises(ValueError):
        ToolRegistry([*builtin_tools(), *builtin_tools()])


def test_tool_parse_reports_bad_arguments() -> None:
    with pytest.raises(ToolInputError, match="JSON"):
        tool("calculate").parse("{not json")
    with pytest.raises(ToolInputError, match="invalid arguments"):
        tool("calculate").parse('{"expression": 3, "extra": true}')


class _Args(BaseModel):
    to: str


async def _noop(_args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult("sent")


SEND = Tool(name="send", description="send", input_model=_Args, handler=_noop)  # defaults: external


class _Approver:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def approve(self, proposal: ActionProposal) -> bool:
        self.asked.append(proposal.describe())
        return self.answer


def proposal(t: Tool, trust: Trust = Trust.OWNER) -> ActionProposal:
    return ActionProposal(t, _Args(to="x") if t is SEND else t.input_model(), trust, "t")


def test_tool_defaults_are_fail_closed() -> None:
    assert SEND.effect is Effect.EXTERNAL
    assert not SEND.concurrency_safe
    assert not SEND.accepts_untrusted_context


async def test_guardian_allows_side_effect_free_tools_without_asking() -> None:
    approver = _Approver(answer=False)
    decision = await Guardian(approver).decide(proposal(tool("current_time"), Trust.WEB))
    assert decision.allowed
    assert approver.asked == []


async def test_guardian_requires_owner_approval_for_external_effects() -> None:
    assert not (await Guardian().decide(proposal(SEND))).allowed  # default: nobody to ask
    yes = _Approver(answer=True)
    assert (await Guardian(yes).decide(proposal(SEND))).allowed
    assert yes.asked == ['send({"to":"x"})']


async def test_guardian_blocks_external_effects_in_untrusted_context_without_asking() -> None:
    approver = _Approver(answer=True)
    decision = await Guardian(approver).decide(proposal(SEND, Trust.THIRD_PARTY))
    assert not decision.allowed
    assert "untrusted" in decision.reason
    assert approver.asked == []  # the owner is not even bothered
