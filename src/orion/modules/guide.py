"""Sections of the constitution read when they matter (D212): every call pays for the system
message, and some sections serve a few times a month."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from orion.modules.base import Module
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


class ConsigneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: str = Field(description="One of the sections your system message lists")


def on_demand(modules: Sequence[Module]) -> list[Module]:
    return [m for m in modules if m.read_when and m.instructions_text()]


def guide_section(modules: Sequence[Module]) -> str:
    """What stands in the system message for the sections left out of it."""
    lines = "\n".join(f"- `{m.name}` : {m.read_when}" for m in on_demand(modules))
    return (
        "# Consignes à lire d'abord\n\n"
        "Dans ces cas, lis d'abord la consigne avec l'outil `consigne`, avant de répondre ou "
        f"d'agir :\n\n{lines}"
    )


def guide_tool(modules: Sequence[Module]) -> Tool:
    sections = {m.name: m.instructions_text() for m in on_demand(modules)}

    async def read(args: ConsigneInput, ctx: ToolContext) -> ToolResult:
        del ctx
        text = sections.get(args.section)
        if text is None:
            return ToolResult(f"No such section; there are: {', '.join(sections)}", is_error=True)
        return ToolResult(text)

    return Tool(
        name="consigne",
        description="Read the instructions your system message says to read first.",
        input_model=ConsigneInput,
        handler=read,
        effect=Effect.NONE,
        reversibility=Reversibility.REVERSIBLE,
        concurrency_safe=True,
        accepts_untrusted_context=True,
    )
