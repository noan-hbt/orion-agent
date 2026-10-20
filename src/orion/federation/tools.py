"""Tools for meetings with other people's Orion."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Trust
from orion.federation.meetings import MeetingDesk
from orion.guardian.client import GuardianUnavailableError
from orion.initiative.tools import describe_moment, parse_moment
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


class PeersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeetingProposeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peer: str = Field(description="Whose Orion: the name the owner paired it under (see peers)")
    title: str = Field(min_length=1, max_length=80, description="Short, e.g. 'Déjeuner'")
    duration_minutes: int = Field(ge=15, le=480)
    window_start: str = Field(description="ISO date-time: earliest start, local time")
    window_end: str = Field(description="ISO date-time: latest end, local time")


class MeetingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def federation_tools(desk: MeetingDesk) -> list[Tool]:
    async def peers(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            found = await desk.peers()
        except GuardianUnavailableError as exc:
            return ToolResult(f"The guardian did not answer: {exc}", is_error=True)
        if not found:
            return ToolResult(
                "Aucun Orion relié. Pour en relier un : `orion federation invite --name <nom>` "
                "sur le serveur, puis l'autre accepte le code."
            )
        return ToolResult("\n".join(f"- {p['label']} ({p['instance']})" for p in found))

    async def meeting_propose(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            start = parse_moment(args.window_start, ctx.timezone)
            end = parse_moment(args.window_end, ctx.timezone)
        except ValueError:
            return ToolResult("window_start and window_end must be ISO date-times", is_error=True)
        if end <= start:
            return ToolResult("The window ends before it starts.", is_error=True)
        if end - start > timedelta(days=31):
            return ToolResult("Keep the window within a month.", is_error=True)
        try:
            meeting_id, slots = await desk.propose(
                args.peer,
                args.title,
                timedelta(minutes=args.duration_minutes),
                start,
                end,
                ctx.grant,
                ctx.correlation_id,
            )
        except LookupError as exc:
            return ToolResult(f"Not sent: {exc}.", is_error=True)
        except GuardianUnavailableError as exc:
            return ToolResult(f"Not sent, the guardian refused: {exc}", is_error=True)
        offered = ", ".join(describe_moment(s, ctx.timezone) for s in slots)
        return ToolResult(
            f"Proposition envoyée ({meeting_id}) avec {len(slots)} créneau(x) libre(s) : "
            f"{offered}. La réponse arrivera d'elle-même.",
            data={"meeting_id": meeting_id},
        )

    async def meetings(args: Any, ctx: ToolContext) -> ToolResult:
        await desk.start()
        known = sorted(desk.state.meetings.values(), key=lambda m: m.id)
        if not known:
            return ToolResult("Aucun rendez-vous négocié avec un autre Orion.")
        lines: list[str] = []
        for m in known:
            when = f", {describe_moment(m.slot, ctx.timezone)}" if m.slot else ""
            side = "proposé par toi" if m.role == "asked" else "proposé par lui"
            lines.append(f"- « {m.title} » avec {m.label} ({side}) : {m.status}{when}")
        return ToolResult("\n".join(lines), trust=Trust.TRUSTED_PEER)

    return [
        Tool(
            name="peers",
            description="The other people's Orions this one is paired with.",
            input_model=PeersInput,
            handler=peers,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="meeting_propose",
            description="Arrange a meeting with someone through their Orion: offers the owner's "
            "free slots in the window (nothing else of their calendar), the other Orion picks "
            "one with its owner's agreement. Needs the owner's approval. The outcome is "
            "reported when it lands.",
            input_model=MeetingProposeInput,
            handler=meeting_propose,
            effect=Effect.EXTERNAL,
            reversibility=Reversibility.IRREVERSIBLE,
        ),
        Tool(
            name="meetings",
            description="Meetings negotiated with other Orions, and where each stands.",
            input_model=MeetingsInput,
            handler=meetings,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]
