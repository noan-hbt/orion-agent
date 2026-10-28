"""Built-in tools. Side-effect free.

The current time is a tool, not part of the system prompt: the prompt stays byte-stable for the
cache, and the model never has to guess the date. Date arithmetic is left to the model; tools
that take a date (`remind_me`) echo the exact weekday back so a mistake shows.

During a long turn (D97), Orion keeps a plan (`plan`: every step with its status, the whole
list each time, so the plan lives in the conversation and nowhere else). It is his own: the
owner hears what he finds, never the plan (D169). What he finds on the way goes out with `say`
or as text beside a tool call (D173).
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


class SayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000, description="The message, as you'd text it")


async def _say(_args: Any, _ctx: ToolContext) -> ToolResult:
    # The runtime sends the text as soon as the call is made (D173): nothing left to do here.
    return ToolResult("Envoyé. Continue.")


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=2, max_length=160, description="The step, a few words")
    status: Literal["todo", "doing", "done"] = "todo"


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[Step] = Field(
        min_length=1, max_length=12, description="Every step, in order, each time"
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
    return ToolResult(status)


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
            name="say",
            description="Send him a message now and keep working: what you just found or did "
            "that matters to him, never what you are about to do. Call it with your next tool, "
            "not alone. Your last answer is your reply, not a `say`.",
            input_model=SayInput,
            handler=_say,
            **common,
        ),
        Tool(
            name="plan",
            description="Your plan, only for work that is long, ambiguous or costly, where "
            "you could lose track: every step with its status (todo, doing, done), the whole "
            "list each time you move on, one `doing` at a time. He does not see it. "
            "Never for one action or a few obvious steps: do them, without announcing them.",
            input_model=PlanInput,
            handler=_plan,
            **common,
        ),
    ]
