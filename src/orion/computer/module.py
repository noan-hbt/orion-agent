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
from orion.computer.direct import BrowserDesk, Refused, browser_tools
from orion.computer.vault import LocalVault
from orion.core.events import Trust
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import grouped
from orion.tools.base import Tool, ToolContext

if TYPE_CHECKING:
    from orion.config import OrionConfig

SessionFactory = Callable[[], Session]
PHONE = {"width": 412, "height": 860}


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
        "approuve : `[modules.computer] enabled = true`. Il se connecte à ses comptes (URSSAF, "
        "banque, Doctolib…) avec les identifiants de son coffre, sans jamais les voir."
    )

    def offers(self) -> list[str]:
        if not self.enabled:
            return super().offers()
        lend = (
            "Aller sur ses comptes (LinkedIn, Leboncoin, sa banque…) : il se connecte une fois "
            "depuis son téléphone, dans ton navigateur, qui le reste (`browser` hand_over)"
        )
        console = dict(self.config.modules.get("console") or {})
        if not str(console.get("public_url") or "").startswith("https://"):
            lend += " ; il faut Telegram et `[modules.console] public_url` en https"
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
        profile = self.config.data_dir / "browser"

        def browser() -> Session:
            # Like a person (D115), with one profile kept on disk (D116), posing as a phone
            # so the owner can take it in hand from his.
            session = Session(False, executable=executable)
            session.profile = profile
            session.phone = PHONE
            return session

        sessions = self._sessions or browser
        # The guardian keeps the logins when it runs apart (D111); else Orion does (D120).
        vault = LocalVault(self.config) if plate.guardian is None else None
        self.desk = BrowserDesk(plate.journal, sessions, guardian=plate.guardian, vault=vault)
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
                "send forms. It stays signed in to the sites he signed in to; a page that needs "
                "him (sign in, a code): hand_over.",
                browser_tools(self.desk, keep, looks=plate.has("photos")),
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
                    "browser_hand_over": "hand_over",
                    "browser_forget": "forget",
                    "browser_save_password": "save_password",
                    **({"browser_screenshot": "screenshot"} if keep is not None else {}),
                },
            ),
        ]

    async def serve(self, daemon: Daemon) -> None:
        """The hand-over (D116): a Telegram button, a page the console serves."""
        from orion.channels.telegram import TelegramError
        from orion.channels.telegram_module import TelegramModule
        from orion.computer.handover import handover_app
        from orion.console.module import ConsoleModule

        desk = self.desk
        console = daemon.plate.module(ConsoleModule)
        bot = daemon.plate.module(TelegramModule)
        if desk is None or console is None or console.console is None or bot is None:
            return
        held = console.console
        desk.base_url = f"{console.base_url()}/console/browser"
        daemon.alerts.append(desk)

        async def notify(text: str, url: str) -> None:
            try:
                await bot.web_app_button(text, url)
            except TelegramError as exc:
                raise Refused(f"Telegram did not take the button: {exc}") from exc

        async def is_owner(init_data: str) -> bool:
            check = held.telegram_login
            return (
                check is not None
                and held.owner_telegram != 0
                and (await check(init_data) == held.owner_telegram)
            )

        desk.notify = notify
        held.browser = handover_app(desk, is_owner)

    async def close(self) -> None:
        if self.desk is not None:
            await self.desk.aclose()

    def doctor(self) -> list[str]:
        return ["your own browser (no separate web agent, D113)"]
