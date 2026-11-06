"""Workflow runs, seen from Orion: where each one stands, and the uncertain ones told to the owner.

The runs themselves belong to the modules that start them (delegation…); this module only reads
the journal.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Report
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult
from orion.workflows.engine import RunProjection, State, describe

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.workflows.engine import Run


class WorkflowSettings(ModuleSettings):
    enabled: bool = True


class WorkflowsStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowsModule(Module):
    name = "workflows"
    settings_model = WorkflowSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.projection = RunProjection()
        self._runner: ProjectionRunner | None = None

    async def sync(self) -> None:
        if self._runner is not None:
            await self._runner.catch_up()

    async def assemble(self, plate: Plate) -> list[Tool]:
        self._runner = ProjectionRunner(plate.journal, [self.projection])
        await self.sync()
        return [workflows_status_tool(self)]

    async def serve(self, daemon: Daemon) -> None:
        daemon.reports.append(self)

    async def reports(self) -> list[Report]:
        """An uncertain step is said once: the owner decides, nothing is tried blindly."""
        await self.sync()
        return [_uncertain(run) for run in self.projection.runs.values() if _is_uncertain(run)]

    async def review(self, now: datetime) -> list[str]:
        await self.sync()
        return [
            describe(run)
            for run in self.projection.runs.values()
            if run.state in (State.UNCERTAIN, State.WAITING)
        ]


def _is_uncertain(run: Run) -> bool:
    return run.state is State.UNCERTAIN and run.current is not None


def _uncertain(run: Run) -> Report:
    step = run.current or "?"
    return Report(
        key=f"workflow:{run.id}:{step}:{run.attempts.get(step, 1)}",
        notice=f"Le travail « {run.goal} » ({run.id}) s'est arrêté pendant l'étape {step}, "
        f"qui agit hors d'Orion ({run.blocked}). On ne sait pas si elle a eu lieu : rien n'est "
        "refait sans vérification. Dis-le-lui tel quel, sans dire que c'est fait ni raté.",
        why="un travail en plusieurs étapes s'est arrêté sans qu'on sache où il en est",
        fallback=f"« {run.goal} » : interrompu pendant {step}, issue incertaine, rien n'est "
        "refait sans vérification.",
    )


def workflows_status_tool(module: WorkflowsModule) -> Tool:
    async def workflows_status(args: Any, ctx: ToolContext) -> ToolResult:
        await module.sync()
        runs = list(module.projection.runs.values())
        live = [describe(r) for r in runs if not r.finished]
        done = [describe(r) for r in runs if r.finished][-10:]
        lines = live + done
        return ToolResult("\n".join(lines) if lines else "Aucun travail en plusieurs étapes.")

    return Tool(
        name="workflows_status",
        description="List multi-step work (delegated jobs…) with, for each: its state (running, "
        "paused, uncertain, done, failed), what blocks it, its next step and its result. An "
        "uncertain step may or may not have happened: never say it is done.",
        input_model=WorkflowsStatusInput,
        handler=workflows_status,
        effect=Effect.NONE,
        reversibility=Reversibility.REVERSIBLE,
        concurrency_safe=True,
        accepts_untrusted_context=True,
    )
