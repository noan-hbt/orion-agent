"""Built-in tools. Side-effect free.

The current time is a tool, not part of the system prompt: the prompt stays byte-stable for the
cache, and the model never has to guess the date. Date arithmetic is left to the model; tools
that take a date (`remind_me`) echo the exact weekday back so a mistake shows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

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
        f"{weekday} {now:%Y-%m-%d %H:%M} ({tz_name}, UTC{now:%z})",
        data={"iso": now.isoformat(), "weekday": weekday, "timezone": tz_name},
    )


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
    ]
