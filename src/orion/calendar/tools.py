"""The `agenda` tool: what is on the owner's calendars, day by day."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.calendar.service import Calendar, describe_day
from orion.calendar.write import CalendarWriteError, NewCalendarEvent
from orion.guardian.client import GuardianUnavailableError
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


class AgendaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str | None = Field(
        default=None, description="First day, ISO date (e.g. '2026-10-23'). Default: today"
    )
    days: int = Field(default=1, ge=1, le=31)


class CalendarAddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    start: datetime = Field(description="Local time, ISO, e.g. '2026-10-23T14:00'")
    end: datetime = Field(description="Local time, ISO")
    location: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=1000)


def local(moment: datetime, timezone: str) -> datetime:
    """A time the model gave without an offset is the owner's local time."""
    return moment.replace(tzinfo=ZoneInfo(timezone)) if moment.tzinfo is None else moment


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

    async def calendar_add(args: Any, ctx: ToolContext) -> ToolResult:
        start, end = local(args.start, ctx.timezone), local(args.end, ctx.timezone)
        if end <= start:
            return ToolResult("The end must come after the start.", is_error=True)
        event = NewCalendarEvent(
            title=args.title, start=start, end=end, location=args.location, notes=args.notes
        )
        try:
            account, _ = await calendar.add(event, ctx.grant, ctx.correlation_id)
        except (CalendarWriteError, GuardianUnavailableError) as exc:
            return ToolResult(f"Not added: {exc}", is_error=True)
        return ToolResult(f"Ajouté à l'agenda « {account} » : {args.title}, {start:%d/%m %H:%M}.")

    tools = [
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
    if calendar.settings.writable() is not None:
        tools.append(
            Tool(
                name="calendar_add",
                description="Add an event to the owner's calendar (no attendees: nobody is "
                "invited). The owner approves it first.",
                input_model=CalendarAddInput,
                handler=calendar_add,
                effect=Effect.EXTERNAL,
                reversibility=Reversibility.UNDO_WINDOW,
                accepts_untrusted_context=True,
            )
        )
    return tools
