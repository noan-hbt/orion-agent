"""Guardian policy: deterministic rules over tool calls, in the core.

These rules need the conversation (the trust of the context a call would run in), so they live
where the conversation is. What must hold even if the core itself is compromised (secrets,
destinations, approvals checked with the owner) is enforced by the guardian process
(`orion.guardian.service`, D28). The rules do not depend on anything the model writes: only on
the tool's declared properties, the trust of the context it would run in, and the owner's
answers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

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
    ask_reason: str = ""
    """Why the owner is asked, when it is not just the tool's effect."""

    def describe(self) -> str:
        return f"{self.tool.name}({self.arguments.model_dump_json()})"


@dataclass(frozen=True)
class Approval:
    """The owner's answer. `grant` names the approval the guardian process recorded, which the
    approved action presents so the guardian can check it with the owner itself (D28)."""

    granted: bool
    grant: str | None = None

    def __bool__(self) -> bool:
        return self.granted


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    grant: str | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


class Approver(Protocol):
    async def approve(self, proposal: ActionProposal) -> Approval: ...


class DenyAllApprover:
    """Used when no human is reachable: anything needing approval is refused."""

    async def approve(self, proposal: ActionProposal) -> Approval:
        return Approval(False)


UNTRUSTED_REASON = (
    "la conversation contient du contenu venu d'ailleurs (mail, appel, invitation…) qui "
    "pourrait m'avoir influencé"
)


Autonomous = Callable[[str, dict[str, Any]], Awaitable[bool]]
"""Whether the owner let Orion do this action alone, with these parameters, within the limits
they set (`orion.guardian.autonomy`)."""


class Guardian:
    def __init__(
        self, approver: Approver | None = None, autonomous: Autonomous | None = None
    ) -> None:
        self._approver = approver or DenyAllApprover()
        self._autonomous = autonomous

    async def decide(self, proposal: ActionProposal) -> Decision:
        tool = proposal.tool
        if proposal.context_trust < TRUSTED_FLOOR and not tool.accepts_untrusted_context:
            # Content from elsewhere may have steered the model. Acting on the world from such a
            # context is refused outright: that is what an injected instruction would ask for,
            # and the owner must not get used to approving it. An internal write (memory,
            # reminders, playbook) is put to the owner instead (D26).
            if tool.effect is Effect.EXTERNAL:
                return Decision(
                    Verdict.DENY,
                    f"{tool.name} cannot run in a context containing untrusted content "
                    f"(trust {proposal.context_trust.name})",
                )
            approval = await self._approver.approve(replace(proposal, ask_reason=UNTRUSTED_REASON))
            if approval:
                return Decision(
                    Verdict.ALLOW, "approved by owner despite untrusted context", approval.grant
                )
            return Decision(
                Verdict.DENY,
                f"{tool.name} in a context containing untrusted content "
                f"(trust {proposal.context_trust.name}) needs the owner's approval, not given",
            )
        if tool.effect is Effect.NONE:
            return Decision(Verdict.ALLOW, "side-effect free")
        if tool.effect is Effect.INTERNAL:
            return Decision(Verdict.ALLOW, "internal effect only")
        if (
            proposal.context_trust >= TRUSTED_FLOOR
            and self._autonomous is not None
            and await self._autonomous(tool.name, proposal.arguments.model_dump())
        ):
            return Decision(Verdict.ALLOW, "the owner lets Orion do this alone")
        if proposal.context_trust < TRUSTED_FLOOR:
            # A tool built to act on outside content (answering a mail): the owner sees the
            # exact action, and why this one is asked with care.
            proposal = replace(proposal, ask_reason=UNTRUSTED_REASON)
        approval = await self._approver.approve(proposal)
        if approval:
            return Decision(Verdict.ALLOW, "approved by owner", approval.grant)
        return Decision(Verdict.DENY, "owner approval required and not given")
