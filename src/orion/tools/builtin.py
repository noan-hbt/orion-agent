"""Built-in tools. Side-effect free.

The current time is no tool: every turn's new message opens on it (`render_user_message`, D175).
Date arithmetic is left to the model; tools that take a date (`remind_me`) echo the exact
weekday back so a mistake shows.

During a long turn (D97), Orion keeps a plan (`plan`: every step with its status, the whole
list each time, so the plan lives in the conversation and nowhere else). It is his own: the
owner hears what he finds, never the plan (D169). What he finds on the way goes out with `tell`
or as text beside a tool call (D173).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


class TellInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000, description="The message, as you'd text it")


async def _tell(_args: Any, _ctx: ToolContext) -> ToolResult:
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
            name="tell",
            description="Send him a message now and keep working: what you just found or did "
            "that matters to him and that your previous messages did not already say. Never "
            "what you are about to do or try next. Call it with your next tool, not alone. "
            "Your last answer is your reply, not a `tell`, and it does not repeat them.",
            input_model=TellInput,
            handler=_tell,
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
