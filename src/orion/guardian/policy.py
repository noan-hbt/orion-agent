"""Guardian v0: deterministic policy over tool calls.

M1 runs the guardian in-process behind this interface; it moves to its own process (holding
credentials and channels) when the first external channel arrives in M3. The rules do not
depend on anything the model writes: only on the tool's declared properties, the trust of the
context it would run in, and the owner's answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from orion.core.events import Trust
from orion.tools.base import Effect, Tool

TRUSTED_FLOOR = Trust.SYSTEM
"""Content below this level (other instances, third parties, web) taints a context."""


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class ActionProposal:
    tool: Tool
    arguments: BaseModel
    context_trust: Trust
    thread_id: str

    def describe(self) -> str:
        return f"{self.tool.name}({self.arguments.model_dump_json()})"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


class Approver(Protocol):
    async def approve(self, proposal: ActionProposal) -> bool: ...


class DenyAllApprover:
    """Used when no human is reachable: anything needing approval is refused."""

    async def approve(self, proposal: ActionProposal) -> bool:
        return False


class Guardian:
    def __init__(self, approver: Approver | None = None) -> None:
        self._approver = approver or DenyAllApprover()

    async def decide(self, proposal: ActionProposal) -> Decision:
        tool = proposal.tool
        if proposal.context_trust < TRUSTED_FLOOR and not tool.accepts_untrusted_context:
            return Decision(
                Verdict.DENY,
                f"{tool.name} cannot run in a context containing untrusted content "
                f"(trust {proposal.context_trust.name})",
            )
        if tool.effect is Effect.NONE:
            return Decision(Verdict.ALLOW, "side-effect free")
        if tool.effect is Effect.INTERNAL:
            return Decision(Verdict.ALLOW, "internal effect only")
        if await self._approver.approve(proposal):
            return Decision(Verdict.ALLOW, "approved by owner")
        return Decision(Verdict.DENY, "owner approval required and not given")
