"""The computer as a module: `[modules.computer]` in orion.toml.

Orion drives its own browser (`orion.computer.direct`): it reads, signs in to the owner's
accounts and sends forms itself, the owner approving (D113). There is no separate web agent.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import Field

from orion.computer.browser import Session
from orion.computer.direct import BrowserDesk, browser_tools
from orion.core.events import Trust
from orion.modules.base import Module, ModuleSettings, Plate
from orion.tools.actions import grouped
from orion.tools.base import Tool, ToolContext

if TYPE_CHECKING:
    from orion.config import OrionConfig

SessionFactory = Callable[[], Session]


class ComputerSettings(ModuleSettings):
    executable: str | None = Field(
        default=None, description="A Chromium binary; default: Playwright's own"
    )
    model: str | None = Field(default=None, description="Unused since D113 (no web agent)")
    max_steps: int = Field(default=30, description="Unused since D113 (no web agent)")


class ComputerModule(Module):
    name = "computer"
    offer = (
        "Un navigateur à toi, pour parcourir des sites et envoyer un formulaire qu'il "
        "approuve : `[modules.computer] enabled = true`. Aller sur un de ses comptes (URSSAF, "
        "banque, Doctolib…) demande en plus le gardien séparé, qui garde ses sessions."
    )

    def offers(self) -> list[str]:
        if not self.enabled:
            return super().offers()
        lend = (
            "Aller sur ses comptes (LinkedIn, Leboncoin, sa banque…) : il se connecte depuis son "
            "téléphone dans le navigateur du gardien, ou lui confie son mot de passe "
            "(`browser` connect)"
        )
        if not self.config.guardian.enabled:
            lend += " (il faut d'abord le gardien séparé, `[guardian] enabled = true`)"
        return [lend + "."]

    instructions = "computer.md"
    settings_model = ComputerSettings

    def __init__(self, config: OrionConfig, sessions: SessionFactory | None = None) -> None:
        super().__init__(config)
        self._sessions = sessions
        self.desk: BrowserDesk | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: ComputerSettings = self.settings
        executable = settings.executable or os.environ.get("ORION_CHROMIUM")
        # It browses like a person (D115): it may send; the server's own addresses stay out.
        sessions = self._sessions or (lambda: Session(False, executable=executable))
        self.desk = BrowserDesk(plate.journal, sessions, guardian=plate.guardian)
        from orion.files.module import FilesModule

        files = plate.module(FilesModule)
        store = files.store if files is not None else None
        keep = None
        if store is not None:
            kept_by = store

            async def keep_file(name: str, data: bytes, kind: str, ctx: ToolContext) -> str:
                return (await kept_by.save(name, data, kind, "screenshot", ctx, Trust.WEB)).id

            keep = keep_file
        return [
            grouped(
                "browser",
                "Your own browser, which you drive yourself, page by page: open a page and see "
                "it (its text and numbered elements), then act on the page shown: click, type, "
                "send forms, open his accounts (login).",
                browser_tools(self.desk, keep),
                {
                    "browser_open": "open",
                    "browser_click": "click",
                    "browser_type": "type",
                    "browser_select": "select",
                    "browser_scroll": "scroll",
                    "browser_back": "back",
                    "browser_read": "read",
                    "browser_close": "close",
                    "browser_login": "login",
                    "browser_fill": "fill",
                    "browser_accounts": "accounts",
                    "browser_connect": "connect",
                    **({"browser_screenshot": "screenshot"} if keep is not None else {}),
                },
            ),
        ]

    async def close(self) -> None:
        if self.desk is not None:
            await self.desk.aclose()

    def doctor(self) -> list[str]:
        return ["your own browser (no separate web agent, D113)"]
