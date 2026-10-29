"""The `agenda` tool: what is on the owner's calendars, day by day."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.calendar.service import Calendar, describe_day
from orion.calendar.write import CalendarChange, CalendarWriteError, NewCalendarEvent
from orion.core.events import Trust
from orion.guardian.client import GuardianUnavailableError
from orion.memory.model import CommitmentStatus
from orion.memory.service import MemoryService, Origin
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
    closes: list[str] = Field(
        default_factory=list[str],
        description="Commitment ids (c_…) this event fulfils (e.g. 'book the dentist'): "
        "closed with the event as proof",
    )


class CalendarChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="The event's title, exactly as `agenda` shows it")
    start: datetime = Field(description="Its current start, local time, ISO")
    new_title: str | None = Field(default=None, min_length=1, max_length=120)
    new_start: datetime | None = Field(
        default=None, description="Local time, ISO; without `new_end` it keeps its length"
    )
    new_end: datetime | None = Field(default=None, description="Local time, ISO")
    new_location: str | None = Field(default=None, max_length=200, description="'' clears it")
    new_notes: str | None = Field(default=None, max_length=1000, description="'' clears them")
    cancel: bool = Field(default=False, description="Delete the event instead")


def local(moment: datetime, timezone: str) -> datetime:
    """A time the model gave without an offset is the owner's local time."""
    return moment.replace(tzinfo=ZoneInfo(timezone)) if moment.tzinfo is None else moment


def calendar_tools(calendar: Calendar, memory: MemoryService | None = None) -> list[Tool]:
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
            account, occurrence = await calendar.add(event, ctx.grant, ctx.correlation_id)
        except (CalendarWriteError, GuardianUnavailableError) as exc:
            return ToolResult(f"Not added: {exc}", is_error=True)
        if memory is not None:
            for commitment_id in args.closes:
                await memory.close_commitment(
                    commitment_id,
                    CommitmentStatus.DONE,
                    Origin(
                        actor="orion:core",
                        source=f"calendar:{account}",
                        trust=Trust.SYSTEM,
                        correlation_id=ctx.correlation_id,
                    ),
                    evidence=f"événement ajouté : {occurrence.title}, {start:%d/%m %H:%M}",
                )
        return ToolResult(f"Ajouté à l'agenda « {account} » : {args.title}, {start:%d/%m %H:%M}.")

    async def calendar_change(args: Any, ctx: ToolContext) -> ToolResult:
        def at(moment: datetime | None) -> datetime | None:
            return None if moment is None else local(moment, ctx.timezone)

        edits = (args.new_title, args.new_start, args.new_end, args.new_location, args.new_notes)
        if args.cancel == any(edit is not None for edit in edits):
            return ToolResult("Either cancel the event or give what changes.", is_error=True)
        start = local(args.start, ctx.timezone)
        await calendar.ensure_fresh()
        try:
            account, occurrence = calendar.find(args.title, start)
            change = CalendarChange(
                uid=occurrence.key.rsplit("@", 1)[0],
                title=args.title,
                start=start,
                new_title=args.new_title,
                new_start=at(args.new_start),
                new_end=at(args.new_end),
                new_location=args.new_location,
                new_notes=args.new_notes,
                cancel=args.cancel,
            )
            change.times(occurrence.start, occurrence.end)
            now = await calendar.change(account, occurrence, change, ctx.grant, ctx.correlation_id)
        except (CalendarWriteError, GuardianUnavailableError) as exc:
            return ToolResult(f"Not changed: {exc}", is_error=True)
        if now is None:
            return ToolResult(f"Annulé : {args.title}, {start:%d/%m %H:%M}.")
        return ToolResult(f"Modifié : {now.title}, {now.start:%d/%m %H:%M}-{now.end:%H:%M}.")

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
        tools.append(
            Tool(
                name="calendar_change",
                description="Move, rename, edit or cancel one of the owner's own events (not "
                "an invitation, not a recurring one, none with guests). The owner approves it "
                "first.",
                input_model=CalendarChangeInput,
                handler=calendar_change,
                effect=Effect.EXTERNAL,
                accepts_untrusted_context=True,
            )
        )
    return tools
