"""The web as a module: `[modules.web]` in orion.toml. Uses the model provider's web search,
so it needs no other account; Orion reads the pages itself (D114)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orion.modules.base import Module, Plate
from orion.web.search import WebSearch
from orion.web.service import WebDesk, WebSettings
from orion.web.tools import web_tools

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class WebModule(Module):
    name = "web"
    offer = "Chercher sur le web et lire des pages : `[modules.web] enabled = true`."
    instructions = "web.md"
    settings_model = WebSettings

    def __init__(self, config: OrionConfig, desk: WebDesk | None = None) -> None:
        super().__init__(config)
        self.desk = desk

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: WebSettings = self.settings
        if self.desk is None:
            searcher = WebSearch(plate.provider, self.config.agent.model, settings.max_results)
            self.desk = WebDesk(plate.journal, searcher)
        await self.desk.start()
        return web_tools(self.desk)

    async def close(self) -> None:
        if self.desk is not None:
            await self.desk.aclose()

    def doctor(self) -> list[str]:
        return [f"search through {self.config.agent.model}; Orion reads the pages itself (D114)"]
