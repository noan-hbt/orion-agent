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
from typing import Any, Literal, Protocol

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
    reached: bool = True
    """False when nobody could be asked (no channel up, the question never left): a no that
    is not his, which an answer to someone else must not pass for a refusal (D98)."""

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


SOURCES_FOR_OWNER = {
    Trust.UNKNOWN: "du contenu d'origine inconnue",
    Trust.WEB: "une page web ou un résultat de recherche",
    Trust.THIRD_PARTY: "un mail, un appel ou le message de quelqu'un d'autre",
    Trust.TRUSTED_PEER: "un message d'un autre Orion",
    Trust.OWNER_INSTANCE: "un fichier ou un document lu pour toi",
}
"""What lowered a context to this trust, in the question the owner reads."""

ALONE = "the owner lets Orion do this alone"
"""The reason given when an autonomy the owner granted covers the call."""

NEVER_FROM_UNTRUSTED = frozenset({"autonomy_grant", "module_install", "module_remove"})
"""Never done from a context holding outside content, even with his yes: letting Orion act
alone, or adding code to it (D105), is decided in a clean conversation."""
NEVER_ALONE = frozenset({"module_install", "module_remove"})
"""Always put to him, whatever autonomy the config lists: code that runs with his memory."""


def untrusted_reason(trust: Trust) -> str:
    """Why the owner is asked: what is in the conversation, and what to check (D90)."""
    source = SOURCES_FOR_OWNER.get(trust, "du contenu venu d'ailleurs")
    return (
        f"la conversation contient {source}, qui pourrait m'avoir influencé : vérifie que "
        "c'est bien ce que tu veux"
    )


UNTRUSTED_SOURCES = {
    Trust.UNKNOWN: "content of unknown origin",
    Trust.WEB: "a web page or a search result",
    Trust.THIRD_PARTY: "a mail, a call or someone else's message",
    Trust.TRUSTED_PEER: "another Orion",
    Trust.OWNER_INSTANCE: "a file or a document read for him",
}
"""What lowered a context to this trust, in words the model can repeat to the owner."""


Approvals = Literal["prudent", "equilibre"]
"""What the owner is asked in a context holding outside content (`agent.approvals`, D85)."""

Autonomous = Callable[[str, dict[str, Any]], Awaitable[bool]]
"""Whether the owner let Orion do this action alone, with these parameters, within the limits
they set (`orion.guardian.autonomy`)."""


class Guardian:
    def __init__(
        self,
        approver: Approver | None = None,
        autonomous: Autonomous | None = None,
        approvals: Approvals = "prudent",
        fresh_after_minutes: int = 0,
    ) -> None:
        self._approver = approver or DenyAllApprover()
        self._autonomous = autonomous
        self._approvals: Approvals = approvals
        self._fresh_after = fresh_after_minutes
        """`agent.untrusted_idle_minutes`: when the conversation starts afresh (D26)."""

    def _untrusted_refusal(self, tool: Tool, trust: Trust) -> str:
        """Why, and until when: the model tells the owner, so it must not have to guess."""
        source = UNTRUSTED_SOURCES.get(trust, "outside content")
        until = (
            f"It becomes possible again once the conversation starts afresh, after "
            f"{self._fresh_after} minutes without any message (each message pushes that back)"
            if self._fresh_after
            else "It stays impossible in this conversation"
        )
        return (
            f"{tool.name} cannot run in a context containing untrusted content "
            f"(trust {trust.name}: {source} is in this conversation). Anything that acts on "
            "the world or on your settings is never done from such a context, even when he "
            f"asked: the outside content could have steered you. {until}; or he does it "
            "himself. Tell him exactly this, without promising to retry sooner."
        )

    async def decide(self, proposal: ActionProposal) -> Decision:
        tool = proposal.tool
        effect = tool.effect_for(proposal.arguments)
        if proposal.context_trust < TRUSTED_FLOOR and not tool.accepts_untrusted_for(
            proposal.arguments
        ):
            # Content from elsewhere may have steered the model. An internal write (memory,
            # reminders, playbook) is put to the owner (D26), unless he chose not to be asked
            # for those (D85): what it writes keeps the context's trust anyway. Acting on the
            # world is put to him with the exact action and why he is asked (D90), never done
            # alone; `prudent` refuses it outright, as D26 did.
            reason = untrusted_reason(proposal.context_trust)
            if proposal.ask_reason:  # what happened in a conversation he did not see, first
                reason = f"{proposal.ask_reason}, et {reason}"
            if effect is Effect.EXTERNAL:
                if self._approvals == "prudent" or tool.name in NEVER_FROM_UNTRUSTED:
                    return Decision(
                        Verdict.DENY, self._untrusted_refusal(tool, proposal.context_trust)
                    )
                approval = await self._approver.approve(replace(proposal, ask_reason=reason))
                if approval:
                    return Decision(
                        Verdict.ALLOW, "approved by owner despite untrusted context", approval.grant
                    )
                return Decision(
                    Verdict.DENY,
                    f"{tool.name} in a context containing untrusted content "
                    f"(trust {proposal.context_trust.name}) was put to the owner, who did not "
                    "say yes",
                )
            if self._approvals == "equilibre":
                return Decision(
                    Verdict.ALLOW,
                    f"{effect.value} effect in an untrusted context ({self._approvals} mode)",
                )
            approval = await self._approver.approve(replace(proposal, ask_reason=reason))
            if approval:
                return Decision(
                    Verdict.ALLOW, "approved by owner despite untrusted context", approval.grant
                )
            return Decision(
                Verdict.DENY,
                f"{tool.name} in a context containing untrusted content "
                f"(trust {proposal.context_trust.name}) needs the owner's approval, not given",
            )
        if effect is Effect.NONE:
            return Decision(Verdict.ALLOW, "side-effect free")
        if effect is Effect.INTERNAL:
            return Decision(Verdict.ALLOW, "internal effect only")
        if (
            proposal.context_trust >= TRUSTED_FLOOR
            and self._autonomous is not None
            and tool.name not in NEVER_ALONE
            and await self._autonomous(tool.name, proposal.arguments.model_dump())
        ):
            return Decision(Verdict.ALLOW, ALONE)
        if proposal.context_trust < TRUSTED_FLOOR:
            # A tool built to act on outside content (answering a mail): the owner sees the
            # exact action, and why this one is asked with care.
            proposal = replace(proposal, ask_reason=untrusted_reason(proposal.context_trust))
        approval = await self._approver.approve(proposal)
        if approval:
            return Decision(Verdict.ALLOW, "approved by owner", approval.grant)
        return Decision(Verdict.DENY, "owner approval required and not given")
