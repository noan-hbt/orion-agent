"""Memory tools exposed to the agent.

Writes are INTERNAL effects that refuse untrusted context: content Orion read from the web or a
third party can never be turned into a memory without the owner in the loop. Reads are
side-effect free and allowed everywhere.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.memory.model import Category, CommitmentStatus
from orion.memory.render import format_commitment, format_fact
from orion.memory.service import MemoryService, Origin
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


def origin_of(ctx: ToolContext) -> Origin:
    return Origin(
        actor="orion:core",
        source=f"thread:{ctx.thread_id}",
        trust=ctx.context_trust,
        derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        correlation_id=ctx.correlation_id,
    )


class RememberInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(
        description="One self-contained fact, in the owner's language, e.g. 'Noan est allergique "
        "aux noix de cajou.' Write it so it still makes sense months from now."
    )
    about: list[str] = Field(default_factory=list[str], description="People or things it concerns")
    category: Category = Category.OTHER
    valid_from: str | None = Field(default=None, description="ISO date it became true, if known")
    replaces: list[str] = Field(
        default_factory=list[str],
        description="Ids of facts this one corrects or updates (they are kept as history)",
    )


class ForgetFactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    reason: str = Field(description="Why it is no longer true or was wrong")
    valid_to: str | None = Field(default=None, description="ISO date it stopped being true")


class RecallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    include_history: bool = Field(default=False, description="Also return facts no longer true")


class CommitmentAddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(description="What is owed, concretely, e.g. 'Envoyer le devis à Paul'")
    debtor: str = Field(description="Who owes it: 'owner', 'orion', or a person's name")
    creditor: str = Field(description="Who it is owed to: 'owner', 'orion', or a person's name")
    due: str | None = Field(default=None, description="ISO date or datetime, if there is one")


class CommitmentCloseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: str
    status: CommitmentStatus = CommitmentStatus.DONE
    evidence: str | None = Field(default=None, description="How we know it is done")


class CommitmentListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def memory_tools(memory: MemoryService) -> list[Tool]:
    async def remember(args: Any, ctx: ToolContext) -> ToolResult:
        fact_id = await memory.remember(
            args.statement,
            origin_of(ctx),
            about=args.about,
            category=args.category,
            valid_from=args.valid_from,
            supersedes=args.replaces,
        )
        return ToolResult(f"Retenu ({fact_id}).", data={"fact_id": fact_id})

    async def forget_fact(args: Any, ctx: ToolContext) -> ToolResult:
        if await memory.store.fact(args.fact_id) is None:
            return ToolResult(f"No fact with id {args.fact_id}", is_error=True)
        await memory.invalidate(args.fact_id, args.reason, origin_of(ctx), args.valid_to)
        return ToolResult(f"Fait {args.fact_id} marqué comme plus vrai (gardé en historique).")

    async def recall(args: Any, ctx: ToolContext) -> ToolResult:
        facts = await memory.search(args.query, limit=10, include_past=args.include_history)
        if not facts:
            return ToolResult("Rien en mémoire sur ce sujet.")
        return ToolResult("\n".join(format_fact(f) for f in facts))

    async def commitment_add(args: Any, ctx: ToolContext) -> ToolResult:
        cid = await memory.add_commitment(
            args.what, origin_of(ctx), debtor=args.debtor, creditor=args.creditor, due=args.due
        )
        return ToolResult(f"Engagement noté ({cid}).", data={"commitment_id": cid})

    async def commitment_close(args: Any, ctx: ToolContext) -> ToolResult:
        existing = await memory.store.commitment(args.commitment_id)
        if existing is None:
            return ToolResult(f"No commitment with id {args.commitment_id}", is_error=True)
        if existing.status is not CommitmentStatus.OPEN:
            return ToolResult(f"Already {existing.status.value}.", is_error=True)
        await memory.close_commitment(
            args.commitment_id, args.status, origin_of(ctx), args.evidence
        )
        return ToolResult(f"Engagement {args.commitment_id} : {args.status.value}.")

    async def commitments_list(args: Any, ctx: ToolContext) -> ToolResult:
        open_items = await memory.store.commitments()
        if not open_items:
            return ToolResult("Aucun engagement en cours.")
        return ToolResult("\n".join(format_commitment(c) for c in open_items))

    write: dict[str, Any] = {
        "effect": Effect.INTERNAL,
        "reversibility": Reversibility.REVERSIBLE,
        "accepts_untrusted_context": False,
    }
    read: dict[str, Any] = {
        "effect": Effect.NONE,
        "reversibility": Reversibility.REVERSIBLE,
        "accepts_untrusted_context": True,
        "concurrency_safe": True,
    }
    return [
        Tool(
            name="remember",
            description="Save a durable fact about the owner, someone in their life, or a project. "
            "Use it when you learn something worth knowing in a month. To correct a fact, pass "
            "its id in `replaces`. Never claim you remembered something without calling this.",
            input_model=RememberInput,
            handler=remember,
            **write,
        ),
        Tool(
            name="forget_fact",
            description="Mark a remembered fact as no longer true or wrong. It is kept as history.",
            input_model=ForgetFactInput,
            handler=forget_fact,
            **write,
        ),
        Tool(
            name="recall",
            description="Search memory for facts about a person, topic or project.",
            input_model=RecallInput,
            handler=recall,
            **read,
        ),
        Tool(
            name="commitment_add",
            description="Record a promise: something the owner owes someone, someone owes the "
            "owner, or you (Orion) promised to do. Include a due date when there is one.",
            input_model=CommitmentAddInput,
            handler=commitment_add,
            **write,
        ),
        Tool(
            name="commitment_close",
            description="Mark a commitment as done or cancelled.",
            input_model=CommitmentCloseInput,
            handler=commitment_close,
            **write,
        ),
        Tool(
            name="commitments_list",
            description="List open commitments, earliest due first.",
            input_model=CommitmentListInput,
            handler=commitments_list,
            **read,
        ),
    ]
