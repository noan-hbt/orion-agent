"""What Orion has planned to do for the owner, in one place (D78).

A dozen modules schedule things on their own: reminders he asked for, follow-ups, advice to ask
about, departures to prepare, birthdays, the weekly page, curiosity, missions, papers about to
expire. Each says what it plans (`Module.planned`); this gathers them by day, so « qu'est-ce que
tu as prévu cette semaine ? » has an answer, and each line says how to call it off.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.initiative.timers import Timers
from orion.initiative.tools import REPEAT_FR
from orion.modules.base import Module, Planned
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MAX_LINES = 60


async def everything_planned(
    modules: list[Module], timers: Timers, now: datetime, until: datetime
) -> list[Planned]:
    await timers.sync()
    found = [
        Planned(
            t.due_at,
            f"te rappeler : {t.text}" + (f" ({REPEAT_FR[t.repeat]})" if t.repeat else ""),
            f"later (cancel, {t.id})",
        )
        for t in timers.projection.pending()
        if now <= t.due_at < until
    ]
    for module in modules:
        found += await module.planned(now, until)
    return sorted(found, key=lambda p: (p.at, p.what))


def render(planned: list[Planned], zone: ZoneInfo) -> str:
    if not planned:
        return "Rien de prévu sur cette période."
    lines: list[str] = []
    day = None
    for item in planned[:MAX_LINES]:
        at = item.at.astimezone(zone)
        if at.date() != day:
            day = at.date()
            lines.append(f"{_DAYS[at.weekday()].capitalize()} {at:%d/%m} :")
        undo = f"  [annuler : {item.undo}]" if item.undo else ""
        lines.append(f"- {at:%H:%M} {item.what}{undo}")
    if len(planned) > MAX_LINES:
        lines.append(f"(et {len(planned) - MAX_LINES} autres)")
    return "\n".join(lines)


class PlannedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=7, ge=1, le=60, description="How many days ahead")


def planned_tools(modules: list[Module], timers: Timers, timezone: str) -> list[Tool]:
    zone = ZoneInfo(timezone)

    async def show(args: Any, _ctx: ToolContext) -> ToolResult:
        now = datetime.now(zone)
        found = await everything_planned(modules, timers, now, now + timedelta(days=args.days))
        return ToolResult(render(found, zone))

    return [
        Tool(
            name="planned",
            description="Everything you will do for him on your own in the coming days "
            "(reminders, follow-ups, departures to prepare, birthdays, your weekly page, "
            "curiosity, missions…), with how to call each off. « Qu'est-ce que tu as prévu ? »",
            input_model=PlannedInput,
            handler=show,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        )
    ]
