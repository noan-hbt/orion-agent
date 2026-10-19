"""Built-in tools available from M1. All are side-effect free.

Date and arithmetic are deterministic tools on purpose: models are unreliable at both, and
wrong dates are what destroys trust in a personal agent fastest.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from datetime import datetime, timedelta
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


class DateMathInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(description="ISO date or datetime, e.g. '2026-10-19' or '2026-10-19T14:00'")
    days: int = 0
    hours: int = 0
    minutes: int = 0


async def _date_math(args: Any, ctx: ToolContext) -> ToolResult:
    try:
        start = datetime.fromisoformat(args.start)
    except ValueError:
        return ToolResult(f"Not an ISO date: {args.start}", is_error=True)
    result = start + timedelta(days=args.days, hours=args.hours, minutes=args.minutes)
    weekday = _WEEKDAYS_FR[result.weekday()]
    return ToolResult(f"{weekday} {result.isoformat()}", data={"iso": result.isoformat()})


class CalculateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(description="Arithmetic expression, e.g. '(12.5 * 3) / 4 ** 2'")


_BINARY: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: operator.mod,
    ast.Pow: lambda a, b: a**b,
}
_UNARY: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent too large")
        return _BINARY[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_evaluate(node.operand))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


async def _calculate(args: Any, ctx: ToolContext) -> ToolResult:
    if len(args.expression) > 500:
        return ToolResult("Expression too long", is_error=True)
    try:
        value = _evaluate(ast.parse(args.expression, mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
        return ToolResult(f"Cannot evaluate: {exc}", is_error=True)
    return ToolResult(repr(value), data={"value": value})


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
            name="date_math",
            description="Add days/hours/minutes to a date and get the resulting weekday. "
            "Use it for any date arithmetic.",
            input_model=DateMathInput,
            handler=_date_math,
            **common,
        ),
        Tool(
            name="calculate",
            description="Evaluate an arithmetic expression exactly. Use it for any computation.",
            input_model=CalculateInput,
            handler=_calculate,
            **common,
        ),
    ]
