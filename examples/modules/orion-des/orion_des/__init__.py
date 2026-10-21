"""An example of a native module for Orion (D105): dice, really at random.

A module is a `Module` subclass declared under the `orion.modules` entry point (see
pyproject.toml). It gives tools to the model, may add a few lines to Orion's instructions, and
reads its settings from `[modules.des]` in orion.toml.
"""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.modules.base import Module, ModuleSettings, Plate
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult


class DesSettings(ModuleSettings):
    faces: int = Field(default=6, ge=2, le=1000, description="Faces of a die by default")


class RollInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(default=1, ge=1, le=20, description="How many dice")
    faces: int | None = Field(default=None, ge=2, le=1000, description="Faces of each die")


class DesModule(Module):
    name = "des"
    settings_model = DesSettings

    def instructions_text(self) -> str:
        return (
            "# Les dés\n\nPour un tirage au sort, un dé, pile ou face (un dé à 2 faces) : "
            "`des_lancer`. Tu ne choisis jamais toi-même un résultat « au hasard »."
        )

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: DesSettings = self.settings

        async def roll(args: Any, _ctx: ToolContext) -> ToolResult:
            faces = args.faces or settings.faces
            rolls = [secrets.randbelow(faces) + 1 for _ in range(args.count)]
            total = f" (total {sum(rolls)})" if len(rolls) > 1 else ""
            return ToolResult(f"{', '.join(map(str, rolls))}{total} sur des dés à {faces} faces")

        return [
            Tool(
                name="des_lancer",
                description="Roll dice, truly at random (a coin is a 2-faced die).",
                input_model=RollInput,
                handler=roll,
                effect=Effect.NONE,
                reversibility=Reversibility.REVERSIBLE,
                concurrency_safe=True,
            )
        ]
