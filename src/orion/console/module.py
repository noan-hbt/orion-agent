"""The control page as a module: `[modules.console]`. Served by the core on localhost; put it
behind the HTTPS proxy (`public_url`) to reach it from the phone."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import uvicorn
from pydantic import BaseModel, ConfigDict, Field

from orion.console.app import PREFIX, Console, console_app
from orion.console.auth import Gate, session_key
from orion.initiative.planned import everything_planned
from orion.memory.provenance import Provenances
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.providers.spending import Spending
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig


class ConsoleSettings(ModuleSettings):
    host: str = "127.0.0.1"
    port: int = 8790
    public_url: str = Field(
        default="", description="Where the owner's browser reaches it (https://…); default: local"
    )


class LinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsoleModule(Module):
    name = "console"
    offer = (
        "Une page de contrôle où il voit et corrige ce que tu sais de lui, tes engagements et "
        "ton activité : `[modules.console] enabled = true`."
    )
    instructions = "console.md"
    settings_model = ConsoleSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.gate: Gate | None = None
        self.console: Console | None = None

    def base_url(self) -> str:
        settings: ConsoleSettings = self.settings
        return (settings.public_url or f"http://{settings.host}:{settings.port}").rstrip("/")

    async def link(self) -> str:
        if self.gate is None:  # pragma: no cover - assemble always runs first
            raise RuntimeError("console not assembled")
        return f"{self.base_url()}{PREFIX}/login?t={await self.gate.new_link()}"

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.gate = Gate(plate.journal, session_key(self.config.data_dir))
        self.console = Console(
            self.gate,
            plate.journal,
            plate.memory,
            plate.autonomy,
            Provenances(plate.journal, plate.memory, plate.timers),
            self.config.agent.timezone,
            secure_cookie=self.base_url().startswith("https://"),
            planned=lambda now, until: everything_planned(plate.modules, plate.timers, now, until),
            spending=plate.provider if isinstance(plate.provider, Spending) else None,
        )

        async def console_link(_args: Any, _ctx: ToolContext) -> ToolResult:
            return ToolResult(
                f"Lien de la console (une seule fois, 10 minutes) : {await self.link()}"
            )

        return [
            Tool(
                name="console_link",
                description="A one-time link to the owner's control page (memory, commitments, "
                "playbook, autonomy, activity). Only when the owner asks for it; give it in "
                "your reply, never anywhere else.",
                input_model=LinkInput,
                handler=console_link,
                effect=Effect.INTERNAL,
                reversibility=Reversibility.REVERSIBLE,
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.console is None:  # pragma: no cover
            return
        settings: ConsoleSettings = self.settings
        server = uvicorn.Server(
            uvicorn.Config(
                console_app(self.console),
                host=settings.host,
                port=settings.port,
                lifespan="off",
                log_config=None,
                access_log=False,
                proxy_headers=True,
            )
        )
        daemon.add_service("console", server.serve)

    def doctor(self) -> list[str]:
        return [f"served at {self.base_url()}{PREFIX}/"]
