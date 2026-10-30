"""Reminder tools: the only way Orion can promise to come back to the owner later.

The tool computes and echoes the exact local date and weekday itself, so the model never has
to work out "mardi prochain" on its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Provenance
from orion.initiative.timers import Repeat, Timer, Timers
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

_WEEKDAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
REMINDER_ACTIONS = {"reminders_list": "list", "reminder_move": "move", "reminder_cancel": "cancel"}
"""The reminder tools grouped as `reminders` actions; `remind_me` stays apart."""
_GRACE = timedelta(minutes=1)
_HORIZON = timedelta(days=3 * 366)
REPEAT_FR = {
    "daily": "chaque jour",
    "weekdays": "chaque jour de semaine",
    "weekly": "chaque semaine",
    "monthly": "chaque mois",
    "yearly": "chaque année",
}


def describe_moment(moment: datetime, timezone: str) -> str:
    local = moment.astimezone(ZoneInfo(timezone))
    return f"{_WEEKDAYS_FR[local.weekday()]} {local:%Y-%m-%d} à {local:%H:%M}"


def parse_moment(value: str, timezone: str) -> datetime:
    """ISO date-time; a naive value is read in the owner's timezone."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(timezone))
    return moment


class RemindMeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: str = Field(
        description="ISO date-time of the reminder, e.g. '2026-10-23T09:00'. Local time unless "
        "an offset is given."
    )
    what: str = Field(description="What to remind the owner of, self-contained")
    by_call: bool = Field(
        default=False, description="Call the owner instead of messaging (e.g. a wake-up call)"
    )
    repeat: Repeat = Field(
        default="", description="Again at the same time each day, weekday, week, month or year"
    )
    own: bool = Field(
        default=False,
        description="A note to yourself (check something, pick work up): it wakes you, and "
        "you tell him only if it matters",
    )


class ReminderMoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reminder_id: str
    when: str = Field(description="New ISO date-time, local unless an offset is given")


class ReminderCancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reminder_id: str
    reason: str = "owner request"


class RemindersListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _line(timer: Timer, timezone: str) -> str:
    pace = f" ({REPEAT_FR[timer.repeat]})" if timer.repeat else ""
    return f"({timer.id}) {describe_moment(timer.due_at, timezone)}{pace} : {timer.text}"


def reminder_tools(timers: Timers, clock: Any = None, calls: bool = False) -> list[Tool]:
    """`clock` returns the current aware datetime; injectable for tests. `calls` says whether
    the phone is on (`[voice]`): the schema stays the same either way, for the prompt cache."""

    def now(timezone: str) -> datetime:
        return clock() if clock is not None else datetime.now(ZoneInfo(timezone))

    def check(when: str, timezone: str) -> datetime | ToolResult:
        try:
            due = parse_moment(when, timezone)
        except ValueError:
            return ToolResult(f"Not an ISO date-time: {when!r}", is_error=True)
        current = now(timezone)
        if due < current - _GRACE:
            return ToolResult(
                f"{describe_moment(due, timezone)} is in the past "
                f"(now: {describe_moment(current, timezone)}).",
                is_error=True,
            )
        if due > current + _HORIZON:
            return ToolResult("Reminders are limited to three years ahead.", is_error=True)
        return due

    async def remind_me(args: Any, ctx: ToolContext) -> ToolResult:
        if args.by_call and not calls:
            return ToolResult(
                "Calls are off: the owner has not enabled [voice] in orion.toml. Say so, and "
                "offer a message reminder instead.",
                is_error=True,
            )
        due = check(args.when, ctx.timezone)
        if isinstance(due, ToolResult):
            return due
        if args.repeat == "weekdays" and due.astimezone(ZoneInfo(ctx.timezone)).weekday() >= 5:
            return ToolResult(
                f"{describe_moment(due, ctx.timezone)} is a weekend day: start the weekday "
                "series on a weekday.",
                is_error=True,
            )
        timer_id = await timers.schedule(
            due,
            args.what,
            ctx.thread_id,
            Provenance(
                source=f"thread:{ctx.thread_id}",
                actor="orion:core",
                derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
            ),
            ctx.context_trust,
            correlation_id=ctx.correlation_id,
            causation_id=ctx.origin_event_id,
            by_call=args.by_call,
            repeat=args.repeat,
            timezone=ctx.timezone,
            own=args.own,
        )
        how = " (par appel)" if args.by_call else " (pour toi)" if args.own else ""
        pace = f", puis {REPEAT_FR[args.repeat]}" if args.repeat else ""
        return ToolResult(
            f"Rappel programmé ({timer_id}) pour {describe_moment(due, ctx.timezone)}{pace}{how}.",
            data={"reminder_id": timer_id, "due_at": due.isoformat()},
        )

    async def reminder_move(args: Any, ctx: ToolContext) -> ToolResult:
        await timers.sync()
        if timers.projection.get(args.reminder_id) is None:
            return ToolResult(f"No pending reminder {args.reminder_id}", is_error=True)
        due = check(args.when, ctx.timezone)
        if isinstance(due, ToolResult):
            return due
        timer_id = await timers.move(
            args.reminder_id,
            due,
            Provenance(source=f"thread:{ctx.thread_id}", actor="orion:core"),
            ctx.timezone,
        )
        return ToolResult(
            f"Rappel déplacé ({timer_id}) à {describe_moment(due, ctx.timezone)}.",
            data={"reminder_id": timer_id, "due_at": due.isoformat()},
        )

    async def reminders_list(args: Any, ctx: ToolContext) -> ToolResult:
        await timers.sync()
        pending = timers.projection.pending()
        if not pending:
            return ToolResult("Aucun rappel programmé.")
        return ToolResult("\n".join(_line(t, ctx.timezone) for t in pending))

    async def reminder_cancel(args: Any, ctx: ToolContext) -> ToolResult:
        await timers.sync()
        if timers.projection.get(args.reminder_id) is None:
            return ToolResult(f"No pending reminder {args.reminder_id}", is_error=True)
        await timers.cancel(
            args.reminder_id,
            args.reason,
            Provenance(source=f"thread:{ctx.thread_id}", actor="orion:core"),
        )
        return ToolResult(f"Rappel {args.reminder_id} annulé.")

    return [
        Tool(
            name="remind_me",
            description="Schedule a message to the owner at a precise moment. This is the only "
            "way to follow up later: never promise to remind or come back without calling it.",
            input_model=RemindMeInput,
            handler=remind_me,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="reminder_cancel",
            description="Cancel a scheduled reminder.",
            input_model=ReminderCancelInput,
            handler=reminder_cancel,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="reminder_move",
            description="Move a scheduled reminder to another moment (« plus tard », snooze).",
            input_model=ReminderMoveInput,
            handler=reminder_move,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="reminders_list",
            description="List scheduled reminders, soonest first.",
            input_model=RemindersListInput,
            handler=reminders_list,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]
