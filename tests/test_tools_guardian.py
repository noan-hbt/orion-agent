from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from orion.config import AgentConfig
from orion.core.events import Trust
from orion.guardian.limits import within
from orion.guardian.policy import ActionProposal, Approval, Guardian
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


async def test_current_time_uses_requested_timezone() -> None:
    result = await run("current_time", timezone="America/New_York")
    assert "America/New_York" in result.content
    assert (await run("current_time", timezone="Mars/Olympus")).is_error


def test_registry_is_sorted_and_rejects_duplicates() -> None:
    registry = ToolRegistry(builtin_tools())
    assert [s.name for s in registry.specs()] == ["current_time"]
    with pytest.raises(ValueError):
        ToolRegistry([*builtin_tools(), *builtin_tools()])


def test_tool_parse_reports_bad_arguments() -> None:
    with pytest.raises(ToolInputError, match="JSON"):
        tool("current_time").parse("{not json")
    with pytest.raises(ToolInputError, match="invalid arguments"):
        tool("current_time").parse('{"timezone": 3, "extra": true}')


class _Args(BaseModel):
    to: str


async def _noop(_args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult("sent")


SEND = Tool(name="send", description="send", input_model=_Args, handler=_noop)  # defaults: external


class _Approver:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []
        self.reasons: list[str] = []

    async def approve(self, proposal: ActionProposal) -> Approval:
        self.asked.append(proposal.describe())
        self.reasons.append(proposal.ask_reason)
        return Approval(self.answer)


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


async def test_internal_writes_in_untrusted_context_are_put_to_the_owner() -> None:
    write = Tool(
        name="note",
        description="note",
        input_model=_Args,
        handler=_noop,
        effect=Effect.INTERNAL,
    )
    no = _Approver(answer=False)
    decision = await Guardian(no).decide(
        ActionProposal(write, _Args(to="x"), Trust.THIRD_PARTY, "t")
    )
    assert not decision.allowed and "approval" in decision.reason
    assert no.asked == ['note({"to":"x"})']
    assert no.reasons[0].startswith("la conversation contient du contenu venu d'ailleurs")
    yes = _Approver(answer=True)
    assert (
        await Guardian(yes).decide(ActionProposal(write, _Args(to="x"), Trust.THIRD_PARTY, "t"))
    ).allowed
    # In a clean context, an internal write needs nobody.
    assert (
        await Guardian(no).decide(ActionProposal(write, _Args(to="x"), Trust.OWNER, "t"))
    ).allowed


async def test_in_balanced_mode_orion_writes_its_own_state_without_asking() -> None:
    """D85: a web page in the conversation no longer makes every reminder a question; acting on
    the world from there stays refused, without asking either."""
    write = Tool(
        name="note", description="note", input_model=_Args, handler=_noop, effect=Effect.INTERNAL
    )
    no = _Approver(answer=False)
    balanced = Guardian(no, approvals="equilibre")
    assert (await balanced.decide(ActionProposal(write, _Args(to="x"), Trust.WEB, "t"))).allowed
    assert not (await balanced.decide(proposal(SEND, Trust.WEB))).allowed
    assert no.asked == []
    assert AgentConfig().approvals == "equilibre"


def test_orion_never_changes_what_it_asks_alone() -> None:
    mode = {"changes": [{"path": "agent.approvals", "value": "equilibre"}], "why": "moins"}
    assert not within("configure_change", None, mode)
    times = {"changes": [{"path": "initiative.quiet_start", "value": "23:00:00"}], "why": "x"}
    assert within("configure_change", None, times)
