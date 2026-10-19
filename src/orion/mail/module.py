"""Mail as a module: `[modules.mail]` in orion.toml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orion.mail.quarantine import Quarantine
from orion.mail.service import Fetch, Mailbox, MailSettings
from orion.mail.tools import mail_tools
from orion.modules.base import Daemon, Module, Plate

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class MailModule(Module):
    name = "mail"
    instructions = "mail.md"
    settings_model = MailSettings

    def __init__(self, config: OrionConfig, fetch: Fetch | None = None) -> None:
        super().__init__(config)
        self._fetch = fetch
        self.mailbox: Mailbox | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: MailSettings = self.settings
        model = settings.quarantine_model or self.config.agent.extractor_model
        self.mailbox = Mailbox(
            plate.journal,
            settings,
            Quarantine(plate.provider, model),
            self.config.agent.timezone,
            self._fetch,
        )
        await self.mailbox.start()
        return mail_tools(self.mailbox)

    async def serve(self, daemon: Daemon) -> None:
        mailbox = self.mailbox
        if mailbox is None:  # pragma: no cover - assemble always runs first
            return
        daemon.add_service("mail.sync", mailbox.run)
        daemon.alerts.append(mailbox)
        daemon.digests.append(mailbox)

    def doctor(self) -> list[str]:
        settings: MailSettings = self.settings
        if not settings.accounts:
            return ["no account: add [[modules.mail.accounts]]"]
        lines: list[str] = []
        for account in settings.accounts:
            try:
                account.password.resolve()
                lines.append(f"{account.name} ({account.username} on {account.host}) ok")
            except LookupError as exc:
                lines.append(f"{account.name}: {exc}")
        return lines
