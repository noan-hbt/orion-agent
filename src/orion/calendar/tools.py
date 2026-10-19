"""The `agenda` tool: what is on the owner's calendars, day by day."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.calendar.service import Calendar, describe_day
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


class AgendaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str | None = Field(
        default=None, description="First day, ISO date (e.g. '2026-10-23'). Default: today"
    )
    days: int = Field(default=1, ge=1, le=31)


def calendar_tools(calendar: Calendar) -> list[Tool]:
    async def agenda(args: Any, ctx: ToolContext) -> ToolResult:
        zone = ZoneInfo(ctx.timezone)
        try:
            first = date.fromisoformat(args.start) if args.start else datetime.now(zone).date()
        except ValueError:
            return ToolResult(f"Not an ISO date: {args.start!r}", is_error=True)
        await calendar.ensure_fresh()
        if not calendar.state.synced:
            return ToolResult("No calendar could be read yet.", is_error=True)
        lines: list[str] = []
        for offset in range(args.days):
            day = first + timedelta(days=offset)
            events = calendar.day(day)
            lines.append(f"{describe_day(day)} :")
            lines += [f"- {e}" for e in events] or ["- rien"]
        start = datetime.combine(first, datetime.min.time(), zone)
        waiting = calendar.pending(start, start + timedelta(days=args.days))
        if waiting:
            lines.append(f"({waiting} invitation(s) sans réponse sur la période, non affichées)")
        return ToolResult("\n".join(lines))

    return [
        Tool(
            name="agenda",
            description="The owner's calendar events, day by day (times are local). Use it "
            "before saying anything about their schedule.",
            input_model=AgendaInput,
            handler=agenda,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        )
    ]
