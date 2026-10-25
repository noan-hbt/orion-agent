"""Built-in tools. Side-effect free.

The current time is a tool, not part of the system prompt: the prompt stays byte-stable for the
cache, and the model never has to guess the date. Date arithmetic is left to the model; tools
that take a date (`remind_me`) echo the exact weekday back so a mistake shows.

During a long turn (D97), Orion keeps a plan (`plan`: every step with its status, the whole
list each time, so the plan lives in the conversation and nowhere else). Shown, the plan is
handed to the runtime through `TELL`, which delivers it now, on the channel the owner wrote
from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from orion.context.compiler import night_hint
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

_WEEKDAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class CurrentTimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = Field(
        default=None, description="IANA timezone, e.g. 'Europe/Paris'. Defaults to the owner's."
    )


async def _current_time(args: Any, ctx: ToolContext) -> ToolResult:
    tz_name = args.timezone or ctx.timezone
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ToolResult(f"Unknown timezone: {tz_name}", is_error=True)
    now = datetime.now(tz)
    weekday = _WEEKDAYS_FR[now.weekday()]
    return ToolResult(
        f"{weekday} {now:%Y-%m-%d %H:%M} ({tz_name}, UTC{now:%z}){night_hint(now)}",
        data={"iso": now.isoformat(), "weekday": weekday, "timezone": tz_name},
    )


TELL = "tell"
"""A tool result whose data holds text under this key: the runtime sends it to the owner now,
mid-turn (nothing is sent when no one is waiting, e.g. an initiative turn)."""


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=2, max_length=160, description="The step, a few words")
    status: Literal["todo", "doing", "done"] = "todo"


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[Step] = Field(
        min_length=1, max_length=12, description="Every step, in order, each time"
    )
    show: bool = Field(
        default=False, description="Send him the plan now (when starting a long task)"
    )


_MARK = {"todo": "·", "doing": "→", "done": "✓"}


def render_plan(steps: list[Step]) -> str:
    return "\n".join(f"{_MARK[s.status]} {s.text}" for s in steps)


async def _plan(args: Any, _ctx: ToolContext) -> ToolResult:
    steps: list[Step] = list(args.steps)
    if sum(s.status == "doing" for s in steps) > 1:
        return ToolResult("One step at a time: only one may be `doing`.", is_error=True)
    done = sum(s.status == "done" for s in steps)
    left = [s.text for s in steps if s.status != "done"]
    shown = render_plan(steps)
    status = f"Plan : {done}/{len(steps)} fait.\n{shown}" + (
        f"\nEnsuite : {left[0]}." if left else "\nTout est fait : réponds-lui."
    )
    data = {TELL: f"Je m'y mets :\n{shown}"} if args.show else {}
    return ToolResult(status, data=data)


def builtin_tools() -> list[Tool]:
    common: dict[str, Any] = {
        "effect": Effect.NONE,
        "reversibility": Reversibility.REVERSIBLE,
        "concurrency_safe": True,
        "accepts_untrusted_context": True,
    }
    return [
        Tool(
            name="current_time",
            description="Current date, weekday and time. Use it instead of guessing the date.",
            input_model=CurrentTimeInput,
            handler=_current_time,
            **common,
        ),
        Tool(
            name="plan",
            description="Your plan, only for work that is long, ambiguous or costly, where "
            "you could lose track: every step with its status (todo, doing, done), the whole "
            "list each time you move on, one `doing` at a time. `show` sends him the plan when "
            "you start. Never for one action or a few obvious steps: do them, without "
            "announcing them.",
            input_model=PlanInput,
            handler=_plan,
            **common,
        ),
    ]
