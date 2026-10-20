"""The computer as a module: `[modules.computer]` in orion.toml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from orion.computer.agent import BrowserAgent, SessionFactory
from orion.computer.browser import Session
from orion.computer.direct import BrowserDesk, browser_tools
from orion.core.events import Provenance, Trust
from orion.delegation.jobs import JobService
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig


class ComputerSettings(ModuleSettings):
    model: str | None = Field(default=None, description="The worker's model; default: agent.model")
    max_steps: int = Field(default=30, ge=3, le=100)
    executable: str | None = Field(
        default=None, description="A Chromium binary; default: Playwright's own"
    )


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=80, description="A few words naming the task")
    brief: str = Field(
        min_length=10,
        max_length=4000,
        description="Complete instructions: the worker sees nothing else (no memory, no "
        "conversation). Say what to find or do, where to start if known, the criteria, and "
        "what to report. Put in only the personal details the task needs.",
    )


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComputerModule(Module):
    name = "computer"
    instructions = "computer.md"
    settings_model = ComputerSettings

    def __init__(self, config: OrionConfig, sessions: SessionFactory | None = None) -> None:
        super().__init__(config)
        self._sessions = sessions
        self.reading: JobService | None = None
        self.sending: JobService | None = None
        self.desk: BrowserDesk | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: ComputerSettings = self.settings
        model = settings.model or self.config.agent.model
        executable = settings.executable or os.environ.get("ORION_CHROMIUM")

        def browser(read_only: bool) -> Session:
            return Session(read_only, executable=executable)

        sessions = self._sessions or browser
        workspaces: Path = self.config.data_dir / "computer"
        self.reading = JobService(
            plate.journal,
            BrowserAgent(plate.provider, model, sessions, True, settings.max_steps),
            workspaces,
            result_trust=Trust.WEB,
        )
        self.sending = JobService(
            plate.journal,
            BrowserAgent(plate.provider, model, sessions, False, settings.max_steps),
            workspaces,
            result_trust=Trust.WEB,
        )
        await self.reading.recover()
        await self.sending.recover()
        # Orion's own browser is always read-only: sending stays with an approved brief.
        self.desk = BrowserDesk(plate.journal, lambda: sessions(True))
        return [*computer_tools(self.reading, self.sending), *browser_tools(self.desk)]

    async def serve(self, daemon: Daemon) -> None:
        for jobs in (self.reading, self.sending):
            if jobs is not None:
                daemon.reports.append(jobs)

    async def close(self) -> None:
        if self.desk is not None:
            await self.desk.aclose()

    def doctor(self) -> list[str]:
        settings: ComputerSettings = self.settings
        return [f"browser worker on {settings.model or self.config.agent.model}"]


def computer_tools(reading: JobService, sending: JobService) -> list[Tool]:
    def starter(jobs: JobService, mode: str) -> Any:
        async def start(args: Any, ctx: ToolContext) -> ToolResult:
            job_id = await jobs.start(
                args.title,
                args.brief,
                Provenance(
                    source=f"thread:{ctx.thread_id}",
                    actor="orion:core",
                    derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
                ),
                ctx.correlation_id,
            )
            return ToolResult(
                f"Tâche lancée sur l'ordinateur ({job_id}, {mode}). Le compte rendu arrivera par "
                "message quand elle sera finie.",
                data={"job_id": job_id},
            )

        return start

    async def status(_args: Any, _ctx: ToolContext) -> ToolResult:
        lines: list[str] = []
        for jobs in (reading, sending):
            await jobs.sync()
            lines += [f"- ({j.id}) {j.title} : en cours" for j in jobs.projection.running()]
            lines += [
                f"- ({j.id}) {j.title} : {'terminée' if j.ok else 'échec : ' + (j.error or '?')}"
                for j in jobs.projection.finished()[-5:]
            ]
        return ToolResult("\n".join(lines) if lines else "Aucune tâche sur l'ordinateur.")

    return [
        Tool(
            name="computer_task",
            description="Give a browsing task to your own computer (a browser run by a worker): "
            "compare listings, go through a site, find a form or an opening hour. Read-only: it "
            "can search, read and click, never send anything. Runs in the background; the "
            "report comes later.",
            input_model=TaskInput,
            handler=starter(reading, "lecture seule"),
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="computer_task_send",
            description="Like computer_task, but the worker may send what the brief says (fill "
            "and submit a form, book). The owner approves the exact brief first.",
            input_model=TaskInput,
            handler=starter(sending, "peut envoyer"),
            effect=Effect.EXTERNAL,
            reversibility=Reversibility.IRREVERSIBLE,
        ),
        Tool(
            name="computer_status",
            description="Tasks given to your computer: running, done or failed.",
            input_model=StatusInput,
            handler=status,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]
