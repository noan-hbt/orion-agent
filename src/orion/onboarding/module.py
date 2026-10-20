"""Getting to know the owner, as a module: `[modules.onboarding]`, on by default."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from orion.modules.base import Daemon, Module, Plate
from orion.onboarding.service import Onboarding, OnboardingSettings
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig


class StopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OnboardingModule(Module):
    name = "onboarding"
    instructions = "onboarding.md"
    settings_model = OnboardingSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.onboarding: Onboarding | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        onboarding = Onboarding(
            plate.journal,
            self.settings,
            self.config.agent.timezone,
            plate.memory.store.facts,
            {m.name for m in plate.modules},
        )
        await onboarding.start()
        self.onboarding = onboarding

        async def stop(_args: Any, ctx: ToolContext) -> ToolResult:
            await onboarding.stop(ctx.correlation_id)
            return ToolResult("Plus de questions pour faire connaissance.")

        return [
            Tool(
                name="onboarding_stop",
                description="Stop the getting-to-know questions for good (when the owner says "
                "they have had enough of them).",
                input_model=StopInput,
                handler=stop,
                effect=Effect.INTERNAL,
                reversibility=Reversibility.REVERSIBLE,
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.onboarding is not None:
            daemon.alerts.append(self.onboarding)
