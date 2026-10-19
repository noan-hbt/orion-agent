"""Phone tools. Calling the owner is Orion's own business; calling anyone else is an external
action that needs the owner's approval, every time."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


class Phone(Protocol):
    async def call_owner(self, reason: str, correlation_id: str | None = None) -> str: ...

    async def call_someone(
        self,
        number: str,
        name: str,
        goal: str,
        correlation_id: str | None = None,
        grant: str | None = None,
    ) -> str: ...


class PhoneLink:
    """The phone exists only while `orion serve` runs; the tools are registered regardless,
    so the tool list (and the cached prompt prefix) is the same in every mode."""

    def __init__(self) -> None:
        self.target: Phone | None = None


class CallMeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(description="What the call is about; the voice opens with it")


class CallSomeoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str = Field(pattern=r"^\+[1-9]\d{7,14}$", description="E.164, e.g. +33612345678")
    name: str = Field(description="Who is called (person or business)")
    goal: str = Field(
        description="What the call must achieve, with every detail the voice needs (dates, "
        "names, limits), and nothing private beyond that"
    )


def voice_tools(link: PhoneLink) -> list[Tool]:
    async def call_me(args: Any, ctx: ToolContext) -> ToolResult:
        if link.target is None:
            return ToolResult("Phone is not available right now.", is_error=True)
        call_id = await link.target.call_owner(args.reason, ctx.correlation_id)
        return ToolResult(f"Appel en cours ({call_id}).", data={"call_id": call_id})

    async def call_someone(args: Any, ctx: ToolContext) -> ToolResult:
        if link.target is None:
            return ToolResult("Phone is not available right now.", is_error=True)
        call_id = await link.target.call_someone(
            args.number, args.name, args.goal, ctx.correlation_id, ctx.grant
        )
        return ToolResult(
            f"Appel à {args.name} lancé ({call_id}). Le compte rendu arrivera à la fin.",
            data={"call_id": call_id},
        )

    return [
        Tool(
            name="call_me",
            description="Call the owner now by phone (your voice handles the call). For "
            "something urgent, or when they ask for a call.",
            input_model=CallMeInput,
            handler=call_me,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.IRREVERSIBLE,
        ),
        Tool(
            name="call_someone",
            description="Have your voice call a person or business for the owner (book, ask, "
            "confirm). Needs the owner's approval. The outcome is reported when the call ends.",
            input_model=CallSomeoneInput,
            handler=call_someone,
            effect=Effect.EXTERNAL,
            reversibility=Reversibility.IRREVERSIBLE,
        ),
    ]
